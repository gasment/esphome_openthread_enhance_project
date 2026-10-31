#pragma once
#include "esphome/core/defines.h"
#ifdef USE_OPENTHREAD_NRF52

#include "esphome/components/mdns/mdns_component.h"
#include "esphome/components/network/ip_address.h"
#include "esphome/core/component.h"

#include <openthread/srp_client.h>
#include <openthread/srp_client_buffers.h>
#include <openthread/instance.h>
#include <openthread/thread.h>
#include <openthread/link.h>
#include <openthread/ip6.h>
#include <openthread/udp.h>
#include <openthread/dataset.h>
#include <zephyr/net/openthread.h>

#include "joiner.h"
#include <atomic>
#include <functional>
#include <memory>
#include <optional>
#include <string>
#include <vector>

namespace esphome::openthread_nrf52 {

class InstanceLock;

enum class TeardownStage : uint8_t {
  TEARDOWN_STAGE_NOT_STARTED = 0,
  TEARDOWN_STAGE_STOP_IN_PROCESS,
  TEARDOWN_STAGE_COMPLETED,
};

template<typename... Ts> class OpenThreadComponentPollPeriodAction;

class OpenThreadComponent final : public Component {
 public:
  OpenThreadComponent();
  ~OpenThreadComponent() = default;
  void dump_config() override;
  void setup() override;
  void loop() override;
  void service_remote_restart_();
  static void receive_remote_restart_(void *, otMessage *, const otMessageInfo *);
  bool teardown() override;
  float get_setup_priority() const override { return setup_priority::WIFI; }

  bool is_connected() const { return this->connected_; }
  /// Returns true once the Zephyr context and OT lock are available.
  bool is_lock_initialized() const { return this->lock_initialized_; }
  network::IPAddresses get_ip_addresses();
  std::optional<otIp6Address> get_omr_address();
  // Reuse an already-owned lock for diagnostics; never acquire the OT lock twice.
  std::optional<otIp6Address> get_omr_address(InstanceLock &lock);
  void ot_main();
  void on_factory_reset(std::function<void()> callback);
  void defer_factory_reset_external_callback();

  /// Returns nullptr when no explicit use_address is configured and the address is
  /// derived at runtime from the device name (see network::get_use_address_to()).
  const char *get_use_address() const { return this->use_address_; }
  void set_use_address(const char *use_address) { this->use_address_ = use_address; }
#if CONFIG_OPENTHREAD_MTD
  bool set_poll_period(uint32_t poll_period);
  uint32_t get_poll_period() const { return this->poll_period_; }
  void set_child_timeout(uint32_t seconds) { this->child_timeout_ = seconds; }
  // Zero check timeout selects automatic adaptation; never passed to OT.
  void set_child_supervision(uint16_t interval, uint16_t check_timeout) {
    this->supervision_interval_ = interval;
    this->supervision_timeout_ = check_timeout;
  }
#endif
  void set_output_power(int8_t output_power) { this->output_power_ = output_power; }
  void set_connected(bool connected) { this->connected_ = connected; }
  /// Queue one SED Data Poll. True means queued, not confirmed by the parent.
  bool keep_alive();

#ifdef USE_OPENTHREAD_JOINER
  void set_joiner_pskd(const std::string &pskd) { joiner_.set_pskd(pskd); }
  void set_joiner_name(const std::string &name) { joiner_.set_name(name); }
  void start_joiner(const std::string &pskd, uint32_t timeout, bool override_pskd) { joiner_.start(pskd, timeout, override_pskd); }
  void cancel_joiner() { joiner_.cancel(); }
  bool is_joiner_active() const { return joiner_.active(); }
  void add_joiner_start_trigger(JoinerTrigger *t) { joiner_.add_start(t); }
  void add_joiner_success_trigger(JoinerTrigger *t) { joiner_.add_success(t); }
  void add_joiner_cancel_trigger(JoinerTrigger *t) { joiner_.add_cancel(t); }
  void add_joiner_fail_trigger(JoinerFailTrigger *t) { joiner_.add_fail(t); }
#endif
  static void on_state_changed(otChangedFlags flags, void *context);

 protected:
  // Actions re-apply link mode under the OT lock; allow them to call apply_linkmode_()
  // without exposing this lock-sensitive, raw-instance method on the public API.
  template<typename... Ts> friend class OpenThreadComponentPollPeriodAction;

