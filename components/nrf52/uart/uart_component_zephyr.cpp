#include "uart_component_zephyr.h"
#ifdef USE_ZEPHYR
#include "nrf52_baudrate.h"
#include <algorithm>
#include <cerrno>
#include <climits>
#include <cinttypes>
#include <cstring>
#include <zephyr/pm/device.h>

namespace esphome::uart {
static const char *const TAG = "uart_nrf52.zephyr";

bool ZephyrUARTComponent::hardware_active_() const {
  enum pm_device_state state;
  return pm_device_state_get(this->dev_, &state) != 0 || state == PM_DEVICE_STATE_ACTIVE;
}

void ZephyrUARTComponent::setup() {
  if (!device_is_ready(this->dev_)) {
    ESP_LOGE(TAG, "%s: UART device is not ready", this->dev_->name);
    this->mark_failed(); return;
  }
  this->rx_storage_.resize(this->rx_buffer_size_);
  ring_buf_init(&this->rx_ring_, this->rx_storage_.size(), this->rx_storage_.data());
  k_sem_init(&this->tx_done_, 0, 1); k_sem_init(&this->rx_done_, 0, 1);
  k_mutex_init(&this->tx_mutex_);
  this->load_settings(false);
  if (this->is_failed()) return;
  const int err = uart_callback_set(this->dev_, async_callback_, this);
  if (err != 0) {
    ESP_LOGE(TAG, "%s: cannot install async UART callback: %d", this->dev_->name, err);
    this->mark_failed(); return;
  }
  this->callback_registered_ = true;
  this->ready_ = true;
  if (!this->start_rx_()) {
    ESP_LOGE(TAG, "%s: cannot start DMA receiver", this->dev_->name);
    this->mark_failed();
  }
}

void ZephyrUARTComponent::apply_custom_baud_() {
  if (!nrf52_driver_baudrate(this->baud_rate_)) {
    const uint32_t desired = nrf52_custom_baudrate_register(this->baud_rate_);
    if (this->registers_->BAUDRATE != desired)
      nrf_uarte_baudrate_set(this->registers_, static_cast<nrf_uarte_baudrate_t>(desired));
  }
}

void ZephyrUARTComponent::load_settings(bool dump) {
  if (this->data_bits_ != 8 || this->parity_ == UART_CONFIG_PARITY_ODD ||
      (this->stop_bits_ != 1 && this->stop_bits_ != 2) || this->baud_rate_ < 1200 || this->baud_rate_ > 1000000) {
    ESP_LOGE(TAG, "Unsupported nRF52840 UART settings"); this->mark_failed(); return;
  }
  const bool restart = this->ready_ && !this->paused_;
  const bool lock_needed = this->ready_;
  if (lock_needed && k_mutex_lock(&this->tx_mutex_, K_NO_WAIT) != 0) {
    this->settings_pending_ = true; return;
  }
  if (this->ready_ && (this->tx_active_ || !this->stop_rx_())) {
    this->settings_pending_ = true;
    if (lock_needed) k_mutex_unlock(&this->tx_mutex_);
    return;
  }
  struct uart_config cfg{};
  cfg.baudrate = nrf52_driver_baudrate(this->baud_rate_) ? this->baud_rate_ : 115200;
  cfg.data_bits = UART_CFG_DATA_BITS_8;
  cfg.stop_bits = this->stop_bits_ == 2 ? UART_CFG_STOP_BITS_2 : UART_CFG_STOP_BITS_1;
  cfg.parity = this->parity_ == UART_CONFIG_PARITY_EVEN ? UART_CFG_PARITY_EVEN : UART_CFG_PARITY_NONE;
  cfg.flow_ctrl = UART_CFG_FLOW_CTRL_NONE;
  const int err = uart_configure(this->dev_, &cfg);
  if (err != 0) {
    ESP_LOGE(TAG, "%s: UART configuration rejected: %d (baud=%" PRIu32 ")", this->dev_->name, err, this->baud_rate_);
    if (lock_needed) k_mutex_unlock(&this->tx_mutex_);
    this->mark_failed(); return;
  }
  this->settings_pending_ = false;
  this->apply_custom_baud_();
  if (restart) this->start_rx_();
  if (lock_needed) k_mutex_unlock(&this->tx_mutex_);
  if (dump) this->dump_config();
}

void ZephyrUARTComponent::push_rx_(const uint8_t *data, size_t len) {
  if (!len) return;
  // Async callbacks (including timeout callbacks) are serialized by nrfx.
  const uint32_t stored = ring_buf_put(&this->rx_ring_, data, len);
  if (stored < len) atomic_add(&this->dropped_, len - stored);
}

void ZephyrUARTComponent::offer_rx_buffer_() {
  if (!this->rx_requested_ || this->rx_stopping_ || this->paused_ || !this->ready_) return;
  for (size_t i = 0; i < this->rx_dma_.size(); ++i) {
    bool expected = false;
    if (!this->rx_owned_[i].compare_exchange_strong(expected, true)) continue;
    const int err = uart_rx_buf_rsp(this->dev_, this->rx_dma_[i].data(), DMA_SIZE);
    if (err == 0) this->rx_requested_ = false;
    else { this->rx_owned_[i] = false; atomic_set(&this->rx_api_error_, err); }
    return;
  }
}

void ZephyrUARTComponent::async_callback_(const struct device *, struct uart_event *event, void *context) {
  auto *self = static_cast<ZephyrUARTComponent *>(context);
  switch (event->type) {
    case UART_RX_RDY:
      self->push_rx_(event->data.rx.buf + event->data.rx.offset, event->data.rx.len);
      break;
    case UART_RX_BUF_REQUEST:
      self->rx_requested_ = true; self->offer_rx_buffer_(); break;
    case UART_RX_BUF_RELEASED:
      for (size_t i = 0; i < self->rx_dma_.size(); ++i)
        if (event->data.rx_buf.buf == self->rx_dma_[i].data()) self->rx_owned_[i] = false;
      self->offer_rx_buffer_(); break;
    case UART_RX_STOPPED:
      self->rx_stopping_ = true;
      atomic_or(&self->errors_, event->data.rx_stop.reason);
      atomic_inc(&self->rx_stops_);
      // nrfx separately emits RX_RDY for the remaining bytes. Do not copy the
      // stop event's data again, or frames would contain duplicate bytes.
      break;
    case UART_RX_DISABLED:
      if (!self->paused_ && !self->rx_stopping_) atomic_inc(&self->rx_stops_);
      self->rx_requested_ = false;
      self->rx_active_ = false;
      k_sem_give(&self->rx_done_);
      if (self->ready_ && !self->paused_) self->enable_loop_soon_any_context();
      break;
    case UART_TX_DONE:
    case UART_TX_ABORTED:
      self->tx_result_ = event->type == UART_TX_DONE && event->data.tx.len == self->tx_expected_
          ? UARTFlushResult::UART_FLUSH_RESULT_SUCCESS : UARTFlushResult::UART_FLUSH_RESULT_FAILED;
      self->tx_active_ = false;
      k_sem_give(&self->tx_done_);
      break;
    default: break;
  }
}

bool ZephyrUARTComponent::start_rx_() {
  if (!this->ready_ || this->paused_ || this->is_failed() || !this->hardware_active_()) return false;
  if (this->rx_pin_ == nullptr || this->rx_active_) return true;
  // Never recycle a buffer on STOPPED. Only RELEASED gives ownership back.
  if (this->rx_owned_[0] || this->rx_owned_[1]) return false;
  this->apply_custom_baud_();
  k_sem_reset(&this->rx_done_);
  this->rx_stopping_ = false; this->rx_requested_ = false;
  this->rx_owned_[0] = true; this->rx_active_ = true;
  const int err = uart_rx_enable(this->dev_, this->rx_dma_[0].data(), DMA_SIZE, RX_TIMEOUT_US);
  if (err != 0) {
    this->rx_active_ = false; this->rx_owned_[0] = false;
    atomic_set(&this->rx_api_error_, err); return false;
  }
  return true;
}

bool ZephyrUARTComponent::stop_rx_() {
  this->rx_stopping_ = true; this->rx_requested_ = false;
  if (!this->rx_active_) return !this->rx_owned_[0] && !this->rx_owned_[1];
  k_sem_reset(&this->rx_done_);
  const int err = uart_rx_disable(this->dev_);
  if (err != 0 && err != -EFAULT) { atomic_set(&this->rx_api_error_, err); return false; }
  if (this->rx_active_ && k_sem_take(&this->rx_done_, K_MSEC(100)) != 0) return false;
  return !this->rx_active_ && !this->rx_owned_[0] && !this->rx_owned_[1];
}

bool ZephyrUARTComponent::abort_tx_() {
  if (!this->tx_active_) return true;
  const int err = uart_tx_abort(this->dev_);
  if (err != 0 && err != -EFAULT) return false;
  if (this->tx_active_ && k_sem_take(&this->tx_done_, K_MSEC(100)) != 0) return false;
  return !this->tx_active_;
}

void ZephyrUARTComponent::write_array(const uint8_t *data, size_t len) {
  if (!this->ready_ || this->paused_ || this->is_failed() || !this->hardware_active_() || this->tx_pin_ == nullptr || k_is_in_isr()) {
    this->tx_result_ = UARTFlushResult::UART_FLUSH_RESULT_FAILED; return;
  }
  if (!len) return;
  k_mutex_lock(&this->tx_mutex_, K_FOREVER);
  if (!this->ready_ || this->paused_ || this->is_failed() || !this->hardware_active_() || this->tx_active_) {
    this->tx_result_ = UARTFlushResult::UART_FLUSH_RESULT_FAILED;
    k_mutex_unlock(&this->tx_mutex_); return;
  }
  pm_device_busy_set(this->dev_);
  this->apply_custom_baud_();
  this->tx_result_ = UARTFlushResult::UART_FLUSH_RESULT_SUCCESS;
  const size_t original_len = len;
  const uint8_t *original_data = data;
  const uint64_t bits = 1 + this->data_bits_ + this->stop_bits_ + (this->parity_ != UART_CONFIG_PARITY_NONE);
  while (len && this->ready_ && !this->paused_ && this->tx_result_ == UARTFlushResult::UART_FLUSH_RESULT_SUCCESS) {
    const size_t chunk = std::min(len, DMA_SIZE);
    // Caller memory may be in flash or on a transient stack. DMA only receives
    // this persistent RAM buffer, including after timeout/abort.
    std::memcpy(this->tx_dma_.data(), data, chunk);
    this->tx_expected_ = chunk;
    k_sem_reset(&this->tx_done_);
    this->tx_result_ = UARTFlushResult::UART_FLUSH_RESULT_FAILED; this->tx_active_ = true;
    const int err = uart_tx(this->dev_, this->tx_dma_.data(), chunk, SYS_FOREVER_US);
    if (err != 0) {
      this->tx_active_ = false;
      ESP_LOGW(TAG, "%s: UART DMA transmit submission failed: %d", this->dev_->name, err);
      break;
    }
    const uint32_t timeout = (chunk * bits * 1000 + this->baud_rate_ - 1) / this->baud_rate_ + 100;
    if (k_sem_take(&this->tx_done_, K_MSEC(timeout)) != 0) {
      const bool stopped = this->abort_tx_();
      this->tx_result_ = UARTFlushResult::UART_FLUSH_RESULT_TIMEOUT;
      if (!stopped) { this->ready_ = false; this->mark_failed(); }
      ESP_LOGW(TAG, "%s: UART DMA transmit timeout%s", this->dev_->name, stopped ? "" : "; DMA still owned, UART disabled");
      break;
    }
    if (this->tx_result_ != UARTFlushResult::UART_FLUSH_RESULT_SUCCESS) break;
    data += chunk; len -= chunk;
  }
  if (len && this->tx_result_ == UARTFlushResult::UART_FLUSH_RESULT_SUCCESS)
    this->tx_result_ = UARTFlushResult::UART_FLUSH_RESULT_FAILED;
  if (!this->tx_active_) pm_device_busy_clear(this->dev_);
  const bool success = this->tx_result_ == UARTFlushResult::UART_FLUSH_RESULT_SUCCESS;
  k_mutex_unlock(&this->tx_mutex_);
#ifdef USE_UART_NRF52_DEBUGGER
  if (success)
    for (size_t i = 0; i < original_len; ++i) this->debug_callback_.call(UART_DIRECTION_TX, original_data[i]);
#else
  (void)original_len; (void)original_data; (void)success;
#endif
}

UARTFlushResult ZephyrUARTComponent::flush() {
  if (!this->ready_ || this->is_failed()) return UARTFlushResult::UART_FLUSH_RESULT_FAILED;
  k_mutex_lock(&this->tx_mutex_, K_FOREVER);
  const auto result = this->tx_result_.load();
  k_mutex_unlock(&this->tx_mutex_); return result;
}

size_t ZephyrUARTComponent::available() {
  if (!this->ready_ || this->is_failed() || this->rx_pin_ == nullptr) return 0;
  return ring_buf_size_get(&this->rx_ring_);
}

bool ZephyrUARTComponent::peek_byte(uint8_t *data) {
  if (!this->ready_ || this->is_failed() || this->rx_pin_ == nullptr || !this->check_read_timeout_()) return false;
  return ring_buf_peek(&this->rx_ring_, data, 1) == 1;
}

bool ZephyrUARTComponent::read_array(uint8_t *data, size_t len) {
  if (!this->ready_ || this->is_failed() || this->rx_pin_ == nullptr || len > this->rx_buffer_size_ || !this->check_read_timeout_(len)) return false;
  // Single producer (driver callback), single consumer (main loop). No global
  // irq_lock around a potentially large copy; Thread IRQs remain serviceable.
  const uint32_t count = ring_buf_get(&this->rx_ring_, data, len);
#ifdef USE_UART_NRF52_DEBUGGER
  for (uint32_t i = 0; i < count; ++i) this->debug_callback_.call(UART_DIRECTION_RX, data[i]);
#endif
  return count == len;
}

bool ZephyrUARTComponent::prepare_sleep() {
  this->paused_ = true;
  if (!this->callback_registered_) return true;
  // Serialize against a writer which passed its initial paused check on
  // another thread but has not submitted its DMA transaction yet.
  if (k_mutex_lock(&this->tx_mutex_, K_NO_WAIT) != 0) return false;
  const bool stopped = !this->tx_active_ && this->stop_rx_();
  k_mutex_unlock(&this->tx_mutex_);
  return stopped;
}

void ZephyrUARTComponent::resume_from_sleep() {
  this->paused_ = false;
  if (this->ready_ && !this->is_failed()) this->start_rx_();
}

void ZephyrUARTComponent::loop() {
  const uint32_t now = millis();
  if (this->settings_pending_ && this->ready_ && !this->paused_ && !this->is_failed() && this->hardware_active_() &&
      now - this->settings_retry_at_ >= 20) {
    this->settings_retry_at_ = now;
    this->load_settings(false);
  }
  if (this->ready_ && !this->paused_ && !this->is_failed() && this->rx_pin_ != nullptr && !this->rx_active_ &&
      this->hardware_active_() && now - this->rx_retry_at_ >= 20) {
    this->rx_retry_at_ = now;
    this->start_rx_();
  }
  if (now - this->last_warning_ms_ < 1000) return;
  this->last_warning_ms_ = now;
  const auto dropped = atomic_set(&this->dropped_, 0), errors = atomic_set(&this->errors_, 0);
  const auto stops = atomic_set(&this->rx_stops_, 0), api_error = atomic_set(&this->rx_api_error_, 0);
  if (dropped) ESP_LOGW(TAG, "%s: RX buffer overflow: dropped %ld bytes", this->dev_->name, static_cast<long>(dropped));
  if (errors || stops) ESP_LOGW(TAG, "%s: UART DMA receive stopped %ld time(s): 0x%lx%s%s%s%s", this->dev_->name,
      static_cast<long>(stops), static_cast<unsigned long>(errors),
      (errors & UART_ERROR_OVERRUN) ? " OVERRUN" : "", (errors & UART_ERROR_PARITY) ? " PARITY" : "",
      (errors & UART_ERROR_FRAMING) ? " FRAMING" : "", (errors & UART_BREAK) ? " BREAK" : "");
  if (api_error) ESP_LOGW(TAG, "%s: UART DMA buffer/restart error: %ld", this->dev_->name, static_cast<long>(api_error));
}

void ZephyrUARTComponent::dump_config() {
  ESP_LOGCONFIG(TAG, "Peripheral UART %s: async EasyDMA 2x%u B, baud=%" PRIu32 ", data=%u, stop=%u, parity=%s, RX ring=%zu B",
      this->dev_->name, unsigned(DMA_SIZE), this->baud_rate_, this->data_bits_, this->stop_bits_,
      LOG_STR_ARG(parity_to_str(this->parity_)), this->rx_buffer_size_);
  LOG_PIN("  TX Pin: ", this->tx_pin_); LOG_PIN("  RX Pin: ", this->rx_pin_);
}

void ZephyrUARTComponent::on_shutdown() {
  this->paused_ = true;
  if (!this->callback_registered_) return;
  const bool rx_stopped = this->stop_rx_(), tx_stopped = this->abort_tx_();
  this->ready_ = false;
  if (rx_stopped && tx_stopped) {
    uart_callback_set(this->dev_, nullptr, nullptr);
    this->callback_registered_ = false;
  }
  // On timeout keep callback and owned storage alive; never hand live DMA
  // memory back to a writer or allocator.
}
}  // namespace esphome::uart
#endif
