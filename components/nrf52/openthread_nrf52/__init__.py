"""Independent nRF52/Zephyr OpenThread integration (ESPHome 2026.9.1)."""
from pathlib import Path

from esphome import automation
import esphome.codegen as cg
import esphome.config_validation as cv
import esphome.final_validate as fv
from esphome.components.mdns import MDNSComponent, enable_mdns_storage
from esphome.components.network import add_use_address
from esphome.components.zephyr import add_extra_build_file, zephyr_add_prj_conf
from esphome.const import CONF_CHANNEL, CONF_ID, CONF_OUTPUT_POWER, CONF_USE_ADDRESS
from esphome.core import CORE, CoroPriority, coroutine_with_priority

from .const import (
    CONF_CHILD_TIMEOUT, CONF_DEVICE_TYPE, CONF_EXT_PAN_ID, CONF_FORCE_DATASET, CONF_LOG_LEVEL,
    CONF_MDNS_ID, CONF_MESH_LOCAL_PREFIX, CONF_NETWORK_KEY, CONF_NETWORK_NAME,
    CONF_PAN_ID, CONF_POLL_PERIOD, CONF_PSKC, CONF_SRP_ID, CONF_TLV,
)
from .dataset import dataset_tlv, fixed_hex, mesh_local_prefix, network_name, validate_tlv
from . import joiner_config

AUTO_LOAD = ["network", "mdns"]
DEPENDENCIES = ["nrf52"]
CONFLICTS_WITH = ["openthread", "openthread_info", "thread_br", "wifi"]

ns = cg.esphome_ns.namespace("openthread_nrf52")
OpenThreadComponent = ns.class_("OpenThreadComponent", cg.Component)
OpenThreadSrpComponent = ns.class_("OpenThreadSrpComponent", cg.Component)

# OpenThread stack levels, independent of ESPHome's logger.level.
OT_LOG_LEVELS = {"NONE": None, "ERROR": "CRIT", "WARN": "WARN", "NOTICE": "NOTE", "INFO": "INFO", "DEBUG": "DEBG"}


def poll_period(value):
    period = cv.positive_time_period_milliseconds(value)
    if period.total_milliseconds != 0 and not 10 <= period.total_milliseconds <= 0x3FFFFFF:
        raise cv.Invalid("poll_period must be 0s (MED, continuous reception) or between 10ms and 67108863ms (SED)")
    return period


def child_timeout(value):
    period = cv.positive_time_period_milliseconds(value)
    if period.total_milliseconds % 1000 or not 1 <= period.total_seconds <= 0xFFFFFFFF:
        raise cv.Invalid("child_timeout must be whole seconds between 1s and 4294967295s")
    return period


def validate(config):
    if not CORE.is_nrf52 or not CORE.using_zephyr:
        raise cv.Invalid("openthread_nrf52 requires nRF52 with Zephyr")
    if CONF_USE_ADDRESS not in config:
        config[CONF_USE_ADDRESS] = f"{CORE.name}.local"
    if config[CONF_DEVICE_TYPE] != "MTD":
        for key in (CONF_POLL_PERIOD, CONF_CHILD_TIMEOUT):
            if key in config:
                raise cv.Invalid(f"{key} is only allowed with device_type: MTD", path=[key])
    return config


def validate_network(config):
    if CONF_TLV in config and any(key not in (CONF_TLV, CONF_FORCE_DATASET) for key in config):
        raise cv.Invalid("Use either tlv or individual dataset fields, not both")
    return config


