#pragma once

#include "esphome/core/component.h"
#include "esphome/components/sensor/sensor.h"
#include <zephyr/drivers/adc.h>
#include <vector>

namespace esphome::battery_level_nrf52 {

class BatteryLevel : public sensor::Sensor, public PollingComponent {
 public:
  void set_adc_channel(const adc_dt_spec *channel) { channel_ = channel; }
  void set_adc_samples(uint8_t samples) { adc_samples_ = samples; }
  void set_sampling_mode(uint8_t mode) { sampling_mode_ = mode; }
  void set_voltage_multiplier(float multiplier) { voltage_multiplier_ = multiplier; }
  void set_voltage_sensor(sensor::Sensor *voltage) { voltage_ = voltage; }
  void set_samples(uint8_t samples) { samples_ = samples; }
  void set_voltage_range(float minimum, float maximum) { minimum_ = minimum; maximum_ = maximum; }
  void add_curve_point(float voltage, float percentage) { curve_.push_back({voltage, percentage}); }
  float get_setup_priority() const override { return setup_priority::DATA; }
  void setup() override;
  void update() override;
  void dump_config() override;

 protected:
  struct Point { float voltage, percentage; };
  float estimate_(float voltage) const;
  float sample_voltage_();
  bool updating_{false};
  const adc_dt_spec *channel_{nullptr};
  sensor::Sensor *voltage_{nullptr};
  uint8_t adc_samples_{1}, sampling_mode_{0};
  float voltage_multiplier_{1.0f};
  std::vector<Point> curve_;
  uint8_t samples_{3}, count_{0};
  float sum_{0}, minimum_{1.7f}, maximum_{3.6f};
};

}  // namespace esphome::battery_level_nrf52
