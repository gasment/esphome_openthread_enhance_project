"""Independent System ON sleep lifecycle for nRF52/Zephyr."""
from pathlib import Path

from esphome import automation, pins
import esphome.codegen as cg
import esphome.config_validation as cv
import esphome.final_validate as fv
from esphome.components.zephyr import add_extra_build_file, zephyr_add_prj_conf
from esphome.const import CONF_ID, CONF_PIN, CONF_NUMBER, CONF_TRIGGER_ID
from esphome.core import CORE

DEPENDENCIES = ["nrf52"]
CONFLICTS_WITH = ["deep_sleep"]
ns = cg.esphome_ns.namespace("deep_sleep_nrf52")
DeepSleepComponent = ns.class_("DeepSleepComponent", cg.Component)
WakeupPinMode = ns.enum("WakeupPinMode")
WakeupMode = ns.enum("WakeupMode")
WakeupCause = ns.enum("WakeupCause")
AbortReason = ns.enum("AbortReason")
TriggerEvent = ns.enum("TriggerEvent", is_class=True)
EVENTS = {name: getattr(TriggerEvent, name) for name in ("one_click", "two_click", "hold")}
MODES = {name: getattr(WakeupPinMode, name) for name in ("KEEP_AWAKE", "IGNORE", "INVERT_WAKEUP")}
GROUP_MODES = {name: getattr(WakeupMode, "WAKE_" + name)
               for name in ("ANY", "ANY_LOW", "ANY_HIGH", "ALL_LOW", "ALL_HIGH")}
SleepTrigger = ns.class_("SleepTrigger", automation.Trigger.template())
WakeTrigger = ns.class_("WakeTrigger", automation.Trigger.template(WakeupCause, cg.int32))
PinEventTrigger = ns.class_("PinEventTrigger", automation.Trigger.template(TriggerEvent, cg.uint32))
VbusWakeTrigger = ns.class_("VbusWakeTrigger", automation.Trigger.template())
AbortTrigger = ns.class_("AbortTrigger", automation.Trigger.template(AbortReason))


def duration(value):
    return cv.All(cv.positive_time_period_milliseconds,
                  cv.Range(min=cv.TimePeriodMilliseconds(milliseconds=1),
                           max=cv.TimePeriodMilliseconds(milliseconds=0xFFFFFFFE)))(value)


def wake_pin(value):
    if isinstance(value, str) and value.upper() == "VBUS":
        value = {CONF_NUMBER: "VBUS"}
    if isinstance(value, dict) and str(value.get(CONF_NUMBER, "")).upper() == "VBUS":
        return cv.Schema({
            cv.Required(CONF_NUMBER): cv.one_of("VBUS", upper=True),
            cv.Optional("inverted", default=False): cv.boolean,
        })(value)
    value = pins.internal_gpio_input_pin_schema(value)
    # The installed nRF52 schema also accepts 48 (outside P1.0..P1.15).
    if not 0 <= value[CONF_NUMBER] <= 47:
        raise cv.Invalid("Wakeup pin must be P0.0..P0.31 or P1.0..P1.15")
    return value


def trigger_automations(value):
    automation_schema = automation.validate_automation({
        cv.GenerateID(CONF_TRIGGER_ID): cv.declare_id(PinEventTrigger),
        cv.Optional("event"): cv.enum(EVENTS)})
    if isinstance(value, dict) and any(name in value for name in EVENTS):
        # Normalize named branches to the same internal filtered triggers.
        grouped = cv.Schema({cv.Optional(name): automation.validate_automation({
            cv.GenerateID(CONF_TRIGGER_ID): cv.declare_id(PinEventTrigger)}) for name in EVENTS})(value)
        result = []
        for name, branches in grouped.items():
            for branch in branches:
                branch["event"] = cv.enum(EVENTS)(name)
                result.append(branch)
        return result
    # Retain the previous filtered/list and unfiltered lambda forms.
    return automation_schema(value)