CONFIG_SCHEMA = cv.All(
    cv.Schema({
        cv.GenerateID(): cv.declare_id(OpenThreadComponent),
        cv.GenerateID(CONF_SRP_ID): cv.declare_id(OpenThreadSrpComponent),
        cv.GenerateID(CONF_MDNS_ID): cv.use_id(MDNSComponent),
        cv.Optional(CONF_DEVICE_TYPE, default="FTD"): cv.one_of("FTD", "MTD", upper=True),
        cv.Optional(CONF_LOG_LEVEL, default="NONE"): cv.one_of(*OT_LOG_LEVELS, upper=True),
        cv.Optional("thread_network_config"): cv.All(cv.Schema({
            cv.Optional(CONF_FORCE_DATASET, default=False): cv.boolean,
            cv.Optional(CONF_TLV): cv.sensitive(validate_tlv),
            cv.Optional(CONF_CHANNEL): cv.int_range(min=11, max=26),
            cv.Optional(CONF_NETWORK_NAME): network_name,
            cv.Optional(CONF_NETWORK_KEY): cv.sensitive(fixed_hex(16)),
            cv.Optional(CONF_PAN_ID): cv.hex_int_range(min=0, max=0xFFFE),
            cv.Optional(CONF_EXT_PAN_ID): fixed_hex(8),
            cv.Optional(CONF_PSKC): cv.sensitive(fixed_hex(16)),
            cv.Optional(CONF_MESH_LOCAL_PREFIX): mesh_local_prefix,
        }), cv.has_exactly_one_key(CONF_NETWORK_KEY, CONF_TLV), validate_network),
        cv.Optional("joiner"): joiner_config.schema(ns),
        cv.Optional(CONF_USE_ADDRESS): cv.string_strict,
        cv.Optional(CONF_OUTPUT_POWER): cv.All(cv.decibel, cv.int_range(min=-40, max=8)),
        cv.Optional(CONF_POLL_PERIOD): poll_period,
        cv.Optional(CONF_CHILD_TIMEOUT): child_timeout,
    }).extend(cv.COMPONENT_SCHEMA),
    cv.has_exactly_one_key("thread_network_config", "joiner"),
    cv.require_esphome_version(2026, 9, 1),
    validate,
)


def final_validate(config):
    full = fv.full_config.get()
    joiner_config.validate_actions(full, "openthread_nrf52", config[CONF_ID], "joiner" in config)
    if not full.get("network", {}).get("enable_ipv6", False):
        raise cv.Invalid("openthread_nrf52 requires network.enable_ipv6: true")
    if full.get("mdns", {}).get("disabled", False):
        raise cv.Invalid("openthread_nrf52 SRP requires mdns to be enabled")
    if config[CONF_DEVICE_TYPE] != "MTD":
        # All IDs have been resolved at this stage, including action references
        # nested in scripts, triggers, conditions and other automations.
        def check_actions(value):
            if isinstance(value, dict):
                for key, entry in value.items():
                    if key in ("openthread_nrf52.keep_alive", "openthread_nrf52.set_poll_period"):
                        if str(entry[CONF_ID]) == str(config[CONF_ID]):
                            raise cv.Invalid(f"{key} is only allowed with device_type: MTD")
                    check_actions(entry)
            elif isinstance(value, list):
                for entry in value:
                    check_actions(entry)
        check_actions(full)


FINAL_VALIDATE_SCHEMA = final_validate


