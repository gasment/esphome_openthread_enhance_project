#pragma once

#include "esphome/core/component.h"
#include "esphome/components/adc/adc_sensor.h"
#include "esphome/components/sensor/sensor.h"
#include <vector>

namespace esphome::battery_level_nrf52 {

class BatteryLevel : public sensor::Sensor, public PollingComponent {
 public:
  void set_voltage_source(adc::ADCSensor *source) { voltage_ = source; }
  void set_samples(uint8_t samples) { samples_ = samples; }
  void set_voltage_range(float minimum, float maximum) { minimum_ = minimum; maximum_ = maximum; }
  void add_curve_point(float voltage, float percentage) { curve_.push_back({voltage, percentage}); }
  void update() override;
  void dump_config() override;

 protected:
  struct Point { float voltage, percentage; };
  float estimate_(float voltage) const;
  adc::ADCSensor *voltage_{nullptr};
  std::vector<Point> curve_;
  uint8_t samples_{3}, count_{0};
  float sum_{0}, minimum_{1.7f}, maximum_{3.6f};
};

}  // namespace esphome::battery_level_nrf52
