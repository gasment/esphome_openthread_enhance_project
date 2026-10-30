#include "joiner_storage.h"
#ifdef USE_OPENTHREAD_JOINER
#include "nvs.h"

namespace esphome::openthread::joiner_storage {
namespace {
// ESP-IDF 5.5.x truncates OT settings keys to uint8_t: 0x8001 aliases
// OT_SETTINGS_KEY_ACTIVE_DATASET. Never put our journal in OT's namespace.
constexpr const char *NAMESPACE = "esphome_ot_jnr";
constexpr const char *KEY = "rollback";
struct Handle {
  nvs_handle_t value{0};
  esp_err_t error;
  explicit Handle(nvs_open_mode_t mode) : error(nvs_open(NAMESPACE, mode, &value)) {}
  ~Handle() { if (error == ESP_OK) nvs_close(value); }
};
otError convert(esp_err_t error) {
  if (error == ESP_OK) return OT_ERROR_NONE;
  if (error == ESP_ERR_NVS_NOT_FOUND) return OT_ERROR_NOT_FOUND;
  if (error == ESP_ERR_NVS_INVALID_LENGTH || error == ESP_ERR_NVS_NOT_ENOUGH_SPACE) return OT_ERROR_NO_BUFS;
  return OT_ERROR_FAILED;
}
}

otError read(otInstance *, uint8_t *data, uint16_t *size) {
  Handle handle(NVS_READONLY);
  if (handle.error != ESP_OK) return convert(handle.error);
  size_t length = *size;
  const auto error = nvs_get_blob(handle.value, KEY, data, &length);
  if (length <= UINT16_MAX) *size = static_cast<uint16_t>(length);
  else return OT_ERROR_NO_BUFS;
  return convert(error);
}

otError write(otInstance *, const uint8_t *data, uint16_t size) {
  Handle handle(NVS_READWRITE);
  if (handle.error != ESP_OK) return convert(handle.error);
  auto error = nvs_set_blob(handle.value, KEY, data, size);
  if (error == ESP_OK) error = nvs_commit(handle.value);
  return convert(error);
}

otError remove(otInstance *) {
  Handle handle(NVS_READWRITE);
  if (handle.error != ESP_OK) return convert(handle.error);
  const auto error = nvs_erase_key(handle.value, KEY);
  if (error != ESP_OK && error != ESP_ERR_NVS_NOT_FOUND) return convert(error);
  const auto committed = nvs_commit(handle.value);
  if (committed != ESP_OK) return convert(committed);
  size_t length = 0;
  // Missing is success; all other results must keep the transaction pending.
  return nvs_get_blob(handle.value, KEY, nullptr, &length) == ESP_ERR_NVS_NOT_FOUND ? OT_ERROR_NONE : OT_ERROR_FAILED;
}
}  // namespace esphome::openthread::joiner_storage
#endif
