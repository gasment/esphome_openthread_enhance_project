"""Independent native API with optional Noise encryption for Thread SED and REED devices."""
import hashlib
import base64
import esphome.codegen as cg
import esphome.config_validation as cv
import esphome.final_validate as fv
from esphome import automation
from esphome.components import mdns, socket
from esphome.components.noise import validate_encryption_key, new_psk_progmem, enable_spare_ephemeral
from esphome.components.logger import request_log_listener
from esphome.const import CONF_ID, CONF_KEY, CONF_ENCRYPTION
from esphome.core import CORE, CoroPriority, coroutine_with_priority

def AUTO_LOAD(config):
    components = ['network', 'socket', 'mdns']
    if config and CONF_ENCRYPTION in config:
        components.append('noise')
    return components
CONFLICTS_WITH = ['api']
DEPENDENCIES = ['network']
ns = cg.esphome_ns.namespace('thread_sed_api')
APIServer = ns.class_('APIServer', cg.Component)
CanSleepCondition = ns.class_('CanSleepCondition', automation.Condition)

@automation.register_condition('thread_sed_api.can_sleep', CanSleepCondition,
    cv.Schema({cv.GenerateID():cv.use_id(APIServer)}))
async def can_sleep_to_code(config, condition_id, template_arg, args):
    return cg.new_Pvariable(condition_id,template_arg,await cg.get_variable(config[CONF_ID]))

def noise_key(value):
    value = validate_encryption_key(value)
    if not any(base64.b64decode(value)):
        raise cv.Invalid('An all-zero Noise key is not permitted')
    return value

def validate(config):
    if not 10 <= config['fast_poll_interval'].total_milliseconds <= 1000:
        raise cv.Invalid('fast_poll_interval must be between 10ms and 1s')
    if config['sleep_ack_timeout'].total_milliseconds <= 0:
        raise cv.Invalid('sleep_ack_timeout must be greater than zero')
    if 'deep_sleep_id' in config and not CORE.is_nrf52:
        raise cv.Invalid('deep_sleep_id currently integrates deep_sleep_nrf52 System ON sleep only')
    return config

def sleep_id(value):
    from esphome.components.deep_sleep_nrf52 import DeepSleepComponent
    return cv.use_id(DeepSleepComponent)(value)

CONFIG_SCHEMA = cv.All(cv.Schema({
    cv.GenerateID(): cv.declare_id(APIServer),
    cv.Optional(CONF_ENCRYPTION): cv.Schema({cv.Required(CONF_KEY): cv.sensitive(noise_key)}),
    cv.Optional('port', default=6053): cv.port,
    cv.Optional('deep_sleep_id'): sleep_id,
    cv.Optional('sleep_duration'): cv.positive_time_period_milliseconds,
    cv.Optional('sleep_ack_timeout', default='10s'): cv.positive_time_period_milliseconds,
    cv.Optional('fast_poll_interval', default='20ms'): cv.positive_time_period_milliseconds,
    cv.Optional('batch_delay', default='0ms'): cv.positive_time_period_milliseconds,
}).extend(cv.COMPONENT_SCHEMA), validate, socket.consume_sockets(3, 'thread_sed_api', socket.SocketType.TCP))

def final_validate(config):
    full = fv.full_config.get()
    if not any(key in full for key in ('openthread', 'openthread_nrf52')):
        raise cv.Invalid('thread_sed_api requires openthread or openthread_nrf52')
    if full['mdns'].get('disabled'):
        raise cv.Invalid('Thread SED discovery requires mDNS/SRP')
    if not full.get('network', {}).get('enable_ipv6'):
        raise cv.Invalid('Thread SED requires network.enable_ipv6: true')
    if 'deep_sleep_id' in config:
        timeout = full['deep_sleep_nrf52']['prepare_timeout']
        if config['sleep_ack_timeout'].total_milliseconds >= timeout.total_milliseconds:
            raise cv.Invalid('sleep_ack_timeout must be shorter than deep_sleep_nrf52.prepare_timeout')
    # Add an ordinary extra mDNS service BEFORE mDNS calculates its fixed storage.
    # OpenThread's SRP bridge consumes this same service list.
    services = full['mdns']['services']
    services[:] = [s for s in services if s['service'] != '_thread-sed']
    services.append(mdns.SERVICE_SCHEMA({
        'service': '_thread-sed', 'protocol': '_tcp', 'port': config['port'],
        'txt': {'v': '1', 'name': CORE.name,
                'mac': '',
                'encryption': 'noise' if CONF_ENCRYPTION in config else 'none',
                'sleep': '1' if 'deep_sleep_id' in config else '0'},
    }))
    # MAC is a runtime value; mdns codegen reserves its dynamic TXT slot.
    from esphome.core import Lambda
    services[-1]['txt']['mac'] = Lambda('return get_mac_address();')
    return config

FINAL_VALIDATE_SCHEMA = final_validate

@coroutine_with_priority(CoroPriority.WEB)
async def to_code(config):
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)
    CORE.register_controller(var)
    request_log_listener()
    cg.add(var.set_port(config['port']))
    cg.add(var.set_reboot_timeout(0))
    cg.add(var.set_batch_delay(config['batch_delay']))
    if CONF_ENCRYPTION in config:
        cg.add(var.set_noise_psk(new_psk_progmem(config[CONF_ENCRYPTION][CONF_KEY])))
        cg.add_define('USE_THREAD_SED_API_NOISE')
        cg.add_define('USE_THREAD_SED_API_NOISE_PSK_FROM_YAML')
        enable_spare_ephemeral()
    else:
        cg.add_define('USE_THREAD_SED_API_PLAINTEXT')
    cg.add(var.set_sleep_budget(config['sleep_ack_timeout']))
    if 'sleep_duration' in config:
        cg.add(var.set_sleep_seconds(int(config['sleep_duration'].total_milliseconds)//1000))
    for flag in ('USE_THREAD_SED_API', 'USE_THREAD_SED_API_OUTGOING_CONNECTION',
                 'USE_THREAD_SED_API_HOMEASSISTANT_SERVICES', 'USE_THREAD_SED_API_HOMEASSISTANT_STATES'):
        cg.add_define(flag)
    cg.add_define('MAX_THREAD_SED_API_CONNECTIONS', 2)
    cg.add_define('THREAD_SED_API_MAX_SEND_QUEUE', 16)
    cg.add_define('THREAD_SED_API_OUTGOING_CONNECTION_PORT', 6064)
    cg.add_define('THREAD_SED_API_OUTGOING_CONNECTION_DELAY', 1000)
    # Stable across wakes; any firmware configuration edit invalidates schema.
    fingerprint = hashlib.sha256(('thread-sed-v1:' + str(CORE.raw_config)).encode()).hexdigest()[:32]
    cg.add_define('THREAD_SED_SCHEMA', fingerprint)
    cg.add_define('THREAD_SED_FAST_POLL_MS', int(config['fast_poll_interval'].total_milliseconds))
    cg.add_define('USE_THREAD_SED_NRF52' if CORE.is_nrf52 else 'USE_THREAD_SED_ESP32')
    if 'deep_sleep_id' in config:
        cg.add_define('USE_THREAD_SED_SLEEP')
        cg.add(var.set_deep_sleep(await cg.get_variable(config['deep_sleep_id'])))
    if CORE.is_nrf52:
        from esphome.components.zephyr import zephyr_add_prj_conf
        zephyr_add_prj_conf('NET_TCP', True)
    cg.add_global(ns.using)