def pin_schema(value):
    raw_pin = value.get(CONF_PIN) if isinstance(value, dict) else None
    raw_number = raw_pin.get(CONF_NUMBER) if isinstance(raw_pin, dict) else raw_pin
    is_vbus = str(raw_number).upper() == "VBUS"
    fields = {
        cv.Required(CONF_PIN): wake_pin,
        cv.Optional("wakeup_pin_mode", default="KEEP_AWAKE"): cv.enum(MODES, upper=True),
    }
    if is_vbus:
        fields[cv.Optional("on_wake")] = automation.validate_automation({
            cv.GenerateID(CONF_TRIGGER_ID): cv.declare_id(VbusWakeTrigger)})
    else:
        if isinstance(value, dict) and "on_wake" in value:
            raise cv.Invalid("GPIO on_wake has been replaced by on_trigger_event")
        fields[cv.Optional("on_trigger_event")] = trigger_automations
        fields[cv.Optional("trigger_event")] = TRIGGER_EVENT_SCHEMA
    result = cv.Schema(fields)(value)
    if not is_vbus:
        if "trigger_event" in result and "on_trigger_event" not in result:
            raise cv.Invalid("trigger_event requires on_trigger_event")
        if "on_trigger_event" in result:
            result.setdefault("trigger_event", TRIGGER_EVENT_SCHEMA({}))
    return result


def event_duration(value):
    return cv.All(duration, cv.Range(max=cv.TimePeriodMilliseconds(milliseconds=60000)))(value)


def validate_event(config):
    ms = {key: value.total_milliseconds for key, value in config.items()}
    if not ms["debounce"] < ms["click_interval"]:
        raise cv.Invalid("click_interval must exceed debounce")
    if not ms["debounce"] < ms["hold_time"]:
        raise cv.Invalid("hold_time must exceed debounce")
    if ms["timeout"] <= ms["hold_time"] + ms["debounce"]:
        raise cv.Invalid("timeout must exceed hold_time plus debounce")
    return config


TRIGGER_EVENT_SCHEMA = cv.All(cv.Schema({
    cv.Optional("debounce", default="20ms"): event_duration,
    cv.Optional("click_interval", default="400ms"): event_duration,
    cv.Optional("hold_time", default="1s"): event_duration,
    cv.Optional("timeout", default="10s"): event_duration,
}), validate_event)


def validate(config):
    if not CORE.is_nrf52 or not CORE.using_zephyr:
        raise cv.Invalid("deep_sleep_nrf52 requires nRF52 with Zephyr")
    if "wakeup_pin" in config and "wakeup_pins" in config:
        raise cv.Invalid("Use either wakeup_pin or wakeup_pins")
    if "wakeup_pin_mode" in config and "wakeup_pin" not in config:
        raise cv.Invalid("wakeup_pin_mode requires wakeup_pin; use per-pin mode for wakeup_pins")
    numbers = [p[CONF_PIN][CONF_NUMBER] for p in config.get("wakeup_pins", [])]
    if len(numbers) != len(set(numbers)):
        raise cv.Invalid("Duplicate wakeup pin")
    group = str(config["wakeup_mode"])
    if group != "ANY":
        gpio_entries = [p for p in config.get("wakeup_pins", []) if p[CONF_PIN][CONF_NUMBER] != "VBUS"]
        if not gpio_entries:
            raise cv.Invalid("Explicit wakeup_mode requires GPIO entries in wakeup_pins; VBUS is independent")
        if any(str(p["wakeup_pin_mode"]) == "INVERT_WAKEUP" for p in gpio_entries):
            raise cv.Invalid("Explicit wakeup_mode cannot use GPIO INVERT_WAKEUP")
        if group in ("ALL_LOW", "ALL_HIGH"):
            modes = {str(p["wakeup_pin_mode"]) for p in gpio_entries}
            if len(modes) != 1:
                raise cv.Invalid("ALL mode GPIO entries must use the same wakeup_pin_mode")
    if not any(key in config for key in ("sleep_duration", "wakeup_pin", "wakeup_pins")):
        raise cv.Invalid("At least sleep_duration or a wakeup pin is required")
    return config


def peripheral_manager_schema(value):
    config = cv.Schema({
        cv.Optional("enabled", default=True): cv.boolean,
        cv.Optional("force_offload", default=False): cv.boolean,
    })(value if isinstance(value, dict) else {"enabled": value})
    if config["force_offload"] and not config["enabled"]:
        raise cv.Invalid("force_offload requires peripheral_auto_mgr.enabled: true")
    return config


