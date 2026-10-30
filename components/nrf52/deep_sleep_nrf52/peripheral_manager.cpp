#include "esphome/core/defines.h"
#include "esphome/core/log.h"
#include "peripheral_manager.h"
#include "peripheral_hardware.h"
#include "peripheral_pm.h"

#ifdef USE_DEEP_SLEEP_NRF52_PERIPHERAL_AUTO_MGR
#include <zephyr/devicetree.h>
#include <zephyr/pm/device_runtime.h>
#include <cerrno>
#include <cstring>
#include <cstdio>
#endif

namespace esphome::deep_sleep_nrf52 {
static const char *const TAG = "deep_sleep_nrf52.pm";

#ifdef USE_DEEP_SLEEP_NRF52_PERIPHERAL_AUTO_MGR
namespace {
// Bounded stack storage; no heap allocation, timers or log-flush wait.
struct DeviceLogSummary {
  char names[160]{};
  size_t used{0};
  unsigned count{0};
  bool truncated{false};
  void add(const char *name) {
    ++count;
    if (truncated) return;
    const int written = std::snprintf(names + used, sizeof(names) - used, "%s%s", used ? ", " : "", name);
    if (written < 0) { truncated = true; return; }
    if (static_cast<size_t>(written) >= sizeof(names) - used) {
      names[used] = '\0';
      truncated = true;
      return;
    }
    used += written;
  }
};
#define NRF52_INTERFACE(node, kind) {DEVICE_DT_NAME(node), DT_REG_ADDR(node), PeripheralKind::kind},
#define NRF52_UART(node) NRF52_INTERFACE(node, UART)
#define NRF52_UARTE(node) NRF52_INTERFACE(node, UARTE)
#define NRF52_TWI(node) NRF52_INTERFACE(node, TWI)
#define NRF52_TWIM(node) NRF52_INTERFACE(node, TWIM)
#define NRF52_SPI(node) NRF52_INTERFACE(node, SPI)
#define NRF52_SPIM(node) NRF52_INTERFACE(node, SPIM)
// No device symbols are referenced: even a node without a compiled driver
// is checked for a controller left enabled by boot or failed setup.
const HardwareInterface INTERFACES[] = {
    DT_FOREACH_STATUS_OKAY(nordic_nrf_uart, NRF52_UART)
    DT_FOREACH_STATUS_OKAY(nordic_nrf_uarte, NRF52_UARTE)
    DT_FOREACH_STATUS_OKAY(nordic_nrf_twi, NRF52_TWI)
    DT_FOREACH_STATUS_OKAY(nordic_nrf_twim, NRF52_TWIM)
    DT_FOREACH_STATUS_OKAY(nordic_nrf_spi, NRF52_SPI)
    DT_FOREACH_STATUS_OKAY(nordic_nrf_spim, NRF52_SPIM)
    {nullptr, 0, PeripheralKind::UART},
};
#undef NRF52_INTERFACE
#undef NRF52_UART
#undef NRF52_UARTE
#undef NRF52_TWI
#undef NRF52_TWIM
#undef NRF52_SPI
#undef NRF52_SPIM

const HardwareInterface *find_interface(const device *dev) {
  for (const auto &item : INTERFACES)
    if (item.name != nullptr && std::strcmp(item.name, dev->name) == 0) return &item;
  return nullptr;
}

bool valid_interfaces() {
  for (const auto &a : INTERFACES) {
    if (a.name == nullptr) break;
    for (const auto &b : INTERFACES) {
      if (b.name == nullptr) break;
      if (&a != &b && a.base == b.base) {
        ESP_LOGE(TAG, "Overlapping DT controllers %s and %s; refusing register access", a.name, b.name);
        return false;
      }
    }
  }
  return true;
}

bool ensure_stopped(const HardwareInterface &interface, bool force) {
  const uint32_t enabled = peripheral_enable(interface);
  if (enabled == 0) return true;
  ESP_LOGE(TAG, "%s: hardware still enabled (ENABLE=0x%08lx)", interface.name,
           static_cast<unsigned long>(enabled));
  if (!force) return false;
  if (!peripheral_force_stop(interface)) {
    ESP_LOGE(TAG, "%s: bounded hardware STOP/disable failed; sleep forbidden", interface.name);
    return false;
  }
  ESP_LOGW(TAG, "%s: hardware fallback verified disabled", interface.name);
  return true;
}
}  // namespace
#endif

bool PeripheralManager::suspend() {
#ifdef USE_DEEP_SLEEP_NRF52_PERIPHERAL_AUTO_MGR
  if (!enabled_) return true;
  if (!suspended_.empty() || restore_failed_ || peripheral_pm_faulted() || !valid_interfaces()) return false;
  DeviceLogSummary summary;
  const device *devices;
  const size_t count = z_device_get_all_static(&devices);
  for (size_t i = count; i > 0; --i) {
    const auto *dev = &devices[i - 1];
    const auto *interface = find_interface(dev);
    if (interface == nullptr) continue;
    if (!device_is_ready(dev)) {
      if (!ensure_stopped(*interface, force_offload_)) return false;
      continue;
    }
    pm_device_state state;
    int err = pm_device_state_get(dev, &state);
    if (err != 0) {
      ESP_LOGE(TAG, "%s: device PM unavailable (%d)", dev->name, err);
      return false;
    }
    if (state == PM_DEVICE_STATE_SUSPENDED || state == PM_DEVICE_STATE_OFF) {
      if (!ensure_stopped(*interface, force_offload_)) return false;
      continue;
    }
    if (pm_device_runtime_is_enabled(dev)) {
      ESP_LOGE(TAG, "%s: runtime PM owns this controller", dev->name);
      return false;
    }
    if (!force_offload_ && (pm_device_is_busy(dev) || pm_device_wakeup_is_enabled(dev))) {
      ESP_LOGW(TAG, "%s: busy or configured as a wake source; sleep cancelled", dev->name);
      return false;
    }
    auto *baudrate = peripheral_baudrate(*interface);
    SuspendedDevice saved{dev, interface, baudrate != nullptr ? *baudrate : 0};
    err = peripheral_pm_call(dev, PM_DEVICE_ACTION_SUSPEND);
    if (peripheral_pm_faulted()) {
      restore_failed_ = true;
      ESP_LOGE(TAG, "%s: PM deadline exceeded (100ms); PM fault latched until reset, sleep forbidden", dev->name);
      return false;
    }
    if (err == -EALREADY) {
      if (!ensure_stopped(*interface, force_offload_)) return false;
      continue;
    }
    if (err != 0) {
      ESP_LOGE(TAG, "%s: suspend failed (%d)", dev->name, err);
      // Recover partial teardown without racing a driver which did not return.
      if (err != -EBUSY && err != -ENOSYS && err != -ENOTSUP) {
        const int restore = peripheral_pm_call(dev, PM_DEVICE_ACTION_RESUME, true, baudrate, saved.saved_baudrate);
        if (restore != 0) {
          restore_failed_ = true;
          ESP_LOGE(TAG, "%s: failed-stop recovery failed (%d)", dev->name, restore);
        }
      }
      return false;
    }
    // Record before verifying so a false driver success is rolled back too.
    suspended_.push_back(saved);
    if (!ensure_stopped(*interface, force_offload_)) return false;
    summary.add(dev->name);
  }
  // Includes nodes without a device instance/compiled driver.
  for (const auto &interface : INTERFACES) {
    if (interface.name == nullptr) break;
    if (!ensure_stopped(interface, force_offload_)) return false;
  }
  if (summary.count)
    ESP_LOGD(TAG, "Suspended %u controllers [%s%s]; hardware ENABLE=0 verified", summary.count,
             summary.names, summary.truncated ? ", ..." : "");
#endif
  return true;
}

bool PeripheralManager::verify_stopped() {
#ifdef USE_DEEP_SLEEP_NRF52_PERIPHERAL_AUTO_MGR
  if (!enabled_) return true;
  if (restore_failed_ || peripheral_pm_faulted() || !valid_interfaces()) return false;
  // A final read-only gate after wake sources have been armed.
  for (const auto &interface : INTERFACES) {
    if (interface.name == nullptr) break;
    if (!ensure_stopped(interface, false)) return false;
  }
#endif
  return true;
}

bool PeripheralManager::resume() {
  bool success = !restore_failed_;
#ifdef USE_DEEP_SLEEP_NRF52_PERIPHERAL_AUTO_MGR
  DeviceLogSummary summary;
  for (auto it = suspended_.rbegin(); it != suspended_.rend(); ++it) {
    if (peripheral_pm_faulted()) {
      ESP_LOGE(TAG, "%s: restore blocked by latched PM timeout; reset required", it->dev->name);
      success = false;
      continue;
    }
    int err = peripheral_pm_call(it->dev, PM_DEVICE_ACTION_RESUME, false,
                                 peripheral_baudrate(*it->interface), it->saved_baudrate);
    if (err == -EALREADY) err = 0;
    if (err != 0) {
      success = false;
      ESP_LOGE(TAG, "%s: resume failed (%d)", it->dev->name, err);
    } else {
      summary.add(it->dev->name);
    }
  }
  if (summary.count)
    ESP_LOGD(TAG, "Resumed %u controllers [%s%s]", summary.count,
             summary.names, summary.truncated ? ", ..." : "");
#endif
  suspended_.clear();
  return success;
}

void PeripheralManager::dump_config() {
  ESP_LOGCONFIG(TAG, "Peripheral auto manager: %s, force_offload: %s", YESNO(enabled_), YESNO(force_offload_));
  if (enabled_)
    ESP_LOGCONFIG(TAG, "  Hardware ENABLE verification; PM deadline=100ms; fallback STOP deadline=50ms");
}

}  // namespace esphome::deep_sleep_nrf52
