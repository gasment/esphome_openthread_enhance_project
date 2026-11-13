#include "api_server.h"
#include "esphome/core/hal.h"
#include "esphome/core/helpers.h"
#include <algorithm>

namespace esphome::thread_proxy_client {
void ThreadProxyClient::sed_auto_published(APIConnection *client, bool queued) {
  if (!this->sed_auto_enabled_ || this->sed_manual_send_ || this->shutting_down_ || this->sed_sleep_finishing_) return;
  if (this->sed_auto_client_ != nullptr && this->sed_auto_client_ != client) return;
  if (this->sed_auto_client_ == nullptr) this->sed_auto_client_ = client;
  if (!this->sed_auto_pending()) {
    this->sed_auto_event_cursor_=0;
    this->sed_auto_retries_left_=this->sed_auto_resend_;
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

void ThreadProxyClient::sed_auto_finish(bool success, const char *reason) {
  if (!this->sed_auto_pending()) return;
  if (!success) {
    if (this->sed_auto_client_ != nullptr) this->sed_auto_client_->sed_cancel_snapshot();
    this->udp_.cancel();
  }
  if (success) {
    this->sed_events_.erase(std::remove_if(this->sed_events_.begin(),this->sed_events_.end(),
        [this](const SedEvent &event){return event.sequence<=this->sed_auto_marker_event_cutoff_;}),this->sed_events_.end());
  }
  this->sed_auto_event_cursor_=0;
  this->sed_auto_retries_left_=this->sed_auto_resend_;
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
    ESP_LOGD("thread_proxy_client", "auto %s ACK in %u ms", token.c_str(), static_cast<unsigned>(elapsed));
    this->sed_auto_ack_trigger_.trigger(token, elapsed, pending);
  } else {
    ESP_LOGW("thread_proxy_client", "auto %s failed: %s", token.c_str(), reason);
    this->sed_auto_error_trigger_.trigger(token, std::string(reason));
  }
}

void ThreadProxyClient::sed_auto_tick() {
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
  if (this->sed_auto_waiting_) {
    if (millis()-this->sed_auto_started_ >= (this->sed_auto_timeout_+5000U)*(this->sed_auto_resend_+1U)+10000U)
      this->sed_auto_finish(false,"delivery_stalled");
    return;
  }
  if (millis()-this->sed_auto_started_ >= 5000U) { this->sed_auto_finish(false,"snapshot_stalled"); return; }
  if (this->sed_batch_done_ || this->sed_sleep_finishing_) return;
  if (this->sed_auto_client_ == nullptr || !this->sed_auto_client_->sed_flush_ready()) return;
  if (!this->udp_.can_flush()) return;
  for (auto &event : this->sed_events_) {
    if (event.sequence<=this->sed_auto_event_cursor_) continue;
    const std::string value=std::to_string(this->sed_boot_)+":"+std::to_string(event.sequence)+":"+
                            std::to_string(event.key)+":"+event.type;
    if (!this->sed_send_to(this->sed_auto_client_,"thread_sed.batch_event",value)) {
      this->sed_auto_finish(false,"event_write_failed"); return;
    }
    this->sed_auto_event_cursor_=event.sequence;
    return;
  }
  if (!this->sed_send_to(this->sed_auto_client_, "thread_sed.batch", this->sed_auto_token_)) {
    this->sed_auto_finish(false, "write_failed"); return;
  }
  this->sed_auto_marker_generation_ = this->sed_auto_generation_;
  this->sed_auto_waiting_ = true;
  this->sed_auto_marker_event_cutoff_=this->sed_auto_event_cursor_;
  this->sed_auto_ack_started_=millis();
}
}
