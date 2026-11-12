import math

import esphome.codegen as cg
import esphome.config_validation as cv
from esphome.components import sensor
from esphome import pins
from esphome.components.nrf52.const import AIN_TO_GPIO, EXTRA_ADC
from esphome.components.zephyr import zephyr_add_overlay, zephyr_add_overlay_builder, zephyr_add_prj_conf
from esphome.const import CONF_ANALOG, CONF_INPUT, CONF_NUMBER
from esphome.core import CORE
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


def adc_pin(value):
    # Use nRF52's analog pin validation, without importing/loading adc.
    return pins.gpio_pin_schema({CONF_ANALOG: True, CONF_INPUT: True}, internal=True)(value)


adc_dt_spec = cg.global_ns.struct("adc_dt_spec")
ADC_SCHEMA = cv.Schema({
    cv.GenerateID("adc_spec_id"): cv.declare_id(adc_dt_spec),
    cv.Optional("pin", default="VDD"): adc_pin,
    cv.Optional("samples", default=1): cv.int_range(min=1, max=255),
    cv.Optional("sampling_mode", default="avg"): cv.one_of("avg", "min", "max", lower=True),
    cv.Optional("voltage_multiplier", default=1.0): cv.All(finite_number, cv.float_range(min=0, min_included=False)),
})


CONFIG_SCHEMA = cv.All(sensor.sensor_schema(
    BatteryLevel, unit_of_measurement="%", accuracy_decimals=0,
    device_class="battery", entity_category="diagnostic",
).extend({
    cv.Optional("voltage_id"): cv.invalid("voltage_id was removed; configure the built-in adc and optional voltage sensor instead"),
    cv.Optional("adc", default={}): ADC_SCHEMA,
    cv.Optional("voltage"): sensor.sensor_schema(
        unit_of_measurement="V", accuracy_decimals=2, device_class="voltage",
        state_class="measurement", entity_category="diagnostic",
    ),
    cv.Optional("samples", default=3): cv.int_range(min=1, max=255),
    cv.Optional("curve", default="CR2450"): curve,
    cv.Optional("min_voltage", default=1.7): cv.All(finite_number, cv.positive_float),
    cv.Optional("max_voltage", default=3.6): cv.All(finite_number, cv.positive_float),
}).extend(cv.polling_component_schema("never")), cv.only_on_nrf52, cv.only_with_framework("zephyr"), validate)


def _overlay_io_channels():
    # Zephyr infers an io-channels binding for this standard node. Arbitrary
    # unbound nodes retain the DTS text but do not emit the required C macros.
    entries = ", ".join(f"<&adc {i}>" for i in range(CORE.data["adc_channel_id"]))
    return f"/ {{ zephyr,user {{ io-channels = {entries}; }}; }};"


async def to_code(config):
    var = await sensor.new_sensor(config)
    await cg.register_component(var, config)
    adc = config["adc"]
    # Coordinate hardware channel IDs with independently configured inputs.
    # The shared DT list is hardware metadata, not an ADC entity dependency.
    channel_id = CORE.data.setdefault("adc_channel_id", 0)
    if channel_id >= 8:
        raise cv.Invalid("nRF52840 SAADC supports at most 8 configured channels")
    CORE.data["adc_channel_id"] = channel_id + 1
    pin = adc["pin"][CONF_NUMBER]
    if isinstance(pin, int):
        pin = {number: ain for ain, number in AIN_TO_GPIO.items()}[pin]
    elif pin not in EXTRA_ADC:
        raise cv.Invalid("Unsupported nRF52 ADC input")
    gain = "ADC_GAIN_1_2" if pin == "VDDHDIV5" else "ADC_GAIN_1_6"
    zephyr_add_prj_conf("ADC", True)
    zephyr_add_overlay_builder(_overlay_io_channels)
    zephyr_add_overlay(f"""
        &adc {{
            #address-cells = <1>;
            #size-cells = <0>;
            channel@{channel_id} {{
                reg = <{channel_id}>;
                zephyr,gain = "{gain}";
                zephyr,reference = "ADC_REF_INTERNAL";
                zephyr,acquisition-time = <ADC_ACQ_TIME_DEFAULT>;
                zephyr,input-positive = <NRF_SAADC_{pin}>;
                zephyr,resolution = <14>;
                zephyr,oversampling = <8>;
            }};
        }};
    """)
    spec = cg.new_Pvariable(adc["adc_spec_id"], cg.RawExpression(f"ADC_DT_SPEC_GET_BY_IDX(DT_PATH(zephyr_user), {channel_id})"))
    cg.add(var.set_adc_channel(spec))
    cg.add(var.set_adc_samples(adc["samples"]))
    cg.add(var.set_sampling_mode({"avg": 0, "min": 1, "max": 2}[adc["sampling_mode"]]))
    cg.add(var.set_voltage_multiplier(adc["voltage_multiplier"]))
    if "voltage" in config:
        cg.add(var.set_voltage_sensor(await sensor.new_sensor(config["voltage"])))
    cg.add(var.set_samples(config["samples"]))
    cg.add(var.set_voltage_range(config["min_voltage"], config["max_voltage"]))
    for point in config["curve"]:
        cg.add(var.add_curve_point(point["voltage"], point["percentage"]))
