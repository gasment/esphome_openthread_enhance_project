#include "uart_button.h"
#include "esphome/core/log.h"

namespace esphome::uart_nrf52 {

static const char *const TAG = "uart_nrf52.button";

void UARTButton::press_action() {
  ESP_LOGD(TAG, "'%s': Sending data", this->get_name().c_str());
  this->write_array(this->data_.data(), this->data_.size());
}

void UARTButton::dump_config() { LOG_BUTTON("", "uart_nrf52 Button", this); }

}  // namespace esphome::uart_nrf52
