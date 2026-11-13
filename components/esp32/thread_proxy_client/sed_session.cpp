#include "api_server.h"
#include "esphome/core/application.h"
#include "esphome/core/helpers.h"
#ifdef USE_THREAD_PROXY_CLIENT_NRF52
#include "esphome/components/openthread_nrf52/openthread_nrf52.h"
namespace sed_ot = esphome::openthread_nrf52;
#else
#include "esphome/components/openthread/openthread.h"
namespace sed_ot = esphome::openthread;
#endif
#include <openthread/link.h>
#include <cstring>
namespace esphome::thread_proxy_client {
bool ThreadProxyClient::sed_send_to(APIConnection *client, const char *service, const std::string &value) {
  HomeassistantActionRequest call;
  call.service = StringRef(service);
  call.data.init(1);
  HomeassistantServiceMap item;
  item.key = StringRef::from_lit("token");
  item.value = StringRef(value);  // Encoded synchronously before value expires.
  call.data.push_back(item);
  if (!client->sed_flush_ready()) return false;
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
  const bool queued = client->send_homeassistant_action(call);
  if (std::strcmp(service, "thread_sed.event") == 0) this->sed_auto_published(client, queued);
  return queued;
}
bool ThreadProxyClient::sed_send(const char *service, const std::string &value) {
  bool sent = false;
  for (auto &client : this->active_clients()) sent |= this->sed_send_to(client.get(), service, value);
  return sent;
}
void ThreadProxyClient::sed_control(APIConnection *client, StringRef value) {
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
bool ThreadProxyClient::sed_sleep_guard() {
  if (this->sed_batch_done_ || this->sed_auto_pending() || this->sed_auto_callbacks_running()) return false;
  if (!this->sed_sleep_pending_) {
    this->sed_sleep_pending_=true;
    this->sed_sleep_started_=millis();
  }
  if (this->udp_.pending()) {
    if (millis()-this->sed_sleep_started_<this->sed_sleep_budget_) return false;
    ESP_LOGW("thread_proxy_client", "UDP sleep deadline expired; queued events remain for next wake");
    this->udp_.cancel();
  }
  this->sed_poll(false);
  if (this->sed_fast_poll_) return false;
  if (!this->sed_sleep_finishing_) this->udp_.sleep();
  this->sed_sleep_finishing_=true;
  return true;
}

void ThreadProxyClient::sed_wake() {
  this->sed_sleep_pending_ = false; this->sed_sleep_ack_ = false; this->sed_sleep_finishing_ = false;
  this->sed_poll(true);
  this->udp_.wake();
}
uint32_t ThreadProxyClient::sed_sleep_seconds() const {
#ifdef USE_THREAD_PROXY_CLIENT_SLEEP
  auto *sleep = static_cast<deep_sleep_nrf52::DeepSleepComponent *>(this->sed_sleep_);
  if (sleep != nullptr) return sleep->get_sleep_duration().value_or(0) / 1000;
#endif
  return this->sed_expected_sleep_seconds_;
}
void ThreadProxyClient::sed_poll(bool fast) {
  auto *ot = sed_ot::global_openthread_component;
  if (ot == nullptr || !ot->is_lock_initialized()) return;
  auto lock = sed_ot::InstanceLock::try_acquire(0);
  if (!lock) return;
  auto *instance = lock.get_instance();
  if (instance == nullptr) return;
  if (otThreadGetLinkMode(instance).mRxOnWhenIdle) { this->sed_fast_poll_=false; return; }
  const uint32_t current=otLinkGetPollPeriod(instance);
  if (fast) {
    if (!this->sed_fast_poll_) this->sed_saved_poll_=current;
    uint32_t interval=this->sed_saved_poll_;
    if (interval==0 || interval>THREAD_PROXY_CLIENT_FAST_POLL_MS) interval=THREAD_PROXY_CLIENT_FAST_POLL_MS;
    if (current!=interval) {
      if (otLinkSetPollPeriod(instance,interval)!=OT_ERROR_NONE) return;
      ESP_LOGV("thread_proxy_client","Fast poll: %u -> %u ms",static_cast<unsigned>(current),static_cast<unsigned>(interval));
    }
  } else {
    if (!this->sed_fast_poll_) return;
    if (otLinkSetPollPeriod(instance,this->sed_saved_poll_)!=OT_ERROR_NONE) return;
    ESP_LOGV("thread_proxy_client","Restore poll: %u ms",static_cast<unsigned>(this->sed_saved_poll_));
  }
  this->sed_fast_poll_=fast;
}
void ThreadProxyClient::sed_queue_event(EntityBase *entity, StringRef type) {
  if (this->sed_boot_ == 0) this->sed_boot_ = random_uint32();
  if (this->sed_events_.size() >= 32) {
    ESP_LOGW("thread_proxy_client", "Event queue full; newest event dropped"); return;
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
void ThreadProxyClient::sed_tick() {
  this->sed_auto_tick();
  this->sed_batch_tick();
#ifdef USE_THREAD_PROXY_CLIENT_SLEEP
  auto *sleep_check = static_cast<deep_sleep_nrf52::DeepSleepComponent *>(this->sed_sleep_);
  if (sleep_check != nullptr && !sleep_check->is_pending() && !sleep_check->is_sleeping() && this->sed_sleep_pending_) this->sed_wake();
#endif
  if (this->sed_sleep_finishing_) { this->sed_poll(false); return; }
  this->sed_poll(!this->sed_sleep_pending_ || !this->sed_sleep_ack_);
#ifdef USE_THREAD_PROXY_CLIENT_SLEEP
  auto *sleep = static_cast<deep_sleep_nrf52::DeepSleepComponent *>(this->sed_sleep_);
  if (sleep != nullptr && !sleep->is_pending() && this->sed_sleep_pending_) this->sed_wake();
#endif
}
}
