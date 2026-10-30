#pragma once

#ifdef USE_ZEPHYR
#include "uart_component.h"
#include <hal/nrf_uarte.h>
#include <zephyr/device.h>
#include <zephyr/drivers/uart.h>
#include <zephyr/kernel.h>
#include <zephyr/sys/atomic.h>
#include <zephyr/sys/ring_buffer.h>
#include <atomic>

namespace esphome::uart_nrf52 {

class ZephyrUARTComponent : public UARTComponent, public Component {
 public:
  ZephyrUARTComponent(const struct device *dev, NRF_UARTE_Type *registers) : dev_(dev), registers_(registers) {}
  void setup() override;
  void loop() override;
  void dump_config() override;
  void on_shutdown() override;
  float get_setup_priority() const override { return setup_priority::BUS; }
  using UARTComponent::write_array;
  void write_array(const uint8_t *data, size_t len) override;
  bool read_array(uint8_t *data, size_t len) override;
  bool peek_byte(uint8_t *data) override;
  size_t available() override;
  UARTFlushResult flush() override;
  void load_settings(bool dump_config = true);

 protected:
  void check_logger_conflict() override {}
  bool hardware_active_() const;
  static void irq_handler_(const struct device *dev, void *context);
  const struct device *dev_;
  NRF_UARTE_Type *registers_;
  uint32_t last_warning_ms_{0};
  struct ring_buf rx_ring_{};
  std::vector<uint8_t> rx_storage_;
  struct k_sem tx_done_{};
  struct k_mutex tx_mutex_{};
  const uint8_t *tx_data_{nullptr};
  size_t tx_remaining_{0};
  bool tx_active_{false};
  std::atomic<bool> ready_{false};
  atomic_t dropped_{0};
  atomic_t errors_{0};
  UARTFlushResult tx_result_{UARTFlushResult::UART_FLUSH_RESULT_SUCCESS};
};

}  // namespace esphome::uart_nrf52
#endif
