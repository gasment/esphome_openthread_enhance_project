"""Project nRF52 board profiles and optional SWD/RTT logging."""

import os
from pathlib import Path
import esphome.codegen as cg
import esphome.config_validation as cv
from esphome.components import logger
from esphome.components.zephyr import zephyr_add_prj_conf, zephyr_data
from esphome.const import CONF_ID
from esphome.core import CORE, CoroPriority, coroutine_with_priority

DEPENDENCIES = ["nrf52", "logger"]
CONF_SWD_RTT_LOGS = "swd_rtt_logs"
CONF_E73_OR_ND05_BOARD_CONFIG = "e73_or_nd05_board_config"
CONF_FAULT_DIAGNOSTICS = "fault_diagnostics"
CUSTOM_BOARD = "e73_or_nd05/nrf52840"
CONFIG_SCHEMA = cv.All(
    cv.Schema({
        cv.Optional(CONF_SWD_RTT_LOGS, default=True): cv.boolean,
        cv.Optional(CONF_E73_OR_ND05_BOARD_CONFIG, default=False): cv.boolean,
        cv.Optional(CONF_FAULT_DIAGNOSTICS, default=False): cv.boolean,
    }),
    cv.only_on_nrf52,
    cv.only_with_framework("zephyr"),
)


def final_validate(config):
    from esphome import final_validate as fv

    board = zephyr_data()["board"]
    board_profile = config[CONF_E73_OR_ND05_BOARD_CONFIG]
    if board_profile and board != CUSTOM_BOARD:
        raise cv.Invalid(
            f"{CONF_E73_OR_ND05_BOARD_CONFIG}: true requires "
            f"nrf52.board: {CUSTOM_BOARD}"
        )
    if board == CUSTOM_BOARD and not board_profile:
        raise cv.Invalid(
            f"nrf52.board: {CUSTOM_BOARD} requires "
            f"zephyr_board_function.{CONF_E73_OR_ND05_BOARD_CONFIG}: true"
        )
    if (
        board == CUSTOM_BOARD
        and (fv.full_config.get().get("nrf52") or {}).get("dcdc") is True
    ):
        raise cv.Invalid(
            f"nrf52.board: {CUSTOM_BOARD} fixes REG1 to LDO; "
            "nrf52.dcdc: true is not supported"
        )

    if config[CONF_SWD_RTT_LOGS]:
        if "debug" in fv.full_config.get():
            raise cv.Invalid(
                "zephyr_board_function.swd_rtt_logs cannot be combined with debug"
            )
        if fv.full_config.get()["logger"].get("baud_rate", 115200) != 0:
            raise cv.Invalid(
                "zephyr_board_function.swd_rtt_logs requires logger.baud_rate: 0 "
                "to release physical logging ports"
            )
        if fv.full_config.get()["logger"].get("task_log_buffer_size", 768) == 0:
            raise cv.Invalid(
                "zephyr_board_function.swd_rtt_logs requires "
                "logger.task_log_buffer_size > 0 for worker-thread logs"
            )
    elif config[CONF_FAULT_DIAGNOSTICS]:
        raise cv.Invalid(
            f"{CONF_FAULT_DIAGNOSTICS} requires {CONF_SWD_RTT_LOGS}: true"
        )
    return config


FINAL_VALIDATE_SCHEMA = final_validate


@coroutine_with_priority(CoroPriority.EARLY_INIT - 1)
async def to_code(config):
    if config[CONF_E73_OR_ND05_BOARD_CONFIG]:
        # BOARD_ROOT points to a directory containing a "boards" child. This
        # component is the root, while the target lives under boards/e73_or_nd05.
        board_root = Path(__file__).parent
        existing = os.environ.get("BOARD_ROOT")
        board_root_value = (
            f"{board_root};{existing}" if existing else str(board_root)
        )
        # Zephyr consumes CMake's BOARD_ROOT. Keep the prefixed environment
        # variable as a project-level alias for diagnostics and wrappers.
        os.environ["BOARD_ROOT"] = board_root_value
        os.environ["ZEPHYR_BOARD_ROOT"] = board_root_value

    if not config[CONF_SWD_RTT_LOGS]:
        return

    # baud_rate=0 also suppresses Logger::write_msg_(). Use the public listener
    # interface, which still receives formatted messages before that gate.
    logger.request_log_listener()
    log = await cg.get_variable(CORE.config["logger"][CONF_ID])
    cg.add(cg.RawStatement(f"zephyr_board_function::install_log_bridge({log});"))
    # Platform defaults must be emitted before the final RTT Kconfig overrides.
    CORE.add_job(configure_rtt, config[CONF_FAULT_DIAGNOSTICS])


async def configure_rtt(fault_diagnostics=False):
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
        # Immediate mode is opt-in for fault investigation: a fatal handler
        # may reboot before the deferred logging thread can drain its queue.
        "LOG_MODE_DEFERRED": not fault_diagnostics,
        "LOG_MODE_IMMEDIATE": fault_diagnostics,
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
    if fault_diagnostics:
        # Catch a stack crossing before it corrupts a saved return address or
        # another thread's context. Do not change any thread's stack budget.
        options.update({
            "THREAD_NAME": True,
            "THREAD_STACK_INFO": True,
            "INIT_STACKS": True,
            "HW_STACK_PROTECTION": True,
            "STACK_SENTINEL": True,
        })
        # These symbols are only available in deferred mode.
        for option in ("LOG_PROCESS_THREAD", "LOG_PROCESS_TRIGGER_THRESHOLD", "LOG_BUFFER_SIZE", "LOG_BLOCK_IN_THREAD"):
            options.pop(option)
    for option, value in options.items():
        zephyr_add_prj_conf(option, value)

    # RTT must not change UART device status or pinctrl. Peripheral UARTs are
    # owned by the UART component; baud_rate=0 prevents logger from using them.
