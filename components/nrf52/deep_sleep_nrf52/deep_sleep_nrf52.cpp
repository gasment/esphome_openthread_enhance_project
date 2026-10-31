#include "deep_sleep_nrf52.h"
#include "sleep_watchdog.h"
#include "esphome/core/hal.h"
#include "esphome/core/log.h"
#include "esphome/core/wake.h"
#include <zephyr/kernel.h>
#include <algorithm>
#include <cinttypes>
#include <cstdint>

namespace esphome::deep_sleep {
bool global_has_deep_sleep = false;
}

namespace esphome::deep_sleep_nrf52 {
static const char *const TAG = "deep_sleep_nrf52";
static constexpr atomic_val_t GPIO_CONFIG_ERROR = 16;

#ifdef USE_DEEP_SLEEP_NRF52_VBUS
#if !NRF_POWER_HAS_USBREG
#error "VBUS wake requires an nRF52 with POWER USBREG support (e.g. nRF52840)"
#endif
static DeepSleepComponent *vbus_owner = nullptr;
static constexpr uint32_t USB_EVENT_MASK = NRF_POWER_INT_USBDETECTED_MASK | NRF_POWER_INT_USBREMOVED_MASK |
                                           NRF_POWER_INT_USBPWRRDY_MASK;

bool DeepSleepComponent::setup_vbus_() {
  if (!vbus_enabled_) return true;
  const auto key = irq_lock();
  if (vbus_owner != nullptr) { irq_unlock(key); return false; }
  const uint32_t enabled = nrf_power_int_enable_get(NRF_POWER) & USB_EVENT_MASK;
  previous_vbus_handler_ = nrfx_power_usb_handler_get();
  vbus_owner = this;
  nrfx_power_usbevt_config_t config{};
  config.handler = vbus_callback_;
  // init disables USB interrupts: restore the exact existing mask under IRQ lock.
  nrfx_power_usbevt_init(&config);
  nrf_power_int_enable(NRF_POWER, enabled);
  irq_unlock(key);
  return true;
}

void DeepSleepComponent::vbus_callback_(nrfx_power_usb_evt_t event) {
  auto *owner = vbus_owner;
  if (owner == nullptr) return;
  if (atomic_get(&owner->vbus_armed_)) {
    const bool target = atomic_get(&owner->vbus_target_present_) != 0;
    const bool matches = target ? event == NRFX_POWER_USB_EVT_DETECTED : event == NRFX_POWER_USB_EVT_REMOVED;
    // Ignore an old pending event whose physical state is no longer current.
    if (matches && nrf_power_usbregstatus_vbusdet_get(NRF_POWER) == target) {
      atomic_or(&owner->wake_flags_, VBUS);
      esphome::wake_loop_threadsafe();
    }
  }
  // Preserve every original USB event, including POWER READY and removals.
  if (owner->previous_vbus_handler_ != nullptr) owner->previous_vbus_handler_(event);
}

bool DeepSleepComponent::arm_vbus_() {
  if (!vbus_enabled_) return true;
  const auto key = irq_lock();
  if (nrfx_power_usb_handler_get() != vbus_callback_) { irq_unlock(key); return false; }
  const bool present = nrf_power_usbregstatus_vbusdet_get(NRF_POWER);
  bool target = !vbus_inverted_;
  if (vbus_mode_ == INVERT_WAKEUP && present == target) target = !target;
  atomic_set(&vbus_target_present_, target);
  atomic_set(&vbus_armed_, 1);
  const uint32_t mask = NRF_POWER_INT_USBDETECTED_MASK | NRF_POWER_INT_USBREMOVED_MASK;
  vbus_added_interrupts_ = mask & ~nrf_power_int_enable_get(NRF_POWER);
  nrf_power_int_enable(NRF_POWER, mask);
  // Covers a change between the last readiness check and enabling events.
  if (nrf_power_usbregstatus_vbusdet_get(NRF_POWER) == target) atomic_or(&wake_flags_, VBUS);
  irq_unlock(key);
  return true;
}

void DeepSleepComponent::disarm_vbus_() {
  const auto key = irq_lock();
  atomic_clear(&vbus_armed_);
  if (vbus_owner == this && nrfx_power_usb_handler_get() == vbus_callback_)
    nrf_power_int_disable(NRF_POWER, vbus_added_interrupts_);
  vbus_added_interrupts_ = 0;
  irq_unlock(key);
}

void DeepSleepComponent::restore_vbus_() {
  disarm_vbus_();
  const auto key = irq_lock();
  if (vbus_owner == this) {
    if (nrfx_power_usb_handler_get() == vbus_callback_) {
      const uint32_t enabled = nrf_power_int_enable_get(NRF_POWER) & USB_EVENT_MASK;
      nrfx_power_usbevt_config_t config{};
      config.handler = previous_vbus_handler_;
      nrfx_power_usbevt_init(&config);
      if (previous_vbus_handler_ != nullptr) nrf_power_int_enable(NRF_POWER, enabled);
    }
    vbus_owner = nullptr;
  }
  irq_unlock(key);
}
#endif

void DeepSleepComponent::add_wakeup_pin(InternalGPIOPin *pin, WakeupPinMode mode) {
  auto item = std::make_unique<WakePin>();
  item->owner = this;
  item->pin = pin;
  item->mode = mode;
  item->port_pin = pin->get_pin() % 32;
  item->port = pin->get_pin() < 32 ? DEVICE_DT_GET_OR_NULL(DT_NODELABEL(gpio0)) :
                                    DEVICE_DT_GET_OR_NULL(DT_NODELABEL(gpio1));
  pins_.push_back(std::move(item));
}

void DeepSleepComponent::set_pin_trigger_event(uint8_t pin, uint32_t debounce, uint32_t interval,
                                              uint32_t hold, uint32_t timeout) {
  for (auto &item : pins_) {
    if (item->pin->get_pin() != pin) continue;
    auto events = std::make_unique<EventState>(TriggerEventConfig{debounce, interval, hold, timeout});
    item->events = events.get();
    event_states_.push_back(std::move(events));
    return;
  }
}

void DeepSleepComponent::setup() {
  deep_sleep::global_has_deep_sleep = true;
  for (auto &item : pins_) {
    if (item->port == nullptr || !device_is_ready(item->port)) {
      ESP_LOGE(TAG, "Wake GPIO port not ready");
      mark_failed();
      return;
    }
    // No register writes outside the Zephyr GPIO driver: it owns nrfx GPIOTE.
    item->pin->setup();
    gpio_init_callback(&item->callback, gpio_callback_, BIT(item->port_pin));
    if (gpio_add_callback(item->port, &item->callback) != 0) {
      ESP_LOGE(TAG, "Cannot register wake GPIO callback");
      mark_failed();
      return;
    }
    item->registered = true;
  }
#ifdef USE_DEEP_SLEEP_NRF52_VBUS
  if (!setup_vbus_()) {
    ESP_LOGE(TAG, "Cannot register VBUS wake callback");
    mark_failed();
    return;
  }
#endif
  schedule_();
}

void DeepSleepComponent::schedule_() {
  if (run_duration_.has_value() && !shutdown_ && !prevent_)
    set_timeout("sleep", *run_duration_, [this]() { request_sleep(); });
}

void DeepSleepComponent::request_sleep(optional<uint32_t> sleep_duration) {
  if (is_failed() || shutdown_ || prevent_ || phase_ != Phase::AWAKE) return;
  if (sleep_duration.has_value()) sleep_duration_ = *sleep_duration;
  cancel_timeout("sleep");
  phase_ = Phase::WAITING;
}

void DeepSleepComponent::set_prevent(bool prevent) {
  if (prevent_ == prevent) return;
  prevent_ = prevent;
  if (prevent) {
    cancel_timeout("sleep");
    if (is_pending()) abort_(CANCELLED);
  } else if (phase_ == Phase::AWAKE && !is_failed()) {
    // Start a fresh awake interval; never replay an enter rejected earlier.
    schedule_();
  }
}

void DeepSleepComponent::acquire_wake_lock(const void *owner) {
  if (std::find(locks_.begin(), locks_.end(), owner) == locks_.end()) locks_.push_back(owner);
}
void DeepSleepComponent::release_wake_lock(const void *owner) {
  locks_.erase(std::remove(locks_.begin(), locks_.end(), owner), locks_.end());
}

bool DeepSleepComponent::gpio_target_high_(const WakePin &item) const {
  if (wakeup_mode_ == WAKE_ANY_LOW || wakeup_mode_ == WAKE_ALL_LOW) return false;
  if (wakeup_mode_ == WAKE_ANY_HIGH || wakeup_mode_ == WAKE_ALL_HIGH) return true;
  return !item.pin->is_inverted();
}

bool DeepSleepComponent::gpio_group_matches_() const {
  if (pins_.empty()) return false;
  for (const auto &item : pins_) {
    const bool matches = physical_high_(*item) == gpio_target_high_(*item);
    if (gpio_all_() && !matches) return false;
    if (!gpio_all_() && matches) return true;
  }
  return gpio_all_();
}

bool DeepSleepComponent::pins_ready_() {
  if (gpio_all_()) {
    // A partial combination must not prevent sleep; ALL GPIO modes are equal.
    if (!pins_.empty() && pins_.front()->mode == KEEP_AWAKE && gpio_group_matches_()) return false;
  } else {
    for (auto &item : pins_)
      if (item->mode == KEEP_AWAKE && physical_high_(*item) == gpio_target_high_(*item)) return false;
  }
#ifdef USE_DEEP_SLEEP_NRF52_VBUS
  if (vbus_enabled_ && vbus_mode_ == KEEP_AWAKE &&
      nrf_power_usbregstatus_vbusdet_get(NRF_POWER) != vbus_inverted_) return false;
#endif
  return true;
}

bool DeepSleepComponent::can_sleep() {
  if (shutdown_ || is_failed() || prevent_ || active_event_pin_ != nullptr || !locks_.empty() || !pins_ready_()) return false;
  for (auto *trigger : wake_triggers_) if (trigger->is_action_running()) return false;
  for (auto &entry : pin_event_triggers_) if (entry.trigger->is_action_running()) return false;
#ifdef USE_DEEP_SLEEP_NRF52_VBUS
  for (auto *trigger : vbus_wake_triggers_) if (trigger->is_action_running()) return false;
#endif
  for (auto *trigger : abort_triggers_) if (trigger->is_action_running()) return false;
  for (auto &guard : guards_) if (!guard()) return false;
  return true;
}

void DeepSleepComponent::abort_(AbortReason reason) {
  disarm_pins_();
  // Set state first: cancellation during an automation must not re-enter here.
  phase_ = Phase::AWAKE;
  if (!peripheral_mgr_.resume()) this->mark_failed();
  for (auto *trigger : sleep_triggers_) trigger->stop_action();
  schedule_();
  for (auto *trigger : abort_triggers_) trigger->trigger(reason);
}

void DeepSleepComponent::cancel_sleep() {
  if (is_pending()) abort_(CANCELLED);
}

void DeepSleepComponent::wake() {
  atomic_or(&wake_flags_, EXTERNAL);
  esphome::wake_loop_threadsafe();
}

void DeepSleepComponent::gpio_callback_(const device *port, gpio_callback *cb, uint32_t pins) {
  auto *item = CONTAINER_OF(cb, WakePin, callback);
  if (!(pins & BIT(item->port_pin))) return;
  // A level may stay active for seconds. Disable it before leaving the ISR.
  gpio_pin_interrupt_configure(port, item->port_pin, GPIO_INT_DISABLE);
  if (item->events != nullptr && atomic_get(&item->events->capturing)) {
    capture_edge_(*item);
    return;
  }
  if (item->owner->gpio_all_() && !item->owner->gpio_group_matches_()) {
    // Nordic SENSE is OR hardware. For ALL, observe both transitions by arming
    // the opposite of the current level. Incomplete combinations do not resume
    // ESPHome's main loop and cannot storm on an already-held key.
    const bool high = physical_high_(*item);
    const int error = gpio_pin_interrupt_configure(port, item->port_pin,
                                                   high ? GPIO_INT_LEVEL_LOW : GPIO_INT_LEVEL_HIGH);
    if (error != 0) {
      atomic_or(&item->owner->wake_flags_, GPIO_CONFIG_ERROR);
      esphome::wake_loop_threadsafe();
      return;
    }
    if (!item->owner->gpio_group_matches_()) return;
    gpio_pin_interrupt_configure(port, item->port_pin, GPIO_INT_DISABLE);
  }
  // Wake is decided only by the configured GPIO/group condition above.
  // Event recognition never delays or qualifies this notification.
  atomic_cas(&item->owner->wake_pin_, -1, item->pin->get_pin());
  atomic_or(&item->owner->wake_flags_, GPIO);
  esphome::wake_loop_threadsafe();
  if (item->events != nullptr) {
    atomic_set(&item->events->capturing, 1);
    capture_edge_(*item);
  }
}

void DeepSleepComponent::capture_edge_(WakePin &item) {
  auto &events = *item.events;
  const uint64_t at = k_uptime_get();
  const bool high = physical_high_(item);
  const bool active = high != item.pin->is_inverted();
  // Sample the actual post-wake state. Wake polarity/group satisfaction does
  // not imply an event-active level (e.g. waking on release or ALL_HIGH).
  PinEdge edge{at, active};
  if (k_msgq_put(&events.queue, &edge, K_NO_WAIT) != 0) atomic_set(&events.overflow, 1);
  const int error = gpio_pin_interrupt_configure(item.port, item.port_pin,
                                                 high ? GPIO_INT_LEVEL_LOW : GPIO_INT_LEVEL_HIGH);
  if (error != 0) {
    atomic_set(&events.io_error, 1);
    // Failure of post-wake recognition must not turn a real wake into a sleep abort.
  }
  esphome::wake_loop_threadsafe();
}

void DeepSleepComponent::service_trigger_event_() {
  auto *item = active_event_pin_;
  if (item == nullptr) return;
  auto &events = *item->events;
  const auto key = irq_lock();
  const bool error = atomic_get(&events.overflow) || atomic_get(&events.io_error);
  PinEdge edge{};
  if (!error) {
    while (k_msgq_get(&events.queue, &edge, K_NO_WAIT) == 0) {
      if (!events.initialized) {
        events.detector.reset(edge.at);
        events.initialized = true;
      }
      events.detector.edge(edge.at, edge.active);
    }
    if (events.initialized) events.detector.advance(k_uptime_get());
  }
  const bool done = error || (events.initialized && events.detector.done());
  const bool valid = done && !error && events.detector.valid();
  const auto event = events.detector.event();
  const uint32_t duration = events.detector.press_duration();
  if (done) {
    atomic_clear(&events.capturing);
    gpio_pin_interrupt_configure(item->port, item->port_pin, GPIO_INT_DISABLE);
    active_event_pin_ = nullptr;  // Release internal guard before callbacks.
  }
  irq_unlock(key);
  if (!done) return;
  if (!valid) {
    ESP_LOGD(TAG, "GPIO%u trigger window cancelled (invalid pulse/timeout/overflow/IO error)", item->pin->get_pin());
    return;
  }
  for (auto &entry : pin_event_triggers_)
    if (entry.pin == item->pin->get_pin() && entry.trigger->accepts(event)) entry.trigger->trigger(event, duration);
}

bool DeepSleepComponent::arm_pins_() {
  for (auto &item : pins_) {
    if (item->events != nullptr) {
      atomic_clear(&item->events->capturing);
      atomic_clear(&item->events->overflow);
      atomic_clear(&item->events->io_error);
      item->events->initialized = false;
      k_msgq_purge(&item->events->queue);
    }
    bool physical_high = gpio_target_high_(*item);
    if (gpio_all_()) physical_high = !physical_high_(*item);
    else if (item->mode == INVERT_WAKEUP && physical_high_(*item) == physical_high) physical_high = !physical_high;
    int error = gpio_pin_interrupt_configure(item->port, item->port_pin,
                                            physical_high ? GPIO_INT_LEVEL_HIGH : GPIO_INT_LEVEL_LOW);
    if (error != 0) {
      ESP_LOGE(TAG, "Cannot arm SENSE on GPIO%u: %d", item->pin->get_pin(), error);
      return false;
    }
  }
  // IGNORE permits entry with an already-complete combination. There is no
  // completing edge in that case, so only the global callback receives pin=-1.
  if (gpio_all_() && gpio_group_matches_()) atomic_or(&wake_flags_, GPIO);
  return true;
}

void DeepSleepComponent::disarm_pins_(WakePin *keep) {
  for (auto &item : pins_) {
    if (item.get() == keep) continue;
    if (item->events != nullptr) atomic_clear(&item->events->capturing);
    if (item->registered) gpio_pin_interrupt_configure(item->port, item->port_pin, GPIO_INT_DISABLE);
  }
#ifdef USE_DEEP_SLEEP_NRF52_VBUS
  disarm_vbus_();
#endif
}

void DeepSleepComponent::sleep_() {
  atomic_clear(&wake_flags_);
  atomic_set(&wake_pin_, -1);
  phase_ = Phase::SLEEPING;
  if (!peripheral_mgr_.suspend()) {
    ESP_LOGW(TAG, "Peripheral suspend failed; restoring peripherals and cancelling sleep");
    abort_(PERIPHERAL_ERROR);
    return;
  }
  if (!arm_pins_()) { abort_(GPIO_ERROR); return; }
#ifdef USE_DEEP_SLEEP_NRF52_VBUS
  if (!arm_vbus_()) {
    ESP_LOGE(TAG, "VBUS callback ownership changed");
    abort_(GPIO_ERROR);
    return;
  }
#endif
  if (!peripheral_mgr_.verify_stopped()) {
    ESP_LOGE(TAG, "Final hardware-off verification failed; sleep forbidden");
    abort_(PERIPHERAL_ERROR);
    return;
  }
  const int64_t started = k_uptime_get();
  const int64_t deadline = sleep_duration_.has_value() ? started + *sleep_duration_ : INT64_MAX;
  SleepWatchdogGuard sleep_watchdog;
  if (!sleep_watchdog.begin()) { abort_(WATCHDOG_ERROR); return; }
  ESP_LOGD(TAG, "System ON sleep");
  WakeupCause cause = EXTERNAL;
  while (true) {
    const auto flags = atomic_get(&wake_flags_);
    if (flags & GPIO_CONFIG_ERROR) { sleep_watchdog.end(); abort_(GPIO_ERROR); return; }
    if (flags & GPIO) { cause = GPIO; break; }
    if (flags & VBUS) { cause = VBUS; break; }
    if (flags & EXTERNAL) { cause = EXTERNAL; break; }
    // Core wake producers must be allowed to return to the application loop.
    if (esphome::wake_request_take()) { cause = EXTERNAL; break; }
    const int64_t remaining = deadline - k_uptime_get();
    if (remaining <= 0) { cause = TIMER; break; }
    // Wait for an event or the real deadline, not a periodic watchdog tick.
    // UINT32_MAX is the core's K_FOREVER sentinel. Configured durations are
    // bounded to UINT32_MAX - 1; Zephyr's 64-bit timeout handles long waits.
    esphome::internal::wakeable_delay(sleep_duration_.has_value() ? static_cast<uint32_t>(remaining) : UINT32_MAX);
  }
  sleep_watchdog.end();
  // Choose and preserve the selected detector without a gap in edge capture.
  const auto key = irq_lock();
  const auto final_flags = atomic_get(&wake_flags_);
  if (final_flags & GPIO_CONFIG_ERROR) { irq_unlock(key); abort_(GPIO_ERROR); return; }
  if (final_flags & GPIO) cause = GPIO;
  else if (final_flags & VBUS) cause = VBUS;
  const int32_t pin = cause == GPIO ? atomic_get(&wake_pin_) : -1;
  active_event_pin_ = nullptr;
  if (cause == GPIO) {
    for (auto &item : pins_)
      if (item->pin->get_pin() == pin && item->events != nullptr && atomic_get(&item->events->capturing))
        active_event_pin_ = item.get();
  }
  disarm_pins_(active_event_pin_);
  atomic_clear(&wake_flags_);
  phase_ = Phase::AWAKE;
  irq_unlock(key);
  if (!peripheral_mgr_.resume()) this->mark_failed();
  schedule_();
  ESP_LOGD(TAG, "Wake cause=%u pin=%" PRId32, unsigned(cause), pin);
  for (auto *trigger : wake_triggers_) trigger->trigger(cause, pin);
#ifdef USE_DEEP_SLEEP_NRF52_VBUS
  if (cause == VBUS)
    for (auto *trigger : vbus_wake_triggers_) trigger->trigger();
#endif
  // Preserve the component phase request consumed above for queued producers.
  esphome::wake_loop_threadsafe();
}

void DeepSleepComponent::loop() {
  if (shutdown_ || is_failed()) return;
  service_trigger_event_();
  if (phase_ != Phase::SLEEPING && (atomic_set(&wake_flags_, 0) & EXTERNAL)) {
    if (is_pending()) { abort_(CANCELLED); return; }
  }
  if (phase_ == Phase::WAITING && can_sleep()) {
    phase_ = Phase::PREPARING;
    prepare_started_ = k_uptime_get();
    for (auto *trigger : sleep_triggers_) {
      trigger->trigger();
      if (phase_ != Phase::PREPARING) return;
    }
    // Always leave a main-loop boundary after preparation callbacks.
    return;
  }
  if (phase_ != Phase::PREPARING) return;
  if (k_uptime_get() - prepare_started_ >= prepare_timeout_) { abort_(PREPARE_TIMEOUT); return; }
  for (auto *trigger : sleep_triggers_) if (trigger->is_action_running()) return;
  if (can_sleep()) sleep_();
}

void DeepSleepComponent::on_shutdown() {
  shutdown_ = true;
  active_event_pin_ = nullptr;
  cancel_timeout("sleep");
  disarm_pins_();
#ifdef USE_DEEP_SLEEP_NRF52_VBUS
  restore_vbus_();
#endif
  for (auto &item : pins_) {
    if (item->registered) gpio_remove_callback(item->port, &item->callback);
    item->registered = false;
  }
}

void DeepSleepComponent::dump_config() {
  peripheral_mgr_.dump_config();
  ESP_LOGCONFIG(TAG, "nRF52 System ON sleep (event-driven wait; Thread remains schedulable)");
  ESP_LOGCONFIG(TAG, "  Watchdog: pause in CPU sleep, feed at idle boundaries");
  ESP_LOGCONFIG(TAG, "  GPIO group mode: %u", unsigned(wakeup_mode_));
  if (run_duration_) ESP_LOGCONFIG(TAG, "  Run duration: %" PRIu32 " ms", *run_duration_);
  if (sleep_duration_) ESP_LOGCONFIG(TAG, "  Sleep duration: %" PRIu32 " ms", *sleep_duration_);
  for (auto &item : pins_) LOG_PIN("  SENSE wake pin: ", item->pin);
#ifdef USE_DEEP_SLEEP_NRF52_VBUS
  if (vbus_enabled_) ESP_LOGCONFIG(TAG, "  VBUS hardware wake: enabled (inverted=%s)", YESNO(vbus_inverted_));
#endif
}
}  // namespace esphome::deep_sleep_nrf52
