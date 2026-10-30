"""Project SWD/RTT logging, without a physical UART or USB CDC logger."""

import esphome.codegen as cg
import esphome.config_validation as cv
from esphome.components import logger
from esphome.components.zephyr import zephyr_add_prj_conf
from esphome.const import CONF_ID
from esphome.core import CORE, CoroPriority, coroutine_with_priority

DEPENDENCIES = ["nrf52", "logger"]
# The installed debug component selects blocking RTT; do not combine the profiles.
CONFLICTS_WITH = ["debug"]
CONFIG_SCHEMA = cv.All(cv.Schema({}), cv.only_on_nrf52, cv.only_with_framework("zephyr"))


def final_validate(config):
    from esphome import final_validate as fv

    if fv.full_config.get()["logger"].get("baud_rate", 115200) != 0:
        raise cv.Invalid("swd_rtt_nrf52 requires logger.baud_rate: 0 to release physical logging ports")
    if fv.full_config.get()["logger"].get("task_log_buffer_size", 768) == 0:
        raise cv.Invalid("swd_rtt_nrf52 requires logger.task_log_buffer_size > 0 for worker-thread logs")
    return config


FINAL_VALIDATE_SCHEMA = final_validate


@coroutine_with_priority(CoroPriority.EARLY_INIT - 1)
async def to_code(config):
    # baud_rate=0 also suppresses Logger::write_msg_(). Use the public listener
    # interface, which still receives formatted messages before that gate.
    logger.request_log_listener()
    log = await cg.get_variable(CORE.config["logger"][CONF_ID])
    cg.add(cg.RawStatement(f"swd_rtt_nrf52::install_log_bridge({log});"))
    # Platform defaults must be emitted before the final RTT Kconfig overrides.
    CORE.add_job(configure_rtt)


async def configure_rtt():
    options = {
        "USE_SEGGER_RTT": True,
        "CONSOLE": True,
        "PRINTK": True,
        "RTT_CONSOLE": True,
        "UART_CONSOLE": False,
        "SEGGER_RTT_BUFFER_SIZE_UP": 4096,
        "SEGGER_RTT_MODE_NO_BLOCK_SKIP": True,
        "SEGGER_RTT_MODE_BLOCK_IF_FIFO_FULL": False,
        "LOG": True,
        "LOG_MODE_DEFERRED": True,
        "LOG_PRINTK": True,
        "LOG_PROCESS_THREAD": True,
        # Wake on the first message, rather than batching 10 messages or
        # waiting for the default 1000ms timer. No idle polling is added.
        "LOG_PROCESS_TRIGGER_THRESHOLD": 1,
        "LOG_BUFFER_SIZE": 4096,
        "LOG_BLOCK_IN_THREAD": False,
        "LOG_BACKEND_RTT": True,
        "LOG_BACKEND_RTT_BUFFER": 0,
        "LOG_BACKEND_RTT_MODE_DROP": True,
        "LOG_BACKEND_RTT_MODE_BLOCK": False,
        "LOG_BACKEND_RTT_MESSAGE_SIZE": 256,
        "LOG_BACKEND_UART": False,
    }
    for option, value in options.items():
        zephyr_add_prj_conf(option, value)

    # RTT must not change UART device status or pinctrl. Peripheral UARTs are
    # owned by the UART component; baud_rate=0 prevents logger from using them.
