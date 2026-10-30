from typing import Any

from esphome import automation
import esphome.codegen as cg
from esphome.components import esp32
from esphome.components.esp32 import (
    VARIANT_ESP32C5,
    VARIANT_ESP32C6,
    VARIANT_ESP32H2,
    VARIANT_ESP32H4,
    VARIANT_ESP32H21,
    VARIANT_ESP32S31,
    add_idf_sdkconfig_option,
    get_esp32_variant,
    include_builtin_idf_component,
    only_on_variant,
    require_vfs_select,
)
from esphome.components.mdns import MDNSComponent, enable_mdns_storage
from esphome.components.network import add_use_address
from esphome.components.zephyr import zephyr_add_prj_conf
from esphome.config_helpers import filter_source_files_from_platform
import esphome.config_validation as cv
from esphome.const import (
    CONF_CHANNEL,
    CONF_ENABLE_IPV6,
    CONF_FRAMEWORK,
    CONF_ID,
    CONF_LOG_LEVEL,
    CONF_OUTPUT_POWER,
    CONF_USE_ADDRESS,
    PLATFORM_ESP32,
    PlatformFramework,
)
from esphome.core import (
    CORE,
    ID,
    CoroPriority,
    TimePeriodMilliseconds,
    coroutine_with_priority,
)
from esphome.cpp_generator import MockObj, TemplateArgsType
import esphome.final_validate as fv
from esphome.types import ConfigType

from .const import (
    CONF_DEVICE_TYPE,
    CONF_EXT_PAN_ID,
    CONF_FORCE_DATASET,
    CONF_IPV4_DNS,
    CONF_MDNS_ID,
    CONF_MESH_LOCAL_PREFIX,
    CONF_NETWORK_KEY,
    CONF_NETWORK_NAME,
    CONF_PAN_ID,
    CONF_POLL_PERIOD,
    CONF_PSKC,
    CONF_SRP_ID,
    CONF_TLV,
)
from .dataset import fixed_hex, mesh_local_prefix, network_name
from . import joiner_config

# Project-maintained fork; upstream attribution and baseline are in upstream.json.

AUTO_LOAD = ["network"]

# Wi-fi / Bluetooth / Thread coexistence isn't implemented at this time
# TODO: Doesn't conflict with wifi if you're using another ESP as an RCP (radio coprocessor), but this isn't implemented yet
CONFLICTS_WITH = ["wifi", "thread_br"]

IDF_TO_OT_LOG_LEVEL = {
    "NONE": "NONE",
    "ERROR": "CRIT",
    "WARN": "WARN",
    "INFO": "NOTE",
    "DEBUG": "INFO",
    "VERBOSE": "DEBG",
}

CONF_DEVICE_TYPES = [
    "FTD",
    "MTD",
]


def _validate_txpower(value: Any) -> int | float:
    if CORE.is_esp32:
        variant = get_esp32_variant()

        # HW limits: Datasheet section "802.15.4 RF Transmitter (TX) Characteristics"
        # Further regulatory/soft limit may apply, e.g. by region
        if variant in (VARIANT_ESP32C6, VARIANT_ESP32C5):
            return cv.int_range(min=-15, max=20)(value)
        if variant == VARIANT_ESP32H2:
            return cv.int_range(min=-24, max=20)(value)

    return value  # Unsupported, fail later with clear error


def require_openthread_crypto() -> None:
    """Keep OpenThread's non-TLS crypto through ESPHome's final mbedTLS trim."""
    # SRP signs with P-256 ECDSA even when Joiner/Commissioner (DTLS) are off.
    # These requirement APIs were added in 2026.10; retain 2026.9 compatibility.
    require_ecp = getattr(esp32, "require_mbedtls_ecp", None)
    if require_ecp is not None:
        require_ecp()
    require_extras = getattr(esp32, "require_mbedtls_tls_extras", None)
    if require_extras is not None:
        require_extras(("CONFIG_MBEDTLS_CCM_C", "CONFIG_MBEDTLS_ECDSA_DETERMINISTIC"))

    # Explicit defaults also repair previously cached sdkconfig values. Do not
    # override user sdkconfig_options or enable the unrelated TLS stack.
    set_default = getattr(esp32, "set_idf_sdkconfig_default", add_idf_sdkconfig_option)
    for option in (
        "CONFIG_MBEDTLS_ECP_C",
        "CONFIG_MBEDTLS_ECDH_C",  # IDF 5.5 ECDSA Kconfig depends on ECDH.
        "CONFIG_MBEDTLS_ECDSA_C",
        "CONFIG_MBEDTLS_ECP_DP_SECP256R1_ENABLED",
        "CONFIG_MBEDTLS_ECDSA_DETERMINISTIC",
        "CONFIG_MBEDTLS_CCM_C",
    ):
        set_default(option, True)


