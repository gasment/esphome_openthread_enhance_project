#include "battery_level_nrf52.h"
#include "esphome/core/log.h"
#include <algorithm>
#include <cmath>

namespace esphome::battery_level_nrf52 {
static const char *const TAG = "battery_level_nrf52";

float BatteryLevel::estimate_(float voltage) const {
  if (voltage <= curve_.front().voltage) return curve_.front().percentage;
  for (size_t i = 1; i < curve_.size(); ++i) {
    const auto &left = curve_[i - 1];
    const auto &right = curve_[i];
    if (voltage <= right.voltage)
      return left.percentage + (voltage - left.voltage) *
             (right.percentage - left.percentage) / (right.voltage - left.voltage);
  }
  return curve_.back().percentage;
}

void BatteryLevel::update() {
  if (voltage_ == nullptr || voltage_->is_failed() || samples_ == 0 || curve_.size() < 2) {
    ESP_LOGW(TAG, "Voltage source or curve unavailable");
    status_set_warning();
    return;
  }
  // Read hardware once, not the cached state. The nRF52 ADC sample() call is
  // synchronous. Source filters/automations still receive the fresh voltage,
  // while delayed filters cannot make the estimator reuse an older sample.
  const float voltage = voltage_->sample();
  if (!std::isfinite(voltage) || voltage < minimum_ || voltage > maximum_) {
    ESP_LOGW(TAG, "Invalid voltage sample; retaining previous estimate and sample count");
    status_set_warning();
    return;
  }
  voltage_->publish_state(voltage);
  status_clear_warning();
  sum_ += voltage;
  if (++count_ < samples_) return;
  const float average = sum_ / static_cast<float>(samples_);
  count_ = 0;
  sum_ = 0;
  const float percentage = std::round(std::max(0.0f, std::min(100.0f, estimate_(average))));
  publish_state(percentage);
  ESP_LOGV(TAG, "Average=%.3fV, estimated level=%.0f%% (%u samples)",
           static_cast<double>(average), static_cast<double>(percentage), unsigned(samples_));
}

void BatteryLevel::dump_config() {
  LOG_SENSOR("", "Voltage-based Battery Estimate", this);
  ESP_LOGCONFIG(TAG, "  Samples per estimate: %u; valid voltage: %.2f..%.2fV; curve points: %u",
                unsigned(samples_), static_cast<double>(minimum_), static_cast<double>(maximum_), unsigned(curve_.size()));
  ESP_LOGCONFIG(TAG, "  RAM only; each update samples once; curve is an estimate, not fuel-gauge calibration");
  LOG_UPDATE_INTERVAL(this);
}

}  // namespace esphome::battery_level_nrf52
