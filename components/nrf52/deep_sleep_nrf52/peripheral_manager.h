#pragma once

#include <zephyr/device.h>
#include <cstdint>
#include <vector>

namespace esphome::deep_sleep_nrf52 {
struct HardwareInterface;

// Manages hardware controllers, independently of ESPHome component instances.
class PeripheralManager {
 public:
  void set_enabled(bool enabled) { enabled_ = enabled; }
  void set_force_offload(bool force) { force_offload_ = force; }
  bool suspend();
  bool resume();
  bool verify_stopped();
  void dump_config();

 protected:
  struct SuspendedDevice {
    const device *dev;
    const HardwareInterface *interface;
    uint32_t saved_baudrate;
  };
  bool enabled_{true};
  bool force_offload_{false};
  bool restore_failed_{false};
  std::vector<SuspendedDevice> suspended_;
};

}  // namespace esphome::deep_sleep_nrf52