CONFIG_SCHEMA = cv.All(cv.Schema({
    cv.GenerateID(): cv.declare_id(DeepSleepComponent),
    cv.Optional("run_duration"): duration,
    cv.Optional("sleep_duration"): duration,
    cv.Optional("prepare_timeout", default="30s"): duration,
    cv.Optional("peripheral_auto_mgr", default=True): peripheral_manager_schema,
    cv.Optional("wakeup_mode", default="ANY"): cv.enum(GROUP_MODES, upper=True),
    cv.Optional("wakeup_pin"): wake_pin,
    cv.Optional("wakeup_pin_mode"): cv.enum(MODES, upper=True),
    cv.Optional("wakeup_pins"): cv.All(cv.ensure_list(pin_schema), cv.Length(min=1, max=49)),
    cv.Optional("on_sleep"): automation.validate_automation({cv.GenerateID(CONF_TRIGGER_ID): cv.declare_id(SleepTrigger)}),
    cv.Optional("on_wake"): automation.validate_automation({cv.GenerateID(CONF_TRIGGER_ID): cv.declare_id(WakeTrigger)}),
    cv.Optional("on_sleep_aborted"): automation.validate_automation({cv.GenerateID(CONF_TRIGGER_ID): cv.declare_id(AbortTrigger)}),
}).extend(cv.COMPONENT_SCHEMA), cv.require_esphome_version(2026, 9, 1), validate)


def final_validate(config):
    full = fv.full_config.get()
    entries = config.get("wakeup_pins", [])
    wake_pins = {p[CONF_PIN][CONF_NUMBER]: p[CONF_PIN] for p in entries}
    if "wakeup_pin" in config:
        wake_pins[config["wakeup_pin"][CONF_NUMBER]] = config["wakeup_pin"]
    # Shared pins may only be polled by another reader. The Nordic interrupt
    # configuration belongs to this component while sleeping.
    for sensor in full.get("binary_sensor", []):
        pin = sensor.get(CONF_PIN, {})
        if (sensor.get("platform") == "gpio"
                and pins.PIN_SCHEMA_REGISTRY.get_key(pin) == "nrf52"
                and pin.get(CONF_NUMBER) in wake_pins):
            if sensor.get("use_interrupt", False):
                raise cv.Invalid("Shared wakeup binary_sensor must use use_interrupt: false")
            wake_mode = wake_pins[pin[CONF_NUMBER]]["mode"]
            for flag in ("input", "output", "pullup", "pulldown", "open_drain"):
                if pin["mode"].get(flag, False) != wake_mode.get(flag, False):
                    raise cv.Invalid("Shared wakeup pin must use the same input/pull configuration")


FINAL_VALIDATE_SCHEMA = final_validate


