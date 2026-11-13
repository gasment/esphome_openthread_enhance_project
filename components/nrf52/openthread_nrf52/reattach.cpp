#include "esphome/core/defines.h"
#ifdef USE_OPENTHREAD_NRF52
#include "openthread_nrf52.h"
#include "esphome/core/log.h"
#include <openthread/platform/settings.h>

namespace esphome::openthread_nrf52 {
bool OpenThreadComponent::reattach() {
  static const char *const TAG = "openthread_nrf52";
  if (!this->is_ready() || this->is_failed() || this->teardown_stage_ != TeardownStage::TEARDOWN_STAGE_NOT_STARTED) {
    ESP_LOGW(TAG, "Reattach skipped: OpenThread unavailable or stopping");
    return false;
  }
#ifdef USE_OPENTHREAD_JOINER
  if (this->is_joiner_active()) {
    ESP_LOGW(TAG, "Reattach skipped: commissioning in progress");
    return false;
  }
#endif
  auto lock = InstanceLock::try_acquire(100);
  if (!lock) {
    ESP_LOGW(TAG, "Reattach skipped: OpenThread API lock busy");
    return false;
  }
  auto *instance = lock.get_instance();
  if (!otDatasetIsCommissioned(instance) || !otIp6IsEnabled(instance)) {
    ESP_LOGW(TAG, "Reattach skipped: no complete Dataset or IPv6 disabled");
    return false;
  }
  const bool was_enabled = otThreadGetDeviceRole(instance) != OT_DEVICE_ROLE_DISABLED;
  otError error = otThreadSetEnabled(instance, false);
  if (error != OT_ERROR_NONE) {
    ESP_LOGW(TAG, "Cannot stop Thread for reattach: %s", otThreadErrorToString(error));
    return false;
  }
  this->connected_ = false;
  // Preserve NetworkInfo (identity and frame counters), Dataset, SLAAC secrets,
  // SRP settings and application preferences. Only remove the parent record.
  error = otPlatSettingsDelete(instance, OT_SETTINGS_KEY_PARENT_INFO, -1);
  if (error == OT_ERROR_NONE || error == OT_ERROR_NOT_FOUND) {
    uint16_t length = 0;
    error = otPlatSettingsGet(instance, OT_SETTINGS_KEY_PARENT_INFO, 0, nullptr, &length);
    if (error == OT_ERROR_NOT_FOUND) error = OT_ERROR_NONE;
    else if (error == OT_ERROR_NONE) error = OT_ERROR_FAILED;
  }
  if (error != OT_ERROR_NONE) {
    ESP_LOGW(TAG, "Cannot clear saved parent: %s", otThreadErrorToString(error));
    if (was_enabled) {
      const auto restore = otThreadSetEnabled(instance, true);
      if (restore != OT_ERROR_NONE)
        ESP_LOGW(TAG, "Cannot resume Thread: %s", otThreadErrorToString(restore));
    }
    return false;
  }
  error = otThreadSetEnabled(instance, true);
  if (error == OT_ERROR_NONE) {
    // Start() may try Child Update with the old RLOC still in RAM. Detach
    // explicitly invalidates the live parent/RLOC and requests a full attach.
    error = otThreadBecomeDetached(instance);
  }
  if (error != OT_ERROR_NONE) {
    ESP_LOGW(TAG, "Cannot start full reattach: %s", otThreadErrorToString(error));
    return false;
  }
  ESP_LOGI(TAG, "Saved parent cleared; full Thread attachment started (Dataset retained)");
  return true;
}
}  // namespace esphome::openthread_nrf52
#endif