def set_sdkconfig_options(config: ConfigType) -> None:
    require_openthread_crypto()
    enabled = "joiner" in config
    add_idf_sdkconfig_option("CONFIG_OPENTHREAD_JOINER", enabled)
    if enabled:
        add_idf_sdkconfig_option("CONFIG_OPENTHREAD_HEADER_CUSTOM", True)
        add_idf_sdkconfig_option("CONFIG_OPENTHREAD_CUSTOM_HEADER_PATH", "src/esphome/components/openthread")
        add_idf_sdkconfig_option("CONFIG_OPENTHREAD_CUSTOM_HEADER_FILE_NAME", "joiner_ot_config.h")
        require_tls = getattr(esp32, "require_mbedtls_tls", None)
        if require_tls is not None:
            require_tls()
        require_extras = getattr(esp32, "require_mbedtls_tls_extras", None)
        if require_extras is not None:
            require_extras(("CONFIG_MBEDTLS_CCM_C", "CONFIG_MBEDTLS_CMAC_C", "CONFIG_MBEDTLS_ECJPAKE_C", "CONFIG_MBEDTLS_KEY_EXCHANGE_ECJPAKE", "CONFIG_MBEDTLS_SSL_PROTO_DTLS"))
        for option in ("CONFIG_MBEDTLS_SSL_PROTO_TLS1_2", "CONFIG_MBEDTLS_SSL_PROTO_DTLS", "CONFIG_MBEDTLS_CMAC_C", "CONFIG_MBEDTLS_ECJPAKE_C", "CONFIG_MBEDTLS_KEY_EXCHANGE_ECJPAKE"):
            add_idf_sdkconfig_option(option, True)
        add_idf_sdkconfig_option("CONFIG_MBEDTLS_TLS_DISABLED", False)

    # and expose options for using SPI/UART RCPs
    add_idf_sdkconfig_option("CONFIG_IEEE802154_ENABLED", True)
    add_idf_sdkconfig_option("CONFIG_OPENTHREAD_RADIO_NATIVE", True)

    # There is a conflict if the logger's uart also uses the default UART, which is seen as a watchdog failure on "ot_cli"
    add_idf_sdkconfig_option("CONFIG_OPENTHREAD_CLI", False)
    # Console is the transport layer for CLI; disable it too since CLI is disabled
    add_idf_sdkconfig_option("CONFIG_OPENTHREAD_CONSOLE_ENABLE", False)

    # Diag unused, if needed for lab/cert/etc tests then enable separately
    add_idf_sdkconfig_option("CONFIG_OPENTHREAD_DIAG", False)

    add_idf_sdkconfig_option("CONFIG_OPENTHREAD_ENABLED", True)
    # Link-local + ML-EID already use two slots. Leave room for overlapping OMR
    # prefixes during BR changes and manually registered application addresses.
    set_default = getattr(esp32, "set_idf_sdkconfig_default", add_idf_sdkconfig_option)
    set_default("CONFIG_LWIP_IPV6_NUM_ADDRESSES", 8)

    network = config.get("thread_network_config", {})
    if not network.get(CONF_TLV):
        if (pan_id := network.get(CONF_PAN_ID)) is not None:
            add_idf_sdkconfig_option("CONFIG_OPENTHREAD_NETWORK_PANID", pan_id)

        if channel := network.get(CONF_CHANNEL):
            add_idf_sdkconfig_option("CONFIG_OPENTHREAD_NETWORK_CHANNEL", channel)

        if (network_key := network.get(CONF_NETWORK_KEY)) is not None:
            add_idf_sdkconfig_option(
                "CONFIG_OPENTHREAD_NETWORK_MASTERKEY", network_key
            )

        if network_name := network.get(CONF_NETWORK_NAME):
            add_idf_sdkconfig_option("CONFIG_OPENTHREAD_NETWORK_NAME", network_name)

        if (ext_pan_id := network.get(CONF_EXT_PAN_ID)) is not None:
            add_idf_sdkconfig_option(
                "CONFIG_OPENTHREAD_NETWORK_EXTPANID", ext_pan_id
            )
        if (mesh_local_prefix := network.get(CONF_MESH_LOCAL_PREFIX)) is not None:
            add_idf_sdkconfig_option(
                "CONFIG_OPENTHREAD_MESH_LOCAL_PREFIX", f"{mesh_local_prefix}".lower()
            )
        if (pskc := network.get(CONF_PSKC)) is not None:
            add_idf_sdkconfig_option(
                "CONFIG_OPENTHREAD_NETWORK_PSKC", pskc
            )

    add_idf_sdkconfig_option("CONFIG_OPENTHREAD_DNS64_CLIENT", True)
    add_idf_sdkconfig_option("CONFIG_OPENTHREAD_DNS_SERVER_ADDR", str(config.get(CONF_IPV4_DNS, "8.8.8.8")))
    add_idf_sdkconfig_option("CONFIG_OPENTHREAD_SRP_CLIENT", True)
    add_idf_sdkconfig_option("CONFIG_OPENTHREAD_SRP_CLIENT_MAX_SERVICES", 5)

    # TODO: Add support for synchronized sleepy end devices (SSED)
    add_idf_sdkconfig_option(f"CONFIG_OPENTHREAD_{config.get(CONF_DEVICE_TYPE)}", True)


