"""Independent two-bus I2C implementation for nRF52840/Zephyr."""

from pathlib import Path
from typing import Any

import esphome
from esphome import pins
import esphome.codegen as cg
from esphome.components.zephyr import (
    add_extra_build_file,
    zephyr_add_overlay,
    zephyr_add_prj_conf,
)
import esphome.config_validation as cv
from esphome.const import (
    CONF_FREQUENCY,
    CONF_I2C_ID,
    CONF_ID,
    CONF_ADDRESS,
    CONF_TIMEOUT,
    CONF_SCAN,
    CONF_SCL,
    CONF_SDA,
)
from esphome.core import CoroPriority, coroutine_with_priority
import esphome.final_validate as fv
from esphome.cpp_generator import MockObj
from esphome.types import ConfigType

CODEOWNERS = ["@gasment"]
DEPENDENCIES = ["nrf52"]
MULTI_CONF = True
MULTI_CONF_NO_DEFAULT = True

i2c_ns = cg.esphome_ns.namespace("i2c")
I2CBus = i2c_ns.class_("I2CBus")
I2CDevice = i2c_ns.class_("I2CDevice")
ZephyrI2CBus = i2c_ns.class_("ZephyrI2CBus", I2CBus, cg.Component)

CONF_HARDWARE_I2C = "hardware_i2c"
PORT_NAMES = {"I2C0": 0, "I2C1": 1}
SPI_CONFLICTS = {0: "spi0", 1: "spi1"}

CONFIG_SCHEMA = cv.All(
    cv.Schema(
        {
            cv.GenerateID(): cv.declare_id(ZephyrI2CBus),
            cv.Optional(CONF_HARDWARE_I2C, default="I2C0"): cv.one_of(*PORT_NAMES, upper=True),
            cv.Required(CONF_SDA): pins.internal_gpio_pin_number,
            cv.Required(CONF_SCL): pins.internal_gpio_pin_number,
            cv.Optional(CONF_FREQUENCY, default="100kHz"): cv.frequency,
            cv.Optional(CONF_SCAN, default=True): cv.boolean,
        }
    ),
    cv.only_on_nrf52,
    cv.only_with_framework("zephyr"),
)


def _entries(value: Any) -> list[ConfigType]:
    if value is None:
        return []
    if isinstance(value, dict):
        return [value]
    return list(value)


def _final_validate(config: ConfigType) -> ConfigType:
    full = fv.full_config.get()
    if "i2c" in full and "i2c_nrf52" in full:
        raise cv.Invalid("i2c and i2c_nrf52 cannot be configured together")

    domain = "i2c" if "i2c" in full and "i2c_nrf52" not in full else "i2c_nrf52"
    buses = _entries(full.get(domain))
    if len(buses) > len(PORT_NAMES):
        raise cv.Invalid("nRF52840 supports at most two i2c_nrf52 buses")

    used_ports: set[int] = set()
    used_pins: dict[int, tuple[str, str]] = {}
    for bus in buses:
        port = PORT_NAMES[bus[CONF_HARDWARE_I2C]]
        if port in used_ports:
            raise cv.Invalid(
                f"{bus[CONF_HARDWARE_I2C]} is assigned to multiple i2c_nrf52 buses"
            )
        used_ports.add(port)

        for key in (CONF_SDA, CONF_SCL):
            pin = bus[key]
            if pin in used_pins:
                previous_port, previous_key = used_pins[pin]
                raise cv.Invalid(
                    f"GPIO{pin} is used by {bus[CONF_HARDWARE_I2C]}.{key} "
                    f"and {previous_port}.{previous_key}"
                )
            used_pins[pin] = (bus[CONF_HARDWARE_I2C], key)

    configured_spi = set()
    for bus in _entries(full.get("spi")):
        interface = bus.get("interface")
        if isinstance(interface, str):
            configured_spi.add(interface.lower())
    for port in used_ports:
        conflict = SPI_CONFLICTS[port]
        if conflict in configured_spi:
            raise cv.Invalid(
                f"I2C{port} and {conflict.upper()} share nRF52840 hardware "
                "and cannot be enabled together"
            )
    return config


FINAL_VALIDATE_SCHEMA = _final_validate


