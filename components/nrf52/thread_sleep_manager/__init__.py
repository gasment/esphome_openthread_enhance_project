"""Centralized Thread activity and System ON sleep coordinator for nRF52."""

import esphome.codegen as cg
import esphome.config_validation as cv
from esphome import automation
from esphome.components import binary_sensor, output
from esphome.components.deep_sleep_nrf52 import DeepSleepComponent
from esphome.components.openthread_nrf52 import OpenThreadComponent
from esphome.const import CONF_ID

DEPENDENCIES = ["nrf52", "deep_sleep_nrf52", "openthread_nrf52"]

ns = cg.esphome_ns.namespace("thread_sleep_manager")
ThreadSleepManager = ns.class_("ThreadSleepManager", cg.Component)
ThreadProxyClient = cg.esphome_ns.namespace("thread_proxy_client_nrf52").class_(
    "ThreadProxyClient", cg.Component
)
ThreadP2PTransporter = cg.esphome_ns.namespace(
    "thread_p2p_transporter_nrf52"
).class_("ThreadP2PTransporter", cg.Component)
StatusFlag = ns.enum("StatusFlag")
StatusCondition = ns.class_("StatusCondition", automation.Condition)

STATUS_CONDITIONS = {
    "is_awake": "PHASE_AWAKE",
    "is_activity": "PHASE_ACTIVITY",
    "is_wait_reconnect": "PHASE_WAIT_RECONNECT",
    "is_reattaching": "PHASE_REATTACHING",
    "is_ready": "PHASE_READY",
    "is_sleeping": "PHASE_SLEEPING",
    "link_ready": "LINK_READY",
    "transport_busy": "TRANSPORT_BUSY",
    "joiner_active": "JOINER_ACTIVE",
    "can_sleep": "CAN_SLEEP",
}


def validate(config):
    if config["wake_timeout"].total_milliseconds <= config["success_duration"].total_milliseconds:
        raise cv.Invalid("wake_timeout must be longer than success_duration")
    if (
        config["wake_timeout"].total_milliseconds
        <= config["error_blink_interval"].total_milliseconds
    ):
        raise cv.Invalid("wake_timeout must be longer than error_blink_interval")
    return config


CONFIG_SCHEMA = cv.All(
    cv.Schema(
        {
            cv.GenerateID(): cv.declare_id(ThreadSleepManager),
            cv.Required("deep_sleep_id"): cv.use_id(DeepSleepComponent),
            cv.Required("openthread_id"): cv.use_id(OpenThreadComponent),
            cv.Optional("proxy_client_id"): cv.use_id(ThreadProxyClient),
            cv.Optional("p2p_transporter_id"): cv.use_id(ThreadP2PTransporter),
            cv.Optional("binary_sensor_input", default=[]): cv.All(
                cv.ensure_list(cv.use_id(binary_sensor.BinarySensor)), cv.Unique()
            ),
            cv.Optional("indicator_output_id"): cv.use_id(output.BinaryOutput),
            cv.Optional("wake_timeout", default="45s"): cv.positive_time_period_milliseconds,
            cv.Optional("reconnect_after", default="10s"): cv.positive_time_period_milliseconds,
            cv.Optional("reconnect_timeout", default="20s"): cv.positive_time_period_milliseconds,
            cv.Optional("quiet_delay", default="200ms"): cv.positive_time_period_milliseconds,
            cv.Optional("activity_blink_duration", default="120ms"): cv.positive_time_period_milliseconds,
            cv.Optional("success_duration", default="500ms"): cv.positive_time_period_milliseconds,
            cv.Optional("error_blinks", default=3): cv.int_range(min=1, max=20),
            cv.Optional("error_blink_interval", default="200ms"): cv.positive_time_period_milliseconds,
            cv.Optional("joiner_blink_interval", default="500ms"): cv.positive_time_period_milliseconds,
        }
    ).extend(cv.COMPONENT_SCHEMA),
    validate,
)


async def to_code(config):
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)

    cg.add(var.set_deep_sleep(await cg.get_variable(config["deep_sleep_id"])))
    cg.add(var.set_openthread(await cg.get_variable(config["openthread_id"])))

    if "proxy_client_id" in config:
        cg.add_define("USE_THREAD_SLEEP_MANAGER_PROXY")
        cg.add(var.set_proxy_client(await cg.get_variable(config["proxy_client_id"])))

    if "p2p_transporter_id" in config:
        cg.add_define("USE_THREAD_SLEEP_MANAGER_P2P")
        cg.add(var.set_p2p_transporter(await cg.get_variable(config["p2p_transporter_id"])))

    for sensor_id in config["binary_sensor_input"]:
        cg.add(var.add_binary_sensor_input(await cg.get_variable(sensor_id)))

    if "indicator_output_id" in config:
        cg.add(var.set_indicator_output(await cg.get_variable(config["indicator_output_id"])))

    cg.add(var.set_timing(
        config["wake_timeout"].total_milliseconds,
        config["reconnect_after"].total_milliseconds,
        config["reconnect_timeout"].total_milliseconds,
        config["quiet_delay"].total_milliseconds,
        config["activity_blink_duration"].total_milliseconds,
        config["success_duration"].total_milliseconds,
        config["error_blinks"],
        config["error_blink_interval"].total_milliseconds,
        config["joiner_blink_interval"].total_milliseconds,
    ))

    cg.add_define("USE_THREAD_SLEEP_MANAGER")


def register_status_condition(name, flag):
    @automation.register_condition(
        f"thread_sleep_manager.{name}",
        StatusCondition,
        cv.maybe_simple_value(
            {cv.GenerateID(): cv.use_id(ThreadSleepManager)}, key=CONF_ID
        ),
    )
    async def status_condition_to_code(config, condition_id, template_arg, args):
        var = cg.new_Pvariable(
            condition_id, template_arg, await cg.get_variable(config[CONF_ID])
        )
        cg.add(var.set_flag(flag))
        return var


for _name, _flag in STATUS_CONDITIONS.items():
    register_status_condition(_name, getattr(StatusFlag, _flag))