@coroutine_with_priority(CoroPriority.COMMUNICATION)
async def to_code(config):
    cg.add_define("USE_OPENTHREAD_NRF52")
    level = config[CONF_LOG_LEVEL]
    cg.add_define("USE_OPENTHREAD_NRF52_LOG_LEVEL", level)
    zephyr_add_prj_conf("OPENTHREAD_DEBUG", level != "NONE")
    for suffix in ("CRIT", "WARN", "NOTE", "INFO", "DEBG"):
        zephyr_add_prj_conf("OPENTHREAD_LOG_LEVEL_" + suffix, OT_LOG_LEVELS[level] == suffix)
    if level != "NONE":
        zephyr_add_prj_conf("LOG", True)
    # ESPHome network/mDNS/factory-reset integration uses this capability flag.
    # Only a forwarding header is supplied; the official component is not loaded.
    cg.add_define("USE_OPENTHREAD")
    if not add_extra_build_file(
        "src/esphome/components/openthread/openthread.h",
        Path(__file__).parent / "compat" / "openthread.h",
    ):
        raise cv.Invalid("Another component already supplies the OpenThread compatibility header")
    if "thread_network_config" in config:
        network = config["thread_network_config"]
        cg.add_define("USE_OPENTHREAD_NRF52_TLVS", dataset_tlv(network))
        if network[CONF_FORCE_DATASET]:
            cg.add_define("USE_OPENTHREAD_NRF52_FORCE_DATASET")
    enable_mdns_storage()
    ot = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(ot, config)
    if "joiner" in config:
        await joiner_config.to_code(config["joiner"], ot)
        # Use independent guards, never change the user's prevent flag.
        full = CORE.config
        sleeps = full.get("deep_sleep_nrf52", [])
        if isinstance(sleeps, dict):
            sleeps = [sleeps]
        for entry in sleeps:
            sleep = await cg.get_variable(entry[CONF_ID])
            cg.add(sleep.add_sleep_guard(cg.RawExpression(f"[]() {{ return !{config[CONF_ID]}->is_joiner_active(); }}")))
    zephyr_add_prj_conf("OPENTHREAD_JOINER", "joiner" in config)
    zephyr_add_prj_conf("OPENTHREAD_JOINER_AUTOSTART", False)
    if "joiner" in config:
        zephyr_add_prj_conf("MBEDTLS_HEAP_SIZE", 16384)
        zephyr_add_prj_conf("OPENTHREAD_CUSTOM_PARAMETERS", "OPENTHREAD_CONFIG_JOINER_ADV_EXPERIMENTAL_ENABLE=1")
    add_use_address(ot, config[CONF_USE_ADDRESS])
    if config[CONF_DEVICE_TYPE] == "MTD" and CONF_POLL_PERIOD in config:
        cg.add(ot.set_poll_period(config[CONF_POLL_PERIOD]))
    if config[CONF_DEVICE_TYPE] == "MTD" and CONF_CHILD_TIMEOUT in config:
        cg.add(ot.set_child_timeout(config[CONF_CHILD_TIMEOUT].total_seconds))
    if CONF_OUTPUT_POWER in config:
        cg.add(ot.set_output_power(config[CONF_OUTPUT_POWER]))
    srp = cg.new_Pvariable(config[CONF_SRP_ID])
    await cg.register_component(srp, config)
    cg.add(srp.set_mdns(await cg.get_variable(config[CONF_MDNS_ID])))
    for key in (
        "NET_L2_OPENTHREAD", "OPENTHREAD_MANUAL_START", "OPENTHREAD_SRP_CLIENT",
        "OPENTHREAD_SLAAC", "NET_UDP",
    ):
        zephyr_add_prj_conf(key, True)
    # Match ESPHome's Nordic feature-set selection and build OT from sources.
    zephyr_add_prj_conf("OPENTHREAD_SOURCES", True)
    zephyr_add_prj_conf(f"OPENTHREAD_NORDIC_LIBRARY_{config[CONF_DEVICE_TYPE]}", True)
    zephyr_add_prj_conf(f"OPENTHREAD_{config[CONF_DEVICE_TYPE]}", True)
    if config[CONF_DEVICE_TYPE] == "MTD":
        # Link mode is controlled by poll_period, including runtime actions.
        zephyr_add_prj_conf("OPENTHREAD_MTD_SED", False)
    zephyr_add_prj_conf("MAIN_STACK_SIZE", 4096)
    # Reserve space for link-local/ML-EID and overlapping OMR/application addresses.
    zephyr_add_prj_conf("NET_IF_UNICAST_IPV6_ADDR_COUNT", 8)


PollPeriodAction = ns.class_(
    "OpenThreadComponentPollPeriodAction", automation.Action,
    cg.Parented.template(OpenThreadComponent),
)

KeepAliveAction = ns.class_("OpenThreadKeepAliveAction", automation.Action)


@automation.register_action(
    "openthread_nrf52.keep_alive", KeepAliveAction,
    cv.maybe_simple_value({cv.GenerateID(): cv.use_id(OpenThreadComponent)}, key=CONF_ID),
    synchronous=True,
)
async def keep_alive_to_code(config, action_id, template_arg, args):
    parent = await cg.get_variable(config[CONF_ID])
    return cg.new_Pvariable(action_id, template_arg, parent)


@automation.register_action(
    "openthread_nrf52.set_poll_period", PollPeriodAction,
    automation.maybe_conf(CONF_POLL_PERIOD, cv.Schema({
        cv.GenerateID(): cv.use_id(OpenThreadComponent),
        cv.Required(CONF_POLL_PERIOD): cv.templatable(poll_period),
    })),
    synchronous=True,
)
async def poll_period_to_code(config, action_id, template_arg, args):
    parent = await cg.get_variable(config[CONF_ID])
    var = cg.new_Pvariable(action_id, template_arg, parent)
    cg.add(var.set_poll_period(await cg.templatable(config[CONF_POLL_PERIOD], args, cg.uint32)))
    return var

joiner_config.register_actions("openthread_nrf52", ns, OpenThreadComponent)
