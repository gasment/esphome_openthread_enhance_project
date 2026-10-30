#pragma once

#include "esphome/components/logger/logger.h"

namespace esphome::thread_ota_nrf52 {

// Used only when swd_rtt_nrf52 is absent; its existing bridge already feeds printk.
void install_log_bridge(logger::Logger *log);

}  // namespace esphome::thread_ota_nrf52
