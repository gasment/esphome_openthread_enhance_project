#include "joiner_storage.h"
#ifdef USE_OPENTHREAD_JOINER
#include <openthread/platform/settings.h>

namespace esphome::openthread_nrf52::joiner_storage {
// Zephyr preserves all 16 key bits (ot/8001), unlike ESP-IDF's OT backend.
static constexpr uint16_t JOURNAL_KEY = 0x8001;
otError read(otInstance *instance, uint8_t *data, uint16_t *size) {
  return otPlatSettingsGet(instance, JOURNAL_KEY, 0, data, size);
}
otError write(otInstance *instance, const uint8_t *data, uint16_t size) {
  return otPlatSettingsSet(instance, JOURNAL_KEY, data, size);
}
otError remove(otInstance *instance) {
  const auto error = otPlatSettingsDelete(instance, JOURNAL_KEY, -1);
  if (error != OT_ERROR_NONE && error != OT_ERROR_NOT_FOUND) return error;
  // Zephyr supports a length-only lookup without a value buffer.
  uint16_t size = 0;
  return otPlatSettingsGet(instance, JOURNAL_KEY, 0, nullptr, &size) == OT_ERROR_NOT_FOUND ? OT_ERROR_NONE : OT_ERROR_FAILED;
}
}  // namespace esphome::openthread_nrf52::joiner_storage
#endif
