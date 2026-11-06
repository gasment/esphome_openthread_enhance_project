#pragma once

#include "esphome/core/component.h"
#include "esphome/core/gpio.h"
#include "esphome/components/switch/switch.h"
#include "esphome/components/thread_br/thread_br.h"

#ifdef USE_THREAD_BR_NATIVE
namespace esphome::thread_br_led {

class ThreadBRLEDSwitch : public switch_::Switch, public Component {
 public:
  void set_parent(thread_br::ThreadBorderRouter *parent) { parent_ = parent; }
  void set_pin(InternalGPIOPin *pin) { pin_ = pin; }
  void set_activity(bool activity) { activity_ = activity; }
  float get_setup_priority() const override { return setup_priority::HARDWARE; }
  void setup() override;
  void loop() override;
  void dump_config() override;
  void on_shutdown() override;

 protected:
  void write_state(bool enabled) override;
  void drive_(bool lit);
  thread_br::ThreadBorderRouter *parent_{nullptr};
  InternalGPIOPin *pin_{nullptr};
  bool activity_{false}, enabled_{true}, lit_{false};
  bool seen_activity_{false};
  uint32_t last_sequence_{0}, last_activity_at_{0}, activity_started_at_{0};
};

}  // namespace esphome::thread_br_led
#endif
