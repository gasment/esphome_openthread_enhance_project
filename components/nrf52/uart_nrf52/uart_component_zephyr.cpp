#include "uart_component_zephyr.h"

#ifdef USE_ZEPHYR
#include "nrf52_baudrate.h"
#include <algorithm>
#include <climits>
#include <cinttypes>
#include <zephyr/pm/device.h>

namespace esphome::uart_nrf52 {
static const char *const TAG = "uart_nrf52.zephyr";

bool ZephyrUARTComponent::hardware_active_() const {
  enum pm_device_state state;
  return pm_device_state_get(this->dev_, &state) != 0 || state == PM_DEVICE_STATE_ACTIVE;
}

void ZephyrUARTComponent::setup() {
  if (!device_is_ready(this->dev_)) {
    ESP_LOGE(TAG, "%s: uart_nrf52 device is not ready", this->dev_->name);
    this->mark_failed();
    return;
  }
  this->rx_storage_.resize(this->rx_buffer_size_);
  ring_buf_init(&this->rx_ring_, this->rx_storage_.size(), this->rx_storage_.data());
  k_sem_init(&this->tx_done_, 0, 1);
  k_mutex_init(&this->tx_mutex_);
  this->load_settings(false);
  if (this->is_failed())
    return;
  int err = uart_irq_callback_user_data_set(this->dev_, irq_handler_, this);
  if (err != 0) {
    ESP_LOGE(TAG, "Cannot install uart_nrf52 interrupt callback: %d", err);
    this->mark_failed();
    return;
  }
  this->ready_ = true;
  if (this->rx_pin_ != nullptr)
    uart_irq_rx_enable(this->dev_);
  uart_irq_err_enable(this->dev_);
}

void ZephyrUARTComponent::load_settings(bool dump) {
  if (this->data_bits_ != 8 || this->parity_ == UART_CONFIG_PARITY_ODD ||
      (this->stop_bits_ != 1 && this->stop_bits_ != 2) || this->baud_rate_ < 1200 || this->baud_rate_ > 1000000) {
    ESP_LOGE(TAG, "Unsupported nRF52840 uart_nrf52 settings");
    this->mark_failed();
    return;
  }
  struct uart_config cfg{};
  const bool custom_baud = !nrf52_driver_baudrate(this->baud_rate_);
  // Configure the frame format through the driver first. Its legacy API rejects
  // 256000 and other non-table speeds, so set the actual divider through HAL.
  cfg.baudrate = custom_baud ? 115200 : this->baud_rate_;
  cfg.data_bits = UART_CFG_DATA_BITS_8;
  cfg.stop_bits = this->stop_bits_ == 2 ? UART_CFG_STOP_BITS_2 : UART_CFG_STOP_BITS_1;
  cfg.parity = this->parity_ == UART_CONFIG_PARITY_EVEN ? UART_CFG_PARITY_EVEN : UART_CFG_PARITY_NONE;
  cfg.flow_ctrl = UART_CFG_FLOW_CTRL_NONE;
  const int err = uart_configure(this->dev_, &cfg);
  if (err != 0) {
    ESP_LOGE(TAG, "%s: uart_nrf52 configuration rejected: %d (requested baud=%" PRIu32 ")",
             this->dev_->name, err, this->baud_rate_);
    this->mark_failed();
  } else if (custom_baud) {
    nrf_uarte_baudrate_set(this->registers_, static_cast<nrf_uarte_baudrate_t>(nrf52_custom_baudrate_register(this->baud_rate_)));
    ESP_LOGD(TAG, "%s: custom baud=%" PRIu32 ", BAUDRATE=0x%08" PRIx32,
             this->dev_->name, this->baud_rate_, nrf52_custom_baudrate_register(this->baud_rate_));
  }
  if (dump)
    this->dump_config();
}

void ZephyrUARTComponent::irq_handler_(const struct device *dev, void *context) {
  auto *self = static_cast<ZephyrUARTComponent *>(context);
  if (!uart_irq_update(dev))
    return;
  const int errors = uart_err_check(dev);
  if (errors > 0)
    atomic_or(&self->errors_, errors);
  if (uart_irq_rx_ready(dev) > 0) {
    // Drain even when the software ring is full, to clear the RX condition.
    uint8_t bytes[16];
    int count;
    while ((count = uart_fifo_read(dev, bytes, sizeof(bytes))) > 0) {
      const uint32_t stored = ring_buf_put(&self->rx_ring_, bytes, count);
      if (stored < static_cast<uint32_t>(count))
        atomic_add(&self->dropped_, count - stored);
    }
  }
  if (uart_irq_tx_ready(dev) > 0) {
    if (self->tx_active_ && self->tx_remaining_ > 0) {
      const int sent = uart_fifo_fill(dev, self->tx_data_, std::min(self->tx_remaining_, size_t(INT_MAX)));
      if (sent > 0) {
        self->tx_data_ += sent;
        self->tx_remaining_ -= sent;
      } else if (sent < 0) {
        self->tx_result_ = UARTFlushResult::UART_FLUSH_RESULT_FAILED;
        self->tx_active_ = false;
        uart_irq_tx_disable(dev);
        k_sem_give(&self->tx_done_);
      }
    } else {
      // On this NCS nrfx backend TX-ready is TXSTOPPED: the last DMA block
      // has left the wire. Do not use this assumption for other uart_nrf52 drivers.
      uart_irq_tx_disable(dev);
      self->tx_active_ = false;
      k_sem_give(&self->tx_done_);
    }
  }
}

void ZephyrUARTComponent::write_array(const uint8_t *data, size_t len) {
  if (!this->ready_ || this->is_failed() || !this->hardware_active_() || this->tx_pin_ == nullptr || k_is_in_isr()) {
    this->tx_result_ = UARTFlushResult::UART_FLUSH_RESULT_FAILED;
    return;
  }
  if (len == 0)
    return;
  k_mutex_lock(&this->tx_mutex_, K_FOREVER);
  // A writer may have waited behind the sleep transition.
  if (!this->ready_ || this->is_failed() || !this->hardware_active_()) {
    this->tx_result_ = UARTFlushResult::UART_FLUSH_RESULT_FAILED;
    k_mutex_unlock(&this->tx_mutex_);
    return;
  }
  k_sem_reset(&this->tx_done_);
  pm_device_busy_set(this->dev_);
  unsigned int key = irq_lock();
  this->tx_data_ = data;
  this->tx_remaining_ = len;
  this->tx_result_ = UARTFlushResult::UART_FLUSH_RESULT_SUCCESS;
  this->tx_active_ = true;
  uart_irq_tx_enable(this->dev_);
  irq_unlock(key);
  const uint64_t bits = 1 + this->data_bits_ + this->stop_bits_ + (this->parity_ != UART_CONFIG_PARITY_NONE);
  const uint64_t timeout = std::min<uint64_t>((uint64_t(len) * bits * 1000 + this->baud_rate_ - 1) / this->baud_rate_ + 100, INT_MAX);
  const int err = k_sem_take(&this->tx_done_, K_MSEC(timeout));
  if (err != 0) {
    key = irq_lock();
    uart_irq_tx_disable(this->dev_);
    this->tx_active_ = false;
    this->tx_data_ = nullptr;
    this->tx_remaining_ = 0;
    irq_unlock(key);
    this->tx_result_ = UARTFlushResult::UART_FLUSH_RESULT_TIMEOUT;
    ESP_LOGE(TAG, "uart_nrf52 transmit timed out");
  }
  pm_device_busy_clear(this->dev_);
  k_mutex_unlock(&this->tx_mutex_);
#ifdef USE_UART_NRF52_DEBUGGER
  if (this->tx_result_ == UARTFlushResult::UART_FLUSH_RESULT_SUCCESS)
    for (size_t i = 0; i < len; i++)
      this->debug_callback_.call(UART_DIRECTION_TX, data[i]);
#endif
}

UARTFlushResult ZephyrUARTComponent::flush() {
  if (!this->ready_ || this->is_failed())
    return UARTFlushResult::UART_FLUSH_RESULT_FAILED;
  k_mutex_lock(&this->tx_mutex_, K_FOREVER);
  const auto result = this->tx_result_;
  k_mutex_unlock(&this->tx_mutex_);
  return result;
}

size_t ZephyrUARTComponent::available() {
  if (!this->ready_ || this->is_failed() || this->rx_pin_ == nullptr)
    return 0;
  const unsigned int key = irq_lock();
  const size_t count = ring_buf_size_get(&this->rx_ring_);
  irq_unlock(key);
  return count;
}

bool ZephyrUARTComponent::peek_byte(uint8_t *data) {
  if (!this->ready_ || this->is_failed() || this->rx_pin_ == nullptr || !this->check_read_timeout_())
    return false;
  const unsigned int key = irq_lock();
  const uint32_t count = ring_buf_peek(&this->rx_ring_, data, 1);
  irq_unlock(key);
  return count == 1;
}

bool ZephyrUARTComponent::read_array(uint8_t *data, size_t len) {
  if (!this->ready_ || this->is_failed() || this->rx_pin_ == nullptr || len > this->rx_buffer_size_ || !this->check_read_timeout_(len))
    return false;
  const unsigned int key = irq_lock();
  const uint32_t count = ring_buf_get(&this->rx_ring_, data, len);
  irq_unlock(key);
#ifdef USE_UART_NRF52_DEBUGGER
  for (uint32_t i = 0; i < count; i++)
    this->debug_callback_.call(UART_DIRECTION_RX, data[i]);
#endif
  return count == len;
}

void ZephyrUARTComponent::loop() {
  // Aggregate errors while noisy/disconnected inputs persist. Logging each
  // main-loop pass can itself flood RTT and delay the application.
  const uint32_t now = millis();
  if (now - this->last_warning_ms_ < 1000)
    return;
  this->last_warning_ms_ = now;
  const auto dropped = atomic_set(&this->dropped_, 0);
  const auto errors = atomic_set(&this->errors_, 0);
  if (dropped)
    ESP_LOGW(TAG, "%s: RX buffer overflow: dropped %ld bytes", this->dev_->name, static_cast<long>(dropped));
  if (errors)
    ESP_LOGW(TAG, "%s: uart_nrf52 receive errors: 0x%lx%s%s%s%s", this->dev_->name, static_cast<unsigned long>(errors),
             (errors & UART_ERROR_OVERRUN) ? " OVERRUN" : "", (errors & UART_ERROR_PARITY) ? " PARITY" : "",
             (errors & UART_ERROR_FRAMING) ? " FRAMING" : "", (errors & UART_BREAK) ? " BREAK" : "");
}

void ZephyrUARTComponent::dump_config() {
  ESP_LOGCONFIG(TAG, "Peripheral uart_nrf52 %s: baud=%" PRIu32 ", data=%u, stop=%u, parity=%s, RX buffer=%zu",
                this->dev_->name, this->baud_rate_, this->data_bits_, this->stop_bits_,
                LOG_STR_ARG(parity_to_str(this->parity_)), this->rx_buffer_size_);
  LOG_PIN("  TX Pin: ", this->tx_pin_);
  LOG_PIN("  RX Pin: ", this->rx_pin_);
}

void ZephyrUARTComponent::on_shutdown() {
  if (this->ready_) {
    uart_irq_rx_disable(this->dev_);
    uart_irq_tx_disable(this->dev_);
    uart_irq_err_disable(this->dev_);
    uart_irq_callback_user_data_set(this->dev_, nullptr, nullptr);
    this->ready_ = false;
  }
}

}  // namespace esphome::uart_nrf52
#endif