  /** Apply Link Mode settings (incl poll period).
   * Callers running outside the OpenThread task must hold InstanceLock.
   * ot_main() runs on the OpenThread task itself and must not acquire the lock.
   */
  bool apply_linkmode_(otInstance *instance);
#if CONFIG_OPENTHREAD_MTD
  bool supervision_timeout_for_poll_(uint32_t period, uint16_t &timeout) const;
  void log_supervision_failures_(otInstance *instance);
#endif
  void log_network_diagnostics_(otInstance *instance);
  void sync_ip6_addresses_();

#ifdef USE_OPENTHREAD_JOINER
  JoinerManager joiner_;
  void loop_joiner_();
#endif
  otInstance *get_openthread_instance_();
  int openthread_stop_();
  std::function<void()> factory_reset_external_callback_;
#if CONFIG_OPENTHREAD_MTD
  uint32_t poll_period_{0};
  std::optional<uint32_t> child_timeout_{};
  uint16_t supervision_interval_{129}, supervision_timeout_{0}, supervision_failures_{0};
#endif
  std::optional<int8_t> output_power_{};
  std::atomic<bool> lock_initialized_{false};
  std::atomic<TeardownStage> teardown_stage_{TeardownStage::TEARDOWN_STAGE_NOT_STARTED};
  std::atomic<bool> connected_{false};
  otUdpSocket restart_socket_{};
  bool restart_socket_open_{false};
  struct RestartRequest {
    uint8_t bytes[21]{};
    otIp6Address peer{};
    uint16_t port{0};
  } restart_request_{};
  std::atomic<bool> restart_request_pending_{false};
  std::atomic<bool> restart_scheduled_{false};
  uint32_t restart_due_{0};
  std::atomic<bool> network_diagnostics_pending_{true};
  std::atomic<uint32_t> ip6_generation_{1};
  uint32_t ip6_synced_generation_{0}, ip6_sync_at_{0}, ip6_warning_signature_{0};
  bool ip6_sync_retry_{true}, ip6_warned_{false}, ip6_sync_logged_{false};
  struct openthread_state_changed_cb state_callback_{};
  bool callback_registered_{false};

 private:
  // Stores a pointer to a string literal (static storage duration).
  // ONLY set from Python-generated code with string literals - never dynamic strings.
  const char *use_address_{nullptr};
};

extern OpenThreadComponent *global_openthread_component;  // NOLINT(cppcoreguidelines-avoid-non-const-global-variables)

class OpenThreadSrpComponent final : public Component {
 public:
  void set_mdns(esphome::mdns::MDNSComponent *mdns);
  // This has to run after the mdns component or else no services are available to advertise
  float get_setup_priority() const override { return this->mdns_->get_setup_priority() - 1.0f; }
  void setup() override;
  static void srp_callback(otError err, const otSrpClientHostInfo *host_info, const otSrpClientService *services,
                           const otSrpClientService *removed_services, void *context);
  static void srp_start_callback(const otSockAddr *server_socket_address, void *context);
  static void srp_factory_reset_callback(otError err, const otSrpClientHostInfo *host_info,
                                         const otSrpClientService *services, const otSrpClientService *removed_services,
                                         void *context);

 protected:
  esphome::mdns::MDNSComponent *mdns_{nullptr};
  std::vector<std::unique_ptr<uint8_t[]>> memory_pool_;
  void *pool_alloc_(size_t size);
};

// RAII guard for the OpenThread API lock. Modeled on std::unique_lock: the
// guard may or may not own the lock (try_acquire can fail), so check it with
// operator bool before use. Non-copyable and non-movable: the factories return
// by value via guaranteed copy elision, so a guard is never duplicated and the
// lock is released exactly once, when the owning guard goes out of scope.
class InstanceLock {
 public:
  // May fail to acquire within delay ms; check the returned guard with operator bool.
  static InstanceLock try_acquire(int delay);
  // Blocks until the lock is held.
  static InstanceLock acquire();
  InstanceLock(const InstanceLock &) = delete;
  InstanceLock(InstanceLock &&) = delete;
  InstanceLock &operator=(const InstanceLock &) = delete;
  InstanceLock &operator=(InstanceLock &&) = delete;
  ~InstanceLock();

  explicit operator bool() const { return this->owns_; }

  // Returns the global openthread instance. Only valid on an owning guard
  // (operator bool is true); the instance must not be used without the lock held.
  otInstance *get_instance();

 private:
  explicit InstanceLock(bool owns) : owns_(owns) {}
  bool owns_;
};

}  // namespace esphome::openthread_nrf52
#endif

#include "joiner_automation.h"
