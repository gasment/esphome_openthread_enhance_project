#pragma once
#include <cstdint>

namespace esphome::uart_nrf52 {
// The NCS 2.9.2 legacy nrfx driver accepts only this fixed table.
constexpr bool nrf52_driver_baudrate(uint32_t baud) {
  switch (baud) {
    case 1200: case 2400: case 4800: case 9600: case 14400:
    case 19200: case 28800: case 31250: case 38400: case 56000:
    case 57600: case 76800: case 115200: case 230400: case 250000:
    case 460800: case 921600: case 1000000:
      return true;
    default:
      return false;
  }
}

// nRF52840 UARTE fractional divider: 16MHz base, 32-bit fraction, lower
// 12 bits unused. Keep the vendor's table values for its supported rates.
constexpr uint32_t nrf52_custom_baudrate_register(uint32_t baud) {
  return static_cast<uint32_t>((((uint64_t(baud) << 32) + 8000000) / 16000000 + 0x800) & 0xFFFFF000);
}
static_assert(nrf52_custom_baudrate_register(256000) == 0x04189000);
}  // namespace esphome::uart_nrf52
