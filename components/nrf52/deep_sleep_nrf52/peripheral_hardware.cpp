#include "peripheral_hardware.h"
#ifdef USE_DEEP_SLEEP_NRF52_PERIPHERAL_AUTO_MGR
#include <hal/nrf_uart.h>
#include <hal/nrf_uarte.h>
#include <hal/nrf_twi.h>
#include <hal/nrf_twim.h>
#include <hal/nrf_spi.h>
#include <hal/nrf_spim.h>
#include <zephyr/kernel.h>

namespace esphome::deep_sleep_nrf52 {
namespace {
constexpr int64_t STOP_TIMEOUT_MS = 50;
uint32_t expected_enable(PeripheralKind kind) {
  switch (kind) {
    case PeripheralKind::UART: return UART_ENABLE_ENABLE_Enabled;
    case PeripheralKind::UARTE: return UARTE_ENABLE_ENABLE_Enabled;
    case PeripheralKind::TWI: return TWI_ENABLE_ENABLE_Enabled;
    case PeripheralKind::TWIM: return TWIM_ENABLE_ENABLE_Enabled;
    case PeripheralKind::SPI: return SPI_ENABLE_ENABLE_Enabled;
    case PeripheralKind::SPIM: return SPIM_ENABLE_ENABLE_Enabled;
  }
  return UINT32_MAX;
}
bool wait_stopped(volatile uint32_t *event, volatile uint32_t *other = nullptr) {
  const int64_t deadline = k_uptime_get() + STOP_TIMEOUT_MS;
  do {
    if (*event != 0 && (other == nullptr || *other != 0)) return true;
    k_busy_wait(10);
  } while (k_uptime_get() < deadline);
  return false;
}
}  // namespace

uint32_t peripheral_enable(const HardwareInterface &interface) {
  switch (interface.kind) {
    case PeripheralKind::UART: return reinterpret_cast<NRF_UART_Type *>(interface.base)->ENABLE;
    case PeripheralKind::UARTE: return reinterpret_cast<NRF_UARTE_Type *>(interface.base)->ENABLE;
    case PeripheralKind::TWI: return reinterpret_cast<NRF_TWI_Type *>(interface.base)->ENABLE;
    case PeripheralKind::TWIM: return reinterpret_cast<NRF_TWIM_Type *>(interface.base)->ENABLE;
    case PeripheralKind::SPI: return reinterpret_cast<NRF_SPI_Type *>(interface.base)->ENABLE;
    case PeripheralKind::SPIM: return reinterpret_cast<NRF_SPIM_Type *>(interface.base)->ENABLE;
  }
  return UINT32_MAX;
}

volatile uint32_t *peripheral_baudrate(const HardwareInterface &interface) {
  if (interface.kind == PeripheralKind::UART) return &reinterpret_cast<NRF_UART_Type *>(interface.base)->BAUDRATE;
  if (interface.kind == PeripheralKind::UARTE) return &reinterpret_cast<NRF_UARTE_Type *>(interface.base)->BAUDRATE;
  return nullptr;
}

bool peripheral_force_stop(const HardwareInterface &interface) {
  const uint32_t enabled = peripheral_enable(interface);
  if (enabled == 0) return true;
  // These controllers share register blocks on nRF52. Never issue tasks for
  // the DT-selected personality if the hardware is enabled in another mode.
  if (enabled != expected_enable(interface.kind)) return false;
  // Mask controller interrupts while issuing STOP, so an ISR cannot restart
  // DMA during shutdown. On failure restore the exact original mask.
  switch (interface.kind) {
    case PeripheralKind::UARTE: {
      auto *reg = reinterpret_cast<NRF_UARTE_Type *>(interface.base);
      const uint32_t interrupts = reg->INTENSET;
      nrf_uarte_int_disable(reg, UINT32_MAX);
      nrf_uarte_event_clear(reg, NRF_UARTE_EVENT_RXTO);
      nrf_uarte_event_clear(reg, NRF_UARTE_EVENT_TXSTOPPED);
      nrf_uarte_task_trigger(reg, NRF_UARTE_TASK_STOPRX);
      nrf_uarte_task_trigger(reg, NRF_UARTE_TASK_STOPTX);
      if (!wait_stopped(&reg->EVENTS_RXTO, &reg->EVENTS_TXSTOPPED)) {
        nrf_uarte_int_enable(reg, interrupts);
        return false;
      }
      nrf_uarte_disable(reg);
      break;
    }
    case PeripheralKind::TWI: {
      auto *reg = reinterpret_cast<NRF_TWI_Type *>(interface.base);
      const uint32_t interrupts = reg->INTENSET;
      nrf_twi_int_disable(reg, UINT32_MAX);
      nrf_twi_event_clear(reg, NRF_TWI_EVENT_STOPPED);
      nrf_twi_task_trigger(reg, NRF_TWI_TASK_STOP);
      if (!wait_stopped(&reg->EVENTS_STOPPED)) {
        nrf_twi_int_enable(reg, interrupts);
        return false;
      }
      nrf_twi_disable(reg);
      break;
    }
    case PeripheralKind::TWIM: {
      auto *reg = reinterpret_cast<NRF_TWIM_Type *>(interface.base);
      const uint32_t interrupts = reg->INTENSET;
      nrf_twim_int_disable(reg, UINT32_MAX);
      nrf_twim_event_clear(reg, NRF_TWIM_EVENT_STOPPED);
      nrf_twim_task_trigger(reg, NRF_TWIM_TASK_STOP);
      if (!wait_stopped(&reg->EVENTS_STOPPED)) {
        nrf_twim_int_enable(reg, interrupts);
        return false;
      }
      nrf_twim_disable(reg);
      break;
    }
    case PeripheralKind::SPIM: {
      auto *reg = reinterpret_cast<NRF_SPIM_Type *>(interface.base);
      const uint32_t interrupts = reg->INTENSET;
      nrf_spim_int_disable(reg, UINT32_MAX);
      nrf_spim_event_clear(reg, NRF_SPIM_EVENT_STOPPED);
      nrf_spim_task_trigger(reg, NRF_SPIM_TASK_STOP);
      if (!wait_stopped(&reg->EVENTS_STOPPED)) {
        nrf_spim_int_enable(reg, interrupts);
        return false;
      }
      nrf_spim_disable(reg);
      break;
    }
    case PeripheralKind::SPI:
      // Legacy SPI has no EasyDMA/STOP task; disabling ends controller activity.
      nrf_spi_int_disable(reinterpret_cast<NRF_SPI_Type *>(interface.base), UINT32_MAX);
      nrf_spi_disable(reinterpret_cast<NRF_SPI_Type *>(interface.base));
      break;
    case PeripheralKind::UART:
      // Legacy UART has no TXSTOPPED confirmation. Do not invent a completion
      // event or disable it while transmission may still be in progress.
      return false;
  }
  return peripheral_enable(interface) == 0;
}
}  // namespace esphome::deep_sleep_nrf52
#endif
