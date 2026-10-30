#pragma once

#include "esphome/core/automation.h"
#include "esphome/core/defines.h"
#include "esphome/core/component.h"
#include "esphome/core/gpio.h"
#include "trigger_event.h"
#include "peripheral_manager.h"
#include <zephyr/kernel.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/sys/atomic.h>
#include <functional>
#include <memory>
#include <vector>
#ifdef USE_DEEP_SLEEP_NRF52_VBUS
#include <nrfx_power.h>
#endif

namespace esphome::deep_sleep_nrf52 {

enum WakeupPinMode : uint8_t { KEEP_AWAKE, IGNORE, INVERT_WAKEUP };
enum WakeupMode : uint8_t { WAKE_ANY, WAKE_ANY_LOW, WAKE_ANY_HIGH, WAKE_ALL_LOW, WAKE_ALL_HIGH };
enum WakeupCause : uint8_t { TIMER = 1, GPIO = 2, EXTERNAL = 4, VBUS = 8 };
enum AbortReason : uint8_t { CANCELLED, PREPARE_TIMEOUT, GPIO_ERROR, PERIPHERAL_ERROR };
enum class Phase : uint8_t { AWAKE, WAITING, PREPARING, SLEEPING };
class PinEventTrigger;

class DeepSleepComponent : public Component {
 public:
  void setup() override;
  void loop() override;
  void dump_config() override;
  void on_shutdown() override;
  float get_setup_priority() const override { return setup_priority::LATE; }
  void set_run_duration(uint32_t ms) { run_duration_ = ms; }
  void set_sleep_duration(uint32_t ms) { sleep_duration_ = ms; }
  void set_prepare_timeout(uint32_t ms) { prepare_timeout_ = ms; }
  void set_wakeup_mode(WakeupMode mode) { wakeup_mode_ = mode; }
  void set_peripheral_auto_mgr(bool enabled) { peripheral_mgr_.set_enabled(enabled); }
  void set_force_offload(bool force) { peripheral_mgr_.set_force_offload(force); }
  void add_wakeup_pin(InternalGPIOPin *pin, WakeupPinMode mode);
  void set_pin_trigger_event(uint8_t pin, uint32_t debounce, uint32_t interval, uint32_t hold, uint32_t timeout);
#ifdef USE_DEEP_SLEEP_NRF52_VBUS
  void set_vbus_wakeup(bool inverted, WakeupPinMode mode) {
    vbus_enabled_ = true;
    vbus_inverted_ = inverted;
    vbus_mode_ = mode;
  }
  void add_vbus_wake_trigger(Trigger<> *trigger) { vbus_wake_triggers_.push_back(trigger); }
#endif
  // A prevented request is discarded, including any duration override.
  void request_sleep(optional<uint32_t> sleep_duration = {});
  void set_prevent(bool prevent);
  void cancel_sleep();
  // May be called from an ISR or another thread. Automations run on main only.
  void wake();
  bool is_sleeping() const { return phase_ == Phase::SLEEPING; }
  bool is_pending() const { return phase_ == Phase::WAITING || phase_ == Phase::PREPARING; }
  bool can_sleep();
  // Main-thread interfaces for transporters. Locks are independent and idempotent.
  void acquire_wake_lock(const void *owner);
  void release_wake_lock(const void *owner);
  void add_sleep_guard(std::function<bool()> guard) { guards_.push_back(std::move(guard)); }
  void add_sleep_trigger(Trigger<> *trigger) { sleep_triggers_.push_back(trigger); }
  void add_wake_trigger(Trigger<WakeupCause, int32_t> *trigger) { wake_triggers_.push_back(trigger); }
  void add_pin_event_trigger(uint8_t pin, PinEventTrigger *trigger) {
    pin_event_triggers_.push_back({pin, trigger});
  }
  void add_abort_trigger(Trigger<AbortReason> *trigger) { abort_triggers_.push_back(trigger); }