openthread_ns = cg.esphome_ns.namespace("openthread")
OpenThreadComponent = openthread_ns.class_("OpenThreadComponent", cg.Component)
OpenThreadSrpComponent = openthread_ns.class_("OpenThreadSrpComponent", cg.Component)

_CONNECTION_SCHEMA = cv.Schema(
    {
        cv.Optional(CONF_PAN_ID): cv.hex_int_range(min=0, max=0xFFFE),
        cv.Optional(CONF_CHANNEL): cv.int_range(min=11, max=26),
        cv.Optional(CONF_NETWORK_KEY): cv.sensitive(fixed_hex(16)),
        cv.Optional(CONF_EXT_PAN_ID): fixed_hex(8),
        cv.Optional(CONF_NETWORK_NAME): network_name,
        cv.Optional(CONF_PSKC): cv.sensitive(fixed_hex(16)),
        cv.Optional(CONF_MESH_LOCAL_PREFIX): mesh_local_prefix,
    }
)


def _validate(config: ConfigType) -> ConfigType:
    if CONF_USE_ADDRESS not in config:
        config[CONF_USE_ADDRESS] = f"{CORE.name}.local"
    device_type = config.get(CONF_DEVICE_TYPE)
    poll_period = config.get(CONF_POLL_PERIOD)
    if (
        device_type == "FTD"
        and poll_period
        and poll_period > TimePeriodMilliseconds(milliseconds=0)
    ):
        raise cv.Invalid(
            f"{CONF_POLL_PERIOD} can only be used with {CONF_DEVICE_TYPE}: MTD"
        )

    return config


def _require_vfs_select(config: ConfigType) -> ConfigType:
    """Register VFS select requirement during config validation."""
    # OpenThread uses esp_vfs_eventfd which requires VFS select support (ESP32 only)
    if CORE.is_esp32:
        require_vfs_select()
    return config


def _validate_platform(config: ConfigType) -> ConfigType:
    if not CORE.is_esp32 or CORE.target_framework != "esp-idf":
        raise cv.Invalid("This project's OpenThread fork supports ESP32 with ESP-IDF only")
    return only_on_variant(
        supported=[
            VARIANT_ESP32C5,
            VARIANT_ESP32C6,
            VARIANT_ESP32H2,
            VARIANT_ESP32H4,
            VARIANT_ESP32H21,
            VARIANT_ESP32S31,
        ]
    )(config)


