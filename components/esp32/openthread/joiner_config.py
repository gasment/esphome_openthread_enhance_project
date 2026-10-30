"""Joiner schema/actions. Keep identical in the ESP32 and nRF52 forks."""
from esphome import automation
from esphome.core import CORE
import esphome.codegen as cg
import esphome.config_validation as cv


def pskd(value):
    value = cv.string_strict(value)
    if not 6 <= len(value) <= 32 or any(c not in "0123456789ABCDEFGHJKLMNPRSTUVWXY" for c in value):
        raise cv.Invalid("PSKd must be 6..32 uppercase letters/digits, excluding I/O/Q/Z")
    return value


def window(value):
    value = cv.positive_time_period_milliseconds(value)
    if not 1000 <= value.total_milliseconds <= 600000:
        raise cv.Invalid("Joiner timeout must be between 1s and 10min")
    return value


def joiner_name(value):
    value = cv.string_strict(value)
    if not 1 <= len(value.encode("utf-8")) <= 48 or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in value):
        raise cv.Invalid("Joiner name must be 1..48 UTF-8 bytes without control characters")
    return value


def schema(ns):
    return cv.Schema({
        cv.Optional("name"): joiner_name,
        cv.Optional("pskd"): cv.sensitive(pskd),
        cv.Optional("on_joiner_start"): automation.validate_automation({cv.GenerateID("trigger_id"): cv.declare_id(ns.class_("JoinerTrigger", automation.Trigger.template()))}),
        cv.Optional("on_joiner_success"): automation.validate_automation({cv.GenerateID("trigger_id"): cv.declare_id(ns.class_("JoinerTrigger", automation.Trigger.template()))}),
        cv.Optional("on_joiner_cancel"): automation.validate_automation({cv.GenerateID("trigger_id"): cv.declare_id(ns.class_("JoinerTrigger", automation.Trigger.template()))}),
        cv.Optional("on_joiner_fail"): automation.validate_automation({cv.GenerateID("trigger_id"): cv.declare_id(ns.class_("JoinerFailTrigger", automation.Trigger.template(cg.std_string)))}),
    })


async def to_code(config, ot):
    cg.add_define("USE_OPENTHREAD_JOINER")
    # esphome.name may exceed the advertisement's bounded display-name field.
    name = config.get("name", CORE.name.encode("utf-8")[:48].decode("utf-8", errors="ignore"))
    cg.add(ot.set_joiner_name(name))
    if "pskd" in config:
        cg.add(ot.set_joiner_pskd(config["pskd"]))
    for event in ("start", "success", "cancel", "fail"):
        for entry in config.get("on_joiner_" + event, []):
            trigger = cg.new_Pvariable(entry["trigger_id"])
            cg.add(getattr(ot, "add_joiner_" + event + "_trigger")(trigger))
            await automation.build_automation(trigger, [(cg.std_string, "reason")] if event == "fail" else [], entry)


def register_actions(domain, ns, component):
    start = ns.class_("JoinerStartAction", automation.Action)
    cancel = ns.class_("JoinerCancelAction", automation.Action)
    condition = ns.class_("JoinerActiveCondition", automation.Condition)

    @automation.register_action(domain + ".joiner.start", start, cv.Schema({
        cv.GenerateID(): cv.use_id(component),
        cv.Optional("pskd"): cv.templatable(cv.sensitive(pskd)),
        cv.Optional("timeout", default="120s"): cv.templatable(window),
    }), synchronous=True)
    async def start_to_code(config, action_id, template_arg, args):
        parent = await cg.get_variable(config["id"])
        var = cg.new_Pvariable(action_id, template_arg, parent)
        if "pskd" in config:
            cg.add(var.set_pskd(await cg.templatable(config["pskd"], args, cg.std_string)))
        cg.add(var.set_timeout(await cg.templatable(config["timeout"], args, cg.uint32)))
        return var

    simple = cv.maybe_simple_value({cv.GenerateID(): cv.use_id(component)}, key="id")

    @automation.register_action(domain + ".joiner.cancel", cancel, simple, synchronous=True)
    async def cancel_to_code(config, action_id, template_arg, args):
        return cg.new_Pvariable(action_id, template_arg, await cg.get_variable(config["id"]))

    @automation.register_condition(domain + ".joiner.is_active", condition, simple)
    async def condition_to_code(config, condition_id, template_arg, args):
        return cg.new_Pvariable(condition_id, template_arg, await cg.get_variable(config["id"]))


def validate_actions(full, domain, component_id, enabled):
    if isinstance(full, dict):
        for key, value in full.items():
            if isinstance(key, str) and key.startswith(domain + ".joiner.") and isinstance(value, dict) and str(value.get("id")) == str(component_id) and not enabled:
                raise cv.Invalid(key + " requires joiner mode")
            validate_actions(value, domain, component_id, enabled)
    elif isinstance(full, list):
        for value in full:
            validate_actions(value, domain, component_id, enabled)
