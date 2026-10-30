"""Event-driven, explicitly selected Thread entity sends for ESP32/ESP-IDF."""
import math
import re
import ipaddress

from esphome import automation
import esphome.codegen as cg
import esphome.config_validation as cv
from esphome.components import sensor, binary_sensor, text_sensor, event, openthread
from esphome.components import switch, number, select, button, text
from esphome.components.esp32 import include_builtin_idf_component
from esphome.const import CONF_ID, CONF_TRIGGER_ID
from esphome.core import CORE, Lambda
from collections.abc import Mapping

DEPENDENCIES = ["esp32", "openthread"]
CONFLICTS_WITH = ["thread_sed_transporter_nrf52", "thread_reed_transporter"]
AUTO_LOAD = ["sensor", "binary_sensor", "text_sensor", "event", "thread_entity_bridge"]
ns = cg.esphome_ns.namespace("thread_sed_transporter")
ThreadSedTransporter = ns.class_("ThreadSedTransporter", cg.Component)
ReportSensor = ns.class_("ReportSensor")
ReportBinarySensor = ns.class_("ReportBinarySensor")
ReportTextSensor = ns.class_("ReportTextSensor")
ReportEvent = ns.class_("ReportEvent")
SendAction = ns.class_("SendAction", automation.Action)
ReportSuccessTrigger = ns.class_("ReportSuccessTrigger", automation.Trigger.template(cg.uint32))
ReportFailTrigger = ns.class_("ReportFailTrigger", automation.Trigger.template(cg.uint32))
ReportCompleteTrigger = ns.class_("ReportCompleteTrigger", automation.Trigger.template(
    cg.bool_, cg.uint32, cg.uint32, cg.uint32, cg.uint32))


def identifier(value):
    value = cv.string_strict(value)
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,32}", value):
        raise cv.Invalid("Expected 1..32 ASCII letters, digits, '_' or '-'")
    return value


def multicast(value):
    try:
        address = ipaddress.IPv6Address(cv.string_strict(value))
    except ValueError as err:
        raise cv.Invalid(str(err)) from err
    if not address.is_multicast or address.packed[1] != 0x13:
        raise cv.Invalid("Use a transient realm-local multicast address, e.g. ff13::8000:8562")
    return str(address)


def delta(value):
    value = cv.float_(value)
    if not math.isfinite(value) or not 0 <= value <= 3.402823466e38:
        raise cv.Invalid("delta must be finite and non-negative")
    return value


def entry_schema(source_type, record_type, extra):
    return cv.maybe_simple_value(cv.Schema({
        cv.Required(CONF_ID): cv.use_id(source_type),
        cv.GenerateID("entry_id"): cv.declare_id(record_type),
        cv.Optional("source_components"): cv.invalid("Source components own acquisition; remove source_components and use source update_interval or component.update automations"),
        **extra,
    }), key=CONF_ID)


ENTRY_SCHEMA = entry_schema(sensor.Sensor, ReportSensor, {cv.Optional("delta", default=0): delta})
BINARY_SCHEMA = entry_schema(binary_sensor.BinarySensor, ReportBinarySensor, {})
TEXT_SCHEMA = entry_schema(text_sensor.TextSensor, ReportTextSensor,
                          {cv.Optional("max_length", default=32): cv.int_range(min=1, max=128)})

EVENT_SCHEMA = entry_schema(event.Event, ReportEvent, {})

CONTROL_TYPES = {
    "switches": ("switch", switch.Switch),
    "numbers": ("number", number.Number),
    "selects": ("select", select.Select),
    "buttons": ("button", button.Button),
    "text_inputs": ("input_text", text.Text),
}


def control_schema(source_type):
    return cv.maybe_simple_value({cv.Required(CONF_ID): cv.use_id(source_type)}, key=CONF_ID)


def source_metadata(entry, full_config):
    path = full_config.get_path_for_id(entry[CONF_ID])[:-1]
    config = full_config.get_config_for_path(path)
    fields = ("name", "unit_of_measurement", "device_class", "icon", "state_class",
              "accuracy_decimals", "entity_category", "disabled_by_default", "force_update",
              "trigger_on_initial_state", "min_value", "max_value", "step", "options", "mode", "event_types")
    return {field: config[field] for field in fields if field in config}