async def to_code(config):
    cg.add_define("USE_DEEP_SLEEP_NRF52")
    # Zephyr HAL uses this for watchdog pause; API uses the compatibility flag.
    cg.add_define("USE_DEEP_SLEEP")
    if not add_extra_build_file("src/esphome/components/deep_sleep/deep_sleep_component.h",
                                Path(__file__).parent / "compat" / "deep_sleep_component.h"):
        raise cv.Invalid("Another component supplies the deep_sleep compatibility header")
    zephyr_add_prj_conf("GPIO", True)
    zephyr_add_prj_conf("GPIO_NRFX_INTERRUPT", True)
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)
    cg.add(var.set_wakeup_mode(config["wakeup_mode"]))
    manager = config["peripheral_auto_mgr"]
    cg.add(var.set_peripheral_auto_mgr(manager["enabled"]))
    cg.add(var.set_force_offload(manager["force_offload"]))
    if manager["enabled"]:
        cg.add_define("USE_DEEP_SLEEP_NRF52_PERIPHERAL_AUTO_MGR")
        zephyr_add_prj_conf("PM_DEVICE", True)
    for key in ("run_duration", "sleep_duration", "prepare_timeout"):
        if key in config:
            cg.add(getattr(var, "set_" + key)(config[key]))
    entries = list(config.get("wakeup_pins", []))
    if "wakeup_pin" in config:
        entries.append({CONF_PIN: config["wakeup_pin"],
                        "wakeup_pin_mode": config.get("wakeup_pin_mode", WakeupPinMode.KEEP_AWAKE)})
    for entry in entries:
        is_vbus = entry[CONF_PIN][CONF_NUMBER] == "VBUS"
        if is_vbus:
            cg.add_define("USE_DEEP_SLEEP_NRF52_VBUS")
            zephyr_add_prj_conf("NRFX_POWER", True)
            cg.add(var.set_vbus_wakeup(entry[CONF_PIN]["inverted"], entry["wakeup_pin_mode"]))
        else:
            pin = await cg.gpio_pin_expression(entry[CONF_PIN])
            cg.add(var.add_wakeup_pin(pin, entry["wakeup_pin_mode"]))
            if "trigger_event" in entry:
                options = entry["trigger_event"]
                cg.add(var.set_pin_trigger_event(entry[CONF_PIN][CONF_NUMBER], options["debounce"],
                    options["click_interval"], options["hold_time"], options["timeout"]))
        key = "on_wake" if is_vbus else "on_trigger_event"
        for conf in entry.get(key, []):
            if is_vbus:
                trigger = cg.new_Pvariable(conf[CONF_TRIGGER_ID], var)
            else:
                trigger = cg.new_Pvariable(conf[CONF_TRIGGER_ID], var, entry[CONF_PIN][CONF_NUMBER])
                if "event" in conf:
                    cg.add(trigger.set_event_filter(conf["event"]))
            await automation.build_automation(trigger, [] if is_vbus else [(TriggerEvent, "event"), (cg.uint32, "press_duration")], conf)
    for key, args in (("on_sleep", []), ("on_wake", [(WakeupCause, "cause"), (cg.int32, "pin")]),
                      ("on_sleep_aborted", [(AbortReason, "reason")])):
        for conf in config.get(key, []):
            trigger = cg.new_Pvariable(conf[CONF_TRIGGER_ID], var)
            await automation.build_automation(trigger, args, conf)


ACTION_SCHEMA = automation.maybe_simple_id(cv.Schema({cv.GenerateID(): cv.use_id(DeepSleepComponent)}))


@automation.register_action("deep_sleep_nrf52.enter", ns.class_("EnterAction", automation.Action),
    automation.maybe_simple_id(cv.Schema({cv.GenerateID(): cv.use_id(DeepSleepComponent),
        cv.Optional("sleep_duration"): cv.templatable(duration)})), synchronous=True)
async def enter_to_code(config, action_id, template_arg, args):
    parent = await cg.get_variable(config[CONF_ID])
    var = cg.new_Pvariable(action_id, template_arg, parent)
    if "sleep_duration" in config:
        cg.add(var.set_sleep_duration(await cg.templatable(config["sleep_duration"], args, cg.uint32)))
    return var


PREVENT_SCHEMA = automation.maybe_simple_id(cv.Schema({
    cv.GenerateID(): cv.use_id(DeepSleepComponent),
    cv.Optional("value", default=True): cv.templatable(cv.boolean),
}))


def prevent_schema(value):
    # Boolean shorthand resolves the unique sleep component automatically.
    return PREVENT_SCHEMA({"value": value} if isinstance(value, bool) else value)


@automation.register_action("deep_sleep_nrf52.prevent", ns.class_("PreventAction", automation.Action),
                            prevent_schema, synchronous=True)
async def prevent_to_code(config, action_id, template_arg, args):
    var = cg.new_Pvariable(action_id, template_arg, await cg.get_variable(config[CONF_ID]))
    cg.add(var.set_value(await cg.templatable(config["value"], args, cg.bool_)))
    return var


def register_condition(name, class_name):
    @automation.register_condition("deep_sleep_nrf52." + name, ns.class_(class_name, automation.Condition), ACTION_SCHEMA)
    async def to_code(config, condition_id, template_arg, args):
        return cg.new_Pvariable(condition_id, template_arg, await cg.get_variable(config[CONF_ID]))


for _name, _class in (("is_sleeping", "IsSleepingCondition"), ("is_pending", "IsPendingCondition"),
                       ("can_sleep", "CanSleepCondition")):
    register_condition(_name, _class)
