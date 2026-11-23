#pragma once

#include "esphome/components/logger/logger.h"

namespace esphome::thread_ota_nrf52 {

// Used only when zephyr_board_function is absent or RTT logging is disabled.
void install_log_bridge(logger::Logger *log);

}  // namespace esphome::thread_ota_nrf52
