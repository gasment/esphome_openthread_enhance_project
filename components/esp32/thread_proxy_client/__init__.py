"""Read-only Thread node uplink to a local BR, without SRP or HA connections."""
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
from esphome.components import socket
from esphome.components.noise import validate_encryption_key
from esphome.components.logger import request_log_listener
from esphome.config_helpers import get_logger_level
from esphome.const import CONF_ID, CONF_KEY, CONF_ENCRYPTION
from esphome.core import CORE, CoroPriority, coroutine_with_priority

AUTO_LOAD = ['network', 'socket']
CONFLICTS_WITH = ['api', 'thread_sed_api']
DEPENDENCIES = ['network']
ns = cg.esphome_ns.namespace('thread_proxy_client')
ThreadProxyClient = ns.class_('ThreadProxyClient', cg.Component)
CanSleepCondition = ns.class_('CanSleepCondition', automation.Condition)
IsReadyCondition = ns.class_('IsReadyCondition', automation.Condition)
SendAction = ns.class_('SendAction', automation.Action)

@automation.register_condition('thread_proxy_client.is_ready', IsReadyCondition,
    cv.maybe_simple_value({cv.GenerateID():cv.use_id(ThreadProxyClient)},key=CONF_ID))
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

@automation.register_action('thread_proxy_client.send', SendAction, cv.Schema({
    cv.GenerateID():cv.use_id(ThreadProxyClient),
    cv.Optional('timeout'):cv.invalid('Use action ack_timeout and ack_resend instead of timeout'),
    cv.Optional('ack_timeout',default='500ms'):cv.templatable(ack_timeout),
    cv.Optional('ack_resend',default=2):cv.templatable(ack_resend),
    cv.Optional('entity_id'):cv.All(cv.ensure_list(cv.use_id(cg.EntityBase)),cv.Length(min=1)),
    cv.Optional('on_success'):automation.validate_action_list,
    cv.Optional('on_error'):automation.validate_action_list,
}), synchronous=False)
async def send_to_code(config, action_id, template_arg, args):
    var=cg.new_Pvariable(action_id,template_arg,await cg.get_variable(config[CONF_ID]))
    if CORE.config['thread_proxy_client']['send_mode']!='manual':
        raise cv.Invalid('thread_proxy_client.send requires send_mode: manual')
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

@automation.register_condition('thread_proxy_client.can_sleep', CanSleepCondition,
    cv.Schema({cv.GenerateID():cv.use_id(ThreadProxyClient)}))
async def can_sleep_to_code(config, condition_id, template_arg, args):
    return cg.new_Pvariable(condition_id,template_arg,await cg.get_variable(config[CONF_ID]))

def noise_key(value):
    value = validate_encryption_key(value)
    if not any(base64.b64decode(value)):
        raise cv.Invalid('An all-zero UDP AES key is not permitted')
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
    return socket.consume_sockets(1, 'thread_proxy_client', socket.SocketType.UDP)(config)

