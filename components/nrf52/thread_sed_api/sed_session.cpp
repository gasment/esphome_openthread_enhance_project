#include "api_server.h"
#include "esphome/core/application.h"
#include "esphome/core/helpers.h"
#ifdef USE_THREAD_SED_NRF52
#include "esphome/components/openthread_nrf52/openthread_nrf52.h"
namespace sed_ot = esphome::openthread_nrf52;
#else
#include "esphome/components/openthread/openthread.h"
namespace sed_ot = esphome::openthread;
#endif
#include <openthread/link.h>
#include <cstring>
namespace esphome::thread_sed_api {
bool APIServer::sed_send_to(APIConnection *client, const char *service, const std::string &value) {
  HomeassistantActionRequest call;
  call.service = StringRef(service);
  call.data.init(1);
  HomeassistantServiceMap item;
  item.key = StringRef::from_lit("token");
  item.value = StringRef(value);  // Encoded synchronously before value expires.
  call.data.push_back(item);
  if (!client->sed_flush_ready()) return false;
#ifdef USE_THREAD_SED_API_UDP
  if (!this->udp_.can_flush() && std::strcmp(service, "thread_sed.batch_event") != 0) return false;
  if (std::strcmp(service, "thread_sed.batch") == 0) {
    const bool manual=this->sed_batch_done_ && client==this->sed_batch_client_;
    return client->send_homeassistant_action(call) && this->udp_.flush("batch:" + value,
        manual?this->sed_batch_timeout_:this->sed_auto_timeout_,
        manual?this->sed_batch_resend_:this->sed_auto_resend_);
  }
  if (std::strcmp(service, "thread_sed.event") == 0) {
    const bool queued=client->send_homeassistant_action(call) && this->udp_.flush("event:" + value.substr(0, value.find(':', value.find(':')+1)));
    this->sed_auto_published(client,queued);
    return queued;
  }
#endif
  const bool queued = client->send_homeassistant_action(call);
  if (std::strcmp(service, "thread_sed.event") == 0) this->sed_auto_published(client, queued);
  return queued;
}
bool APIServer::sed_send(const char *service, const std::string &value) {
  bool sent = false;
  for (auto &client : this->active_clients()) sent |= this->sed_send_to(client.get(), service, value);
  return sent;
}
void APIServer::sed_control(APIConnection *client, StringRef value) {
  if (value == "ready") {
    if (client->is_authenticated() && client->flags_.state_subscription && client->flags_.service_call_subscription)
      client->sed_ready_ = true;
    return;
  }
  if (!client->sed_session_ready()) return;
  if (this->sed_auto_pending() && this->sed_auto_waiting_ && !this->sed_auto_disconnected_ &&
      client == this->sed_auto_client_ && value == "batch:" + this->sed_auto_token_)
    this->sed_auto_acked_ = true;
  if (this->sed_batch_done_ && !this->sed_batch_disconnected_ && client == this->sed_batch_client_ &&
      this->sed_batch_marker_sent_ && value == "batch:" + this->sed_batch_token_)
    this->sed_batch_acked_ = true;
  if (value == std::to_string(this->sed_token_)) this->sed_sleep_ack_ = true;
  if (!this->sed_manual_send_ && !this->sed_events_.empty()) {
    const std::string ack = "event:" + std::to_string(this->sed_boot_) + ":" + std::to_string(this->sed_events_.front().sequence);
    if (value == ack) { this->sed_events_.erase(this->sed_events_.begin()); this->sed_event_sent_ = 0; }
  }
}
bool APIServer::sed_sleep_guard() {
#ifdef USE_THREAD_SED_API_UDP
  if (this->sed_batch_done_ || this->sed_auto_pending() || this->sed_auto_callbacks_running()) return false;
  if (!this->sed_sleep_pending_) {
    this->sed_sleep_pending_=true;
    this->sed_sleep_started_=millis();
  }
  if (this->udp_.pending()) {
    if (millis()-this->sed_sleep_started_<this->sed_sleep_budget_) return false;
    ESP_LOGW("thread_sed_api", "UDP sleep deadline expired; queued events remain for next wake");
    this->udp_.cancel();
  }
  this->sed_poll(false);
  if (this->sed_fast_poll_) return false;
  if (!this->sed_sleep_finishing_) this->udp_.sleep();
  this->sed_sleep_finishing_=true;
  return true;
#endif
  if (this->sed_batch_done_ || this->sed_auto_callbacks_running()) return false;
  const uint32_t now = millis();
  if (!this->sed_sleep_pending_) {
    this->sed_sleep_pending_ = true; this->sed_sleep_ack_ = false;
    this->sed_sleep_started_ = now; this->sed_token_ = random_uint32(); this->sed_marker_sent_ = 0;
  }
  if (!this->sed_sleep_ack_ && now - this->sed_sleep_started_ < this->sed_sleep_budget_) return false;
  if (!this->sed_sleep_ack_) ESP_LOGW("thread_sed_api", "Sleep delivery deadline expired; latest states will resync on wake");
  this->sed_poll(false);
  if (this->sed_fast_poll_) return false;
  if (this->sed_auto_pending()) {
    this->sed_auto_finish(false, "sleep_deadline");
    if (this->sed_auto_callbacks_running()) return false;
  }
  this->sed_sleep_finishing_ = true;
  for (auto &client : this->active_clients()) client->on_fatal_error();
  return true;
}
void APIServer::sed_wake() {
  this->sed_sleep_pending_ = false; this->sed_sleep_ack_ = false; this->sed_sleep_finishing_ = false;
#ifdef USE_THREAD_SED_API_UDP
  this->udp_.wake();
#else
  this->outgoing_conn_.wake();
#endif
}
uint32_t APIServer::sed_sleep_seconds() const {
#ifdef USE_THREAD_SED_SLEEP
  auto *sleep = static_cast<deep_sleep_nrf52::DeepSleepComponent *>(this->sed_sleep_);
  if (sleep != nullptr) return sleep->get_sleep_duration().value_or(0) / 1000;
#endif
  return this->sed_expected_sleep_seconds_;
}
void APIServer::sed_poll(bool fast) {
  auto *ot = sed_ot::global_openthread_component;
  if (ot == nullptr || !ot->is_lock_initialized()) return;
  auto lock = sed_ot::InstanceLock::try_acquire(0);
  if (!lock) return;
  auto *instance = lock.get_instance();
  if (instance == nullptr || otThreadGetLinkMode(instance).mRxOnWhenIdle) return;
  if (fast == this->sed_fast_poll_) return;
  if (fast) {
    this->sed_saved_poll_ = otLinkGetPollPeriod(instance);
    // Only shorten polling, never turn SED into MED.
    uint32_t interval = this->sed_saved_poll_;
    if (interval == 0 || interval > THREAD_SED_FAST_POLL_MS) interval = THREAD_SED_FAST_POLL_MS;
    if (otLinkSetPollPeriod(instance, interval) != OT_ERROR_NONE) return;
  } else if (otLinkSetPollPeriod(instance, this->sed_saved_poll_) != OT_ERROR_NONE) return;
  this->sed_fast_poll_ = fast;
}
void APIServer::sed_queue_event(EntityBase *entity, StringRef type) {
  if (this->sed_boot_ == 0) this->sed_boot_ = random_uint32();
  if (this->sed_events_.size() >= 32) {
    ESP_LOGW("thread_sed_api", "Event queue full; newest event dropped"); return;
  }
  this->sed_events_.push_back({++this->sed_event_sequence_, entity->get_object_id_hash(), std::string(type.c_str(), type.size()), entity});
  if (!this->sed_manual_send_) {
    for (auto &client : this->active_clients()) {
      if (client->sed_session_ready()) { this->sed_auto_published(client.get()); break; }
    }
  }
  // A newly captured event invalidates an outstanding sleep marker.
  if (this->sed_sleep_pending_) { this->sed_sleep_ack_ = false; this->sed_token_ = random_uint32(); this->sed_marker_sent_ = 0; }
}
void APIServer::sed_tick() {
  this->sed_auto_tick();
  this->sed_batch_tick();
#ifdef USE_THREAD_SED_SLEEP
  auto *sleep_check = static_cast<deep_sleep_nrf52::DeepSleepComponent *>(this->sed_sleep_);
  if (sleep_check != nullptr && !sleep_check->is_pending() && !sleep_check->is_sleeping() && this->sed_sleep_pending_) this->sed_wake();
#endif
  if (this->sed_sleep_finishing_) { this->sed_poll(false); return; }
  this->sed_poll(!this->sed_sleep_pending_ || !this->sed_sleep_ack_);
#ifdef USE_THREAD_SED_SLEEP
  auto *sleep = static_cast<deep_sleep_nrf52::DeepSleepComponent *>(this->sed_sleep_);
  if (sleep != nullptr && !sleep->is_pending() && this->sed_sleep_pending_) this->sed_wake();
#endif
#ifndef USE_THREAD_SED_API_UDP
  if (this->sed_sleep_pending_ && !this->sed_sleep_ack_ && !this->sed_auto_pending() && (this->sed_manual_send_ || this->sed_events_.empty()) &&
      (this->sed_marker_sent_ == 0 || millis() - this->sed_marker_sent_ >= 500)) {
    if (this->sed_send("thread_sed.sleep", std::to_string(this->sed_token_))) this->sed_marker_sent_ = millis();
  }
#endif
}
}
