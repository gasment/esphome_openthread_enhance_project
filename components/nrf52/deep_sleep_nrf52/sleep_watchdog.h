#pragma once

namespace esphome::deep_sleep_nrf52 {

// Scoped to the blocking System ON wait. No timer/thread is created.
class SleepWatchdogGuard {
 public:
  bool begin();
  void end();
  ~SleepWatchdogGuard() { this->end(); }

 protected:
  bool active_{false};
};

}  // namespace esphome::deep_sleep_nrf52