CONFIG_SCHEMA = cv.All(cv.Schema({
    cv.GenerateID(): cv.declare_id(ThreadProxyClient),
    cv.Optional(CONF_ENCRYPTION): cv.Schema({cv.Required(CONF_KEY): cv.sensitive(noise_key)}),
    cv.Optional('port', default=6064): cv.port,
    cv.Optional('discovery_timeout', default='3s'): cv.All(cv.positive_time_period_milliseconds, cv.Range(min=cv.TimePeriod(milliseconds=1),max=cv.TimePeriod(milliseconds=60000))),
    cv.Optional('send_mode', default='auto'): cv.one_of('auto','manual',lower=True),
    cv.Optional('on_ready'):automation.validate_automation(single=True),
    cv.Optional('on_disconnect'):automation.validate_automation(single=True),
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
        raise cv.Invalid('thread_proxy_client requires openthread or openthread_nrf52')
    if not full.get('network', {}).get('enable_ipv6'):
        raise cv.Invalid('Thread Proxy client requires network.enable_ipv6: true')
    if 'deep_sleep_id' in config:
        timeout = full['deep_sleep_nrf52']['prepare_timeout']
        if config['sleep_ack_timeout'].total_milliseconds >= timeout.total_milliseconds:
            raise cv.Invalid('sleep_ack_timeout must be shorter than deep_sleep_nrf52.prepare_timeout')
    def validate_send_actions(value):
        if isinstance(value,dict):
            if 'thread_proxy_client.send' in value and config['send_mode']!='manual':
                raise cv.Invalid('thread_proxy_client.send requires send_mode: manual; auto ACKs and callbacks belong to the component')
            for item in value.values(): validate_send_actions(item)
        elif isinstance(value,list):
            for item in value: validate_send_actions(item)
    validate_send_actions(full)
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
    cg.add(var.set_discovery_timeout(config['discovery_timeout'].total_milliseconds))
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
    for event in ('on_ready','on_disconnect'):
        if event in config:
            await automation.build_automation(getattr(var,'get_'+event[3:]+'_trigger')(),[],config[event])
    cg.add(var.set_batch_delay(config['batch_delay']))
    for flag in ('USE_THREAD_PROXY_CLIENT', 'USE_THREAD_PROXY_CLIENT_HOMEASSISTANT_SERVICES', 'USE_THREAD_PROXY_CLIENT_HOMEASSISTANT_STATES'):
        cg.add_define(flag)
    cg.add_define('MAX_THREAD_PROXY_CLIENT_CONNECTIONS', 1)
    cg.add_define('THREAD_PROXY_CLIENT_MAX_SEND_QUEUE', 16)
    cg.add(var.set_sleep_budget(config['sleep_ack_timeout']))
    if 'sleep_duration' in config:
        cg.add(var.set_sleep_seconds(int(config['sleep_duration'].total_milliseconds)//1000))
    cg.add_define('USE_THREAD_PROXY_CLIENT_UDP')
    if CONF_ENCRYPTION in config:
        cg.add_define('USE_THREAD_PROXY_CLIENT_UDP_ENCRYPTION')
        cg.add(var.set_udp_key(list(base64.b64decode(config[CONF_ENCRYPTION][CONF_KEY]))))
        if CORE.is_nrf52:
            from esphome.components.zephyr import zephyr_add_prj_conf
            zephyr_add_prj_conf('PSA_WANT_ALG_GCM', True)
            zephyr_add_prj_conf('PSA_WANT_KEY_TYPE_AES', True)
        else:
            from esphome.components.esp32 import add_idf_sdkconfig_option
            add_idf_sdkconfig_option('CONFIG_MBEDTLS_GCM_C', True)
    # Stable across identical builds/wakes; include component defaults and
    # ESPHome version so descriptor changes need no user revision knob.
    # Validated config contains generated IDs whose allocation order can vary.
    # Canonical user config plus all loaded component sources captures defaults
    # without making the fingerprint depend on those generated IDs.
    material = hashlib.sha256(('thread-proxy-client-v1:' + version('esphome') + json.dumps(_stable_config(CORE.raw_config),sort_keys=True,separators=(',',':'))).encode())
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
    cg.add_define('THREAD_PROXY_CLIENT_SCHEMA', fingerprint)
    cg.add_define('THREAD_PROXY_CLIENT_FAST_POLL_MS', int(config['fast_poll_interval'].total_milliseconds))
    cg.add_define('USE_THREAD_PROXY_CLIENT_NRF52' if CORE.is_nrf52 else 'USE_THREAD_PROXY_CLIENT_ESP32')
    if 'deep_sleep_id' in config:
        cg.add_define('USE_THREAD_PROXY_CLIENT_SLEEP')
        cg.add(var.set_deep_sleep(await cg.get_variable(config['deep_sleep_id'])))
    if CORE.is_nrf52:
        from esphome.components.zephyr import zephyr_add_prj_conf
        zephyr_add_prj_conf('NET_UDP', True)
    cg.add_global(ns.using)


# Only the optional verbose protobuf dump remains; TCP helpers are not shipped.
def FILTER_SOURCE_FILES():
    return ['api_pb2_dump.cpp'] if get_logger_level() != 'VERY_VERBOSE' else []
