#include "api_server.h"
#include "esphome/components/network/util.h"
#include "esphome/core/hal.h"
#include "esphome/core/helpers.h"
#include <algorithm>

namespace esphome::thread_proxy_client_nrf52 {
bool ThreadProxyClient::sed_is_ready() const {
  if (!network::is_connected() || !this->udp_.ready() || this->udp_.pending()) return false;
  if (this->sed_sleep_finishing_ || this->shutting_down_ || this->sed_batch_done_) return false;
  for (auto &client : this->active_clients()) {
    if (client->sed_flush_ready()) return true;
  }
  return false;
}

bool ThreadProxyClient::sed_begin_batch(const void *owner, uint32_t timeout, uint32_t resend, const std::vector<EntityBase *> &entities, std::function<void(bool)> done) {
  if (!this->sed_manual_send_ || timeout==0 || timeout>60000 || resend>255) {
    ESP_LOGW("thread_proxy_client_nrf52", "send rejected: invalid mode or ACK policy");
    return false;
  }
  if (this->sed_batch_done_) {
    ESP_LOGW("thread_proxy_client_nrf52", "send rejected: another batch is pending");
    return false;
  }
  if(this->shutting_down_ || this->sed_sleep_finishing_) {
    ESP_LOGW("thread_proxy_client_nrf52","send rejected: shutting down or entering sleep");return false;
  }
  // One virtual UDP serializer exists even before the first BR is found.
  // Accept this action now; discovery/sync is waited for by sed_batch_tick().
  APIConnection *target=this->clients_[0].get();
  if(target==nullptr) return false;
  for (auto *entity : entities) {
    if (entity == nullptr || entity->is_internal()) {
      ESP_LOGW("thread_proxy_client_nrf52", "send rejected: selected entity is internal or unavailable");
      return false;
    }
  }
  this->sed_batch_entities_ = entities;
  this->sed_batch_client_ = target;
  this->sed_batch_owner_ = owner;
  this->sed_batch_started_ = millis();
  this->sed_batch_timeout_ = timeout;
  this->sed_batch_resend_ = resend;
  this->sed_batch_retries_left_ = resend;
  this->sed_batch_delivery_error_ = nullptr;
  this->sed_batch_token_ = std::to_string(random_uint32()) + ":" + std::to_string(++this->sed_batch_sequence_);
  this->sed_batch_snapshot_started_ = false;
  this->sed_batch_cutoff_fixed_=false;this->sed_batch_snapshot_at_=0;
  this->sed_batch_event_cursor_ = 0;
  this->sed_batch_marker_sent_ = false;
  this->sed_batch_acked_ = false;
  this->sed_batch_disconnected_ = false;
  this->sed_batch_done_ = std::move(done);
  this->sed_sleep_ack_ = false;
  this->sed_marker_sent_ = 0;
  this->sed_poll(true);  // Shorten polling before the first DATA/ACK exchange.
  // Event::trigger() runs on_event automations BEFORE ControllerRegistry
  // notifies this API. Capture the batch in sed_batch_tick(), after the current
  // callback stack has returned and the triggering event has been enqueued.
  ESP_LOGV("thread_proxy_client_nrf52", "send %s started", this->sed_batch_token_.c_str());
  return true;
}

bool ThreadProxyClient::sed_batch_wants(EntityBase *entity) const {
  if (this->sed_batch_entities_.empty()) return true;
  return std::find(this->sed_batch_entities_.begin(), this->sed_batch_entities_.end(), entity) != this->sed_batch_entities_.end();
}

void ThreadProxyClient::sed_cancel_batch(const void *owner) {
  if (this->sed_batch_owner_ != owner || !this->sed_batch_done_) return;
  if (this->sed_batch_client_ != nullptr) this->sed_batch_client_->sed_cancel_snapshot();
  this->udp_.cancel();
  this->sed_batch_done_ = nullptr;
  this->sed_batch_client_ = nullptr;
  this->sed_batch_owner_ = nullptr;
  ESP_LOGV("thread_proxy_client_nrf52", "send cancelled");
}

void ThreadProxyClient::sed_finish_batch(bool success, const char *reason) {
  if (!this->sed_batch_done_) return;
  if (!success) {
    if (this->sed_batch_client_ != nullptr) this->sed_batch_client_->sed_cancel_snapshot();
    this->udp_.cancel();
  }
  auto done = std::move(this->sed_batch_done_);
  this->sed_batch_done_ = nullptr;
  this->sed_batch_client_ = nullptr;
  this->sed_batch_owner_ = nullptr;
  if (success) {
    this->sed_events_.erase(
        std::remove_if(this->sed_events_.begin(), this->sed_events_.end(),
                       [this](const SedEvent &event) { return event.sequence <= this->sed_batch_cutoff_ && this->sed_batch_wants(event.entity); }),
        this->sed_events_.end());
    const auto &delivery=this->udp_.last_delivery();
    ESP_LOGD("thread_proxy_client_nrf52", "send ACK: %u ms, %u B, rounds=%u, group=%u",
             static_cast<unsigned>(millis()-this->sed_batch_started_),unsigned(delivery.bytes),
             unsigned(delivery.rounds),unsigned(delivery.group));
  } else {
    ESP_LOGW("thread_proxy_client_nrf52", "send %s failed: %s", this->sed_batch_token_.c_str(), reason);
  }
  done(success);
}

void ThreadProxyClient::sed_batch_tick() {
  if (!this->sed_batch_done_) return;
  if (this->sed_batch_acked_) { this->sed_finish_batch(true, "ack"); return; }
  if (this->sed_batch_disconnected_) { this->sed_finish_batch(false, "disconnected"); return; }
  if (this->sed_batch_delivery_error_ != nullptr) {
    this->sed_finish_batch(false, this->sed_batch_delivery_error_); return;
  }
  if(!this->udp_.ready() || !this->sed_batch_client_->sed_session_ready()) {
    if(!this->udp_.recovering() && millis()-this->sed_batch_started_>=this->discovery_timeout_)
      this->sed_finish_batch(false,"proxy_discovery_timeout");
    return;
  }
  if (!this->sed_batch_snapshot_started_) {

    // A PROBE can start descriptor sync between the action and this deferred
    // snapshot. Wait for it rather than destroying its active iterator.
    if (!this->sed_batch_client_->sed_flush_ready()) return;
    if (!this->udp_.can_flush()) return;
    if(!this->sed_batch_cutoff_fixed_) { this->sed_batch_cutoff_=this->sed_event_sequence_;this->sed_batch_cutoff_fixed_=true; }
    this->sed_batch_snapshot_started_=true;this->sed_batch_snapshot_at_=millis();
    this->sed_batch_client_->sed_begin_snapshot(this->sed_batch_entities_.empty()?nullptr:&this->sed_batch_entities_);
    ESP_LOGV("thread_proxy_client_nrf52","send snapshot: event cutoff=%u",static_cast<unsigned>(this->sed_batch_cutoff_));
    return;
  }
  if (this->sed_batch_marker_sent_) {
    // A separate safety cap covers interrupted schema negotiation/emission;
    // the UDP transport itself implements the configured per-round ACK timer.
    if (millis()-this->sed_batch_started_ >= (this->sed_batch_timeout_+this->discovery_timeout_+5000U)*(this->sed_batch_resend_+1U)+10000U)
      this->sed_finish_batch(false,"delivery_stalled");
    return;
  }
  if (millis()-this->sed_batch_snapshot_at_ >= 5000U) { this->sed_finish_batch(false,"snapshot_stalled"); return; }
  if (!this->sed_batch_client_->sed_flush_ready()) return;
  if (!this->udp_.can_flush()) return;

  // In manual mode, events share the one batch ACK. Keep them in RAM until it
  // arrives; events recorded after the deferred snapshot boundary belong to the next batch.
  if (this->sed_manual_send_) {
    for (auto &event : this->sed_events_) {
      if (event.sequence <= this->sed_batch_event_cursor_ || event.sequence > this->sed_batch_cutoff_ || !this->sed_batch_wants(event.entity)) continue;
      const std::string value = std::to_string(this->sed_boot_) + ":" + std::to_string(event.sequence) + ":" +
                                std::to_string(event.key) + ":" + event.type;
      if (!this->sed_send_to(this->sed_batch_client_, "thread_sed.batch_event", value)) {
        this->sed_finish_batch(false, "event write failed"); return;
      }
      this->sed_batch_event_cursor_ = event.sequence;
      return;  // Bounded work per ESPHome loop.
    }
  }
  if (!this->sed_send_to(this->sed_batch_client_, "thread_sed.batch", this->sed_batch_token_)) {
    this->sed_finish_batch(false, "marker write failed"); return;
  }
  this->sed_batch_marker_sent_ = true;
  this->sed_batch_ack_started_ = millis();
}
}
