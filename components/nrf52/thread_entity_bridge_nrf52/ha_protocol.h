#pragma once
// HA protocol v3. Identical on both platforms; v2 transport_protocol.h stays intact for REED.
#include "transport_protocol.h"
#include <limits>

namespace esphome::thread_ha {
inline constexpr size_t HEADER_SIZE = 70;
inline constexpr const char *SERVICE = "_esphome-tgw._udp.default.service.arpa.";
using thread_transport::encode;
using thread_transport::hex_id;
using thread_transport::text;
using thread_transport::field;
using thread_transport::sha256;
inline bool identifier(const std::string &s) {
  return !s.empty() && s.size() <= 32 && s.find_first_not_of("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-") == std::string::npos;
}
inline std::string unhex(const std::string &s) {
  if (s.size() % 2 || !hex_id(s, s.size())) return {};
  std::string out;
  for (size_t i = 0; i < s.size(); i += 2) {
    auto nibble = [](char c) { return c <= '9' ? c - '0' : c - 'a' + 10; };
    out.push_back(char((nibble(s[i]) << 4) | nibble(s[i + 1])));
  }
  return out;
}
inline std::string hmac(const std::string &raw, const std::string &secret) {
  auto key = unhex(secret);
  if (key.size() != 32) return {};
  key.resize(64, '\0');
  std::string inner(64, '\0'), outer(64, '\0');
  for (size_t i = 0; i < 64; ++i) { inner[i] = key[i] ^ 0x36; outer[i] = key[i] ^ 0x5c; }
  inner.append("OTG3\0", 5); inner += raw;
  return sha256(outer + unhex(sha256(inner)));
}
inline bool equal(const std::string &a, const std::string &b) {
  if (a.size() != b.size()) return false;
  unsigned difference = 0;
  for (size_t i = 0; i < a.size(); ++i) difference |= uint8_t(a[i]) ^ uint8_t(b[i]);
  return difference == 0;
}
inline std::string frame(const std::string &raw, const std::string &secret) {
  if (raw.empty()) return {};
  const auto signature = secret.empty() ? "-" : hmac(raw, secret);
  if (signature.empty()) return {};
  auto out = "OTG3 " + signature + '\n' + raw;
  return out.size() <= 1232 ? out : std::string{};
}
inline cJSON *parse(const char *data, size_t size, std::string &signature, std::string &raw) {
  if (size > 1232 || size < 9 || std::memcmp(data, "OTG3 ", 5)) return nullptr;
  const auto *end = static_cast<const char *>(std::memchr(data + 5, '\n', size - 5));
  if (!end) return nullptr;
  signature.assign(data + 5, end);
  if (signature != "-" && !hex_id(signature, 64)) return nullptr;
  raw.assign(end + 1, data + size);
  if (raw.find('\0') != std::string::npos) return nullptr;
  auto *json = cJSON_ParseWithLengthOpts(raw.c_str(), raw.size() + 1, nullptr, true);
  // canonical() is used only for validation (duplicate keys/non-finite/depth), never signing.
  const auto *version = field(json, "v");
  if (!cJSON_IsObject(json) || !cJSON_IsNumber(version) || version->valuedouble != 3 ||
      text(json, "type").empty() || thread_transport::canonical(json).empty()) {
    cJSON_Delete(json); return nullptr;
  }
  return json;
}
inline std::string base64(const std::string &raw) {
  static constexpr char alphabet[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
  std::string out;
  for (size_t i = 0; i < raw.size(); i += 3) {
    uint32_t n = uint32_t(uint8_t(raw[i])) << 16;
    if (i + 1 < raw.size()) n |= uint32_t(uint8_t(raw[i + 1])) << 8;
    if (i + 2 < raw.size()) n |= uint8_t(raw[i + 2]);
    out += alphabet[(n >> 18) & 63]; out += alphabet[(n >> 12) & 63];
    out += i + 1 < raw.size() ? alphabet[(n >> 6) & 63] : '=';
    out += i + 2 < raw.size() ? alphabet[n & 63] : '=';
  }
  return out;
}
struct BindingData {
  uint32_t magic{0x33414854}, generation{0};
  uint64_t boot{0};
  char device[17]{}, owner[33]{}, token[33]{}, secret[65]{};
};
class Binding {
 public:
  BindingData data{};
  bool loaded{false};
  bool store(otInstance *instance, const BindingData &next) {
    bool success = false;
#ifdef USE_ESP32
    nvs_handle_t handle;
    if (nvs_open("thread_ha_v3", NVS_READWRITE, &handle) == ESP_OK) {
      success = nvs_set_blob(handle, "binding", &next, sizeof(next)) == ESP_OK && nvs_commit(handle) == ESP_OK;
      nvs_close(handle);
    }
#else
    success = otPlatSettingsSet(instance, 0x8003, reinterpret_cast<const uint8_t *>(&next), sizeof(next)) == OT_ERROR_NONE;
#endif
    if (success) data = next;
    return success;
  }
  bool start(otInstance *instance) {
    if (loaded) return true;
    BindingData next{};
    bool found = false, missing = false;
#ifdef USE_ESP32
    nvs_handle_t handle;
    auto error = nvs_open("thread_ha_v3", NVS_READONLY, &handle);
    missing = error == ESP_ERR_NVS_NOT_FOUND;
    if (error == ESP_OK) {
      size_t size = sizeof(next);
      error = nvs_get_blob(handle, "binding", &next, &size);
      found = error == ESP_OK && size == sizeof(next); missing = error == ESP_ERR_NVS_NOT_FOUND;
      nvs_close(handle);
    }
#else
    uint16_t size = sizeof(next);
    auto error = otPlatSettingsGet(instance, 0x8003, 0, reinterpret_cast<uint8_t *>(&next), &size);
    found = error == OT_ERROR_NONE && size == sizeof(next); missing = error == OT_ERROR_NOT_FOUND;
#endif
    if (!found && !missing) return false;
    const auto device = thread_transport::device(instance);
    if (found && (next.magic != data.magic || next.device[16] || next.owner[32] || next.token[32] || next.secret[64] ||
        device != next.device || (next.owner[0] && (!identifier(next.owner) || !hex_id(next.token, 32) || !hex_id(next.secret, 64))) ||
        (!next.owner[0] && (next.token[0] || next.secret[0])))) return false;
    // cJSON numbers are doubles; fail closed before loss of boot-counter precision.
    if (next.boot >= 9007199254740991ULL) return false;
    ++next.boot;
    snprintf(next.device, sizeof(next.device), "%s", device.c_str());
    if (!store(instance, next)) return false;
    loaded = true; return true;
  }
  bool bind(otInstance *instance, const std::string &owner, const std::string &token, const std::string &secret, uint32_t generation) {
    if (!loaded || !identifier(owner) || !hex_id(token, 32) || !hex_id(secret, 64)) return false;
    if (data.owner[0]) return owner == data.owner && token == data.token && secret == data.secret && generation == data.generation;
    if (data.generation == UINT32_MAX || generation != data.generation + 1) return false;
    auto next = data; next.generation = generation;
    snprintf(next.owner, sizeof(next.owner), "%s", owner.c_str());
    snprintf(next.token, sizeof(next.token), "%s", token.c_str());
    snprintf(next.secret, sizeof(next.secret), "%s", secret.c_str());
    return store(instance, next);
  }
  bool revoke(otInstance *instance) {
    if (!loaded || data.generation == UINT32_MAX) return false;
    auto next = data; ++next.generation;
    std::memset(next.owner, 0, sizeof(next.owner)); std::memset(next.token, 0, sizeof(next.token));
    std::memset(next.secret, 0, sizeof(next.secret));
    return store(instance, next);
  }
};
}  // namespace esphome::thread_ha
