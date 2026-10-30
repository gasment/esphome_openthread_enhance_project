#pragma once

#include <algorithm>
#include <cstdint>
#include <limits>

namespace esphome::deep_sleep_nrf52 {

enum class TriggerEvent : uint8_t { one_click, two_click, hold };

struct TriggerEventConfig {
  uint32_t debounce;
  uint32_t click_interval;
  uint32_t hold_time;
  uint32_t timeout;
};

// Receives ISR timestamps in order; no GPIO, scheduler or binary_sensor dependency.
class TriggerEventDetector {
 public:
  explicit TriggerEventDetector(TriggerEventConfig config) : config_(config) {}
  void reset(uint64_t start) {
    start_ = raw_since_ = start;
    pressed_at_ = released_at_ = 0;
    raw_ = stable_ = done_ = valid_ = saw_active_ = false;
    clicks_ = 0;
    press_duration_ = 0;
  }
  void edge(uint64_t at, bool active) {
    advance(at);
    if (done_ || raw_ == active) return;
    raw_ = active;
    raw_since_ = at;
    if (active) saw_active_ = true;
    advance(at);
  }
  void advance(uint64_t now) {
    constexpr uint64_t NEVER = std::numeric_limits<uint64_t>::max();
    while (!done_) {
      const uint64_t debounce = raw_ != stable_ ? raw_since_ + config_.debounce : NEVER;
      // A pending release suppresses hold until it is debounced; a pending
      // press suppresses single-click expiry until its validity is known.
      const uint64_t hold = stable_ && raw_ ? pressed_at_ + config_.hold_time : NEVER;
      const uint64_t single = !stable_ && !raw_ && clicks_ == 1 ? released_at_ + config_.click_interval : NEVER;
      const uint64_t timeout = start_ + config_.timeout;
      const uint64_t rejected_pulse = saw_active_ && !raw_ && !stable_ && clicks_ == 0 ?
                                          raw_since_ + config_.debounce : NEVER;
      const uint64_t next = std::min(std::min(debounce, hold), std::min(single, std::min(timeout, rejected_pulse)));
      if (next > now) return;
      if (next == debounce) {
        stable_ = raw_;
        if (stable_) {
          pressed_at_ = raw_since_;
        } else {
          press_duration_ = static_cast<uint32_t>(raw_since_ - pressed_at_);
          released_at_ = raw_since_;
          if (++clicks_ == 2) finish_(TriggerEvent::two_click);
        }
      } else if (next == hold) {
        press_duration_ = config_.hold_time;
        finish_(TriggerEvent::hold);
      } else if (next == single) {
        finish_(TriggerEvent::one_click);
      } else {
        done_ = true;  // Timeout/bounce without a valid result: release silently.
      }
    }
  }
  bool done() const { return done_; }
  bool valid() const { return valid_; }
  TriggerEvent event() const { return event_; }
  uint32_t press_duration() const { return press_duration_; }

 protected:
  void finish_(TriggerEvent event) { event_ = event; valid_ = done_ = true; }
  TriggerEventConfig config_;
  TriggerEvent event_{TriggerEvent::one_click};
  uint64_t start_{0}, raw_since_{0}, pressed_at_{0}, released_at_{0};
  bool raw_{false}, stable_{false}, done_{false}, valid_{false}, saw_active_{false};
  uint8_t clicks_{0};
  uint32_t press_duration_{0};
};

}  // namespace esphome::deep_sleep_nrf52