@coroutine_with_priority(CoroPriority.BUS)
async def to_code(config: ConfigType) -> None:
    # Reuse ESPHome's generic I2C base and Zephyr bus implementation without
    # loading the official top-level `i2c:` integration.
    i2c_source = Path(esphome.__file__).parent / "components" / "i2c"
    for filename in (
        "i2c.h",
        "i2c.cpp",
        "i2c_bus.h",
        "i2c_bus.cpp",
        "i2c_bus_zephyr.h",
        "i2c_bus_zephyr.cpp",
    ):
        add_extra_build_file(
            f"src/esphome/components/i2c/{filename}",
            i2c_source / filename,
        )

    port = PORT_NAMES[config[CONF_HARDWARE_I2C]]
    sda = config[CONF_SDA]
    scl = config[CONF_SCL]

    cg.add_define("USE_I2C")
    cg.add_global(i2c_ns.using)
    zephyr_add_prj_conf("I2C", True)
    zephyr_add_overlay(
        f"""
        #include <zephyr/dt-bindings/pinctrl/nrf-pinctrl.h>

        &pinctrl {{
            i2c{port}_default: i2c{port}_default {{
                group1 {{
                    psels = <NRF_PSEL(TWIM_SDA, {sda // 32}, {sda % 32})>,
                        <NRF_PSEL(TWIM_SCL, {scl // 32}, {scl % 32})>;
                }};
            }};
            i2c{port}_sleep: i2c{port}_sleep {{
                group1 {{
                    psels = <NRF_PSEL(TWIM_SDA, {sda // 32}, {sda % 32})>,
                        <NRF_PSEL(TWIM_SCL, {scl // 32}, {scl % 32})>;
                    low-power-enable;
                }};
            }};
        }};

        &i2c{port} {{
            compatible = "nordic,nrf-twim";
            status = "okay";
            pinctrl-0 = <&i2c{port}_default>;
            pinctrl-1 = <&i2c{port}_sleep>;
            pinctrl-names = "default", "sleep";
        }};
        """
    )

    var = cg.new_Pvariable(
        config[CONF_ID], MockObj(f"DEVICE_DT_GET(DT_NODELABEL(i2c{port}))")
    )
    await cg.register_component(var, config)
    cg.add(var.set_sda_pin(sda))
    cg.add(var.set_scl_pin(scl))
    cg.add(var.set_frequency(int(config[CONF_FREQUENCY])))
    cg.add(var.set_scan(config[CONF_SCAN]))


def i2c_device_schema(default_address):
    schema = {cv.GenerateID(CONF_I2C_ID): cv.use_id(I2CBus)}
    if default_address is None:
        schema[cv.Required(CONF_ADDRESS)] = cv.i2c_address
    else:
        schema[cv.Optional(CONF_ADDRESS, default=default_address)] = cv.i2c_address
    return cv.Schema(schema)


async def register_i2c_device(var, config):
    await cg.add_variable(var.set_i2c_bus, config[CONF_I2C_ID])
    cg.add(var.set_i2c_address(config[CONF_ADDRESS]))


def new_i2c_component(config, *args):
    return cg.new_bus_component(register_i2c_device, config, *args)


def final_validate_device_schema(
    name: str,
    *,
    min_frequency: Any = None,
    max_frequency: Any = None,
    min_timeout: Any = None,
    max_timeout: Any = None,
) -> cv.Schema:
    hub_schema = {}
    if min_frequency is not None and max_frequency is not None:
        hub_schema[cv.Required(CONF_FREQUENCY)] = cv.Range(
            min=cv.frequency(min_frequency),
            min_included=True,
            max=cv.frequency(max_frequency),
            max_included=True,
            msg=f"Component {name} requires an I2C frequency compatible with the bus",
        )
    elif min_frequency is not None:
        hub_schema[cv.Required(CONF_FREQUENCY)] = cv.Range(
            min=cv.frequency(min_frequency),
            min_included=True,
            msg=f"Component {name} requires I2C frequency >= {min_frequency}",
        )
    elif max_frequency is not None:
        hub_schema[cv.Required(CONF_FREQUENCY)] = cv.Range(
            max=cv.frequency(max_frequency),
            max_included=True,
            msg=f"Component {name} requires I2C frequency <= {max_frequency}",
        )
    if min_timeout is not None and max_timeout is not None:
        hub_schema[cv.Required(CONF_TIMEOUT)] = cv.Range(
            min=cv.time_period(min_timeout),
            min_included=True,
            max=cv.time_period(max_timeout),
            max_included=True,
            msg=f"Component {name} requires an I2C timeout compatible with the bus",
        )
    elif min_timeout is not None:
        hub_schema[cv.Required(CONF_TIMEOUT)] = cv.Range(
            min=cv.time_period(min_timeout),
            min_included=True,
            msg=f"Component {name} requires I2C timeout >= {min_timeout}",
        )
    elif max_timeout is not None:
        hub_schema[cv.Required(CONF_TIMEOUT)] = cv.Range(
            max=cv.time_period(max_timeout),
            max_included=True,
            msg=f"Component {name} requires I2C timeout <= {max_timeout}",
        )
    return cv.Schema(
        {cv.Required(CONF_I2C_ID): fv.id_declaration_match_schema(hub_schema)},
        extra=cv.ALLOW_EXTRA,
    )
