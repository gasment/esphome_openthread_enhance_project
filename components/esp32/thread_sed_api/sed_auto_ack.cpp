#include "api_server.h"
#include "esphome/core/hal.h"
#include "esphome/core/helpers.h"

namespace esphome::thread_sed_api {
void APIServer::sed_auto_published(APIConnection *client, bool queued) {
  if (!this->sed_auto_enabled_ || this->sed_manual_send_ || this->shutting_down_ || this->sed_sleep_finishing_) return;
  if (this->sed_auto_client_ != nullptr && this->sed_auto_client_ != client) return;
  if (this->sed_auto_client_ == nullptr) this->sed_auto_client_ = client;
  if (!this->sed_auto_pending()) {
    this->sed_auto_started_ = millis();
    this->sed_auto_token_ = "auto:" + std::to_string(random_uint32()) + ":" + std::to_string(++this->sed_auto_sequence_);
  }
  ++this->sed_auto_generation_;
  if (this->sed_sleep_pending_) {
    this->sed_sleep_ack_ = false;
    this->sed_token_ = random_uint32();
    this->sed_marker_sent_ = 0;
  }
  if (!queued) this->sed_auto_error_ = "write_failed";
}

void APIServer::sed_auto_finish(bool success, const char *reason) {
  if (!this->sed_auto_pending()) return;
  const std::string token = this->sed_auto_token_;
  const uint32_t elapsed = millis() - this->sed_auto_started_;
  this->sed_auto_confirmed_ = success ? this->sed_auto_marker_generation_ : this->sed_auto_generation_;
  this->sed_auto_waiting_ = false;
  this->sed_auto_acked_ = false;
  // A write failure after this marker belongs to the next, unconfirmed cycle.
  if (!success || this->sed_auto_generation_ == this->sed_auto_marker_generation_)
    this->sed_auto_error_ = nullptr;
  const bool pending = this->sed_auto_pending();
  if (pending) {
    this->sed_auto_started_ = millis();
    this->sed_auto_token_ = "auto:" + std::to_string(random_uint32()) + ":" + std::to_string(++this->sed_auto_sequence_);
  }
  // Reset all transaction state before executing user automation. A callback
  // can publish another state or request sleep without completing this ID twice.
  if (success) {
    ESP_LOGD("thread_sed_api", "auto %s ACK in %u ms", token.c_str(), static_cast<unsigned>(elapsed));
    this->sed_auto_ack_trigger_.trigger(token, elapsed, pending);
  } else {
    ESP_LOGW("thread_sed_api", "auto %s failed: %s", token.c_str(), reason);
    this->sed_auto_error_trigger_.trigger(token, std::string(reason));
  }
}

void APIServer::sed_auto_tick() {
  if (!this->sed_auto_enabled_ || this->sed_manual_send_ || this->shutting_down_) return;
  // A valid application ACK remains success even if FIN/error follows it.
  if (this->sed_auto_pending() && this->sed_auto_acked_) this->sed_auto_finish(true, "ack");
  if (this->sed_auto_disconnected_) {
    this->sed_auto_disconnected_ = false;
    this->sed_auto_client_ = nullptr;
    if (this->sed_auto_pending()) this->sed_auto_finish(false, "disconnected");
    // A replacement session may already have subscribed before the old one
    // was removed. Rebind its initial state confirmation rather than losing it.
    for (auto &client : this->active_clients()) {
      if (client->sed_session_ready()) { this->sed_auto_published(client.get()); break; }
    }
    return;
  }
  if (!this->sed_auto_pending()) return;
  if (this->sed_auto_error_ != nullptr) { this->sed_auto_finish(false, this->sed_auto_error_); return; }
  if (millis() - this->sed_auto_started_ >= this->sed_auto_timeout_) {
    this->sed_auto_finish(false, "timeout"); return;
  }
  if (this->sed_auto_waiting_ || this->sed_batch_done_ || this->sed_sleep_finishing_) return;
  if (this->sed_auto_client_ == nullptr || !this->sed_auto_client_->sed_flush_ready()) return;
  if (!this->sed_events_.empty()) return;  // Existing auto event ACKs must drain first.
  if (!this->sed_send_to(this->sed_auto_client_, "thread_sed.batch", this->sed_auto_token_)) {
    this->sed_auto_finish(false, "write_failed"); return;
  }
  this->sed_auto_marker_generation_ = this->sed_auto_generation_;
  this->sed_auto_waiting_ = true;
}
}