def _validate_tlv_hex(value: Any) -> str:
    text = cv.string_strict(value).lower()
    if not text or len(text) % 2 or any(c not in "0123456789abcdef" for c in text):
        raise cv.Invalid("tlv must be a nonempty, even-length hexadecimal string")
    raw = bytes.fromhex(text)
    if len(raw) > 254:
        raise cv.Invalid("tlv must fit 254 bytes")
    offset = 0
    while offset < len(raw):
        if offset + 2 > len(raw) or offset + 2 + raw[offset + 1] > len(raw):
            raise cv.Invalid("Truncated dataset TLV")
        offset += 2 + raw[offset + 1]
    return text


def _validate_network(config):
    if CONF_TLV in config and any(key != CONF_TLV and key != CONF_FORCE_DATASET for key in config):
        raise cv.Invalid("Use either tlv or individual dataset fields, not both")
    return config


CONFIG_SCHEMA = cv.All(
    cv.Schema(
        {
            cv.GenerateID(): cv.declare_id(OpenThreadComponent),
            cv.GenerateID(CONF_SRP_ID): cv.declare_id(OpenThreadSrpComponent),
            cv.GenerateID(CONF_MDNS_ID): cv.use_id(MDNSComponent),
            cv.Optional(CONF_DEVICE_TYPE, default="FTD"): cv.one_of(
                *CONF_DEVICE_TYPES, upper=True
            ),

            cv.Optional(CONF_IPV4_DNS, default="8.8.8.8"): cv.All(cv.string_strict, cv.ipv4address),
            cv.Optional("thread_network_config"): cv.All(
                _CONNECTION_SCHEMA.extend({
                    cv.Optional(CONF_FORCE_DATASET, default=False): cv.boolean,
                    cv.Optional(CONF_TLV): cv.sensitive(cv.All(cv.string_strict, _validate_tlv_hex)),
                }),
                cv.has_exactly_one_key(CONF_NETWORK_KEY, CONF_TLV),
                _validate_network,
            ),
            cv.Optional("joiner"): joiner_config.schema(openthread_ns),
            cv.Optional(CONF_USE_ADDRESS): cv.string_strict,
            cv.Optional(CONF_OUTPUT_POWER): cv.All(
                cv.decibel,
                _validate_txpower,
            ),
            cv.Optional(CONF_POLL_PERIOD): cv.positive_time_period_milliseconds,
        }
    ),
    cv.has_exactly_one_key("thread_network_config", "joiner"),
    _validate_platform,
    cv.require_esphome_version(2026, 9, 1),
    _validate,
    _require_vfs_select,
)


def _final_validate(_: ConfigType) -> None:
    full_config = fv.full_config.get()
    joiner_config.validate_actions(full_config, "openthread", _[CONF_ID], "joiner" in _)
    network_config = full_config.get("network", {})
    if not network_config.get(CONF_ENABLE_IPV6, False):
        raise cv.Invalid(
            "OpenThread requires IPv6 to be enabled in the network component. "
            "Please set `enable_ipv6: true` in the `network` configuration."
        )

    if (
        (esp32_config := full_config.get(PLATFORM_ESP32)) is not None
        and (fw_config := esp32_config.get(CONF_FRAMEWORK)) is not None
        and (log_level := fw_config.get(CONF_LOG_LEVEL)) is not None
    ):
        add_idf_sdkconfig_option("CONFIG_OPENTHREAD_LOG_LEVEL_DYNAMIC", False)
        ot_log_level = IDF_TO_OT_LOG_LEVEL.get(log_level, log_level)
        add_idf_sdkconfig_option(f"CONFIG_OPENTHREAD_LOG_LEVEL_{ot_log_level}", True)


FINAL_VALIDATE_SCHEMA = _final_validate

FILTER_SOURCE_FILES = filter_source_files_from_platform(
    {
        "openthread_dns_debug.cpp": {PlatformFramework.ESP32_IDF},
        "openthread_esp.cpp": {
            PlatformFramework.ESP32_IDF,
        },
        "remote_restart.cpp": {PlatformFramework.ESP32_IDF},
        "openthread_zephyr.cpp": {PlatformFramework.NRF52_ZEPHYR},
    }
)


