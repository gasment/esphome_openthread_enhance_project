#include "sleep_watchdog.h"

#include "esphome/core/hal.h"
#include "esphome/core/log.h"
#include <hal/nrf_wdt.h>
#include <zephyr/init.h>
#include <zephyr/sys/atomic.h>
#include <cinttypes>

namespace {
atomic_t sleep_watchdog_active{0};
uint32_t early_wdt_runstatus{0}, early_wdt_config{0};

// Capture once before application main()/arch_init(). No timer or logging in
// this init callback. This distinguishes an inherited running watchdog from
// one started later by the application; it does not identify its owner.
int capture_early_watchdog() {
  early_wdt_runstatus = NRF_WDT->RUNSTATUS;
  early_wdt_config = NRF_WDT->CONFIG;
  return 0;
}
SYS_INIT(capture_early_watchdog, APPLICATION, 0);
}  // namespace

// Zephyr invokes this immediately before WFI/WFE, with interrupts masked.
// Feed only while ESPHome's main thread is in our blocking sleep wait. The
// WDT pauses during CPU sleep; feeding at natural idle boundaries prevents
// short OT/IRQ work from accumulating a timeout, without waking just to feed.
// Never block, log, allocate, or prevent CPU idle from this hook.
extern "C" bool z_arm_on_enter_cpu_idle(void) {
  if (atomic_get(&sleep_watchdog_active)) esphome::arch_feed_wdt();
  return true;
}

namespace esphome::deep_sleep_nrf52 {

bool SleepWatchdogGuard::begin() {
  // Read back hardware: the HAL requests WDT_OPT_PAUSE_IN_SLEEP, but a
  // bootloader or failed setup must not leave an unsafe long wait behind.
  if (NRF_WDT->RUNSTATUS != 0 && (NRF_WDT->CONFIG & WDT_CONFIG_SLEEP_Msk) != 0) {
    ESP_LOGE("deep_sleep_nrf52",
             "Watchdog runs during CPU sleep; sleep forbidden: CONFIG=0x%08" PRIx32
             " CRV=%" PRIu32 " RREN=0x%08" PRIx32 " before_main RUNSTATUS=%" PRIu32 " CONFIG=0x%08" PRIx32,
             uint32_t(NRF_WDT->CONFIG), uint32_t(NRF_WDT->CRV), uint32_t(NRF_WDT->RREN),
             early_wdt_runstatus, early_wdt_config);
    return false;
  }
  arch_feed_wdt();
  atomic_set(&sleep_watchdog_active, 1);
  this->active_ = true;
  return true;
}

void SleepWatchdogGuard::end() {
  if (!this->active_) return;
  atomic_clear(&sleep_watchdog_active);
  this->active_ = false;
  // Restore a full watchdog window for peripheral resume and main-loop work.
  arch_feed_wdt();
}

}  // namespace esphome::deep_sleep_nrf52
