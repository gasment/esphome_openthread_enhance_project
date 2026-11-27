#pragma once
#ifdef USE_ZEPHYR
#include "uart_component.h"
#include <hal/nrf_uarte.h>
#include <zephyr/device.h>
#include <zephyr/drivers/uart.h>
#include <zephyr/kernel.h>
#include <zephyr/sys/atomic.h>
#include <zephyr/sys/ring_buffer.h>
#include <array>
#include <atomic>

namespace esphome::uart {
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
  bool prepare_sleep();
  void resume_from_sleep();

 protected:
  static constexpr size_t DMA_SIZE = 256;
  static constexpr int32_t RX_TIMEOUT_US = 5000;
  void check_logger_conflict() override {}
  bool hardware_active_() const;
  bool start_rx_();
  bool stop_rx_();
  bool abort_tx_();
  void offer_rx_buffer_();
  void apply_custom_baud_();
  void push_rx_(const uint8_t *data, size_t len);
  static void async_callback_(const struct device *dev, struct uart_event *event, void *context);
  const struct device *dev_;
  NRF_UARTE_Type *registers_;
  uint32_t last_warning_ms_{0}, rx_retry_at_{0}, settings_retry_at_{0};
  struct ring_buf rx_ring_{};
  std::vector<uint8_t> rx_storage_;
  // DMA owns these arrays until BUF_RELEASED / TX_DONE / TX_ABORTED.
  alignas(4) std::array<std::array<uint8_t, DMA_SIZE>, 2> rx_dma_{};
  alignas(4) std::array<uint8_t, DMA_SIZE> tx_dma_{};
  std::array<std::atomic<bool>, 2> rx_owned_{};
  std::atomic<bool> rx_active_{false}, rx_requested_{false}, rx_stopping_{false};
  struct k_sem tx_done_{}, rx_done_{};
  struct k_mutex tx_mutex_{};
  size_t tx_expected_{0};
  bool callback_registered_{false};
  std::atomic<bool> settings_pending_{false};
  std::atomic<bool> tx_active_{false}, ready_{false}, paused_{false};
  atomic_t dropped_{0}, errors_{0}, rx_stops_{0}, rx_api_error_{0};
  std::atomic<UARTFlushResult> tx_result_{UARTFlushResult::UART_FLUSH_RESULT_SUCCESS};
};
}  // namespace esphome::uart
#endif
