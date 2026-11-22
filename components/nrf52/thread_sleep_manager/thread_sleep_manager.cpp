#include "thread_sleep_manager.h"

#include "esphome/core/hal.h"
#include "esphome/core/log.h"

#include <algorithm>
#include <cinttypes>

namespace esphome::thread_sleep_manager {

static const char *const TAG = "thread_sleep_manager";

void ThreadSleepManager::setup() {
  if (deep_sleep_ == nullptr || openthread_ == nullptr) {
    ESP_LOGE(TAG, "deep_sleep_id and openthread_id are required");
    mark_failed();
    return;
  }

  deep_sleep_->add_sleep_guard([this]() { return this->can_sleep(); });
  deep_sleep_->add_sleep_callback([this]() {
    phase_ = Phase::SLEEPING;
    set_indicator_state_(false);
  });
  deep_sleep_->add_wake_callback([this](deep_sleep_nrf52::WakeupCause, int32_t) {
    this->begin_wake_cycle_();
  });
  deep_sleep_->add_abort_callback([this](deep_sleep_nrf52::AbortReason) {
    if (shutdown_)
      return;
    phase_ = Phase::AWAKE;
    cycle_started_ = millis();
    reconnect_started_ = 0;
    reattach_started_ = 0;
    update_indicator_(millis());
  });

#ifdef USE_THREAD_SLEEP_MANAGER_PROXY
  if (proxy_client_ != nullptr) {
    proxy_client_->add_on_activity_callback([this](bool active, bool success) {
      this->notify_activity_(active, success);
    });
  }
#endif
#ifdef USE_THREAD_SLEEP_MANAGER_P2P
  if (p2p_transporter_ != nullptr) {
    p2p_transporter_->add_on_activity_callback([this](bool active, bool success) {
      this->notify_activity_(active, success);
    });
  }
#endif

  begin_wake_cycle_();
  set_indicator_state_(false);
}

void ThreadSleepManager::begin_wake_cycle_() {
  phase_ = Phase::AWAKE;
  cycle_started_ = millis();
  reconnect_started_ = 0;
  reattach_started_ = 0;
  last_activity_at_ = 0;
  success_until_ = 0;
  error_until_ = 0;
  activity_until_ = 0;
  active_operations_ = 0;
  activity_seen_ = false;
  last_link_ready_ = false;
}

bool ThreadSleepManager::joiner_active_() const {
#ifdef USE_OPENTHREAD_JOINER
  return openthread_ != nullptr && openthread_->is_joiner_active();
#else
  return false;
#endif
}

bool ThreadSleepManager::link_ready_() const {
  if (openthread_ == nullptr || !openthread_->is_connected())
    return false;
#ifdef USE_THREAD_SLEEP_MANAGER_PROXY
  if (proxy_client_ != nullptr && !proxy_client_->is_link_ready())
    return false;
#endif
#ifdef USE_THREAD_SLEEP_MANAGER_P2P
  if (p2p_transporter_ != nullptr && !p2p_transporter_->is_ready())
    return false;
#endif
  return true;
}

bool ThreadSleepManager::transport_busy_() const {
#ifdef USE_THREAD_SLEEP_MANAGER_PROXY
  if (proxy_client_ != nullptr && proxy_client_->has_pending_operations())
    return true;
#endif
#ifdef USE_THREAD_SLEEP_MANAGER_P2P
  if (p2p_transporter_ != nullptr && p2p_transporter_->is_busy())
    return true;
#endif
  return false;
}

void ThreadSleepManager::notify_activity_(bool active, bool success) {
  if (shutdown_)
    return;
  const uint32_t now = millis();
  if (active) {
    if (active_operations_ == 0)
      activity_seen_ = true;
    active_operations_++;
    last_activity_at_ = now;
    activity_until_ = now + activity_blink_duration_;
    phase_ = Phase::ACTIVITY;
    return;
  }

  if (active_operations_ > 0)
    active_operations_--;
  last_activity_at_ = now;
  if (success) {
    success_until_ = now + success_duration_;
  } else {
    const uint32_t duration = uint32_t(error_blinks_) * error_blink_interval_ * 2U;
    error_until_ = now + duration;
    mark_link_error_(now);
  }
  if (active_operations_ == 0 && phase_ == Phase::ACTIVITY)
    phase_ = Phase::AWAKE;
}

void ThreadSleepManager::mark_link_error_(uint32_t now) {
  const uint32_t duration = uint32_t(error_blinks_) * error_blink_interval_ * 2U;
  error_until_ = std::max(error_until_, now + duration);
}

bool ThreadSleepManager::any_binary_input_on_() const {
  for (const auto *input : binary_sensor_inputs_) {
    if (input != nullptr && input->has_state() && input->state)
      return true;
  }
  return false;
}

void ThreadSleepManager::set_indicator_state_(bool state) {
  indicator_state_ = state;
  if (indicator_output_ != nullptr)
    indicator_output_->set_state(state);
}

void ThreadSleepManager::update_indicator_(uint32_t now) {
  if (indicator_output_ == nullptr)
    return;

  bool output = false;
  if (any_binary_input_on_()) {
    output = true;
  } else if (joiner_active_()) {
    const uint32_t period = std::max<uint32_t>(joiner_blink_interval_, 1);
    output = ((now / period) & 1U) == 0;
  } else if (now < error_until_) {
    const uint32_t period = std::max<uint32_t>(error_blink_interval_, 1);
    const uint32_t elapsed = error_until_ - now;
    const uint32_t total = uint32_t(error_blinks_) * period * 2U;
    const uint32_t passed = total > elapsed ? total - elapsed : 0;
    output = ((passed / period) & 1U) == 0;
  } else if (active_operations_ > 0 || now < activity_until_) {
    output = now < activity_until_;
  } else if (now < success_until_) {
    output = true;
  }

  if (output != indicator_state_)
    set_indicator_state_(output);
}

void ThreadSleepManager::update_state_machine_(uint32_t now) {
  const bool joiner = joiner_active_();
  const bool link = link_ready_();
  if (link && !last_link_ready_ && !shutdown_)
    phase_ = active_operations_ > 0 ? Phase::ACTIVITY : Phase::AWAKE;
  if (!link && last_link_ready_)
    mark_link_error_(now);
  last_link_ready_ = link;

  if (joiner) {
    // A manual joiner owns the wake window. Do not count reconnect failures while
    // commissioning, and restart the grace period after it ends.
    reconnect_started_ = now;
    reattach_started_ = now;
  }

  if (active_operations_ > 0) {
    phase_ = Phase::ACTIVITY;
  } else if (!joiner && !link) {
    if (reconnect_started_ == 0)
      reconnect_started_ = now;
    if (phase_ == Phase::AWAKE || phase_ == Phase::ACTIVITY)
      phase_ = Phase::WAIT_RECONNECT;
    if (phase_ == Phase::WAIT_RECONNECT &&
        uint32_t(now - reconnect_started_) >= reconnect_after_) {
      ESP_LOGW(TAG, "Thread/transport unavailable for %" PRIu32 " ms; reattaching",
               reconnect_after_);
      reattach_started_ = now;
      phase_ = Phase::REATTACHING;
      if (!openthread_->reattach())
        ESP_LOGW(TAG, "reattach request was rejected");
    }
    if (phase_ == Phase::REATTACHING &&
        uint32_t(now - reattach_started_) >= reconnect_timeout_) {
      ESP_LOGW(TAG, "Reconnect deadline expired; allowing System ON sleep");
      phase_ = Phase::READY;
    }
  } else if (!joiner && link) {
    reconnect_started_ = 0;
    reattach_started_ = 0;
    if (phase_ == Phase::WAIT_RECONNECT || phase_ == Phase::REATTACHING)
      phase_ = active_operations_ > 0 ? Phase::ACTIVITY : Phase::AWAKE;
  }

  if (phase_ == Phase::AWAKE && active_operations_ == 0 &&
      activity_seen_ && uint32_t(now - last_activity_at_) >= quiet_delay_ &&
      now >= success_until_ && now >= error_until_) {
    phase_ = Phase::READY;
  }

  if (phase_ != Phase::READY && phase_ != Phase::SLEEPING &&
      uint32_t(now - cycle_started_) >= wake_timeout_) {
    ESP_LOGW(TAG, "Wake cycle deadline expired");
    phase_ = Phase::READY;
  }
}

bool ThreadSleepManager::can_sleep() {
  if (shutdown_ || phase_ != Phase::READY || active_operations_ > 0 ||
      joiner_active_() || transport_busy_())
    return false;
  const uint32_t now = millis();
  return now >= success_until_ && now >= error_until_;
}

void ThreadSleepManager::loop() {
  if (shutdown_ || is_failed())
    return;
  const uint32_t now = millis();
  if (phase_ != Phase::SLEEPING)
    update_state_machine_(now);
  update_indicator_(now);
  if (phase_ == Phase::READY && can_sleep())
    deep_sleep_->request_sleep();
}

void ThreadSleepManager::on_shutdown() {
  shutdown_ = true;
  set_indicator_state_(false);
}

const char *ThreadSleepManager::phase_name_() const {
  switch (phase_) {
    case Phase::AWAKE:
      return "AWAKE";
    case Phase::ACTIVITY:
      return "ACTIVITY";
    case Phase::WAIT_RECONNECT:
      return "WAIT_RECONNECT";
    case Phase::REATTACHING:
      return "REATTACHING";
    case Phase::READY:
      return "READY";
    case Phase::SLEEPING:
      return "SLEEPING";
  }
  return "UNKNOWN";
}

void ThreadSleepManager::dump_config() {
  ESP_LOGCONFIG(TAG, "Thread sleep manager:");
  ESP_LOGCONFIG(TAG, "  Phase: %s", phase_name_());
  ESP_LOGCONFIG(TAG, "  Wake timeout: %" PRIu32 " ms", wake_timeout_);
  ESP_LOGCONFIG(TAG, "  Reconnect after: %" PRIu32 " ms", reconnect_after_);
  ESP_LOGCONFIG(TAG, "  Reconnect timeout: %" PRIu32 " ms", reconnect_timeout_);
  ESP_LOGCONFIG(TAG, "  Binary inputs: %u", unsigned(binary_sensor_inputs_.size()));
  ESP_LOGCONFIG(TAG, "  Indicator output: %s", YESNO(indicator_output_ != nullptr));
#ifdef USE_THREAD_SLEEP_MANAGER_PROXY
  ESP_LOGCONFIG(TAG, "  Proxy client: %s", YESNO(proxy_client_ != nullptr));
#endif
#ifdef USE_THREAD_SLEEP_MANAGER_P2P
  ESP_LOGCONFIG(TAG, "  P2P transporter: %s", YESNO(p2p_transporter_ != nullptr));
#endif
}

}  // namespace esphome::thread_sleep_manager