def validate(config):
    entries = [e for key in ("sensors", "binary_sensors", "text_sensors", "events") for e in config[key]]
    controls = [e for key in CONTROL_TYPES for e in config[key]]
    if not 1 <= len(entries) + len(controls) <= 16:
        raise cv.Invalid("Configure 1..16 total read-only and control entities")
    ids = set()
    for e in entries + controls:
        identity = identifier(str(e[CONF_ID]))
        if identity in ids:
            raise cv.Invalid(f"Duplicate wire entity id: {identity}")
        ids.add(identity)
    if not 1 <= config["discovery"]["timeout"].total_milliseconds <= 10000:
        raise cv.Invalid("discovery.timeout must be 1ms..10s")
    if not 1000 <= config["heartbeat_interval"].total_milliseconds <= 15000:
        raise cv.Invalid("heartbeat_interval must be 1s..15s (gateway lease is 30s)")
    if len(config["name"].encode("utf-8")) > 96:
        raise cv.Invalid("Node name must fit 96 UTF-8 bytes")
    poll = config["active_poll_period"].total_milliseconds
    if poll != 0 and not 10 <= poll <= 0x3FFFFFF:
        raise cv.Invalid("active_poll_period must be 0ms (disabled) or 10ms..67108863ms")
    return config


CONFIG_SCHEMA = cv.All(cv.Schema({
    cv.GenerateID(): cv.declare_id(ThreadSedTransporter),
    cv.Required("openthread_id"): cv.use_id(openthread.OpenThreadComponent),
    cv.Required("node_id"): identifier,
    cv.Optional("name", default="SED node"): cv.string_strict,
    cv.Optional("manifest_revision", default=1): cv.int_range(min=1, max=4294967295),
    cv.Optional("discovery", default={}): cv.Schema({
        cv.Optional("gateway_id"): identifier,
        cv.Optional("multicast_address", default="ff13::8000:8562"): multicast,
        cv.Optional("port", default=18512): cv.port,
        cv.Optional("timeout", default="1s"): cv.positive_time_period_milliseconds,
    }),
    cv.Optional("active_poll_period", default="100ms"): cv.All(cv.positive_time_period_milliseconds, cv.Range(max=cv.TimePeriodMilliseconds(milliseconds=0xFFFFFFFF))),
    cv.Optional("acquisition_delay"): cv.invalid("Source components own acquisition; remove acquisition_delay and schedule acquisition in the source automation"),
    cv.Optional("timeout", default="5s"): cv.All(cv.positive_time_period_milliseconds, cv.Range(min=cv.TimePeriodMilliseconds(milliseconds=1), max=cv.TimePeriodMilliseconds(milliseconds=0xFFFFFFFF))),
    cv.Optional("full_report_every"): cv.invalid("Periodic full reports were removed; use send with force: true in your automation"),
    cv.Optional("control_ready", default=False): cv.templatable(cv.boolean),
    cv.Optional("heartbeat_interval", default="10s"): cv.positive_time_period_milliseconds,
    cv.Optional("sensors", default=[]): cv.ensure_list(ENTRY_SCHEMA),
    cv.Optional("binary_sensors", default=[]): cv.ensure_list(BINARY_SCHEMA),
    cv.Optional("text_sensors", default=[]): cv.ensure_list(TEXT_SCHEMA),
    cv.Optional("events", default=[]): cv.ensure_list(EVENT_SCHEMA),
    **{cv.Optional(key, default=[]): cv.ensure_list(control_schema(t)) for key, (_, t) in CONTROL_TYPES.items()},
    cv.Optional("on_report_success"): automation.validate_automation({
        cv.GenerateID(CONF_TRIGGER_ID): cv.declare_id(ReportSuccessTrigger),
    }),
    cv.Optional("on_report_fail"): automation.validate_automation({
        cv.GenerateID(CONF_TRIGGER_ID): cv.declare_id(ReportFailTrigger),
    }),
    cv.Optional("on_complete"): automation.validate_automation({
        cv.GenerateID(CONF_TRIGGER_ID): cv.declare_id(ReportCompleteTrigger),
    }),
}).extend(cv.COMPONENT_SCHEMA), validate)



