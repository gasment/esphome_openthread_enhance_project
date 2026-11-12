"""Thread SED entity API with UDP/ACK and optional native TCP compatibility."""
import hashlib
import json
import importlib
from pathlib import Path
from importlib.metadata import version
import base64
import esphome.codegen as cg
import esphome.config_validation as cv
import esphome.final_validate as fv
from esphome import automation, yaml_util
from esphome.components import mdns, socket
from esphome.components.noise import validate_encryption_key, new_psk_progmem, enable_spare_ephemeral
from esphome.components.logger import request_log_listener
from esphome.config_helpers import filter_source_files_from_defines, get_logger_level
from esphome.const import CONF_ID, CONF_KEY, CONF_ENCRYPTION
from esphome.core import CORE, CoroPriority, coroutine_with_priority

def AUTO_LOAD(config):
    components = ['network', 'socket', 'mdns']
    if config and CONF_ENCRYPTION in config and config.get('transport','udp') == 'tcp':
        components.append('noise')
    return components
CONFLICTS_WITH = ['api']
DEPENDENCIES = ['network']
ns = cg.esphome_ns.namespace('thread_sed_api')
APIServer = ns.class_('APIServer', cg.Component)
CanSleepCondition = ns.class_('CanSleepCondition', automation.Condition)
IsReadyCondition = ns.class_('IsReadyCondition', automation.Condition)
SendAction = ns.class_('SendAction', automation.Action)

@automation.register_condition('thread_sed_api.is_ready', IsReadyCondition,
    cv.maybe_simple_value({cv.GenerateID():cv.use_id(APIServer)},key=CONF_ID))
async def is_ready_to_code(config, condition_id, template_arg, args):
    return cg.new_Pvariable(condition_id,template_arg,await cg.get_variable(config[CONF_ID]))

def ack_timeout(value):
    value=cv.positive_time_period_milliseconds(value)
    if not 1 <= value.total_milliseconds <= 60000:
        raise cv.Invalid('ack_timeout must be between 1ms and 60s')
    return value

def ack_resend(value):
    if isinstance(value,bool):
        raise cv.Invalid('ack_resend is an additional resend count, not a boolean')
    return cv.int_range(min=0,max=255)(value)

@automation.register_action('thread_sed_api.send', SendAction, cv.Schema({
    cv.GenerateID():cv.use_id(APIServer),
    cv.Optional('timeout'):cv.invalid('Use action ack_timeout and ack_resend instead of timeout'),
    cv.Optional('ack_timeout',default='500ms'):cv.templatable(ack_timeout),
    cv.Optional('ack_resend',default=2):cv.templatable(ack_resend),
    cv.Optional('entity_id'):cv.All(cv.ensure_list(cv.use_id(cg.EntityBase)),cv.Length(min=1)),
    cv.Optional('on_success'):automation.validate_action_list,
    cv.Optional('on_error'):automation.validate_action_list,
}), synchronous=False)
async def send_to_code(config, action_id, template_arg, args):
    var=cg.new_Pvariable(action_id,template_arg,await cg.get_variable(config[CONF_ID]))
    if CORE.config['thread_sed_api']['send_mode']!='manual':
        raise cv.Invalid('thread_sed_api.send requires send_mode: manual')
    cg.add(var.set_ack_timeout(await cg.templatable(config['ack_timeout'],args,cg.uint32)))
    cg.add(var.set_ack_resend(await cg.templatable(config['ack_resend'],args,cg.uint32)))
    if 'entity_id' in config:
        # Resolve the actual entity class, including entities nested inside a
        # custom component rather than declared under a top-level sensor key.
        supported = [cg.esphome_ns.namespace(domain).class_(name) for domain,name in (
            ('sensor','Sensor'),('binary_sensor','BinarySensor'),('text_sensor','TextSensor'),
            ('switch_','Switch'),('number','Number'),('select','Select'),('text','Text'),
            ('light','LightState'),('fan','Fan'),('cover','Cover'),('climate','Climate'),
            ('lock','Lock'),('valve','Valve'),('event','Event'))]
        selected = []
        for entity_id in config['entity_id']:
            path = CORE.config.get_path_for_id(entity_id)
            declaration = CORE.config.get_config_for_path(path)
            if declaration.type is None or not any(declaration.type.inherits_from(t) for t in supported):
                raise cv.Invalid(f"send entity_id '{entity_id}' is not a supported state/event entity")
            entity_config = CORE.config.get_config_for_path(path[:-1])
            if entity_config.get('internal',False):
                raise cv.Invalid(f"send entity_id '{entity_id}' is internal; expose it with a name or internal: false")
            selected.append(await cg.get_variable(entity_id))
        cg.add(var.set_entities(selected))
    for branch in ('on_success','on_error'):
        if branch in config:
            actions=await automation.build_action_list(config[branch],template_arg,args)
            cg.add(getattr(var,'add_'+branch)(actions))
    return var

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
    if config['send_mode']=='manual':
        if any(k in config for k in ('ack_timeout','ack_resend','on_ack','on_ack_error')):
            raise cv.Invalid('manual mode requires ACK options/callbacks on each send action, not on the component')
    else:
        config.setdefault('ack_timeout',ack_timeout('500ms'))
        config.setdefault('ack_resend',2)
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

