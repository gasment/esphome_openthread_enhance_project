#include "openthread.h"
#include "joiner.h"
#include "joiner_storage.h"
#include <algorithm>
#include <utility>
#ifdef USE_OPENTHREAD_JOINER
#include "esphome/core/hal.h"
#include "esphome/core/log.h"
#include <openthread/ip6.h>
#include <openthread/link.h>
#include <cstring>
#include <cstddef>

namespace esphome::openthread {
static const char *const TAG = "openthread.joiner";
namespace {
bool same(const otOperationalDatasetTlvs &a, const otOperationalDatasetTlvs &b, bool pending = false) {
  if (a.mLength != b.mLength) return false;
  size_t offset = 0;
  while (offset < a.mLength) {
    if (offset + 2 > a.mLength || a.mTlvs[offset] != b.mTlvs[offset] ||
        a.mTlvs[offset + 1] != b.mTlvs[offset + 1]) return false;
    const size_t length = a.mTlvs[offset + 1];
    if (offset + 2 + length > a.mLength) return false;
    // Pending Delay Timer (52) decreases between saving and reading it back.
    if (!(pending && a.mTlvs[offset] == 52) &&
        std::memcmp(a.mTlvs + offset + 2, b.mTlvs + offset + 2, length) != 0) return false;
    offset += 2 + length;
  }
  return true;
}
bool valid_pskd(const std::string &value) {
  return value.size() >= 6 && value.size() <= 32 &&
         value.find_first_not_of("0123456789ABCDEFGHJKLMNPRSTUVWXY") == std::string::npos;
}
bool read_dataset(otInstance *instance, bool pending, otOperationalDatasetTlvs &value) {
  value = {};
  const auto error = pending ? otDatasetGetPendingTlvs(instance, &value) : otDatasetGetActiveTlvs(instance, &value);
  return error == OT_ERROR_NONE || error == OT_ERROR_NOT_FOUND;
}
}

uint32_t JoinerManager::checksum_(const Journal &journal) {
  uint32_t value = 2166136261UL;
  const auto *bytes = reinterpret_cast<const uint8_t *>(&journal);
  for (size_t i = 0; i < offsetof(Journal, checksum); ++i) value = (value ^ bytes[i]) * 16777619UL;
  return value;
}

bool JoinerManager::dataset_saved_(otInstance *instance, const otOperationalDatasetTlvs &active,
                                   const otOperationalDatasetTlvs &pending) {
  otOperationalDatasetTlvs a{}, p{};
  return read_dataset(instance, false, a) && read_dataset(instance, true, p) && same(a, active) && same(p, pending, true);
}

bool JoinerManager::remove_backup_(otInstance *instance) {
  const auto error = joiner_storage::remove(instance);
  return error == OT_ERROR_NONE || error == OT_ERROR_NOT_FOUND;
}

bool JoinerManager::save_backup_(otInstance *instance) {
  // Start with zeroed object representation so padding is deterministic.
  std::memset(&journal_, 0, sizeof(journal_));
  journal_.magic = 0x4a4e5231;
  if (!read_dataset(instance, false, journal_.active) || !read_dataset(instance, true, journal_.pending)) return false;
  journal_.address = *otLinkGetExtendedAddress(instance);
  journal_.thread_enabled = otThreadGetDeviceRole(instance) != OT_DEVICE_ROLE_DISABLED;
  journal_.checksum = checksum_(journal_);
  if (joiner_storage::write(instance, reinterpret_cast<const uint8_t *>(&journal_), sizeof(journal_)) != OT_ERROR_NONE)
    return false;
  Journal saved{};
  uint16_t size = sizeof(saved);
  return joiner_storage::read(instance, reinterpret_cast<uint8_t *>(&saved), &size) == OT_ERROR_NONE &&
         size == sizeof(saved) && std::memcmp(&saved, &journal_, sizeof(saved)) == 0;
}

bool JoinerManager::restore_(otInstance *instance, bool restart) {
  otJoinerStop(instance);
  if (otThreadSetEnabled(instance, false) != OT_ERROR_NONE) return false;
  // Keep IPv6 enabled for sockets/glue; Thread is disabled while changing identity.
  if (otLinkSetExtendedAddress(instance, &journal_.address) != OT_ERROR_NONE ||
      otDatasetSetActiveTlvs(instance, &journal_.active) != OT_ERROR_NONE ||
      otDatasetSetPendingTlvs(instance, &journal_.pending) != OT_ERROR_NONE ||
      !dataset_saved_(instance, journal_.active, journal_.pending)) return false;
  if (restart && journal_.thread_enabled && otDatasetIsCommissioned(instance)) {
    if (otIp6SetEnabled(instance, true) != OT_ERROR_NONE || otThreadSetEnabled(instance, true) != OT_ERROR_NONE) return false;
  }
  return remove_backup_(instance);
}

bool JoinerManager::boot_locked(otInstance *instance) {
  uint16_t size = sizeof(journal_);
  const auto error = joiner_storage::read(instance, reinterpret_cast<uint8_t *>(&journal_), &size);
  if (error != OT_ERROR_NOT_FOUND) {
    if (error != OT_ERROR_NONE || size != sizeof(journal_) || journal_.magic != 0x4a4e5231 ||
        journal_.checksum != checksum_(journal_) || !restore_(instance, false)) {
      ESP_LOGE(TAG, "Interrupted migration recovery failed; keeping recovery record");
      boot_failed_ = true;
      return false;
    }
    ESP_LOGW(TAG, "Recovered Dataset after interrupted commissioning");
  }
  return true;
}

void JoinerManager::start(const std::string &pskd, uint32_t timeout, bool override_pskd) {
  if (busy_ || boot_failed_) { ESP_LOGW(TAG, "Start ignored: busy or recovery required"); return; }
  pskd_ = override_pskd ? pskd : default_pskd_;
  if (!valid_pskd(pskd_) || timeout < 1000 || timeout > 600000) {
    pskd_.clear();
    notices_.push_back({3, "invalid_pskd_or_timeout"});
    return;
  }
  timeout_ = timeout;
  started_at_ = millis();  // Includes time waiting for OT initialization/lock.
  phase_ = Phase::QUEUED;
  busy_ = true; cancel_ = cancellation_ = backed_up_ = false;
}

void JoinerManager::native_done_(otError error, void *context) {
  static_cast<JoinerManager *>(context)->result_.store(error);
}

void JoinerManager::attempt_(otInstance *instance) {
  if (!advertise_(instance)) { fail_("cannot_set_joiner_advertisement"); return; }
  result_ = -1;
  const auto error = otJoinerStart(instance, pskd_.c_str(), nullptr, "ESPHome", nullptr, nullptr, nullptr, native_done_, this);
  if (error == OT_ERROR_NONE) phase_ = Phase::JOINING;
  else fail_(otThreadErrorToString(error));
}

bool JoinerManager::advertise_(otInstance *instance) {
  // Project experimental wire format: OUI FFFFFF, ESJ + version 1,
  // stable factory-derived Joiner ID (8 bytes), UTF-8 name (1..48 bytes).
  // This ID is for display/deduplication, never an authentication credential.
  if (name_.empty() || name_.size() > 48) return false;
  uint8_t data[60]{'E', 'S', 'J', 1};
  const auto *id = otJoinerGetId(instance);
  if (id == nullptr) return false;
  std::memcpy(data + 4, id->m8, 8);
  std::memcpy(data + 12, name_.data(), name_.size());
  return otThreadSetJoinerAdvertisement(instance, 0xffffff, data, static_cast<uint8_t>(12 + name_.size())) == OT_ERROR_NONE;
}

void JoinerManager::fail_(const char *reason, bool cancelled) {
  failure_ = reason;
  cancellation_ = cancelled;
  restore_warning_at_ = 0;
  restore_retry_ = false;
  phase_ = Phase::RESTORING;
}

void JoinerManager::finish_(uint8_t event, const std::string &reason) {
  // Erase the attempt's copy; a configured default remains available for next time.
  std::fill(pskd_.begin(), pskd_.end(), '\0'); pskd_.clear();
  busy_ = cancel_ = backed_up_ = false;
  phase_ = Phase::IDLE;
  notices_.push_back({event, reason});
  ESP_LOGI(TAG, "Commissioning %s", event == 1 ? "attached and saved" : event == 2 ? "cancelled" : "failed");
}

void JoinerManager::loop_locked(otInstance *instance) {
  if (!boot_ready_ || boot_failed_ || !busy_) return;
  const uint32_t now = millis();
  if (phase_ != Phase::RESTORING) {
    if (cancel_) fail_("cancelled", true);
    else if (uint32_t(now - started_at_) >= timeout_) fail_("timeout");
  }
  if (phase_ == Phase::RESTORING) {
    if (restore_retry_ && uint32_t(now - retry_at_) < 1000) return;
    if (backed_up_ && !restore_(instance)) {
      restore_retry_ = true; retry_at_ = now;
      if (restore_warning_at_ == 0 || uint32_t(now - restore_warning_at_) >= 5000) {
        ESP_LOGW(TAG, "Dataset restore pending; keeping rollback record and staying awake");
        restore_warning_at_ = now ? now : 1;
      }
      return;
    }
    finish_(cancellation_ ? 2 : 3, failure_);
    return;
  }
  if (phase_ == Phase::QUEUED) {
    if (!save_backup_(instance)) { finish_(3, "backup_failed"); return; }
    backed_up_ = true;
    if (otThreadSetEnabled(instance, false) != OT_ERROR_NONE || otIp6SetEnabled(instance, true) != OT_ERROR_NONE) {
      fail_("cannot_stop_thread"); return;
    }
    otOperationalDatasetTlvs empty{};
    // Prevent an old Pending Dataset from activating during a migration trial.
    if (otDatasetSetPendingTlvs(instance, &empty) != OT_ERROR_NONE) { fail_("cannot_clear_pending"); return; }
    notices_.push_back({0, {}});
    attempt_(instance);
  }
  if (phase_ == Phase::JOINING) {
    const int result = result_.exchange(-1);
    if (result == OT_ERROR_NONE) {
      // Joiner Entrust may save only Network Key, Channel and PAN ID. Network
      // Name/Extended PAN ID arrive during attachment, so IsCommissioned() can
      // legitimately be false here. Let native Thread start with these creds.
      const auto error = otThreadSetEnabled(instance, true);
      if (error != OT_ERROR_NONE) {
        ESP_LOGW(TAG, "Cannot start Thread after Joiner: %s (%d), IPv6 enabled=%s",
                 otThreadErrorToString(error), static_cast<int>(error), YESNO(otIp6IsEnabled(instance)));
        fail_("cannot_attach_received_dataset");
      } else {
        ESP_LOGD(TAG, "Joiner credentials received; waiting for attachment and complete Dataset");
        phase_ = Phase::ATTACHING;
      }
    } else if (result == OT_ERROR_NOT_FOUND || result == OT_ERROR_RESPONSE_TIMEOUT) {
      otJoinerStop(instance); phase_ = Phase::RETRY; retry_at_ = now;
    } else if (result >= 0) fail_(otThreadErrorToString(static_cast<otError>(result)));
  }
  if (phase_ == Phase::RETRY && uint32_t(now - retry_at_) >= 1000) attempt_(instance);
  // A role change can precede the complete Active Dataset. Keep the rollback
  // journal and the original overall deadline until both conditions hold.
  if (phase_ == Phase::ATTACHING && otThreadGetDeviceRole(instance) >= OT_DEVICE_ROLE_CHILD &&
      otDatasetIsCommissioned(instance)) phase_ = Phase::COMMITTING;
  if (phase_ == Phase::COMMITTING) {
    // OT has persisted the received Active Dataset. Verify it can be read before
    // deleting the rollback journal; a reset before deletion safely restores old.
    otOperationalDatasetTlvs active{};
    if (otDatasetGetActiveTlvs(instance, &active) != OT_ERROR_NONE || !otDatasetIsCommissioned(instance)) {
      fail_("dataset_not_saved"); return;
    }
    if (remove_backup_(instance)) {
      // Commit cleanup must never remove/change the actual Active Dataset.
      otOperationalDatasetTlvs committed{};
      if (otDatasetGetActiveTlvs(instance, &committed) != OT_ERROR_NONE || !same(active, committed) ||
          !otDatasetIsCommissioned(instance)) {
        ESP_LOGE(TAG, "Active Dataset changed during journal cleanup; restoring previous credentials");
        fail_("dataset_changed_during_commit");
      } else {
        finish_(1);
      }
    }
  }
}

void JoinerManager::dispatch() {
  auto notices = std::move(notices_);
  notices_.clear();
  for (const auto &notice : notices) {
    if (notice.event == 3) for (auto *t : fail_triggers_) t->trigger(notice.reason);
    else for (auto *t : notice.event == 0 ? start_ : notice.event == 1 ? success_ : cancelled_) t->trigger();
  }
}
void OpenThreadComponent::loop_joiner_() {
  {
    auto lock = InstanceLock::try_acquire(0);
    if (lock) joiner_.loop_locked(lock.get_instance());
  }
  joiner_.dispatch();
}
}  // namespace esphome::openthread
#endif