@coroutine_with_priority(CoroPriority.COMMUNICATION)
async def to_code(config: ConfigType) -> None:
    # Re-enable openthread IDF component (excluded by default)
    if CORE.is_esp32:
        include_builtin_idf_component("openthread")
        cg.add_build_flag("-Wl,--wrap=lwip_hook_dns_external_resolve")
        cg.add(openthread_ns.register_dns_debug())
        # Retain IDF assertion text across software resets, in addition to the
        # generic panic_abort addresses retained by ESPHome's crash recorder.
        cg.add_build_flag("-Wl,--wrap=esp_system_abort")
        cg.add(openthread_ns.report_previous_abort())

    cg.add_define("USE_OPENTHREAD")
    network = config.get("thread_network_config", {})
    if network.get(CONF_FORCE_DATASET):
        cg.add_define("USE_OPENTHREAD_FORCE_DATASET")
    if tlv := network.get(CONF_TLV):
        cg.add_define("USE_OPENTHREAD_TLVS", tlv)

    # OpenThread SRP needs access to mDNS services after setup
    enable_mdns_storage()

    ot = cg.new_Pvariable(config[CONF_ID])
    add_use_address(ot, config[CONF_USE_ADDRESS])
    await cg.register_component(ot, config)
    if "joiner" in config:
        await joiner_config.to_code(config["joiner"], ot)

    # FTD accepts an explicit zero as a no-op, but has no MTD poll-period setter.
    if config[CONF_DEVICE_TYPE] == "MTD" and (poll_period := config.get(CONF_POLL_PERIOD)) is not None:
        cg.add(ot.set_poll_period(poll_period))

    srp = cg.new_Pvariable(config[CONF_SRP_ID])
    mdns_component = await cg.get_variable(config[CONF_MDNS_ID])
    cg.add(srp.set_mdns(mdns_component))
    await cg.register_component(srp, config)

    if (output_power := config.get(CONF_OUTPUT_POWER)) is not None:
        cg.add(ot.set_output_power(output_power))

    if CORE.is_esp32:
        set_sdkconfig_options(config)
    elif CORE.using_zephyr:
        zephyr_add_prj_conf("NET_L2_OPENTHREAD", True)
        zephyr_add_prj_conf(
            f"OPENTHREAD_NORDIC_LIBRARY_{config.get(CONF_DEVICE_TYPE)}", True
        )
        zephyr_add_prj_conf(f"OPENTHREAD_{config.get(CONF_DEVICE_TYPE)}", True)
        zephyr_add_prj_conf("MAIN_STACK_SIZE", 4096)


# Actions
OpenThreadComponentPollPeriodAction = openthread_ns.class_(
    "OpenThreadComponentPollPeriodAction",
    automation.Action,
    cg.Parented.template(OpenThreadComponent),
)

POLL_PERIOD_ACTION_SCHEMA = automation.maybe_conf(
    CONF_POLL_PERIOD,
    cv.Schema(
        {
            cv.GenerateID(): cv.use_id(OpenThreadComponent),
            cv.Required(CONF_POLL_PERIOD): cv.templatable(
                cv.positive_time_period_milliseconds
            ),
        }
    ),
)


@automation.register_action(
    "openthread.set_poll_period",
    OpenThreadComponentPollPeriodAction,
    POLL_PERIOD_ACTION_SCHEMA,
    synchronous=True,
)
async def openthread_poll_period_action_to_code(
    config: ConfigType,
    action_id: ID,
    template_arg: cg.TemplateArguments,
    args: TemplateArgsType,
) -> MockObj:
    paren = await cg.get_variable(config[CONF_ID])
    var = cg.new_Pvariable(action_id, template_arg, paren)
    template_ = await cg.templatable(config[CONF_POLL_PERIOD], args, cg.uint32)
    cg.add(var.set_poll_period(template_))
    return var

joiner_config.register_actions("openthread", openthread_ns, OpenThreadComponent)
