#pragma once
#include "esphome/core/defines.h"
#include <cstdint>

namespace esphome::deep_sleep_nrf52 {
enum class PeripheralKind : uint8_t { UART, UARTE, TWI, TWIM, SPI, SPIM };
struct HardwareInterface {
  const char *name;
  uintptr_t base;
  PeripheralKind kind;
};
#ifdef USE_DEEP_SLEEP_NRF52_PERIPHERAL_AUTO_MGR
uint32_t peripheral_enable(const HardwareInterface &interface);
volatile uint32_t *peripheral_baudrate(const HardwareInterface &interface);
bool peripheral_force_stop(const HardwareInterface &interface);
#endif
}  // namespace esphome::deep_sleep_nrf52
