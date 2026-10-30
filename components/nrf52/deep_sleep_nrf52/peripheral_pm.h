#pragma once
#include "esphome/core/defines.h"
#ifdef USE_DEEP_SLEEP_NRF52_PERIPHERAL_AUTO_MGR
#include <zephyr/device.h>
#include <zephyr/pm/device.h>
#include <cstdint>

namespace esphome::deep_sleep_nrf52 {
// A timed-out driver keeps ownership of its request until reset. Never abort
// its thread or race it with a second PM/register operation.
int peripheral_pm_call(const device *dev, pm_device_action action, bool direct = false,
                       volatile uint32_t *baudrate = nullptr, uint32_t saved_baudrate = 0);
bool peripheral_pm_faulted();
}  // namespace esphome::deep_sleep_nrf52
#endif
