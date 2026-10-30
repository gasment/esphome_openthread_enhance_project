"""Compressed MCUboot OTA over Thread for the project's nRF52840 target."""

import esphome.config_validation as cv
import esphome.codegen as cg
from esphome.components import logger
from esphome.components.nrf52.framework import include_west_project
from esphome.components.zephyr import zephyr_add_prj_conf, zephyr_data
from esphome.components.zephyr.const import BOOTLOADER_MCUBOOT, KEY_BOOTLOADER, KEY_SYSBUILD
from esphome.const import CONF_ID
from esphome.core import CORE, coroutine_with_priority
from esphome.coroutine import CoroPriority

CODEOWNERS = ["@gasment"]
DEPENDENCIES = ["openthread_nrf52", "logger"]

CONFIG_SCHEMA = cv.All(
    cv.Schema({}),
    cv.only_on_nrf52,
    cv.only_with_framework("zephyr"),
)


def _final_validate(config):
    if zephyr_data()[KEY_BOOTLOADER] != BOOTLOADER_MCUBOOT:
        raise cv.Invalid("thread_ota_nrf52 requires nrf52.bootloader: mcuboot")
    if zephyr_data()["board"] != "adafruit_feather_nrf52840/nrf52840":
        raise cv.Invalid("thread_ota_nrf52 first version supports only adafruit_feather_nrf52840/nrf52840")


FINAL_VALIDATE_SCHEMA = _final_validate


@coroutine_with_priority(CoroPriority.OTA_UPDATES)
async def to_code(config):
    zephyr_data()[KEY_SYSBUILD] = True
    include_west_project("mcuboot")
    include_west_project("zcbor")

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
    # The SWD component already bridges ESPHome messages into Zephyr printk.
    if "swd_rtt_nrf52" not in CORE.loaded_integrations:
        logger.request_log_listener()
        log = await cg.get_variable(CORE.config["logger"][CONF_ID])
        cg.add(cg.RawStatement(f"thread_ota_nrf52::install_log_bridge({log});"))