 protected:
  struct PinEdge {
    uint64_t at;
    bool active;
  };
  struct EventState {
    explicit EventState(TriggerEventConfig config) : detector(config) {
      k_msgq_init(&queue, buffer, sizeof(PinEdge), 32);
    }
    TriggerEventDetector detector;
    k_msgq queue{};
    alignas(PinEdge) char buffer[sizeof(PinEdge) * 32];
    atomic_t capturing{0};
    atomic_t overflow{0};
    atomic_t io_error{0};
    bool initialized{false};
  };
  struct WakePin {
    gpio_callback callback{};
    DeepSleepComponent *owner{nullptr};
    InternalGPIOPin *pin{nullptr};
    const device *port{nullptr};
    WakeupPinMode mode{KEEP_AWAKE};
    uint8_t port_pin{0};
    bool registered{false};
    EventState *events{nullptr};
  };
  static void gpio_callback_(const device *port, gpio_callback *cb, uint32_t pins);
  bool gpio_all_() const { return wakeup_mode_ == WAKE_ALL_LOW || wakeup_mode_ == WAKE_ALL_HIGH; }
  bool gpio_target_high_(const WakePin &item) const;
  bool gpio_group_matches_() const;
  static bool physical_high_(const WakePin &item) { return item.pin->digital_read() != item.pin->is_inverted(); }
  bool pins_ready_();
  bool arm_pins_();
  void disarm_pins_(WakePin *keep = nullptr);
  static void capture_edge_(WakePin &item);
  void service_trigger_event_();
  void sleep_();
  void schedule_();
  void abort_(AbortReason reason);
#ifdef USE_DEEP_SLEEP_NRF52_VBUS
  static void vbus_callback_(nrfx_power_usb_evt_t event);
  bool setup_vbus_();
  bool arm_vbus_();
  void disarm_vbus_();
  void restore_vbus_();
  bool vbus_enabled_{false};
  bool vbus_inverted_{false};
  WakeupPinMode vbus_mode_{KEEP_AWAKE};
  atomic_t vbus_armed_{0};
  atomic_t vbus_target_present_{1};
  uint32_t vbus_added_interrupts_{0};
  nrfx_power_usb_event_handler_t previous_vbus_handler_{nullptr};
  std::vector<Trigger<> *> vbus_wake_triggers_;
#endif
  Phase phase_{Phase::AWAKE};
  bool prevent_{false};
  bool shutdown_{false};
  WakeupMode wakeup_mode_{WAKE_ANY};
  optional<uint32_t> run_duration_;
  optional<uint32_t> sleep_duration_;
  uint32_t prepare_timeout_{30000};
  int64_t prepare_started_{0};
  atomic_t wake_flags_{0};
  atomic_t wake_pin_{-1};
  std::vector<std::unique_ptr<WakePin>> pins_;
  std::vector<std::unique_ptr<EventState>> event_states_;
  WakePin *active_event_pin_{nullptr};
  std::vector<const void *> locks_;
  std::vector<std::function<bool()>> guards_;
  PeripheralManager peripheral_mgr_;
  std::vector<Trigger<> *> sleep_triggers_;
  std::vector<Trigger<WakeupCause, int32_t> *> wake_triggers_;
  struct PinAutomation {
    uint8_t pin;
    PinEventTrigger *trigger;
  };
  std::vector<PinAutomation> pin_event_triggers_;
  std::vector<Trigger<AbortReason> *> abort_triggers_;
};

class SleepTrigger : public Trigger<> {
 public:
  explicit SleepTrigger(DeepSleepComponent *parent) { parent->add_sleep_trigger(this); }
};
class WakeTrigger : public Trigger<WakeupCause, int32_t> {
 public:
  explicit WakeTrigger(DeepSleepComponent *parent) { parent->add_wake_trigger(this); }
};
class PinEventTrigger : public Trigger<TriggerEvent, uint32_t> {
 public:
  PinEventTrigger(DeepSleepComponent *parent, uint8_t pin) { parent->add_pin_event_trigger(pin, this); }
  void set_event_filter(TriggerEvent event) { filter_ = event; }
  bool accepts(TriggerEvent event) const { return !filter_.has_value() || *filter_ == event; }
 protected:
  optional<TriggerEvent> filter_;
};
#ifdef USE_DEEP_SLEEP_NRF52_VBUS
class VbusWakeTrigger : public Trigger<> {
 public:
  explicit VbusWakeTrigger(DeepSleepComponent *parent) { parent->add_vbus_wake_trigger(this); }
};
#endif
class AbortTrigger : public Trigger<AbortReason> {
 public:
  explicit AbortTrigger(DeepSleepComponent *parent) { parent->add_abort_trigger(this); }
};

template<typename... Ts> class EnterAction : public Action<Ts...> {
 public:
  explicit EnterAction(DeepSleepComponent *parent) : parent_(parent) {}
  TEMPLATABLE_VALUE(uint32_t, sleep_duration);
  void play(const Ts &...x) override {
    if (sleep_duration_.has_value()) parent_->request_sleep(sleep_duration_.value(x...));
    else parent_->request_sleep();
  }
 protected:
  DeepSleepComponent *parent_;
};

template<typename... Ts> class PreventAction : public Action<Ts...> {
 public:
  explicit PreventAction(DeepSleepComponent *parent) : parent_(parent) {}
  TEMPLATABLE_VALUE(bool, value);
  void play(const Ts &...x) override { parent_->set_prevent(this->value_.value(x...)); }
 protected:
  DeepSleepComponent *parent_;
};

#define NRF52_SLEEP_CONDITION(name, method) \
  template<typename... Ts> class name : public Condition<Ts...> { \
   public: \
    explicit name(DeepSleepComponent *parent) : parent_(parent) {} \
    bool check(const Ts &...x) override { return parent_->method(); } \
   protected: \
    DeepSleepComponent *parent_; \
  };
NRF52_SLEEP_CONDITION(IsSleepingCondition, is_sleeping)
NRF52_SLEEP_CONDITION(IsPendingCondition, is_pending)
NRF52_SLEEP_CONDITION(CanSleepCondition, can_sleep)
#undef NRF52_SLEEP_CONDITION

}  // namespace esphome::deep_sleep_nrf52
