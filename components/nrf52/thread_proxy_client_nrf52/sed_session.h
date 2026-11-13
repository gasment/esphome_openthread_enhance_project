#pragma once
#ifdef USE_THREAD_PROXY_CLIENT_NRF52_SLEEP
#include "esphome/components/deep_sleep_nrf52/deep_sleep_nrf52.h"
namespace esphome::thread_proxy_client_nrf52 {
class SedWakeTrigger : public deep_sleep_nrf52::WakeTrigger {
 public:
  using WakeTrigger::WakeTrigger;
  void add_callback(std::function<void(deep_sleep_nrf52::WakeupCause, int32_t)> callback) {
    auto *automation = new Automation<deep_sleep_nrf52::WakeupCause, int32_t>(this);
    automation->add_action(new WakeAction(std::move(callback)));
  }
 private:
  class WakeAction : public Action<deep_sleep_nrf52::WakeupCause, int32_t> {
   public:
    explicit WakeAction(std::function<void(deep_sleep_nrf52::WakeupCause, int32_t)> callback) : callback_(std::move(callback)) {}
    void play(const deep_sleep_nrf52::WakeupCause &cause, const int32_t &pin) override { callback_(cause, pin); }
   private:
    std::function<void(deep_sleep_nrf52::WakeupCause, int32_t)> callback_;
  };
};
}
#endif