def reserve_sockets(config):
    udp = config['transport']=='udp'
    return socket.consume_sockets(1 if udp else 3, 'thread_sed_api',
        socket.SocketType.UDP if udp else socket.SocketType.TCP)(config)

CONFIG_SCHEMA = cv.All(cv.Schema({
    cv.GenerateID(): cv.declare_id(APIServer),
    cv.Optional(CONF_ENCRYPTION): cv.Schema({cv.Required(CONF_KEY): cv.sensitive(noise_key)}),
    cv.Optional('transport', default='udp'): cv.one_of('udp','tcp',lower=True),
    cv.Optional('port', default=6064): cv.port,
    cv.Optional('send_mode', default='auto'): cv.one_of('auto','manual',lower=True),
    cv.Optional('on_ack'):automation.validate_automation(single=True),
    cv.Optional('on_ack_error'):automation.validate_automation(single=True),
    cv.Optional('auto_ack_timeout'):cv.invalid('Use component ack_timeout in auto mode'),
    cv.Optional('ack_timeout'):ack_timeout,
    cv.Optional('ack_resend'):ack_resend,
    cv.Optional('deep_sleep_id'): sleep_id,
    cv.Optional('sleep_duration'): cv.positive_time_period_milliseconds,
    cv.Optional('sleep_ack_timeout', default='10s'): cv.positive_time_period_milliseconds,
    cv.Optional('fast_poll_interval', default='20ms'): cv.positive_time_period_milliseconds,
    cv.Optional('batch_delay', default='0ms'): cv.positive_time_period_milliseconds,
}).extend(cv.COMPONENT_SCHEMA), validate, reserve_sockets)

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
    def validate_send_actions(value):
        if isinstance(value,dict):
            if 'thread_sed_api.send' in value and config['send_mode']!='manual':
                raise cv.Invalid('thread_sed_api.send requires send_mode: manual; auto ACKs and callbacks belong to the component')
            for item in value.values(): validate_send_actions(item)
        elif isinstance(value,list):
            for item in value: validate_send_actions(item)
    validate_send_actions(full)
    # Add an ordinary extra mDNS service BEFORE mDNS calculates its fixed storage.
    # OpenThread's SRP bridge consumes this same service list.
    services = full['mdns']['services']
    services[:] = [s for s in services if s['service'] != '_thread-sed']
    services.append(mdns.SERVICE_SCHEMA({
        'service': '_thread-sed', 'protocol': '_udp' if config['transport']=='udp' else '_tcp', 'port': config['port'],
        'txt': {'v': '1', 'name': CORE.name,
                'mac': '',
                'encryption': ('aes-gcm' if config['transport']=='udp' else 'noise') if CONF_ENCRYPTION in config else 'none',
                'transport': config['transport'],
                'sleep': '1' if 'deep_sleep_id' in config else '0'},
    }))
    # MAC is a runtime value; mdns codegen reserves its dynamic TXT slot.
    from esphome.core import Lambda
    services[-1]['txt']['mac'] = Lambda('return get_mac_address();')
    return config

FINAL_VALIDATE_SCHEMA = final_validate

