import math

import esphome.codegen as cg
import esphome.config_validation as cv
from esphome.components import sensor
from esphome.components.adc.sensor import ADCSensor
from esphome.const import CONF_ID

DEPENDENCIES = ["nrf52"]
ns = cg.esphome_ns.namespace("battery_level_nrf52")
BatteryLevel = ns.class_("BatteryLevel", sensor.Sensor, cg.PollingComponent)

# Engineering estimate only; not a Philips discharge/capacity calibration.
CR2450 = [(2.00, 0), (2.50, 5), (2.70, 10), (2.80, 25), (2.90, 50), (3.00, 80), (3.20, 100)]


def finite_number(value):
    value = cv.float_(value)
    if not math.isfinite(value):
        raise cv.Invalid("Expected a finite number")
    return value


POINT = cv.Schema({
    cv.Required("voltage"): cv.All(finite_number, cv.float_range(min=0, min_included=False)),
    cv.Required("percentage"): cv.All(finite_number, cv.float_range(min=0, max=100)),
})


def curve(value):
    if isinstance(value, str):
        cv.one_of("CR2450", upper=True)(value)
        return [{"voltage": voltage, "percentage": percentage} for voltage, percentage in CR2450]
    points = cv.All(cv.ensure_list(POINT), cv.Length(min=2, max=32))(value)
    for previous, current in zip(points, points[1:]):
        if current["voltage"] <= previous["voltage"] or current["percentage"] < previous["percentage"]:
            raise cv.Invalid("Curve voltages must strictly increase and percentages must not decrease")
    return points


def validate(config):
    if config["min_voltage"] >= config["max_voltage"]:
        raise cv.Invalid("min_voltage must be less than max_voltage")
    return config


CONFIG_SCHEMA = cv.All(sensor.sensor_schema(
    BatteryLevel, unit_of_measurement="%", accuracy_decimals=0,
    device_class="battery", entity_category="diagnostic",
).extend({
    cv.Required("voltage_id"): cv.use_id(ADCSensor),
    cv.Optional("samples", default=3): cv.int_range(min=1, max=255),
    cv.Optional("curve", default="CR2450"): curve,
    cv.Optional("min_voltage", default=1.7): cv.All(finite_number, cv.positive_float),
    cv.Optional("max_voltage", default=3.6): cv.All(finite_number, cv.positive_float),
}).extend(cv.polling_component_schema("never")), cv.only_on_nrf52, validate)


def final_validate(config):
    from esphome import final_validate as fv
    full = fv.full_config.get()
    source = full.get_config_for_path(full.get_path_for_id(config["voltage_id"])[:-1])
    if source.get("raw", False):
        raise cv.Invalid("battery_level_nrf52 requires ADC raw: false (volts)")
    return config


FINAL_VALIDATE_SCHEMA = final_validate


async def to_code(config):
    var = await sensor.new_sensor(config)
    await cg.register_component(var, config)
    source = await cg.get_variable(config["voltage_id"])
    cg.add(var.set_voltage_source(source))
    cg.add(var.set_samples(config["samples"]))
    cg.add(var.set_voltage_range(config["min_voltage"], config["max_voltage"]))
    for point in config["curve"]:
        cg.add(var.add_curve_point(point["voltage"], point["percentage"]))
