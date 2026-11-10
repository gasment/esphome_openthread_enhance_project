from esphome import pins
import esphome.codegen as cg
import esphome.config_validation as cv
import esphome.final_validate as fv
from esphome.components import switch, thread_br
from esphome.const import CONF_ID, CONF_PIN, CONF_TYPE

DEPENDENCIES = ["thread_br"]
ns = cg.esphome_ns.namespace("thread_br_led")
ThreadBRLEDSwitch = ns.class_("ThreadBRLEDSwitch", switch.Switch, cg.Component)

CONFIG_SCHEMA = switch.switch_schema(
    ThreadBRLEDSwitch, block_inverted=True,
    default_restore_mode="RESTORE_DEFAULT_ON",
).extend({
    cv.Required("thread_br_id"): cv.use_id(thread_br.ThreadBorderRouter),
    cv.Required(CONF_TYPE): cv.one_of("activity", "connection", lower=True),
    cv.Required(CONF_PIN): pins.internal_gpio_output_pin_schema,
}).extend(cv.COMPONENT_SCHEMA)


def final_validate(config):
    full = fv.full_config.get()
    parent = full.get("thread_br", {})
    if parent.get("radio", "uart_rcp") != "native":
        raise cv.Invalid("thread_br_led requires thread_br radio: native")
    if parent.get(CONF_ID) != config["thread_br_id"]:
        raise cv.Invalid("thread_br_id must reference the native Thread BR")
    matching = [item for item in full.get("switch", [])
                if item.get("platform") == "thread_br_led"
                and item.get("thread_br_id") == config["thread_br_id"]
                and item.get(CONF_TYPE) == config[CONF_TYPE]]
    if len(matching) > 1:
        raise cv.Invalid(f"Only one {config[CONF_TYPE]} LED is allowed per Thread BR")


FINAL_VALIDATE_SCHEMA = final_validate


async def to_code(config):
    var = await switch.new_switch(config)
    await cg.register_component(var, config)
    parent = await cg.get_variable(config["thread_br_id"])
    cg.add(var.set_parent(parent))
    cg.add(var.set_activity(config[CONF_TYPE] == "activity"))
    pin = await cg.gpio_pin_expression(config[CONF_PIN])
    cg.add(var.set_pin(pin))
