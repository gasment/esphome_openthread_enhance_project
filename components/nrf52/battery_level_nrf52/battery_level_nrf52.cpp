#include "battery_level_nrf52.h"
#include "esphome/core/log.h"
#include <algorithm>
#include <cmath>

namespace esphome::battery_level_nrf52 {
static const char *const TAG = "battery_level_nrf52";

void BatteryLevel::setup() {
  if (channel_ == nullptr || !adc_is_ready_dt(channel_)) {
    ESP_LOGE(TAG, "Built-in ADC controller is not ready");
    mark_failed();
    return;
  }
  const int error = adc_channel_setup_dt(channel_);
  if (error < 0) {
    ESP_LOGE(TAG, "ADC channel setup failed: %d", error);
    mark_failed();
  }
}

float BatteryLevel::sample_voltage_() {
  int32_t aggregate = 0;
  for (uint16_t i = 0; i < adc_samples_; ++i) {
    int16_t raw = 0;
    adc_sequence sequence{};
    sequence.buffer = &raw;
    sequence.buffer_size = sizeof(raw);
    int error = adc_sequence_init_dt(channel_, &sequence);
    if (error == 0) error = adc_read(channel_->dev, &sequence);
    if (error < 0) {
      ESP_LOGW(TAG, "ADC sampling failed: %d", error);
      return NAN;
    }
    const int32_t value = std::max<int32_t>(0, raw);
    if (i == 0 || sampling_mode_ == 0) aggregate += value;
    else if (sampling_mode_ == 1) aggregate = std::min(aggregate, value);
    else aggregate = std::max(aggregate, value);
  }
  if (sampling_mode_ == 0) aggregate /= adc_samples_;
  const int error = adc_raw_to_millivolts_dt(channel_, &aggregate);
  if (error < 0) {
    ESP_LOGW(TAG, "ADC voltage conversion failed: %d", error);
    return NAN;
  }
  return (aggregate / 1000.0f) * voltage_multiplier_;
}

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
  // Automations on either output may request another update synchronously.
  // Keep one sampling transaction active until both publications complete.
  if (updating_) return;
  updating_ = true;
  struct UpdateGuard {
    bool &updating;
    ~UpdateGuard() { updating = false; }
  } guard{updating_};
  if (channel_ == nullptr || is_failed() || adc_samples_ == 0 || samples_ == 0 || curve_.size() < 2) {
    ESP_LOGW(TAG, "Built-in ADC or curve unavailable");
    status_set_warning();
    return;
  }
  const float voltage = sample_voltage_();
  if (!std::isfinite(voltage) || voltage < minimum_ || voltage > maximum_) {
    ESP_LOGW(TAG, "Invalid voltage sample; retaining previous estimate and sample count");
    status_set_warning();
    return;
  }
  if (voltage_ != nullptr) voltage_->publish_state(voltage);
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
  if (voltage_ != nullptr) LOG_SENSOR("  ", "Battery Voltage", voltage_);
  ESP_LOGCONFIG(TAG, "  Built-in ADC: channel=%u, readings/update=%u, mode=%u, voltage multiplier=%.4f",
                channel_ == nullptr ? 0U : unsigned(channel_->channel_id), unsigned(adc_samples_),
                unsigned(sampling_mode_), static_cast<double>(voltage_multiplier_));
  ESP_LOGCONFIG(TAG, "  Samples per estimate: %u; valid voltage: %.2f..%.2fV; curve points: %u",
                unsigned(samples_), static_cast<double>(minimum_), static_cast<double>(maximum_), unsigned(curve_.size()));
  ESP_LOGCONFIG(TAG, "  RAM only; each update takes one voltage measurement; curve is an estimate, not fuel-gauge calibration");
  LOG_UPDATE_INTERVAL(this);
}

}  // namespace esphome::battery_level_nrf52
