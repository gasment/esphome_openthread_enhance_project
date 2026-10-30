#include "commissioner.h"
#ifdef CONFIG_THREAD_BR_COMMISSIONER_WEB
#include <algorithm>
#include <cstdio>
#include <cstring>
#include <cmath>
#include "esphome/core/hal.h"
#include "esphome/core/log.h"
#include "esp_openthread.h"
#include "esp_openthread_lock.h"
#include "openthread/dataset.h"
#include "openthread/border_routing.h"
#include "openthread/ip6.h"
#include "thread_br_web.h"

namespace esphome::thread_br {
static const char *const TAG = "thread_br.commissioner";
namespace {
constexpr uint32_t CANDIDATE_TTL_MS = 120000, PETITION_TIMEOUT_MS = 15000;
const cJSON *field(const cJSON *object, const char *key) { return cJSON_GetObjectItemCaseSensitive(object, key); }
const char *string(const cJSON *object, const char *key) {
  const auto *item = field(object, key);
  return cJSON_IsString(item) && item->valuestring ? item->valuestring : "";
}
bool hex(const char *text, uint8_t *out, size_t size) {
  if (std::strlen(text) != size * 2) return false;
  for (size_t i = 0; i < size; ++i) {
    unsigned byte = 0;
    for (size_t j = 0; j < 2; ++j) {
      char c = text[i * 2 + j];
      unsigned digit;
      if (c >= '0' && c <= '9') digit = c - '0';
      else if (c >= 'a' && c <= 'f') digit = c - 'a' + 10;
      else if (c >= 'A' && c <= 'F') digit = c - 'A' + 10;
      else return false;
      byte = (byte << 4) | digit;
    }
    out[i] = byte;
  }
  return true;
}
void format_hex(const uint8_t *bytes, char *out) {
  static const char HEX[] = "0123456789abcdef";
  for (size_t i = 0; i < 8; ++i) { out[i * 2] = HEX[bytes[i] >> 4]; out[i * 2 + 1] = HEX[bytes[i] & 15]; }
  out[16] = 0;
}
bool pskd_valid(const char *value) {
  size_t length = std::strlen(value);
  if (length < 6 || length > 32) return false;
  for (size_t i = 0; i < length; ++i) {
    const char c = value[i];
    if (!((c >= '0' && c <= '9') || (c >= 'A' && c <= 'Z')) || c == 'I' || c == 'O' || c == 'Q' || c == 'Z') return false;
  }
  return true;
}
cJSON *response(int &status, int code, const char *message) {
  status = code;
  auto *json = cJSON_CreateObject();
  cJSON_AddNumberToObject(json, "error", code >= 400 ? code : 0);
  cJSON_AddStringToObject(json, "message", message);
  return json;
}
struct Lock {
  SemaphoreHandle_t mutex;
  bool acquired;
  explicit Lock(SemaphoreHandle_t m, TickType_t wait = 0) : mutex(m), acquired(m && xSemaphoreTake(m, wait) == pdTRUE) {}
  ~Lock() { if (acquired) xSemaphoreGive(mutex); }
};
}  // namespace

void CommissionerManager::setup() {
  mutex_ = xSemaphoreCreateMutex();
  events_ = xQueueCreate(32, sizeof(Event));
  network_ = cJSON_Parse(network_json_.c_str());
  network_json_.clear();
  thread_br_commissioner_web_t web{this, get_, post_};
  thread_br_web_set_commissioner(&web);
  if (!mutex_ || !events_ || !cJSON_IsObject(network_))
    ESP_LOGE(TAG, "Commissioner storage initialization failed");
}

void CommissionerManager::discovery_(const otThreadDiscoveryRequestInfo *info, void *context) {
  auto *self = static_cast<CommissionerManager *>(context);
  if (!info->mIsJoiner || self->shutdown_ || !self->events_) return;
  Event event{millis(), info->mExtAddress, 0, info->mVersion};
  if (xQueueSend(self->events_, &event, 0) != pdTRUE) self->dropped_++;
}
void CommissionerManager::identified_discovery_(const otExtAddress *address, uint8_t version,
                                                const otExtAddress *id, const char *name, void *context) {
  auto *self = static_cast<CommissionerManager *>(context);
  if (self->shutdown_ || !self->events_) return;
  Event event{millis(), *address, 0, version};
  event.device_id = *id;
  event.identified = true;
  std::memcpy(event.name, name, std::strlen(name) + 1);
  if (xQueueSend(self->events_, &event, 0) != pdTRUE) self->dropped_++;
}
void CommissionerManager::state_(otCommissionerState state, void *context) {
  auto *self = static_cast<CommissionerManager *>(context);
  Event event{millis(), {}, 1, static_cast<uint8_t>(state)};
  if (self->events_ && xQueueSend(self->events_, &event, 0) != pdTRUE) self->dropped_++;
}
void CommissionerManager::joiner_(otCommissionerJoinerEvent type, const otJoinerInfo *, const otExtAddress *address,
                                  void *context) {
  auto *self = static_cast<CommissionerManager *>(context);
  Event event{millis(), {}, 2, static_cast<uint8_t>(type)};
  if (address) event.address = *address;
  if (self->events_ && xQueueSend(self->events_, &event, 0) != pdTRUE) self->dropped_++;
}

bool CommissionerManager::network_matches_(otInstance *instance) const {
  otOperationalDataset dataset{};
  if (otDatasetGetActive(instance, &dataset) != OT_ERROR_NONE) return false;
  const auto &c = dataset.mComponents;
  if (!c.mIsChannelPresent || !c.mIsPanIdPresent || !c.mIsExtendedPanIdPresent || !c.mIsNetworkKeyPresent ||
      !c.mIsNetworkNamePresent || !c.mIsPskcPresent || !c.mIsMeshLocalPrefixPresent) return false;
  const auto *channel = field(network_, "channel"), *pan = field(network_, "pan_id");
  if (!cJSON_IsNumber(channel) || !cJSON_IsNumber(pan) || channel->valueint != dataset.mChannel ||
      pan->valueint != dataset.mPanId || std::strcmp(string(network_, "network_name"), dataset.mNetworkName.m8)) return false;
  uint8_t bytes[16];
  if (!hex(string(network_, "network_key"), bytes, 16) || std::memcmp(bytes, dataset.mNetworkKey.m8, 16)) return false;
  if (!hex(string(network_, "pskc"), bytes, 16) || std::memcmp(bytes, dataset.mPskc.m8, 16)) return false;
  if (!hex(string(network_, "ext_pan_id"), bytes, 8) || std::memcmp(bytes, dataset.mExtendedPanId.m8, 8)) return false;
  otIp6Address prefix{};
  std::string text = string(network_, "mesh_local_prefix");
  const auto slash = text.find('/');
  if (slash == std::string::npos) return false;
  text.resize(slash);
  return otIp6AddressFromString(text.c_str(), &prefix) == OT_ERROR_NONE &&
         std::memcmp(prefix.mFields.m8, dataset.mMeshLocalPrefix.m8, 8) == 0;
}

void CommissionerManager::close_(otInstance *instance, const char *phase, const char *message) {
  if (owns_) otCommissionerStop(instance);
  if (!std::strcmp(phase, "complete"))
    for (auto &candidate : candidates_) if (candidate.used && !std::strcmp(candidate.id, request_.candidate)) candidate.used = false;
  owns_ = queued_ = authorized_ = finalized_ = cancel_ = false;
  std::memset(request_.pskd, 0, sizeof(request_.pskd));
  phase_ = phase; message_ = message;
  ESP_LOGI(TAG, "%s", message);
}

void CommissionerManager::event_(const Event &event, otInstance *instance) {
  if (event.kind == 0) {
    char address[17]; format_hex(event.address.m8, address);
    char id[19]{}, device_id[17]{};
    if (event.identified) {
      format_hex(event.device_id.m8, device_id);
      id[0] = 'j'; id[1] = ':';
      std::memcpy(id + 2, device_id, sizeof(device_id));
    } else {
      std::memcpy(id, address, sizeof(address));
    }
    auto found = std::find_if(candidates_.begin(), candidates_.end(), [&](const Candidate &c) {
      return c.used && !std::strcmp(c.id, id);
    });
    if (found == candidates_.end()) found = std::find_if(candidates_.begin(), candidates_.end(), [](const Candidate &c) { return !c.used; });
    if (found == candidates_.end()) found = std::min_element(candidates_.begin(), candidates_.end(), [&](const Candidate &a, const Candidate &b) {
      const bool a_selected = running_() && !std::strcmp(a.id, request_.candidate);
      const bool b_selected = running_() && !std::strcmp(b.id, request_.candidate);
      if (a_selected != b_selected) return !a_selected;
      return uint32_t(event.at - a.seen) > uint32_t(event.at - b.seen);
    });
    std::memcpy(found->id, id, sizeof(id));
    std::memcpy(found->address, address, sizeof(address));
    std::memcpy(found->device_id, device_id, sizeof(device_id));
    std::memcpy(found->name, event.name, sizeof(event.name));
    found->seen = event.at; found->version = event.value; found->used = true;
  } else if (event.kind == 2 && owns_ && authorized_) {
    switch (event.value) {
      case OT_COMMISSIONER_JOINER_START: phase_ = "connecting"; message_ = "Joiner session started"; break;
      case OT_COMMISSIONER_JOINER_CONNECTED: phase_ = "authenticating"; message_ = "Joiner authenticated"; break;
      case OT_COMMISSIONER_JOINER_FINALIZE: finalized_ = true; phase_ = "finalizing"; message_ = "Joiner finalization in progress"; break;
      case OT_COMMISSIONER_JOINER_END:
        if (finalized_) close_(instance, "complete", "Commissioning session completed; node attachment is not yet verified");
        else { phase_ = "waiting"; message_ = "Joiner session ended; waiting for retry"; }
        break;
      default: break;
    }
  }
}

void CommissionerManager::loop(bool ready) {
  if (shutdown_ || !events_ || !network_) return;
  Lock lock(mutex_);
  if (!lock.acquired) return;
  if (!ready) { ready_ = matched_ = false; active_omr_prefix_.clear(); return; }
  if (!esp_openthread_lock_acquire(0)) return;
  auto *instance = esp_openthread_get_instance();
  // Display the network's favored runtime prefix, not this BR's configured
  // or automatically generated local candidate.
  otIp6Prefix omr{};
  otRoutePreference preference;
  active_omr_prefix_.clear();
  if (otBorderRoutingGetFavoredOmrPrefix(instance, &omr, &preference) == OT_ERROR_NONE) {
    char text[OT_IP6_PREFIX_STRING_SIZE];
    otIp6PrefixToString(&omr, text, sizeof(text));
    active_omr_prefix_ = text;
  }
  if (!registered_) {
    thread_br_joiner_set_discovery_callback(identified_discovery_, this);
    otThreadSetDiscoveryRequestCallback(instance, discovery_, this);
    registered_ = true;
  }
  ready_ = otThreadGetDeviceRole(instance) >= OT_DEVICE_ROLE_CHILD;
  const uint32_t now = millis();
  if (!ready_) matched_ = false;
  else if (queued_ || dataset_check_at_ == 0 || uint32_t(now - dataset_check_at_) >= 1000) {
    matched_ = network_matches_(instance);
    dataset_check_at_ = now;
  }
  if (!running_() && !std::strcmp(phase_, "idle"))
    message_ = !ready_ ? "Waiting for Thread attachment" : !matched_ ?
               "Active dataset differs from firmware configuration" : "Ready for commissioning";
  for (auto &candidate : candidates_)
    if (candidate.used && uint32_t(now - candidate.seen) > CANDIDATE_TTL_MS &&
        !(running_() && !std::strcmp(candidate.id, request_.candidate))) candidate.used = false;
  Event event;
  for (unsigned i = 0; i < 8 && xQueueReceive(events_, &event, 0) == pdTRUE; ++i) event_(event, instance);
  if (cancel_) close_(instance, "cancelled", "Commissioning cancelled");
  if (queued_) {
    queued_ = false;
    if (!ready_ || !matched_) close_(instance, "failed", "Thread unavailable or active dataset differs from firmware configuration");
    else if (otCommissionerGetState(instance) != OT_COMMISSIONER_STATE_DISABLED)
      close_(instance, "failed", "Local commissioner is already running");
    else {
      otError error = otCommissionerSetId(instance, id_.c_str());
      if (error == OT_ERROR_NONE) error = otCommissionerStart(instance, state_, joiner_, this);
      if (error != OT_ERROR_NONE) close_(instance, "failed", "Commissioner start failed");
      else { owns_ = true; phase_ = "petitioning"; message_ = "Requesting commissioner role"; requested_at_ = now; }
    }
  }
  if (owns_) {
    const auto state = otCommissionerGetState(instance);
    if (!ready_ || !matched_) close_(instance, "failed", "Thread unavailable or active dataset changed");
    else if (state == OT_COMMISSIONER_STATE_DISABLED) close_(instance, "failed", "Commissioner role unavailable or lost");
    else if (!authorized_ && uint32_t(now - requested_at_) >= PETITION_TIMEOUT_MS)
      close_(instance, "failed", "Commissioner role request timed out");
    else if (!authorized_ && state == OT_COMMISSIONER_STATE_ACTIVE) {
      const auto error = otCommissionerAddJoiner(instance, request_.has_eui ? &request_.eui : nullptr,
                                                request_.pskd, request_.seconds);
      std::memset(request_.pskd, 0, sizeof(request_.pskd));
      if (error != OT_ERROR_NONE) close_(instance, "failed", "Joiner authorization failed");
      else { authorized_ = true; window_at_ = now; phase_ = "waiting"; message_ = "Waiting for Joiner"; ESP_LOGI(TAG, "Commissioning window opened (%us)", unsigned(request_.seconds)); }
    } else if (authorized_ && uint32_t(now - window_at_) >= uint32_t(request_.seconds) * 1000)
      close_(instance, "timeout", "Commissioning window expired");
  }
  esp_openthread_lock_release();
}

cJSON *CommissionerManager::get_(void *context, int *status) { return static_cast<CommissionerManager *>(context)->snapshot_(*status); }
cJSON *CommissionerManager::post_(void *context, const cJSON *request, int *status) {
  return static_cast<CommissionerManager *>(context)->handle_request_(request, *status);
}
cJSON *CommissionerManager::snapshot_(int &status) {
  Lock lock(mutex_, pdMS_TO_TICKS(20));
  if (!lock.acquired || !events_ || !network_ || shutdown_) return response(status, 503, "Commissioner unavailable");
  auto *root = response(status, 200, "OK");
  if (!root) { status = 500; return nullptr; }
  cJSON_AddBoolToObject(root, "enabled", true);
  cJSON_AddStringToObject(root, "commissioner_id", id_.c_str());
  cJSON_AddStringToObject(root, "phase", phase_);
  cJSON_AddStringToObject(root, "detail", message_);
  cJSON_AddBoolToObject(root, "busy", running_());
  cJSON_AddBoolToObject(root, "attached", ready_);
  cJSON_AddBoolToObject(root, "dataset_matches", matched_);
  cJSON_AddNumberToObject(root, "dropped_events", dropped_.load());
  cJSON_AddNumberToObject(root, "remaining_s", authorized_ ?
      std::max(0, int(request_.seconds) - int(uint32_t(millis() - window_at_) / 1000)) : 0);
  auto *network = cJSON_Duplicate(network_, true);
  if (network) {
    cJSON_DeleteItemFromObjectCaseSensitive(network, "force_dataset");
    cJSON_DeleteItemFromObjectCaseSensitive(network, "omr_prefix");
    cJSON_AddStringToObject(network, "omr_prefix", active_omr_prefix_.c_str());
    cJSON_AddItemToObject(root, "network", network);
  }
  auto *candidates = cJSON_AddArrayToObject(root, "candidates");
  for (const auto &candidate : candidates_) {
    if (!candidate.used || uint32_t(millis() - candidate.seen) > CANDIDATE_TTL_MS) continue;
    auto *item = cJSON_CreateObject();
    cJSON_AddStringToObject(item, "id", candidate.id);
    cJSON_AddStringToObject(item, "address", candidate.address);
    cJSON_AddStringToObject(item, "device_id", candidate.device_id);
    cJSON_AddStringToObject(item, "name", candidate.name);
    cJSON_AddBoolToObject(item, "identified", candidate.device_id[0] != 0);
    cJSON_AddNumberToObject(item, "version", candidate.version);
    cJSON_AddNumberToObject(item, "age_ms", uint32_t(millis() - candidate.seen));
    cJSON_AddItemToArray(candidates, item);
  }
  return root;
}

cJSON *CommissionerManager::handle_request_(const cJSON *json, int &status) {
  Lock lock(mutex_, pdMS_TO_TICKS(20));
  if (!lock.acquired || !events_ || !network_ || shutdown_) return response(status, 503, "Commissioner unavailable");
  const cJSON *member;
  cJSON_ArrayForEach(member, json) {
    bool known = false;
    for (const auto *key : {"action", "pskd", "eui64", "candidate", "timeout_s"})
      if (member->string && !std::strcmp(member->string, key)) known = true;
    if (!known) return response(status, 400, "Unknown field; Dataset writes and migration modes are not implemented");
  }
  for (const auto *key : {"action", "pskd", "eui64", "candidate"})
    if (field(json, key) && !cJSON_IsString(field(json, key))) return response(status, 400, "Expected string field");
  const char *action = string(json, "action");
  if (!std::strcmp(action, "cancel")) { cancel_ = true; return response(status, 202, "Cancellation queued"); }
  if (std::strcmp(action, "start")) return response(status, 400, "Expected start or cancel");
  if (running_() || cancel_) return response(status, 409, "Commissioning already in progress");
  if (!ready_ || !matched_) return response(status, 409, "Thread unavailable or active dataset differs from firmware configuration");
  Request request;
  const char *pskd = string(json, "pskd");
  if (!pskd_valid(pskd)) return response(status, 400, "Invalid PSKd (6..32 uppercase letters/digits, excluding I/O/Q/Z)");
  const auto *timeout = field(json, "timeout_s");
  if (timeout && (!cJSON_IsNumber(timeout) || !std::isfinite(timeout->valuedouble) ||
      timeout->valuedouble < 10 || timeout->valuedouble > 600 || std::floor(timeout->valuedouble) != timeout->valuedouble))
    return response(status, 400, "timeout_s must be an integer in 10..600");
  request.seconds = static_cast<uint16_t>(timeout ? timeout->valueint : 120);
  const char *eui = string(json, "eui64");
  if (*eui) {
    if (!hex(eui, request.eui.m8, 8)) return response(status, 400, "Invalid factory EUI64");
    request.has_eui = true;
  }
  const char *candidate = string(json, "candidate");
  if (*candidate) {
    const auto found = std::find_if(candidates_.begin(), candidates_.end(), [&](const Candidate &c) {
      return c.used && !std::strcmp(c.id, candidate) && uint32_t(millis() - c.seen) <= CANDIDATE_TTL_MS;
    });
    if (found == candidates_.end()) return response(status, 409, "Candidate expired; refresh and retry");
    std::memcpy(request.candidate, found->id, sizeof(request.candidate));
  }
  std::memcpy(request.pskd, pskd, std::strlen(pskd) + 1);
  request_ = request;
  queued_ = true; phase_ = "queued"; message_ = "Commissioning request queued";
  return response(status, 202, "Commissioning request queued");
}

void CommissionerManager::on_shutdown() {
  shutdown_ = true;
  Lock lock(mutex_);
  if (lock.acquired && registered_ && esp_openthread_lock_acquire(0)) {
    auto *instance = esp_openthread_get_instance();
    otThreadSetDiscoveryRequestCallback(instance, nullptr, nullptr);
    thread_br_joiner_set_discovery_callback(nullptr, nullptr);
    if (owns_) otCommissionerStop(instance);
    esp_openthread_lock_release();
  }
}
void CommissionerManager::dump_config() {
  ESP_LOGCONFIG(TAG, "Commissioner enabled, ID: %s; candidates: 32, TTL: 120s", id_.c_str());
}
}  // namespace esphome::thread_br
#endif
