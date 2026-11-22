#pragma once

#include "esphome/components/binary_sensor/binary_sensor.h"
#include "esphome/components/deep_sleep_nrf52/deep_sleep_nrf52.h"
#include "esphome/components/openthread_nrf52/openthread_nrf52.h"
#include "esphome/components/output/binary_output.h"
#include "esphome/core/automation.h"
#include "esphome/core/component.h"

#include <vector>

#ifdef USE_THREAD_SLEEP_MANAGER_PROXY
#include "esphome/components/thread_proxy_client_nrf52/api_server.h"
#endif
#ifdef USE_THREAD_SLEEP_MANAGER_P2P
#include "esphome/components/thread_p2p_transporter_nrf52/thread_p2p_transporter_nrf52.h"
#endif

namespace esphome::thread_sleep_manager {

enum class StatusFlag : uint8_t {
  PHASE_AWAKE,
  PHASE_ACTIVITY,
  PHASE_WAIT_RECONNECT,
  PHASE_REATTACHING,
  PHASE_READY,
  PHASE_SLEEPING,
  LINK_READY,
  TRANSPORT_BUSY,
  JOINER_ACTIVE,
  CAN_SLEEP,
};

class ThreadSleepManager final : public Component {
 public:
  enum class Phase : uint8_t {
    AWAKE,
    ACTIVITY,
    WAIT_RECONNECT,
    REATTACHING,
    READY,
    SLEEPING,
  };

  void setup() override;
  void loop() override;
  void dump_config() override;
  void on_shutdown() override;
  float get_setup_priority() const override { return setup_priority::LATE; }

  void set_deep_sleep(deep_sleep_nrf52::DeepSleepComponent *value) { deep_sleep_ = value; }
  void set_openthread(openthread_nrf52::OpenThreadComponent *value) { openthread_ = value; }
#ifdef USE_THREAD_SLEEP_MANAGER_PROXY
  void set_proxy_client(thread_proxy_client_nrf52::ThreadProxyClient *value) { proxy_client_ = value; }
#endif
#ifdef USE_THREAD_SLEEP_MANAGER_P2P
  void set_p2p_transporter(thread_p2p_transporter_nrf52::ThreadP2PTransporter *value) {
    p2p_transporter_ = value;
  }
#endif
  void add_binary_sensor_input(binary_sensor::BinarySensor *value) {
    binary_sensor_inputs_.push_back(value);
  }
  void set_indicator_output(output::BinaryOutput *value) { indicator_output_ = value; }
  void set_timing(uint32_t wake_timeout, uint32_t reconnect_after, uint32_t reconnect_timeout,
                  uint32_t quiet_delay, uint32_t activity_blink_duration, uint32_t success_duration,
                  uint8_t error_blinks, uint32_t error_blink_interval,
                  uint32_t joiner_blink_interval) {
    wake_timeout_ = wake_timeout;
    reconnect_after_ = reconnect_after;
    reconnect_timeout_ = reconnect_timeout;
    quiet_delay_ = quiet_delay;
    activity_blink_duration_ = activity_blink_duration;
    success_duration_ = success_duration;
    error_blinks_ = error_blinks;
    error_blink_interval_ = error_blink_interval;
    joiner_blink_interval_ = joiner_blink_interval;
  }

  bool can_sleep();
  bool check_status(StatusFlag flag);
  Phase get_phase() const { return phase_; }
  uint32_t get_active_operations() const { return active_operations_; }

 protected:
  void notify_activity_(bool active, bool success);
  bool link_ready_() const;
  bool any_binary_input_on_() const;
  bool joiner_active_() const;
  bool transport_busy_() const;
  void update_state_machine_(uint32_t now);
  void update_indicator_(uint32_t now);
  void set_indicator_state_(bool state);
  void begin_wake_cycle_();
  void mark_link_error_(uint32_t now);
  const char *phase_name_() const;

  deep_sleep_nrf52::DeepSleepComponent *deep_sleep_{nullptr};
  openthread_nrf52::OpenThreadComponent *openthread_{nullptr};
#ifdef USE_THREAD_SLEEP_MANAGER_PROXY
  thread_proxy_client_nrf52::ThreadProxyClient *proxy_client_{nullptr};
#endif
#ifdef USE_THREAD_SLEEP_MANAGER_P2P
  thread_p2p_transporter_nrf52::ThreadP2PTransporter *p2p_transporter_{nullptr};
#endif
  std::vector<binary_sensor::BinarySensor *> binary_sensor_inputs_;
  output::BinaryOutput *indicator_output_{nullptr};

  Phase phase_{Phase::AWAKE};
  uint32_t active_operations_{0};
  uint32_t cycle_started_{0};
  uint32_t reconnect_started_{0};
  uint32_t reattach_started_{0};
  uint32_t last_activity_at_{0};
  uint32_t success_until_{0};
  uint32_t error_until_{0};
  uint32_t activity_until_{0};

  uint32_t wake_timeout_{45000};
  uint32_t reconnect_after_{10000};
  uint32_t reconnect_timeout_{20000};
  uint32_t quiet_delay_{200};
  uint32_t activity_blink_duration_{120};
  uint32_t success_duration_{500};
  uint8_t error_blinks_{3};
  uint32_t error_blink_interval_{200};
  uint32_t joiner_blink_interval_{500};

  bool activity_seen_{false};
  bool last_link_ready_{false};
  bool indicator_state_{false};
  bool shutdown_{false};
};

template<typename... Ts> class StatusCondition : public Condition<Ts...> {
 public:
  explicit StatusCondition(ThreadSleepManager *parent) : parent_(parent) {}
  void set_flag(StatusFlag flag) { flag_ = flag; }
  bool check(const Ts &...x) override { return parent_->check_status(flag_); }

 protected:
  ThreadSleepManager *parent_;
  StatusFlag flag_{StatusFlag::PHASE_AWAKE};
};

}  // namespace esphome::thread_sleep_manager
