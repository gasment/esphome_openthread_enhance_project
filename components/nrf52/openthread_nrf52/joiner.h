#pragma once
#include "esphome/core/defines.h"
#ifdef USE_OPENTHREAD_JOINER
#include "esphome/core/automation.h"
#include <openthread/dataset.h>
#include <openthread/joiner.h>
#include <atomic>
#include <string>
#include <vector>

namespace esphome::openthread_nrf52 {
using JoinerTrigger = Trigger<>;
using JoinerFailTrigger = Trigger<std::string>;

// All methods except native_done_ run on the application thread, or once during
// OT initialization before boot_ready_. Native callbacks only publish a result.
class JoinerManager {
 public:
  void set_pskd(const std::string &pskd) { default_pskd_ = pskd; }
  void set_name(const std::string &name) { name_ = name; }
  void start(const std::string &pskd, uint32_t timeout, bool override_pskd);
  void cancel() { if (busy_) cancel_ = true; }
  bool active() const { return busy_; }
  bool boot_locked(otInstance *instance);
  void finish_boot() { boot_ready_ = true; }
  void loop_locked(otInstance *instance);
  void dispatch();  // Never called with the OT lock held.
  void add_start(JoinerTrigger *t) { start_.push_back(t); }
  void add_success(JoinerTrigger *t) { success_.push_back(t); }
  void add_cancel(JoinerTrigger *t) { cancelled_.push_back(t); }
  void add_fail(JoinerFailTrigger *t) { fail_triggers_.push_back(t); }

 protected:
  enum class Phase : uint8_t { IDLE, QUEUED, JOINING, RETRY, ATTACHING, RESTORING, COMMITTING };
  // Platform-isolated storage, immutable until transaction commit/rollback.
  // Write and read back the full record before disconnecting the old network.
  struct Journal {
    uint32_t magic;
    otOperationalDatasetTlvs active, pending;
    otExtAddress address;
    uint8_t thread_enabled;
    uint32_t checksum;
  } journal_{};
  struct Notice { uint8_t event; std::string reason; };
  static void native_done_(otError error, void *context);
  static uint32_t checksum_(const Journal &journal);
  bool save_backup_(otInstance *instance);
  bool restore_(otInstance *instance, bool restart = true);
  bool remove_backup_(otInstance *instance);
  bool dataset_saved_(otInstance *instance, const otOperationalDatasetTlvs &active, const otOperationalDatasetTlvs &pending);
  void attempt_(otInstance *instance);
  bool advertise_(otInstance *instance);
  void fail_(const char *reason, bool cancelled = false);
  void finish_(uint8_t event, const std::string &reason = {});
  std::string default_pskd_, pskd_, failure_, name_;
  std::vector<JoinerTrigger *> start_, success_, cancelled_;
  std::vector<JoinerFailTrigger *> fail_triggers_;
  std::vector<Notice> notices_;
  std::atomic<int> result_{-1};
  std::atomic<bool> boot_ready_{false}, boot_failed_{false};
  Phase phase_{Phase::IDLE};
  bool busy_{false}, cancel_{false}, backed_up_{false}, cancellation_{false}, restore_retry_{false};
  uint32_t timeout_{120000}, started_at_{0}, retry_at_{0}, restore_warning_at_{0};
};
}  // namespace esphome::openthread_nrf52
#endif