async def to_code(config):
    include_builtin_idf_component("json")
    for flag in ("USE_ENTITY_DEVICE_CLASS", "USE_ENTITY_UNIT_OF_MEASUREMENT", "USE_ENTITY_ICON"):
        cg.add_define(flag)
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)
    ot = await cg.get_variable(config["openthread_id"])
    cg.add(var.set_openthread(ot))
    cg.add(var.set_active_poll_period(config["active_poll_period"]))
    cg.add(var.set_node(config["node_id"], config["name"], config["manifest_revision"]))
    d = config["discovery"]
    cg.add(var.set_discovery(d.get("gateway_id", ""), d["multicast_address"], d["port"], d["timeout"]))
    cg.add(var.set_timing(config["timeout"], config["heartbeat_interval"]))
    ready = await cg.templatable(config["control_ready"], [], cg.bool_)
    if isinstance(config["control_ready"], bool):
        cg.add(var.set_control_ready(ready))
    else:
        cg.add(var.set_control_ready_reader(ready))
    for key, kind in (("sensors", "numeric"), ("binary_sensors", "binary"), ("text_sensors", "text"), ("events", "event")):
        for entry in config[key]:
            out = cg.new_Pvariable(entry["entry_id"])
            source = await cg.get_variable(entry[CONF_ID])
            cg.add(out.set_identity(str(entry[CONF_ID])))
            cg.add(out.set_source(source))
            if key == "binary_sensors":
                metadata = source_metadata(entry, CORE.config)
                cg.add(out.set_initial_state_metadata(metadata.get("trigger_on_initial_state", True)))
            if key == "text_sensors":
                cg.add(out.set_max_length(entry["max_length"]))
            if key == "sensors":
                cg.add(out.set_delta(entry["delta"]))
            cg.add(var.add_sensor(out))
    for key, (kind, _) in CONTROL_TYPES.items():
        for entry in config[key]:
            source = await cg.get_variable(entry[CONF_ID])
            cg.add(var.add_control(str(entry[CONF_ID]), source))
    for key in ("on_report_success", "on_report_fail"):
        for conf in config.get(key, []):
            trigger = cg.new_Pvariable(conf[CONF_TRIGGER_ID], var)
            await automation.build_automation(trigger, [(cg.uint32, "report_id")], conf)
    for conf in config.get("on_complete", []):
        trigger = cg.new_Pvariable(conf[CONF_TRIGGER_ID], var)
        await automation.build_automation(trigger, [
            (cg.bool_, "success"), (cg.uint32, "sent"), (cg.uint32, "unchanged"),
            (cg.uint32, "invalid"), (cg.uint32, "errors")], conf)


def ack_timeout(value):
    value = cv.positive_time_period_milliseconds(value)
    if not 1 <= value.total_milliseconds <= 60000:
        raise cv.Invalid("ack.timeout must be 1ms..60s per attempt")
    return value


RESULT_AUTOMATION = automation.validate_automation({}, single=True)