def _stable_config(value):
    # Normalize leaves separately: avoid alias/object identity and dictionary
    # insertion order leaking into the YAML dumper's whole-document output.
    if isinstance(value, dict):
        return {str(key): _stable_config(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_stable_config(item) for item in value]
    return yaml_util.dump(value)

@coroutine_with_priority(CoroPriority.WEB)
async def to_code(config):
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)
    CORE.register_controller(var)
    request_log_listener()
    cg.add(var.set_port(config['port']))
    cg.add(var.set_reboot_timeout(0))
    cg.add(var.set_manual_send(config['send_mode']=='manual'))
    if config['send_mode']=='auto':
        cg.add(var.enable_auto_ack(config['ack_timeout'],config['ack_resend']))
    if 'on_ack' in config:
        await automation.build_automation(var.get_auto_ack_trigger(),
            [(cg.std_string,'batch_id'),(cg.uint32,'elapsed_ms'),(cg.bool_,'has_pending')],config['on_ack'])
    if 'on_ack_error' in config:
        await automation.build_automation(var.get_auto_ack_error_trigger(),
            [(cg.std_string,'batch_id'),(cg.std_string,'reason')],config['on_ack_error'])
    cg.add(var.set_batch_delay(config['batch_delay']))
    for flag in ('USE_THREAD_SED_API', 'USE_THREAD_SED_API_HOMEASSISTANT_SERVICES', 'USE_THREAD_SED_API_HOMEASSISTANT_STATES'):
        cg.add_define(flag)
    cg.add_define('MAX_THREAD_SED_API_CONNECTIONS', 2)
    cg.add_define('THREAD_SED_API_MAX_SEND_QUEUE', 16)
    cg.add(var.set_sleep_budget(config['sleep_ack_timeout']))
    if 'sleep_duration' in config:
        cg.add(var.set_sleep_seconds(int(config['sleep_duration'].total_milliseconds)//1000))
    if config['transport']=='udp':
        cg.add_define('USE_THREAD_SED_API_UDP')
        if CONF_ENCRYPTION in config:
            cg.add_define('USE_THREAD_SED_UDP_ENCRYPTION')
            cg.add(var.set_udp_key(list(base64.b64decode(config[CONF_ENCRYPTION][CONF_KEY]))))
            if CORE.is_nrf52:
                from esphome.components.zephyr import zephyr_add_prj_conf
                zephyr_add_prj_conf('PSA_WANT_ALG_GCM', True)
                zephyr_add_prj_conf('PSA_WANT_KEY_TYPE_AES', True)
            else:
                from esphome.components.esp32 import add_idf_sdkconfig_option
                add_idf_sdkconfig_option('CONFIG_MBEDTLS_GCM_C', True)
    else:
        if CONF_ENCRYPTION in config:
            cg.add(var.set_noise_psk(new_psk_progmem(config[CONF_ENCRYPTION][CONF_KEY])))
            cg.add_define('USE_THREAD_SED_API_NOISE')
            cg.add_define('USE_THREAD_SED_API_NOISE_PSK_FROM_YAML')
            enable_spare_ephemeral()
        else:
            cg.add_define('USE_THREAD_SED_API_PLAINTEXT')
        cg.add_define('USE_THREAD_SED_API_OUTGOING_CONNECTION')
        cg.add_define('THREAD_SED_API_OUTGOING_CONNECTION_PORT', 6064)
        cg.add_define('THREAD_SED_API_OUTGOING_CONNECTION_DELAY', 1000)
    # Stable across identical builds/wakes; include component defaults and
    # ESPHome version so descriptor changes need no user revision knob.
    # Validated config contains generated IDs whose allocation order can vary.
    # Canonical user config plus all loaded component sources captures defaults
    # without making the fingerprint depend on those generated IDs.
    material = hashlib.sha256(('thread-sed-v2:' + version('esphome') + json.dumps(_stable_config(CORE.raw_config),sort_keys=True,separators=(',',':'))).encode())
    for domain in sorted(CORE.loaded_integrations):
        component = importlib.import_module(f'esphome.components.{domain}')
        filename = getattr(component, '__file__', None)
        if filename is None:
            continue
        material.update(domain.encode())
        for source in sorted(Path(filename).parent.iterdir()):
            if source.suffix in ('.py', '.cpp', '.h'):
                material.update(source.name.encode())
                material.update(source.read_bytes())
    fingerprint = material.hexdigest()[:32]
    cg.add_define('THREAD_SED_SCHEMA', fingerprint)
    cg.add_define('THREAD_SED_FAST_POLL_MS', int(config['fast_poll_interval'].total_milliseconds))
    cg.add_define('USE_THREAD_SED_NRF52' if CORE.is_nrf52 else 'USE_THREAD_SED_ESP32')
    if 'deep_sleep_id' in config:
        cg.add_define('USE_THREAD_SED_SLEEP')
        cg.add(var.set_deep_sleep(await cg.get_variable(config['deep_sleep_id'])))
    if CORE.is_nrf52:
        from esphome.components.zephyr import zephyr_add_prj_conf
        zephyr_add_prj_conf('NET_TCP' if config['transport']=='tcp' else 'NET_UDP', True)
    cg.add_global(ns.using)


# Upstream's optional translation units must follow this fork's feature flags.
# In particular, user_services.h hides its types without USER_DEFINED_ACTIONS.
_define_filter = filter_source_files_from_defines({
    'udp_transport.cpp': 'USE_THREAD_SED_API_UDP',
    'user_services.cpp': 'USE_THREAD_SED_API_USER_DEFINED_ACTIONS',
    'api_frame_helper_noise.cpp': 'USE_THREAD_SED_API_NOISE',
    'api_frame_helper_plaintext.cpp': 'USE_THREAD_SED_API_PLAINTEXT',
    'api_wizard.cpp': 'USE_THREAD_SED_API_WIZARD',
    'api_outgoing_connection.cpp': 'USE_THREAD_SED_API_OUTGOING_CONNECTION',
})

def FILTER_SOURCE_FILES():
    excluded = _define_filter()
    if get_logger_level() != 'VERY_VERBOSE':
        excluded.append('api_pb2_dump.cpp')
    return excluded
