"""Compressed MCUboot OTA over Thread for the project's nRF52840 target."""

import esphome.config_validation as cv
import esphome.codegen as cg
from esphome.components import logger
from esphome.components.nrf52.framework import include_west_project
from esphome.components.zephyr import (
    zephyr_add_overlay,
    zephyr_add_prj_conf,
    zephyr_data,
)
from esphome.components.zephyr.const import BOOTLOADER_MCUBOOT, KEY_BOOTLOADER, KEY_SYSBUILD
from esphome.const import CONF_ID
from esphome.core import CORE, coroutine_with_priority
from esphome.coroutine import CoroPriority

CONF_MCUBOOT_DCDC = "mcuboot_dcdc"
SUPPORTED_BOARDS = {
    "adafruit_feather_nrf52840/nrf52840",
    "e73_or_nd05/nrf52840",
}
CUSTOM_BOARD = "e73_or_nd05/nrf52840"

CODEOWNERS = ["@gasment"]
DEPENDENCIES = ["openthread_nrf52", "logger"]

CONFIG_SCHEMA = cv.All(
    cv.Schema({
        cv.Optional(CONF_MCUBOOT_DCDC): cv.boolean,
    }),
    cv.only_on_nrf52,
    cv.only_with_framework("zephyr"),
)


def _final_validate(config):
    if zephyr_data()[KEY_BOOTLOADER] != BOOTLOADER_MCUBOOT:
        raise cv.Invalid("thread_ota_nrf52 requires nrf52.bootloader: mcuboot")
    if zephyr_data()["board"] not in SUPPORTED_BOARDS:
        raise cv.Invalid(
            "thread_ota_nrf52 supports only: " + ", ".join(sorted(SUPPORTED_BOARDS))
        )
    if (
        zephyr_data()["board"] == CUSTOM_BOARD
        and config.get(CONF_MCUBOOT_DCDC) is True
    ):
        raise cv.Invalid(
            f"{CUSTOM_BOARD} fixes REG1 to LDO for both images; "
            f"{CONF_MCUBOOT_DCDC}: true is not supported"
        )
    return config


FINAL_VALIDATE_SCHEMA = _final_validate


@coroutine_with_priority(CoroPriority.OTA_UPDATES)
async def to_code(config):
    zephyr_data()[KEY_SYSBUILD] = True
    include_west_project("mcuboot")
    include_west_project("zcbor")

    # MCUboot is a separate Zephyr image under Sysbuild; nrf52.dcdc only
    # configures the application. Write the bootloader-specific Kconfig and
    # Devicetree into zephyr/sysbuild/mcuboot.{conf,overlay} using ESPHome's
    # native image-aware APIs. This avoids modifying generated build output.
    # Unset leaves legacy behaviour unchanged.
    if (
        zephyr_data()["board"] != CUSTOM_BOARD
        and CONF_MCUBOOT_DCDC in config
    ):
        enabled = config[CONF_MCUBOOT_DCDC]
        zephyr_add_prj_conf("BOARD_ENABLE_DCDC", enabled, image="mcuboot")
        zephyr_add_prj_conf("SOC_DCDC_NRF52X", enabled, image="mcuboot")
        # nRF52840 REG1 is the 3.3V-fed regulator. Explicitly pin its initial
        # mode as the Kconfig symbols alone may not override board DTS.
        reg_mode = "NRF5X_REG_MODE_DCDC" if enabled else "NRF5X_REG_MODE_LDO"
        zephyr_add_overlay(
            f"""
            #include <zephyr/dt-bindings/regulator/nrf5x.h>
            &reg1 {{
                regulator-initial-mode = <{reg_mode}>;
            }};
            """,
            image="mcuboot",
        )

    for key in (
        "NET_BUF", "ZCBOR", "NET_SOCKETS", "MCUMGR", "MCUMGR_GRP_IMG",
        "IMG_MANAGER", "STREAM_FLASH", "FLASH_MAP", "FLASH",
        "IMG_ERASE_PROGRESSIVELY", "BOOTLOADER_MCUBOOT",
        "MCUMGR_GRP_OS", "MCUMGR_GRP_OS_MCUMGR_PARAMS",
        "MCUMGR_TRANSPORT_UDP", "MCUMGR_TRANSPORT_UDP_IPV6",
        "LOG", "LOG_OUTPUT", "LOG_MODE_DEFERRED", "LOG_PRINTK", "LOG_PROCESS_THREAD", "PRINTK",
    ):
        zephyr_add_prj_conf(key, True)
    zephyr_add_prj_conf("MCUMGR_TRANSPORT_UDP_IPV4", False)
    zephyr_add_prj_conf("MCUMGR_TRANSPORT_UDP_PORT", 1337)
    zephyr_add_prj_conf("MCUMGR_TRANSPORT_UDP_MTU", 512)
    zephyr_add_prj_conf("MCUMGR_TRANSPORT_NETBUF_SIZE", 640)
    zephyr_add_prj_conf("MCUMGR_TRANSPORT_UDP_STACK_SIZE", 1024)
    zephyr_add_prj_conf("LOG_BLOCK_IN_THREAD", False)
    zephyr_add_prj_conf("LOG_PROCESS_TRIGGER_THRESHOLD", 1)
    # The board function component bridges ESPHome messages into Zephyr printk
    # only when its SWD/RTT logger is enabled.
    board_function = CORE.config.get("zephyr_board_function", {})
    rtt_bridge_enabled = (
        "zephyr_board_function" in CORE.loaded_integrations
        and board_function.get("swd_rtt_logs", True)
    )
    if not rtt_bridge_enabled:
        logger.request_log_listener()
        log = await cg.get_variable(CORE.config["logger"][CONF_ID])
        cg.add(cg.RawStatement(f"thread_ota_nrf52::install_log_bridge({log});"))
