"""Independent Thread Proxy management server for ESP32 Thread BRs."""
import base64
import esphome.codegen as cg
import esphome.config_validation as cv
import esphome.final_validate as fv
from esphome.components import mdns, socket
from esphome.components.thread_br import ThreadBorderRouter
from esphome.const import CONF_ID, CONF_ENCRYPTION, CONF_KEY
from esphome.core import CORE, Lambda, CoroPriority, coroutine_with_priority

DEPENDENCIES = ["esp32", "thread_br"]
MULTI_CONF = False
ns = cg.esphome_ns.namespace("thread_proxy_api")
ThreadProxyAPI = ns.class_("ThreadProxyAPI", cg.Component)

def AUTO_LOAD(config):
    return ["network", "socket", "mdns"] + (["noise"] if config and CONF_ENCRYPTION in config else [])

def key(value):
    from esphome.components.noise import validate_encryption_key
    value = validate_encryption_key(value)
    if not any(base64.b64decode(value)):
        raise cv.Invalid("An all-zero Noise key is not permitted")
    return value

def validate(config):
    return socket.consume_sockets(config["max_connections"] + 1, "thread_proxy_api")(config)

CONFIG_SCHEMA = cv.All(cv.Schema({
    cv.GenerateID(): cv.declare_id(ThreadProxyAPI),
    cv.GenerateID("thread_br_id"): cv.use_id(ThreadBorderRouter),
    cv.Optional("port", default=6065): cv.port,
    cv.Optional("udp", default={}): cv.Schema({
        cv.Optional("queue_groups", default=16): cv.int_range(min=1,max=128),
        cv.Optional("queue_bytes", default=131072): cv.int_range(min=32768,max=1048576),
        cv.Optional(CONF_ENCRYPTION): cv.Schema({cv.Required(CONF_KEY): cv.sensitive(key)}),
    }),
    cv.Optional("max_connections", default=2): cv.int_range(min=1, max=4),
    cv.Optional("status_interval", default="30s"): cv.All(cv.positive_time_period_milliseconds, cv.Range(min=cv.TimePeriod(milliseconds=1000), max=cv.TimePeriod(milliseconds=300000))),
    cv.Optional(CONF_ENCRYPTION): cv.Schema({cv.Required(CONF_KEY): cv.sensitive(key)}),
}).extend(cv.COMPONENT_SCHEMA), validate)

def final_validate(config):
    full = fv.full_config.get()
    if "thread_br" not in full:
        raise cv.Invalid("thread_proxy_api requires a configured thread_br")
    if full.get("mdns", {}).get("disabled"):
        raise cv.Invalid("thread_proxy_api discovery requires mdns to be enabled")
    for domain in ("api", "web_server"):
        other = full.get(domain)
        if other and other.get("port", 6053 if domain == "api" else 80) == config["port"]:
            raise cv.Invalid(f"thread_proxy_api.port must differ from {domain}.port")
    web = full["thread_br"].get("web")
    if web and web.get("port", 80) == config["port"]:
        raise cv.Invalid("thread_proxy_api.port must differ from thread_br.web.port")
    services = full["mdns"]["services"]
    if any(service["service"] == "_thread-proxy" for service in services):
        raise cv.Invalid("_thread-proxy service is managed by thread_proxy_api; remove the manual announcement")
    service = mdns.SERVICE_SCHEMA({
        "service": "_thread-proxy", "protocol": "_tcp", "port": config["port"],
        "txt": {"component": "thread_proxy_api", "version": "1", "id": "", "name": CORE.name,
                "encryption": "noise" if CONF_ENCRYPTION in config else "none"},
    })
    service["txt"]["id"] = Lambda("return get_mac_address();")
    services.append(service)
    return config

FINAL_VALIDATE_SCHEMA = final_validate

@coroutine_with_priority(CoroPriority.WEB)
async def to_code(config):
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)
    cg.add(var.set_parent(await cg.get_variable(config["thread_br_id"])))
    cg.add(var.set_port(config["port"]))
    cg.add(var.set_max_connections(config["max_connections"]))
    cg.add(var.set_status_interval(config["status_interval"].total_milliseconds))
    udp=config["udp"]
    cg.add(var.relay().set_limits(udp["queue_groups"],udp["queue_bytes"]))
    if CONF_ENCRYPTION in udp:
        from esphome.components.esp32 import add_idf_sdkconfig_option
        add_idf_sdkconfig_option("CONFIG_MBEDTLS_GCM_C", True)
        cg.add_define("USE_THREAD_PROXY_UDP_ENCRYPTION")
        cg.add(var.relay().set_key(cg.RawExpression("std::array<uint8_t,32>{" + ",".join(str(b) for b in base64.b64decode(udp[CONF_ENCRYPTION][CONF_KEY])) + "}")))
    cg.add_define("USE_THREAD_PROXY_API")
    cg.add_define("THREAD_PROXY_API_MAX_SEND_QUEUE", 4)
    if CONF_ENCRYPTION in config:
        from esphome.components.noise import new_psk_progmem
        cg.add_define("USE_THREAD_PROXY_API_NOISE")
        cg.add(var.set_noise_psk(new_psk_progmem(config[CONF_ENCRYPTION][CONF_KEY])))
    else:
        cg.add_define("USE_THREAD_PROXY_API_PLAINTEXT")
