#pragma once

#include "esphome/components/logger/logger.h"
#include <hal/nrf_clock.h>
#include <hal/nrf_power.h>
#include <zephyr/sys/printk.h>
#include <cstring>

namespace esphome::swd_rtt_nrf52 {

// Read only. A bootloader/debugger may already have cleared these bits;
// zero does not prove a power-on reset, and uncleared bits can accumulate.
inline void log_reset_status() {
  const uint32_t reason = NRF_POWER->RESETREAS;
  ESP_LOGI("swd_rtt_nrf52", "RESETREAS=0x%08lx (raw, may be retained/cleared by bootloader)",
           static_cast<unsigned long>(reason));
  ESP_LOGI("swd_rtt_nrf52", "Reset flags: pin=%d watchdog=%d software=%d lockup=%d system_off=%d",
           (reason & POWER_RESETREAS_RESETPIN_Msk) != 0,
           (reason & POWER_RESETREAS_DOG_Msk) != 0,
           (reason & POWER_RESETREAS_SREQ_Msk) != 0,
           (reason & POWER_RESETREAS_LOCKUP_Msk) != 0,
           (reason & POWER_RESETREAS_OFF_Msk) != 0);
}

inline void log_lfclk_status() {
  // Read only: leave clock ownership and startup to Zephyr.
  const uint32_t status = NRF_CLOCK->LFCLKSTAT;
  const uint32_t configured = NRF_CLOCK->LFCLKSRC;
  const bool running = (status & CLOCK_LFCLKSTAT_STATE_Msk) != 0;
  const uint32_t source = (status & CLOCK_LFCLKSTAT_SRC_Msk) >> CLOCK_LFCLKSTAT_SRC_Pos;
  const char *name = "UNKNOWN";
  switch (source) {
    case CLOCK_LFCLKSTAT_SRC_RC:
      name = "LFRC (internal RC)";
      break;
    case CLOCK_LFCLKSTAT_SRC_Xtal:
      name = (configured & CLOCK_LFCLKSRC_EXTERNAL_Msk) != 0
                 ? "external clock input"
                 : "LFXO (external 32.768 kHz crystal)";
      break;
    case CLOCK_LFCLKSTAT_SRC_Synth:
      name = "LFSYNT (HFCLK derived)";
      break;
  }
  ESP_LOGI("swd_rtt_nrf52", "LFCLK: running=%s, source=%s, LFCLKSTAT=0x%08lx, LFCLKSRC=0x%08lx",
           running ? "YES" : "NO", name, static_cast<unsigned long>(status), static_cast<unsigned long>(configured));
}

// Called during generated setup, immediately after the logger's early init.
// Keep baud_rate=0: no physical UART/CDC device is initialized or written.
inline void install_log_bridge(logger::Logger *log) {
  log->add_log_callback(log, [](void *context, uint8_t level, const char *tag, const char *message, size_t length) {
    // In ESPHome 2026.9.1 dump_config checks uart_dev_ even with baud_rate=0.
    // nullptr is intentional for RTT. Replace only that known false error.
    auto *log_instance = static_cast<logger::Logger *>(context);
    if (log_instance->get_baud_rate() == 0 && level == 1 && std::strcmp(tag, "logger") == 0 &&
        (std::strstr(message, "USB_CDC is not ready.") != nullptr ||
         std::strstr(message, "UART0 is not ready.") != nullptr ||
         std::strstr(message, "UART1 is not ready.") != nullptr)) {
      printk("[swd_rtt_nrf52] Physical logger port disabled; RTT active\n");
      return;
    }
    // Do not call ESP_LOG here: that would recurse into the logger. printk is
    // queued by LOG_PRINTK and drained by Zephyr's nonblocking RTT backend.
    printk("%.*s\n", static_cast<int>(length), message);
  });
  printk("[swd_rtt_nrf52] ESPHome log bridge ready\n");
  log_reset_status();
  log_lfclk_status();
}

}  // namespace esphome::swd_rtt_nrf52
