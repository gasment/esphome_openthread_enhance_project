#include "thread_sed_transporter.h"
#include <algorithm>
#include <cstring>
#include <new>
#include <cstdio>
#include <cctype>
#include "esphome/core/log.h"
#include "esphome/core/hal.h"
#include "esphome/core/wake.h"
#include "esp_timer.h"
#include "openthread/thread.h"
#include "openthread/link.h"
#include "openthread/ip6.h"
#include "openthread/message.h"
#include "openthread/srp_client.h"

namespace esphome::thread_sed_transporter {
static const char *const TAG = "thread_sed_transporter";
static constexpr size_t MAX_PENDING_REPORT_ACKS = 8;
namespace {
const cJSON *item(const cJSON *j, const char *key) { return cJSON_GetObjectItemCaseSensitive(j, key); }
std::string str(const cJSON *j, const char *key) {
  const auto *v = item(j, key);
  return cJSON_IsString(v) && v->valuestring ? v->valuestring : "";
}
bool uint_value(const cJSON *j, uint32_t min = 0, uint32_t max = UINT32_MAX) {
  return cJSON_IsNumber(j) && std::isfinite(j->valuedouble) && j->valuedouble >= min &&
      j->valuedouble <= max && std::floor(j->valuedouble) == j->valuedouble;
}
std::string encode(const cJSON *j) {
  char *raw = cJSON_PrintUnformatted(j);
  if (!raw) return "";
  std::string result(raw); cJSON_free(raw); return result;
}

}

std::string ReportEntry::snapshot() const {
  auto *value = endpoint_.read();
  const bool valid = valid_snapshot(value);
  if (!valid) {
    if (endpoint_.kind == "event") {
      ESP_LOGW(TAG, "Event '%s' rejected: trigger an event first or provide a valid entity_state", id_.c_str());
    } else if (!endpoint_.entity->has_state()) {
      ESP_LOGW(TAG, "Entity '%s' rejected: source has not published its first state", id_.c_str());
    } else if (!value) {
      ESP_LOGW(TAG, "Entity '%s' rejected: source value unavailable/non-finite or allocation failed", id_.c_str());
    } else if (cJSON_IsString(value) && value->valuestring) {
      auto *description = this->descriptor();
      const auto *limit = item(description, "max_length");
      ESP_LOGW(TAG, "Entity '%s' rejected: text length=%u exceeds max_length=%u", id_.c_str(),
               unsigned(std::strlen(value->valuestring)), unsigned(limit ? limit->valueint : 0));
      cJSON_Delete(description);
    } else {
      ESP_LOGW(TAG, "Entity '%s' rejected: invalid snapshot type/value", id_.c_str());
    }
  }
  std::string result = valid ? encode(value) : "";
  if (valid && result.empty()) ESP_LOGW(TAG, "Entity '%s' rejected: JSON encoding/allocation failed", id_.c_str());
  cJSON_Delete(value); return result;
}

void ReportEntry::dump_config() {
  ESP_LOGCONFIG(TAG, "  Bound entity '%s' (%s)", id_.c_str(), endpoint_.entity->get_name().c_str());
}

ReportOutcome ReportEntry::stage(const std::string &snapshot, bool force, uint64_t generation) {
  pending_ = snapshot;
  auto *value = cJSON_Parse(pending_.c_str());
  if (!valid_snapshot(value)) { cJSON_Delete(value); return ReportOutcome::INVALID; }
  const bool changed = force || confirmed_generation_ != generation || changed_(value);
  cJSON_Delete(value);
  return changed ? ReportOutcome::CHANGED : ReportOutcome::UNCHANGED;
}
void ReportEntry::confirm(const std::string &snapshot, uint64_t order, uint64_t generation) {
  if (order <= confirmed_order_) return;
  auto *value = cJSON_Parse(snapshot.c_str());
  if (valid_snapshot(value)) {
    commit_(value); confirmed_order_ = order; confirmed_generation_ = generation;
  }
  cJSON_Delete(value);
}
void ThreadSedTransporter::confirm_(const std::vector<Commit> &commits, uint64_t order, uint64_t generation) {
  // An ACK from before gateway resynchronization cannot establish the new baseline.
  if (generation != full_generation_) return;
  for (auto &commit : commits) {
    if (commit.entry) commit.entry->confirm(commit.snapshot, order, generation);
    else if (commit.control < controls_.size() && order > controls_[commit.control].confirmed_order) {
      controls_[commit.control].previous = commit.snapshot;
      controls_[commit.control].confirmed_order = order;
      controls_[commit.control].confirmed_generation = generation;
    }
  }
}

void ThreadSedTransporter::setup() {
  if (!ot_ || ot_->is_failed()) { mark_failed(); return; }
  packets_ = xQueueCreate(RX_QUEUE_SIZE, sizeof(Packet *));
  if (!packets_) { ESP_LOGE(TAG, "Cannot allocate RX queue"); mark_failed(); return; }
  queue_initialized_ = true;
  uint32_t random[2];
  if (!random_bytes(reinterpret_cast<uint8_t *>(random), sizeof(random))) {
    ESP_LOGE(TAG, "Cannot generate transport session"); mark_failed(); return;
  }
  char session[17]; snprintf(session, sizeof(session), "%08x%08x", unsigned(random[0]), unsigned(random[1]));
  session_ = session;
  request_nonce_ = thread_transport::nonce().substr(0, 16);
  if (request_nonce_.empty()) mark_failed();
}
void ThreadSedTransporter::receive_(void *ctx, otMessage *message, const otMessageInfo *info) {
  auto *self = static_cast<ThreadSedTransporter *>(ctx);
  if (self->shutdown_ || !self->queue_initialized_) return;
  const int length = otMessageGetLength(message) - otMessageGetOffset(message);
  if (length <= 0 || length > 1232) { self->drops_++; return; }
  auto *p = new (std::nothrow) Packet{};
  if (!p) { self->drops_++; return; }
  p->peer = info->mPeerAddr; p->port = info->mPeerPort; p->size = length;
  if (otMessageRead(message, otMessageGetOffset(message), p->data, length) != length) { delete p; self->drops_++; return; }
  p->data[length] = 0;
  if (xQueueSend(self->packets_, &p, 0) != pdTRUE) { delete p; self->drops_++; return; }
  // A core notification resumes main without cancelling a pending sleep request.
  esphome::wake_loop_threadsafe();
}
bool ThreadSedTransporter::open_() {
  if (opened_) return true;
  if (shutdown_ || !ot_ || !ot_->is_lock_initialized() || !ot_->is_connected()) return false;
  auto lock = openthread::InstanceLock::try_acquire(0);
  if (!lock) return false;
  auto *instance = lock.get_instance();
  bool success = false;
  if (instance && otThreadGetDeviceRole(instance) >= OT_DEVICE_ROLE_CHILD &&
      otUdpOpen(instance, &socket_, receive_, this) == OT_ERROR_NONE) {
    otSockAddr local{};
    success = otUdpBind(instance, &socket_, &local, OT_NETIF_THREAD) == OT_ERROR_NONE;
    if (!success) otUdpClose(instance, &socket_);
  }
  return opened_ = success;
}
bool ThreadSedTransporter::send_(const otIp6Address &address, uint16_t port, const std::string &original) {
  const auto payload = wire_payload_(original, port);
  if (shutdown_ || !opened_ || payload.empty() || payload.size() > 1232 ||
      !ot_ || !ot_->is_lock_initialized()) return false;
  auto lock = openthread::InstanceLock::try_acquire(0);
  if (!lock) return false;
  auto *instance = lock.get_instance();
  bool success = false;
  if (instance && otThreadGetDeviceRole(instance) >= OT_DEVICE_ROLE_CHILD) {
    otMessageInfo info{}; info.mPeerAddr = address; info.mPeerPort = port;
    /* Let OpenThread select a routable OMR source for the HA destination. */ info.mHopLimit = 64;
    if (auto *message = otUdpNewMessage(instance, nullptr)) {
      const auto append = otMessageAppend(message, payload.data(), payload.size());
      const auto result = append == OT_ERROR_NONE ? otUdpSend(instance, &socket_, message, &info) : append;
      success = result == OT_ERROR_NONE;
      if (!success) otMessageFree(message);
    }
  }
  return success;
}
bool ThreadSedTransporter::target_valid_() const {
  return retained.port && retained.gateway[0] && (session_ready_ || now_ms_() < retained.expires) &&
      (!binding_.data.owner[0] || std::string(binding_.data.owner) == retained.gateway) &&
      (expected_gateway_.empty() || expected_gateway_ == retained.gateway);
}

void ThreadSedTransporter::renew_target_() {
  // DNS TTL is not extended by application replies. Established sessions keep their
  // route until traffic fails; DNS refresh is only needed when establishing a route.
  retained.expires = now_ms_() + uint64_t(retained.ttl_s ? retained.ttl_s : 60) * 1000;
}

void ThreadSedTransporter::discovery() {
  reset_session_();
  retained.expires = 0;
  endpoints_.clear(); ++dns_epoch_;
  discovery_active_ = true; discovery_started_ = millis();
  dns_at_ = 0; query_attempts_ = 0;
  for (auto &ack : pending_acks_) {
    if (!ack.recovering) { ack.recovering = true; ack.recovery_at = millis(); }
  }
}

cJSON *ThreadSedTransporter::envelope_(const char *type) const {
  auto *j = cJSON_CreateObject();
  cJSON_AddNumberToObject(j, "v", 3); cJSON_AddStringToObject(j, "type", type);
  cJSON_AddStringToObject(j, "device", binding_.data.device);
  cJSON_AddStringToObject(j, "session", session_.c_str());
  if (binding_.data.owner[0]) {
    cJSON_AddStringToObject(j, "backend_id", binding_.data.owner);
    cJSON_AddStringToObject(j, "binding_id", binding_.data.token);
  }
  if (!context_.empty()) cJSON_AddStringToObject(j, "ctx", context_.c_str());
  return j;
}

bool ThreadSedTransporter::ensure_target_() {
  if (!open_() || !refresh_binding_()) return false;
  if (session_ready_ && retained.port) return true;
  const uint32_t now = millis();
  if (dns_pending_ == 0 && (!dns_at_ || uint32_t(now - dns_at_) >= 10000) &&
      (!binding_.data.owner[0] || !target_valid_())) start_dns_();
  // HELLO retry interval is independent of the slower DNS-SD proxy window.
  if (querying_ && uint32_t(now - query_at_) < 1000) return false;
  query_at_ = now; querying_ = true;
  auto *j = envelope_("hello");
  cJSON_DeleteItemFromObjectCaseSensitive(j, "ctx");
  cJSON_AddStringToObject(j, "node", node_.c_str()); cJSON_AddStringToObject(j, "name", name_.c_str());
  cJSON_AddStringToObject(j, "node_type", "sed"); cJSON_AddStringToObject(j, "network", network_.c_str());
  cJSON_AddNumberToObject(j, "boot", double(binding_.data.boot));
  cJSON_AddNumberToObject(j, "generation", binding_.data.generation);
  cJSON_AddStringToObject(j, "request_id", request_nonce_.c_str());
  if (!expected_gateway_.empty()) cJSON_AddStringToObject(j, "expected_backend", expected_gateway_.c_str());
  const auto payload = encode(j); cJSON_Delete(j);
  if (binding_.data.owner[0] && target_valid_()) {
    send_(retained.address, retained.port, payload);
  } else {
    for (const auto &endpoint : endpoints_) {
      if (now_ms_() >= endpoint.expires ||
          (binding_.data.owner[0] && endpoint.id != binding_.data.owner) ||
          (!expected_gateway_.empty() && endpoint.id != expected_gateway_)) continue;
      send_(endpoint.address, endpoint.port, payload);
    }
  }
  return false;
}

bool ThreadSedTransporter::prepare_manifest_() {
  auto *document = cJSON_CreateObject();
  cJSON_AddStringToObject(document, "name", name_.c_str());
  auto *entities = cJSON_AddArrayToObject(document, "entities");
  auto add = [&](cJSON *descriptor) {
    if (!descriptor) return false;
    if (str(descriptor, "name").empty())
      cJSON_ReplaceItemInObjectCaseSensitive(descriptor, "name", cJSON_CreateString(str(descriptor, "id").c_str()));
    const bool valid = str(descriptor, "name").size() <= 96 &&
        thread_entity_bridge::validate_capabilities(str(descriptor, "type"), item(descriptor, "capabilities"));
    if (valid) cJSON_AddItemToArray(entities, descriptor); else cJSON_Delete(descriptor);
    return valid;
  };
  for (const auto *entry : sensors_) if (!add(entry->descriptor())) { cJSON_Delete(document); return false; }
  for (const auto &control : controls_) if (!add(control.descriptor())) { cJSON_Delete(document); return false; }
  const auto raw = encode(document); cJSON_Delete(document);
  if (raw.empty() || raw.size() > 65536) return false;
  manifest_hash_ = thread_ha::sha256(raw);
  manifest_.clear();
  const size_t parts = (raw.size() + 511) / 512;
  for (size_t part = 0; part < parts; ++part) {
    auto *j = envelope_("manifest");
    cJSON_AddStringToObject(j, "manifest_hash", manifest_hash_.c_str());
    cJSON_AddNumberToObject(j, "part", part); cJSON_AddNumberToObject(j, "parts", parts);
    cJSON_AddStringToObject(j, "payload", thread_ha::base64(raw.substr(part * 512, 512)).c_str());
    auto payload = encode(j); cJSON_Delete(j);
    if (payload.size() + thread_ha::HEADER_SIZE > 1232) { manifest_.clear(); return false; }
    manifest_.push_back(std::move(payload));
  }
  return true;
}

void ThreadSedTransporter::service_manifest_() {
  if (!ensure_target_() || confirmed_) return;
  if (manifest_.empty() && !prepare_manifest_()) {
    ESP_LOGE(TAG, "HA manifest is invalid or exceeds protocol limits");
    if (reporting_) { ++summary_.errors; finish_(); }
    return;
  }
  if (manifest_attempts_ && uint32_t(millis() - manifest_at_) < 500) return;
  manifest_at_ = millis(); if (manifest_attempts_ < 255) ++manifest_attempts_;
  for (const auto &payload : manifest_) if (!send_(retained.address, retained.port, payload)) break;
}

void ThreadSedTransporter::process_(const Packet &p) {
  if (p.internal) { process_dns_(p); return; }
  std::string signature, raw;
  auto *j = thread_ha::parse(p.data, p.size, signature, raw);
  if (!j) return;
  const auto type = str(j, "type");
  bool endpoint = p.port == retained.port && std::memcmp(&p.peer, &retained.address, sizeof(p.peer)) == 0;
  bool discovered = false;
  for (const auto &e : endpoints_) if (now_ms_() < e.expires && e.port == p.port &&
      std::memcmp(&p.peer, &e.address, sizeof(p.peer)) == 0 && e.id == str(j, "backend_id")) discovered = true;
  if (str(j, "device") != binding_.data.device || str(j, "session") != session_) { cJSON_Delete(j); return; }
  if (type == "bind_offer") {
    const auto owner = str(j, "backend_id"), token = str(j, "binding_id"), secret = str(j, "secret");
    if (!discovered || str(j, "node") != node_ || str(j, "request_id") != request_nonce_ || signature != "-" ||
        (!expected_gateway_.empty() && owner != expected_gateway_) || !uint_value(item(j, "generation"), 1)) {
      cJSON_Delete(j); return;
    }
    bool saved = false;
    { auto lock = openthread::InstanceLock::try_acquire(0);
      if (lock && lock.get_instance()) saved = binding_.bind(lock.get_instance(), owner, token, secret,
                                                          uint32_t(item(j, "generation")->valuedouble)); }
    if (saved) {
      ESP_LOGI(TAG, "HA binding persisted: %s", owner.c_str());
      set_target_(p, owner);
      auto *ack = envelope_("bind_ack"); cJSON_AddStringToObject(ack, "request_id", request_nonce_.c_str());
      send_(p.peer, p.port, encode(ack)); cJSON_Delete(ack);
    }
    cJSON_Delete(j); return;
  }
  if (!binding_.data.owner[0] || str(j, "backend_id") != binding_.data.owner ||
      str(j, "binding_id") != binding_.data.token ||
      !thread_ha::equal(signature, thread_ha::hmac(raw, binding_.data.secret))) { cJSON_Delete(j); return; }
  if (type == "binding_revoked") {
    if (endpoint || discovered) unbind_requested_ = true;
  } else if (type == "context_expired") {
    if (endpoint) reset_session_();
  } else if (type == "bind_complete") {
    if (endpoint && str(j, "request_id") == request_nonce_) reset_session_();
  } else if (type == "session_offer") {
    if ((endpoint || discovered) && str(j, "request_id") == request_nonce_ &&
        uint_value(item(j, "generation")) && item(j, "generation")->valuedouble == binding_.data.generation &&
        thread_ha::hex_id(str(j, "ctx"), 16)) {
      if (context_ != str(j, "ctx")) {
        reset_session_(false); context_ = str(j, "ctx");
      }
      set_target_(p, binding_.data.owner);
      auto *ack = envelope_("session_ack"); cJSON_AddStringToObject(ack, "request_id", request_nonce_.c_str());
      send_(p.peer, p.port, encode(ack)); cJSON_Delete(ack);
    }
  } else if (endpoint && !context_.empty() && str(j, "ctx") == context_) {
    last_gateway_reply_ = millis();
    if (type == "session_ready") {
      if (!session_ready_) {
        ESP_LOGD(TAG, "HA session ready: %s", binding_.data.owner);
        session_ready_ = true; confirmed_ = false; manifest_attempts_ = 0;
        request_full_sync_();
        if (prepare_manifest_() && str(j, "manifest_hash") == manifest_hash_) confirmed_ = true;
        rebase_reports_(); presence_dirty_ = true;
      }
    } else if (session_ready_) {
      if (type == "report_ack") receive_report_ack_(p, j);
      else if (type == "manifest_request" || (type == "presence_ack" && cJSON_IsTrue(item(j, "request_manifest")))) {
        confirmed_ = false; manifest_attempts_ = 0; request_full_sync_();
      } else if (type == "manifest_ack" && str(j, "manifest_hash") == manifest_hash_ && cJSON_IsTrue(item(j, "complete"))) {
        confirmed_ = true; presence_dirty_ = true;
      } else if (type == "command") command_(j);
    }
  }
  cJSON_Delete(j);
}

void ThreadSedTransporter::send(const std::vector<std::string> &entity_ids, bool force,
                                uint32_t timeout, uint8_t resend, ResultCallback callback) {
  std::vector<SendValue> values;
  for (const auto &id : entity_ids) values.push_back({id, false, {}});
  send_values(values, force, timeout, resend, std::move(callback));
}
void ThreadSedTransporter::send_values(const std::vector<SendValue> &values, bool force,
                                       uint32_t timeout, uint8_t resend, ResultCallback callback) {
  Request request{}; request.force = force; request.ack_timeout = timeout; request.ack_resend = resend;
  // Capture the event now, including queued sends; retries never read the source again.
  bool valid = !values.empty() && values.size() <= 16;
  for (const auto &input : values) {
    const auto &id = input.id;
    Snapshot snapshot; snapshot.id = id;
    for (auto *entry : sensors_) if (entry->identity() == id) {
      snapshot.entry = entry;
      if (input.overridden) {
        auto *value = cJSON_Parse(input.json.c_str());
        if (entry->valid_snapshot(value)) snapshot.value = encode(value);
        else ESP_LOGW(TAG, "Entity '%s' rejected: invalid override type/value or text max_length", id.c_str());
        cJSON_Delete(value);
      } else snapshot.value = entry->snapshot();
      break;
    }
    if (!snapshot.entry) for (size_t k = 0; k < controls_.size(); ++k) if (controls_[k].id == id) {
      snapshot.control = k;
      auto *value = input.overridden ? cJSON_Parse(input.json.c_str()) : controls_[k].read();
      auto *description = controls_[k].descriptor();
      if (description && thread_entity_bridge::validate_value(
          controls_[k].type, item(description, "capabilities"), value, false)) snapshot.value = encode(value);
      cJSON_Delete(description); cJSON_Delete(value); break;
    }
    if (!snapshot.entry && snapshot.control == SIZE_MAX)
      ESP_LOGW(TAG, "Entity '%s' rejected: not registered in this transporter", id.c_str());
    else if (!snapshot.entry && snapshot.value.empty())
      ESP_LOGW(TAG, "Entity '%s' rejected: control state unavailable or invalid", id.c_str());
    for (const auto &previous : request.snapshots) if (previous.id == id) {
      ESP_LOGW(TAG, "Entity '%s' rejected: duplicate entity in batch", id.c_str());
      valid = false;
    }
    if (snapshot.value.empty()) valid = false;
    request.snapshots.push_back(std::move(snapshot));
  }
  // Reject an invalid batch as a whole, rather than acknowledging a partial event.
  if (!valid) request.snapshots.clear();
  enqueue_(std::move(request), std::move(callback));
}
void ThreadSedTransporter::enqueue_(Request request, ResultCallback callback) {
  if (++next_report_id_ == 0) ++next_report_id_;
  request.id = next_report_id_;
  request.order = ++next_order_;
  const bool rejected = shutdown_ || is_failed() || results_.size() >= MAX_PENDING_REPORT_ACKS ||
                        request.snapshots.empty();
  results_.push_back({request.id, request.ack_timeout != 0, std::move(callback)});
  if (rejected) {
    const char *reason = shutdown_ ? "transporter shutting down" : is_failed() ? "transporter setup failed" :
                         results_.size() > MAX_PENDING_REPORT_ACKS ? "pending result queue full (limit=8)" :
                         "invalid/empty batch (see entity diagnostics above)";
    ESP_LOGW(TAG, "Report %u rejected: %s", unsigned(request.id), reason);
    terminal_.push_back({false, request.id});
    defer("sed-results", [this]() { service_results_(); });
    return;
  }
  requests_.push_back(std::move(request));
}
void ThreadSedTransporter::start_next_() {
  if (reporting_ || requests_.empty()) return;
  active_ = std::move(requests_.front()); requests_.pop_front();
  reporting_ = true; summary_ = {}; staged_ = false; started_ = millis();
  phase_ = Phase::SEND;
  report_ack_timeout_ = active_.ack_timeout; report_ack_resend_ = active_.ack_resend;
  need_ack_ = true;
  if (!report_ack_timeout_) { report_ack_timeout_ = 2000; report_ack_resend_ = 2; } ack_tracked_ = false; ack_immediate_failure_ = false;
  report_id_ = active_.id;
  query_attempts_ = 0; manifest_attempts_ = 0;
}
bool ThreadSedTransporter::is_busy() const {
  if (reporting_ || !requests_.empty() || !pending_acks_.empty() || !results_.empty() ||
      !terminal_.empty() || dispatching_ || discovery_active_ || dns_pending_ || ready_() ||
      (queue_initialized_ && uxQueueMessagesWaiting(packets_))) return true;
  for (const auto &guard : result_guards_) if (!guard()) return true;
  return false;
}
void ThreadSedTransporter::service_results_() {
  auto terminal = std::move(terminal_); terminal_.clear();
  dispatching_ = true;
  for (const auto &done : terminal) {
    auto it = std::find_if(results_.begin(), results_.end(), [&](const Result &r) { return r.id == done.id; });
    if (it == results_.end()) continue;
    auto result = std::move(*it); results_.erase(it);
    if (result.callback) result.callback(done.success, done.id);
    if (result.notify_global) {
      if (done.success) success_callbacks_.call(done.id);
      else fail_callbacks_.call(done.id);
    }
  }
  dispatching_ = false;
}
void ThreadSedTransporter::service_poll_() {
#if CONFIG_OPENTHREAD_MTD
  const bool busy = is_busy();
  if (!active_poll_ms_ || !ot_ || !ot_->is_lock_initialized() || busy == poll_overridden_) return;
  auto lock = openthread::InstanceLock::try_acquire(0);
  if (!lock || !lock.get_instance()) return;
  if (busy) {
    saved_poll_ms_ = ot_->get_poll_period();
    // MED already receives continuously; do not turn it into a SED.
    if (saved_poll_ms_ == 0) return;
    if (otLinkSetPollPeriod(lock.get_instance(), std::min(saved_poll_ms_, active_poll_ms_)) != OT_ERROR_NONE) return;
    poll_overridden_ = true;
  } else {
    if (otLinkSetPollPeriod(lock.get_instance(), ot_->get_poll_period()) != OT_ERROR_NONE) return;
    poll_overridden_ = false;
  }
#endif
}
void ThreadSedTransporter::stage_() {
  if (staged_) return;
  staged_ = true;
  for (const auto &snapshot : active_.snapshots) if (snapshot.entry) {
    snapshot.entry->outcome = snapshot.entry->stage(snapshot.value, active_.force, full_generation_);
    summary_.add(snapshot.entry->outcome);
  }
}
void ThreadSedTransporter::transmit_() {
  // Freeze values and baseline commits together; ACK confirms these exact bytes.
  struct Outgoing {
    std::string payload;
    std::vector<Commit> commits;
  };
  std::vector<Outgoing> pages;
  auto make_packet = [&]() {
    auto *j = envelope_("state");
    cJSON_AddNumberToObject(j, "seq", UINT32_MAX);
    cJSON_AddStringToObject(j, "manifest_hash", manifest_hash_.c_str());
    cJSON_AddBoolToObject(j, "snapshot", active_.snapshot);
    if (need_ack_) {
      cJSON_AddBoolToObject(j, "need_ack", true);
      cJSON_AddNumberToObject(j, "report_id", report_id_);
      // Reserve the largest page header; each entity fits wholly in one page.
      cJSON_AddNumberToObject(j, "part", 15);
      cJSON_AddNumberToObject(j, "parts", 16);
    }
    cJSON_AddObjectToObject(j, "values");
    return j;
  };
  auto *packet = make_packet();
  auto *values = cJSON_GetObjectItemCaseSensitive(packet, "values");
  std::vector<Commit> commits;
  auto save_page = [&]() {
    if (pages.size() >= 16) { summary_.errors++; return false; }
    auto payload = encode(packet);
    if (payload.empty()) { summary_.errors++; return false; }
    pages.push_back({std::move(payload), commits});
    return true;
  };
  auto add = [&](const std::string &id, cJSON *value, ReportEntry *entry, size_t control) {
    if (!value) { summary_.errors++; return false; }
    const std::string wire = encode(value);
    cJSON_AddItemToObject(values, id.c_str(), value);
    if (encode(packet).size() + thread_ha::HEADER_SIZE > 1232) {
      value = cJSON_DetachItemFromObjectCaseSensitive(values, id.c_str());
      if (!values->child) { summary_.errors++; cJSON_Delete(value); return false; }
      if (!save_page()) { cJSON_Delete(value); return false; }
      cJSON_Delete(packet); packet = make_packet();
      values = cJSON_GetObjectItemCaseSensitive(packet, "values");
      commits.clear();
      cJSON_AddItemToObject(values, id.c_str(), value);
      if (encode(packet).size() + thread_ha::HEADER_SIZE > 1232) { summary_.errors++; return false; }
    }
    if (entry || control != SIZE_MAX) commits.push_back({entry, control, wire});
    return true;
  };
  bool prepared = summary_.success();
  for (const auto &snapshot : active_.snapshots) {
    if (!prepared) break;
    if (snapshot.entry) {
      if (snapshot.entry->outcome != ReportOutcome::CHANGED) continue;
    } else {
      const auto &control = controls_[snapshot.control];
      if (!active_.force && control.confirmed_generation == full_generation_ && control.previous == snapshot.value) {
        summary_.unchanged++; continue;
      }
    }
    if (!add(snapshot.id, cJSON_Parse(snapshot.value.c_str()), snapshot.entry, snapshot.control)) {
      prepared = false; break;
    }
  }
  // An unchanged report still sends an empty round when a communication ACK was requested.
  if (prepared && (values->child || (need_ack_ && pages.empty()))) prepared = save_page();
  cJSON_Delete(packet);
  // Finalize every sequence number and byte string once. Retries replay these exact bytes.
  std::vector<std::string> payloads;
  bool encoded = true;
  const uint32_t report_sequence = ++sequence_;
  for (size_t k = 0; k < pages.size(); k++) {
    auto *j = cJSON_Parse(pages[k].payload.c_str());
    if (!j) { summary_.errors++; encoded = false; break; }
    cJSON_SetNumberValue(cJSON_GetObjectItemCaseSensitive(j, "seq"), report_sequence);
    if (need_ack_) {
      cJSON_SetNumberValue(cJSON_GetObjectItemCaseSensitive(j, "part"), k);
      cJSON_SetNumberValue(cJSON_GetObjectItemCaseSensitive(j, "parts"), pages.size());
    }
    auto payload = encode(j); cJSON_Delete(j);
    if (payload.empty()) { summary_.errors++; encoded = false; break; }
    payloads.push_back(std::move(payload));
  }
  ESP_LOGV(TAG, "Report %u: submitting %u state packet(s)", unsigned(report_id_), unsigned(payloads.size()));
  std::vector<Commit> report_commits;
  const uint64_t generation = full_generation_;
  if (encoded && prepared) for (size_t k = 0; k < pages.size(); k++) {
    auto &page = pages[k];
    // Track all frozen values: a locally failed send may be recovered by ACK retries.
    report_commits.insert(report_commits.end(), page.commits.begin(), page.commits.end());
    if (!send_(retained.address, retained.port, payloads[k])) {
      summary_.errors++; continue;
    }
    summary_.sent += page.commits.size();
    // Without ACK, each successfully submitted page is the delivery boundary.
    if (!need_ack_) confirm_(page.commits, active_.order, generation);
  }
  if (need_ack_) {
    track_report_ack_(encoded && prepared ? std::move(payloads) : std::vector<std::string>{},
                      std::move(report_commits), generation);
  }
  finish_();
}
void ThreadSedTransporter::track_report_ack_(std::vector<std::string> payloads, std::vector<Commit> commits,
                                            uint64_t generation) {
  ack_tracked_ = true;
  if (payloads.empty()) { ack_immediate_failure_ = true; return; }
  if (pending_acks_.size() >= MAX_PENDING_REPORT_ACKS) {
    ack_immediate_failure_ = true;
    ESP_LOGW(TAG, "Too many pending report ACKs; reporting this communication attempt as unconfirmed");
    return;
  }
  PendingAck ack{};
  ack.id = report_id_; ack.seq = sequence_; ack.started = ack.attempt_at = millis(); ack.parts = payloads.size();
  ack.timeout = report_ack_timeout_; ack.resend_limit = report_ack_resend_;
  ack.payloads = std::move(payloads); ack.commits = std::move(commits);
  ack.order = active_.order; ack.full_generation = generation;
  ack.peer = retained.address; ack.port = retained.port; ack.gateway = retained.gateway;
  pending_acks_.push_back(std::move(ack));
}
void ThreadSedTransporter::receive_report_ack_(const Packet &packet, const cJSON *json) {
  if (!cJSON_IsTrue(item(json, "complete")) || !uint_value(item(json, "report_id"), 1) || !uint_value(item(json, "seq"))) return;
  for (auto &ack : pending_acks_) {
    if (ack.result == PendingAck::Result::WAITING && !ack.recovering && ack.id == uint32_t(item(json, "report_id")->valuedouble) &&
        ack.seq == uint32_t(item(json, "seq")->valuedouble) && packet.port == ack.port &&
        std::memcmp(&packet.peer, &ack.peer, sizeof(packet.peer)) == 0 && str(json, "backend_id") == ack.gateway)
      ack.result = PendingAck::Result::SUCCESS;
  }
}

void ThreadSedTransporter::service_report_ack_() {
  // Erase before callbacks: automations may start a new report and append pending ACKs.
  std::vector<std::pair<bool, uint32_t>> finished;
  for (auto it = pending_acks_.begin(); it != pending_acks_.end();) {
    const uint32_t now = millis();
    const bool success = it->result == PendingAck::Result::SUCCESS;
    // One bounded rediscovery per frozen report; no resampling or baseline commits.
    if (!success && it->recovering) {
      if (uint32_t(now - it->recovery_at) >= timeout_ms_) {
        ESP_LOGW(TAG, "Report %u rediscovery timed out", unsigned(it->id));
        finished.emplace_back(false, it->id);
        it = pending_acks_.erase(it);
        continue;
      }
      if (!confirmed_ || !target_valid_() || !opened_) { ++it; continue; }
      it->peer = retained.address; it->port = retained.port; it->gateway = retained.gateway;
      it->full_generation = full_generation_;
      it->recovering = false; it->resends = 0;
      it->started = it->attempt_at = now;
      ESP_LOGD(TAG, "Report %u resubmitting frozen round after rediscovery", unsigned(it->id));
      for (const auto &payload : it->payloads) send_(it->peer, it->port, payload);
      ++it; continue;
    }
    const bool expired = uint32_t(now - it->started) >= it->timeout * (uint32_t(it->resend_limit) + 1);
    if (!success && expired && !it->recovered) {
      it->recovered = it->recovering = true;
      it->recovery_at = now;
      ESP_LOGW(TAG, "Report %u ACK attempts exhausted; rediscovering once", unsigned(it->id));
      discovery();
      ++it; continue;
    }
    if (!success && !expired) {
      if (uint32_t(now - it->attempt_at) < it->timeout) { ++it; continue; }
      if (it->resends < it->resend_limit) {
        ++it->resends;
        ESP_LOGD(TAG, "Report %u ACK timeout: retransmitting whole round %u/%u",
                 unsigned(it->id), unsigned(it->resends), unsigned(it->resend_limit));
        // No re-acquisition, baseline commit, local publication or summary mutation here.
        for (const auto &payload : it->payloads) send_(it->peer, it->port, payload);
        it->attempt_at = millis();
        ++it; continue;
      }
    }
    if (!success && it->port == retained.port &&
        std::memcmp(&it->peer, &retained.address, sizeof(it->peer)) == 0) {
      retained.expires = 0; reset_session_();
    }
    if (success) confirm_(it->commits, it->order, it->full_generation);
    finished.emplace_back(success, it->id);
    it = pending_acks_.erase(it);
  }
  for (const auto &result : finished) report_result_(result.first, result.second);
}
void ThreadSedTransporter::report_result_(bool success, uint32_t report_id) {
  const auto result = std::find_if(results_.begin(), results_.end(), [report_id](const Result &r) {
    return r.id == report_id;
  });
  const bool acknowledged = result != results_.end() && result->notify_global;
  ESP_LOGD(TAG, "Report %u: %s", unsigned(report_id),
           !success ? "failed" : acknowledged ? "ACK received" : "HA ACK received (global result callbacks disabled)");
  terminal_.push_back({success, report_id});
}
void ThreadSedTransporter::finish_() {
  if (!reporting_) return;
  phase_ = Phase::IDLE;
  if (need_ack_ && !ack_tracked_) track_report_ack_({});
  const bool ack_immediate_failure = ack_immediate_failure_;
  const uint32_t completed_id = report_id_;
  if (!summary_.success()) {
    status_set_warning();
    if (!confirmed_) querying_ = false;
    retain();
  } else status_clear_warning();
  ESP_LOGV(TAG, "Report %u submitted: UDP entities=%u unchanged=%u invalid=%u errors=%u (delivery not confirmed)",
           unsigned(completed_id), unsigned(summary_.sent),unsigned(summary_.unchanged),unsigned(summary_.invalid),unsigned(summary_.errors));
  // Keep the sleep guard set throughout callbacks; nested sends enter the queue.
  complete_callbacks_.call(summary_.success(), summary_.sent, summary_.unchanged, summary_.invalid, summary_.errors);
  reporting_ = false;
  if (ack_immediate_failure) report_result_(false, completed_id);
  else if (!need_ack_) report_result_(summary_.success(), completed_id);
}
void ThreadSedTransporter::presence_(bool ready, bool sleeping) {
  if (!session_ready_ || !retained.port) return;
  auto *j = envelope_("presence");
  cJSON_AddStringToObject(j, "manifest_hash", manifest_hash_.c_str());
  cJSON_AddNumberToObject(j, "heartbeat_interval_ms", heartbeat_ms_);
  cJSON_AddBoolToObject(j, "control_ready", ready && !sleeping);
  cJSON_AddBoolToObject(j, "sleeping", sleeping);
  uint32_t duration = sleep_for_s_;
#ifdef USE_DEEP_SLEEP_NRF52
  if (sleep_ && sleep_->get_sleep_duration().has_value()) duration = (uint64_t(*sleep_->get_sleep_duration()) + 999) / 1000;
#endif
  if (sleeping && duration) cJSON_AddNumberToObject(j, "sleep_for_s", std::min(duration, uint32_t(604800)));
  if (send_(retained.address, retained.port, encode(j))) { heartbeat_at_ = millis(); presence_dirty_ = false; }
  cJSON_Delete(j);
}

void ThreadSedTransporter::service_presence_() {
  const bool ready = ready_();
  if (ready != last_ready_) { last_ready_ = ready; presence_dirty_ = true; }
  if (discovery_active_ && (confirmed_ || uint32_t(millis() - discovery_started_) >= timeout_ms_)) discovery_active_ = false;
  if (ready || reporting_ || discovery_active_ || !pending_acks_.empty()) service_manifest_();
  if (confirmed_ && (presence_dirty_ || (ready && uint32_t(millis() - heartbeat_at_) >= heartbeat_ms_))) presence_(ready);
  if (confirmed_ && ready && uint32_t(millis() - last_gateway_reply_) > heartbeat_ms_ * 3) discovery();
  if (confirmed_ && full_sync_pending_ && !reporting_ && requests_.empty() && pending_acks_.empty()) queue_snapshot_();
}

void ThreadSedTransporter::loop() {
  if(!shutdown_ && !is_failed() && ot_ && ot_->is_connected()) refresh_binding_();
  service_results_();
  if (is_failed() || shutdown_) {
    if (reporting_) { summary_.errors++; finish_(); }
    for (const auto &r : results_) terminal_.push_back({false, r.id});
    requests_.clear(); pending_acks_.clear(); service_results_(); service_poll_(); return;
  }
  start_next_();
  service_poll_();
  Packet *p;
  for (unsigned k = 0; k < 4 && xQueueReceive(packets_, &p, 0) == pdTRUE; k++) {
    process_(*p); delete p;
  }
  service_report_ack_();
  if (shutdown_) return;
  service_presence_();
  if (!reporting_) { service_poll_(); return; }
  if (confirmed_ && target_valid_() && opened_) {
    // Filter the event snapshot against the latest confirmed baseline before submission.
    stage_(); transmit_();
  } else if (uint32_t(millis() - started_) >= timeout_ms_) {
    ESP_LOGW(TAG, "Report %u timed out before state submission", unsigned(report_id_));
    summary_.errors++; finish_();
  }
}
bool ThreadSedTransporter::close_() {
  if (opened_) {
    auto lock = openthread::InstanceLock::try_acquire(0);
    if (!lock || !lock.get_instance()) return false;
    otUdpClose(lock.get_instance(), &socket_); opened_ = false;
  }
  Packet *packet;
  if (queue_initialized_) while (xQueueReceive(packets_, &packet, 0) == pdTRUE) delete packet;
  return true;
}
void ThreadSedTransporter::on_shutdown() {
  if (!shutdown_) presence_(false, true);
  shutdown_ = true;
  close_();
}
bool ThreadSedTransporter::teardown() { on_shutdown(); return close_(); }
void ThreadSedTransporter::dump_config() {
  ESP_LOGCONFIG(TAG, "Thread SED sender '%s': HA v3, DNS-SD, authenticated IPv6 UDP", node_.c_str());
  ESP_LOGCONFIG(TAG, "  Read-only=%u controls=%u timeout=%ums",
      unsigned(sensors_.size()),unsigned(controls_.size()),unsigned(timeout_ms_));
  for (auto *e : sensors_) e->dump_config();
}

void ThreadSedTransporter::command_(const cJSON *j) {
  if (!uint_value(item(j,"command_id"), 1)) return;
  const uint32_t id = item(j,"command_id")->valuedouble;
  for (const auto &c : commands_) if (c.id == id) {
    send_(retained.address,retained.port,c.response); return;
  }
  if (commands_.size() >= 32) {
    auto *response = envelope_("command_result");
    cJSON_AddNumberToObject(response, "command_id", id);
    cJSON_AddBoolToObject(response, "success", false);
    cJSON_AddStringToObject(response, "error", "Command receipt capacity reached; retry after session renewal");
    send_(retained.address, retained.port, encode(response)); cJSON_Delete(response);
    reset_session_(); return;
  }
  const std::string entity = str(j,"entity"), operation = str(j,"operation");
  bool success = false; std::string error = "Unknown control entity";
  for (auto &c : controls_) if (c.id == entity) {
    if (!ready_()) error = "Node is not control-ready";
    else if (operation != (c.type == "button" ? "press" : "set")) error = "Invalid operation";
    else {
      success = c.execute(item(j,"value"));
      error = success ? "" : "Invalid value or source rejected control";
    }
    break;
  }
  auto *response = envelope_("command_result");
  cJSON_AddNumberToObject(response,"command_id",id); cJSON_AddBoolToObject(response,"success",success);
  if (!error.empty()) cJSON_AddStringToObject(response,"error",error.c_str());
  const std::string wire = encode(response); cJSON_Delete(response);
  // Results describe local dispatch; asynchronous hardware confirmation comes from later state reports.
  commands_.push_back({id,wire});
  send_(retained.address,retained.port,wire);
}


std::string ThreadSedTransporter::wire_payload_(const std::string &payload, uint16_t) const {
  return thread_ha::frame(payload, binding_.data.owner[0] ? binding_.data.secret : "");
}

bool ThreadSedTransporter::refresh_binding_() {
  if (!ot_ || !ot_->is_lock_initialized() || !ot_->is_connected()) return false;
  auto lock = openthread::InstanceLock::try_acquire(0);
  if (!lock || !lock.get_instance()) return false;
  const bool first = !binding_.loaded;
  if (!binding_.start(lock.get_instance())) { confirmed_ = session_ready_ = false; return false; }
  if (unbind_requested_) {
    if (!binding_.revoke(lock.get_instance())) return false;
    unbind_requested_ = false; reset_session_(); retained.expires = 0;
    std::memset(retained.gateway, 0, sizeof(retained.gateway));
    for (auto &ack : pending_acks_) terminal_.push_back({false, ack.id});
    pending_acks_.clear();
  }
  const auto network = thread_transport::network(lock.get_instance());
  if (first || network != network_) {
    network_ = network; reset_session_(); endpoints_.clear(); ++dns_epoch_;
    retained.expires = 0; discovery_active_ = true; discovery_started_ = millis(); dns_at_ = 0;
  }
  return true;
}

uint64_t ThreadSedTransporter::now_ms_() const { return uint64_t(esp_timer_get_time() / 1000); }
void ThreadSedTransporter::reset_session_(bool new_request) {
  context_.clear(); commands_.clear(); session_ready_ = confirmed_ = false;
  querying_ = false; manifest_attempts_ = 0; manifest_.clear();
  if (new_request) {
    request_nonce_ = thread_transport::nonce().substr(0, 16);
    if (request_nonce_.empty()) mark_failed();
  }
  for (auto &ack : pending_acks_) { ack.recovering = true; ack.recovery_at = millis(); }
}
void ThreadSedTransporter::set_target_(const Packet &p, const std::string &owner) {
  retained.address = p.peer; retained.port = p.port;
  snprintf(retained.gateway, sizeof(retained.gateway), "%s", owner.c_str());
  retained.ttl_s = 60;
  for (const auto &e : endpoints_) if (now_ms_() < e.expires && e.id == owner && e.port == p.port && std::memcmp(&e.address, &p.peer, sizeof(p.peer)) == 0)
    retained.ttl_s = uint32_t(std::max<uint64_t>(1, (e.expires - now_ms_()) / 1000));
  renew_target_();
}
void ThreadSedTransporter::queue_snapshot_() {
  Request request{}; request.force = true; request.snapshot = true;
  request.ack_timeout = 2000; request.ack_resend = 2;
  for (auto *entry : sensors_) {
    auto *descriptor = entry->descriptor(); const bool event = str(descriptor, "type") == "event"; cJSON_Delete(descriptor);
    if (event) continue;
    auto value = entry->snapshot(); if (!value.empty()) request.snapshots.push_back({entry->identity(), value, entry, SIZE_MAX});
  }
  for (size_t i = 0; i < controls_.size(); ++i) {
    const auto &control = controls_[i]; if (control.type == "button") continue;
    auto *value = control.read(); auto *descriptor = control.descriptor();
    if (descriptor && thread_entity_bridge::validate_value(control.type, item(descriptor, "capabilities"), value, false))
      request.snapshots.push_back({control.id, encode(value), nullptr, i});
    cJSON_Delete(value); cJSON_Delete(descriptor);
  }
  full_sync_pending_ = false;
  if (!request.snapshots.empty()) enqueue_(std::move(request), {});
}
void ThreadSedTransporter::rebase_reports_() {
  for (auto &ack : pending_acks_) {
    ack.seq = ++sequence_; ack.peer = retained.address; ack.port = retained.port; ack.gateway = retained.gateway;
    ack.full_generation = full_generation_;
    for (auto &payload : ack.payloads) {
      auto *json = cJSON_Parse(payload.c_str());
      cJSON_ReplaceItemInObjectCaseSensitive(json, "ctx", cJSON_CreateString(context_.c_str()));
      cJSON_SetNumberValue(cJSON_GetObjectItemCaseSensitive(json, "seq"), ack.seq);
      payload = encode(json); cJSON_Delete(json);
    }
  }
}
bool ThreadSedTransporter::queue_packet_(Packet *packet) {
  if (shutdown_ || !queue_initialized_) { delete packet; return false; }
  if (xQueueSend(packets_, &packet, 0) != pdTRUE) { delete packet; ++drops_; return false; }
  esphome::wake_loop_threadsafe(); return true;
}
void ThreadSedTransporter::start_dns_() {
  auto lock = openthread::InstanceLock::try_acquire(0);
  if (!lock || !lock.get_instance() || dns_pending_) return;
  auto *instance = lock.get_instance();
  auto *server = otSrpClientGetServerAddress(instance);
  if (!server || otIp6IsAddressUnspecified(&server->mAddress)) return;
  otDnsQueryConfig config{}; config.mServerSockAddr = *server; config.mServerSockAddr.mPort = 53;
  config.mResponseTimeout = discovery_timeout_; config.mMaxTxAttempts = 1;
  auto *query = new (std::nothrow) DnsQuery{this, dns_epoch_};
  if (!query) return;
  ++dns_pending_; dns_at_ = millis();
  char address[64]; otIp6AddressToString(&server->mAddress, address, sizeof(address));
  ESP_LOGD(TAG, "DNS-SD browse: server=[%s]:53 timeout=%ums service=%s", address, unsigned(discovery_timeout_), thread_ha::SERVICE);
  const auto error = otDnsClientBrowse(instance, thread_ha::SERVICE, dns_browse_, query, &config);
  if (error != OT_ERROR_NONE) {
    ESP_LOGW(TAG, "DNS-SD browse could not start: %s (%d)", otThreadErrorToString(error), int(error));
    --dns_pending_; delete query;
  }
}
void ThreadSedTransporter::dns_browse_(otError error, const otDnsBrowseResponse *response, void *context) {
  auto *query = static_cast<DnsQuery *>(context); auto *self = query->owner;
  unsigned found = 0;
  if (error == OT_ERROR_NONE && !self->shutdown_) {
    char label[64];
    for (unsigned i = 0; i < 8 && otDnsBrowseResponseGetServiceInstance(response, i, label, sizeof(label)) == OT_ERROR_NONE; ++i) {
      ++found;
      auto *json = cJSON_CreateObject(); cJSON_AddStringToObject(json, "instance", label);
      cJSON_AddNumberToObject(json, "epoch", query->epoch);
      auto wire = encode(json); cJSON_Delete(json);
      auto *packet = new (std::nothrow) Packet{};
      if (packet) { packet->internal = true; packet->size = wire.size(); std::memcpy(packet->data, wire.c_str(), wire.size() + 1); self->queue_packet_(packet); }
    }
  }
  if (error != OT_ERROR_NONE) ESP_LOGD(TAG, "DNS-SD browse failed: %s (%d)", otThreadErrorToString(error), int(error));
  else ESP_LOGD(TAG, "DNS-SD browse completed: %u instance(s)", found);
  --self->dns_pending_; delete query; esphome::wake_loop_threadsafe();
}
void ThreadSedTransporter::dns_service_(otError error, const otDnsServiceResponse *response, void *context) {
  auto *query = static_cast<DnsQuery *>(context); auto *self = query->owner;
  uint8_t txt[256]; char host[256]; otDnsServiceInfo info{};
  info.mTxtData = txt; info.mTxtDataSize = sizeof(txt); info.mHostNameBuffer = host; info.mHostNameBufferSize = sizeof(host);
  const auto parsed = !self->shutdown_ && error == OT_ERROR_NONE ? otDnsServiceResponseGetServiceInfo(response, &info) : error;
  bool accepted = false;
  if (!self->shutdown_ && parsed == OT_ERROR_NONE &&
      !info.mTxtDataTruncated && info.mPort && !otIp6IsAddressUnspecified(&info.mHostAddress) &&
      // Public OpenThread C API in Nordic SDK 2.9.2 has no link-local/multicast predicates.
      !(info.mHostAddress.mFields.m8[0] == 0xfe && (info.mHostAddress.mFields.m8[1] & 0xc0) == 0x80) &&
      info.mHostAddress.mFields.m8[0] != 0xff) {
    std::string id, version;
    otDnsTxtEntryIterator iterator; otDnsTxtEntry entry;
    otDnsInitTxtEntryIterator(&iterator, txt, info.mTxtDataSize);
    while (otDnsGetNextTxtEntry(&iterator, &entry) == OT_ERROR_NONE) {
      if (!entry.mKey || !entry.mValue) continue;
      if (!std::strcmp(entry.mKey, "id")) id.assign(reinterpret_cast<const char *>(entry.mValue), entry.mValueLength);
      if (!std::strcmp(entry.mKey, "v")) version.assign(reinterpret_cast<const char *>(entry.mValue), entry.mValueLength);
    }
    if (thread_ha::identifier(id) && version == "3" && info.mTtl && info.mHostAddressTtl) {
      accepted = true;
      auto *json = cJSON_CreateObject(); cJSON_AddStringToObject(json, "backend", id.c_str());
      cJSON_AddNumberToObject(json, "ttl", std::min(info.mTtl, info.mHostAddressTtl));
      cJSON_AddNumberToObject(json, "epoch", query->epoch);
      auto wire = encode(json); cJSON_Delete(json);
      auto *packet = new (std::nothrow) Packet{};
      if (packet) { packet->internal = true; packet->peer = info.mHostAddress; packet->port = info.mPort;
        packet->size = wire.size(); std::memcpy(packet->data, wire.c_str(), wire.size() + 1); self->queue_packet_(packet); }
    }
  }
  if (parsed != OT_ERROR_NONE) ESP_LOGD(TAG, "DNS-SD resolve failed: %s (%d)", otThreadErrorToString(parsed), int(parsed));
  else if (!accepted && !self->shutdown_) ESP_LOGD(TAG, "DNS-SD service ignored: require TXT v=3/id, SRV port and usable AAAA/TTL");
  --self->dns_pending_; delete query; esphome::wake_loop_threadsafe();
}
void ThreadSedTransporter::process_dns_(const Packet &packet) {
  auto *json = cJSON_Parse(packet.data);
  if (!json || !uint_value(item(json, "epoch")) || uint32_t(item(json, "epoch")->valuedouble) != dns_epoch_) { cJSON_Delete(json); return; }
  const auto label = str(json, "instance");
  if (!label.empty() && dns_pending_ < 8) {
    auto lock = openthread::InstanceLock::try_acquire(0);
    if (lock && lock.get_instance()) {
      auto *instance = lock.get_instance(); auto *server = otSrpClientGetServerAddress(instance);
      if (server && !otIp6IsAddressUnspecified(&server->mAddress)) {
        otDnsQueryConfig config{}; config.mServerSockAddr = *server; config.mServerSockAddr.mPort = 53;
        config.mResponseTimeout = discovery_timeout_; config.mMaxTxAttempts = 1;
        auto *query = new (std::nothrow) DnsQuery{this, dns_epoch_};
        if (query) { ++dns_pending_;
          char address[64]; otIp6AddressToString(&server->mAddress, address, sizeof(address));
          ESP_LOGD(TAG, "DNS-SD resolve: instance=%s server=[%s]:53 timeout=%ums", label.c_str(), address, unsigned(discovery_timeout_));
          const auto error = otDnsClientResolveServiceAndHostAddress(instance, label.c_str(), thread_ha::SERVICE, dns_service_, query, &config);
          if (error != OT_ERROR_NONE) {
            ESP_LOGW(TAG, "DNS-SD resolve could not start: %s (%d)", otThreadErrorToString(error), int(error));
            --dns_pending_; delete query;
          }
        }
      }
    }
  } else if (label.empty() && uint_value(item(json, "ttl"), 1)) {
    auto id = str(json, "backend");
    endpoints_.erase(std::remove_if(endpoints_.begin(), endpoints_.end(), [&](const Endpoint &e) {
      return now_ms_() >= e.expires || (e.id == id && e.port == packet.port && std::memcmp(&e.address, &packet.peer, sizeof(packet.peer)) == 0);
    }), endpoints_.end());
    if (endpoints_.size() < 8) endpoints_.push_back({id, packet.peer, packet.port, now_ms_() + uint64_t(item(json, "ttl")->valuedouble) * 1000});
    char address[64]; otIp6AddressToString(&packet.peer, address, sizeof(address));
    ESP_LOGD(TAG, "Discovered HA backend: %s at [%s]:%u", id.c_str(), address, unsigned(packet.port));
    querying_ = false;
  }
  cJSON_Delete(json);
}

}  // namespace esphome::thread_sed_transporter
