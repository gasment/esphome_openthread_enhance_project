#pragma once
#include <bitset>
#include <cstdint>

namespace esphome::thread_gateway {
// Bounded per-session packet replay window. Events may arrive out of order;
// normal state snapshots continue to use their existing newest-value checks.
class EventWindow {
 public:
  bool accept(uint32_t sequence) {
    if (!initialized_) {
      initialized_ = true; newest_ = sequence; seen_.reset(); seen_.set(0); return true;
    }
    const int32_t advance = static_cast<int32_t>(sequence - newest_);
    if (advance > 0) {
      seen_ <<= static_cast<uint32_t>(advance);
      newest_ = sequence; seen_.set(0); return true;
    }
    const uint32_t age = newest_ - sequence;
    if (age >= seen_.size() || seen_.test(age)) return false;
    seen_.set(age); return true;
  }
 protected:
  std::bitset<256> seen_{};
  uint32_t newest_{0};
  bool initialized_{false};
};
}  // namespace esphome::thread_gateway