def override_value(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        value = cv.float_(value)
        if not math.isfinite(value) or abs(value) > 3.402823466e38:
            raise cv.Invalid("entity_state must be a finite float32 value")
        return value
    value = cv.string_strict(value)
    if "\0" in value:
        raise cv.Invalid("entity_state text must not contain NUL characters")
    return value


ENTITY_SEND_SCHEMA = cv.Schema({
    cv.Required("entity_id"): cv.use_id(cg.EntityBase),
    cv.Optional("entity_state"): cv.templatable(override_value),
})


def send_entries(config):
    if "entities" in config:
        return config["entities"]
    return [{"entity_id": identity, **({"entity_state": config["entity_state"]} if "entity_state" in config else {})}
            for identity in config["entity_id"]]


def registered_kind(parent, identity):
    for key in ("sensors", "binary_sensors", "text_sensors", "events", *CONTROL_TYPES):
        for entry in parent[key]:
            if str(entry[CONF_ID]) == str(identity):
                return key, entry
    return None, None


ACTION_FIELDS = {
    cv.Required(CONF_ID): cv.use_id(ThreadSedTransporter),
    cv.Optional("entity_id"): cv.All(cv.ensure_list(cv.use_id(cg.EntityBase)), cv.Length(min=1, max=16)),
    cv.Optional("entity_state"): cv.templatable(override_value),
    cv.Optional("entities"): cv.All(cv.ensure_list(ENTITY_SEND_SCHEMA), cv.Length(min=1, max=16)),
    cv.Optional("force", default=False): cv.templatable(cv.boolean),
    cv.Optional("ack"): cv.Schema({
        cv.Optional("timeout", default="2s"): ack_timeout,
        cv.Optional("resend", default=0): cv.int_range(min=0, max=10),
    }),
    cv.Optional("on_success"): RESULT_AUTOMATION,
    cv.Optional("on_fail"): RESULT_AUTOMATION,
}
def validate_send(config):
    if ("entity_id" in config) == ("entities" in config):
        raise cv.Invalid("Specify exactly one of entity_id or entities")
    if "entity_state" in config and ("entity_id" not in config or len(config["entity_id"]) != 1):
        raise cv.Invalid("Top-level entity_state requires exactly one entity_id; use entities for batch overrides")
    ids = [str(entry["entity_id"]) for entry in send_entries(config)]
    if len(set(ids)) != len(ids):
        raise cv.Invalid("Duplicate entity_id in send")
    return config


SEND_ACTION_SCHEMA = cv.All(cv.Schema(ACTION_FIELDS), validate_send)


@automation.register_action("thread_sed_transporter.send", SendAction,
                            SEND_ACTION_SCHEMA, synchronous=True)
async def send_to_code(config, action_id, template_arg, args):
    parent = await cg.get_variable(config[CONF_ID])
    var = cg.new_Pvariable(action_id, template_arg, parent)
    parent_config = CORE.config["thread_sed_transporter"]
    for entry in send_entries(config):
        identity = entry["entity_id"]
        if "entity_state" not in entry:
            cg.add(var.add_entity_id(str(identity)))
            continue
        kind, _ = registered_kind(parent_config, identity)
        if kind in ("sensors", "numbers"):
            method, value_type = var.add_numeric_state, cg.float_
        elif kind in ("binary_sensors", "switches"):
            method, value_type = var.add_binary_state, cg.bool_
        else:
            method, value_type = var.add_text_state, cg.std_string
        value = await cg.templatable(entry["entity_state"], args, value_type)
        cg.add(method(str(identity), value))
    force = await cg.templatable(config["force"], args, cg.bool_)
    cg.add(var.set_force(force))
    ack = config.get("ack")
    cg.add(var.set_ack_timeout(ack["timeout"].total_milliseconds if ack is not None else 0))
    cg.add(var.set_ack_resend(ack["resend"] if ack is not None else 0))
    for key, method in (("on_success", var.get_success_trigger), ("on_fail", var.get_fail_trigger)):
        if key in config:
            # One trigger/automation per branch; actions inside can be arbitrarily complex.
            await automation.build_automation(method(), [(cg.uint32, "report_id")], config[key])
    return var


DiscoveryAction = ns.class_("DiscoveryAction", automation.Action)
DISCOVERY_ACTION_SCHEMA = automation.maybe_simple_id({
    cv.Required(CONF_ID): cv.use_id(ThreadSedTransporter),
})


@automation.register_action("thread_sed_transporter.discovery", DiscoveryAction,
                            DISCOVERY_ACTION_SCHEMA, synchronous=True)
async def discovery_to_code(config, action_id, template_arg, args):
    parent = await cg.get_variable(config[CONF_ID])
    return cg.new_Pvariable(action_id, template_arg, parent)


def final_validate(config):
    from esphome import final_validate as fv
    full = fv.full_config.get()
    if full.get("openthread", {}).get("device_type", "FTD") != "MTD":
        raise cv.Invalid("thread_sed_transporter requires openthread.device_type: MTD")
    validate_events(config, full)
    allowed = {str(e[CONF_ID]) for key in ("sensors", "binary_sensors", "text_sensors", "events", *CONTROL_TYPES)
               if key != "buttons" for e in config[key]}

    def walk(value, path=()):
        if isinstance(value, Mapping):
            action = value.get("thread_sed_transporter.send")
            if isinstance(action, Mapping) and str(action[CONF_ID]) == str(config[CONF_ID]):
                for index, entry in enumerate(send_entries(action)):
                    identity = entry["entity_id"]
                    if str(identity) not in allowed:
                        raise cv.Invalid("entity_id must be a state-bearing entity or event registered in this transporter (buttons have no state)")
                    if "entity_state" in entry:
                        state = entry["entity_state"]
                        if isinstance(state, Lambda):
                            continue
                        kind, registered = registered_kind(config, identity)
                        if kind in ("sensors", "numbers") and (isinstance(state, bool) or not isinstance(state, (int, float))):
                            raise cv.Invalid("Numeric entity_state must be a number")
                        if kind in ("binary_sensors", "switches") and not isinstance(state, bool):
                            raise cv.Invalid("Boolean entity_state must be true or false")
                        if kind in ("text_sensors", "events", "selects", "text_inputs") and not isinstance(state, str):
                            raise cv.Invalid("Text entity_state must be a string")
                        if kind == "events" and state not in source_metadata(registered, full).get("event_types", []):
                            types = source_metadata(registered, full).get("event_types", [])
                            location = (*path, "thread_sed_transporter.send", "entities", index, "entity_state") if "entities" in action else (*path, "thread_sed_transporter.send", "entity_state")
                            raise cv.Invalid(
                                f"Event entity_state must be one of the configured event_types: "
                                f"entity_id={str(identity)!r}, entity_state={state!r}, allowed={types!r}; "
                                f"action path: {' -> '.join(map(str, location))}"
                            )
                        if kind == "text_sensors" and len(state.encode("utf-8")) > registered["max_length"]:
                            raise cv.Invalid("entity_state exceeds the registered text max_length")
            for key, child in value.items():
                walk(child, (*path, key))
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, (*path, index))

    walk(full)
    return config


FINAL_VALIDATE_SCHEMA = final_validate


def validate_events(config, full):
    import json
    for entry in config["events"]:
        types = source_metadata(entry, full).get("event_types", [])
        if not 1 <= len(types) <= 32 or len(set(types)) != len(types):
            raise cv.Invalid("Event requires 1..32 distinct event_types")
        if any(not value or len(value.encode("utf-8")) > 64 or "\0" in value for value in types):
            raise cv.Invalid("event_types must be nonempty strings of at most 64 UTF-8 bytes without NUL")
        if len(json.dumps({"event_types": types}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > 900:
            raise cv.Invalid("Event capabilities must fit 900 UTF-8 bytes")

