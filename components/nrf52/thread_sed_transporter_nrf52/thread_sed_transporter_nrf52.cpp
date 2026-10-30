#include "thread_sed_transporter_nrf52.h"
#include <algorithm>
#include <cstring>
#include <new>
#include <cstdio>
#include <cctype>
#include "esphome/core/log.h"
#include "esphome/core/hal.h"
#include "esphome/core/wake.h"
#include "openthread/thread.h"
#include "openthread/link.h"
#include "openthread/ip6.h"
#include "openthread/message.h"

namespace esphome::thread_sed_transporter_nrf52 {
static const char *const TAG = "thread_sed_transporter_nrf52";
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
  k_msgq_init(&packets_, queue_buffer_, sizeof(Packet *), RX_QUEUE_SIZE);
  queue_initialized_ = true;
  uint32_t random[2];
  if (!random_bytes(reinterpret_cast<uint8_t *>(random), sizeof(random))) {
    ESP_LOGE(TAG, "Cannot generate transport session"); mark_failed(); return;
  }
  char session[17]; snprintf(session, sizeof(session), "%08x%08x", unsigned(random[0]), unsigned(random[1]));
#ifdef USE_DEEP_SLEEP_NRF52
  if (sleep_) sleep_->add_sleep_guard([this]() { return !this->is_busy() && !poll_overridden_; });
#endif
  session_ = session;
}
void ThreadSedTransporter::receive_(void *ctx, otMessage *message, const otMessageInfo *info) {
  auto *self = static_cast<ThreadSedTransporter *>(ctx);
  if (self->shutdown_ || !self->queue_initialized_) return;
  const int length = otMessageGetLength(message) - otMessageGetOffset(message);
  if (length <= 0 || length > 1232) { self->drops_++; return; }
  auto *p = new (std::nothrow) Packet;
  if (!p) { self->drops_++; return; }
  p->peer = info->mPeerAddr; p->port = info->mPeerPort; p->size = length;
  if (otMessageRead(message, otMessageGetOffset(message), p->data, length) != length) { delete p; self->drops_++; return; }
  p->data[length] = 0;
  if (k_msgq_put(&self->packets_, &p, K_NO_WAIT) != 0) { delete p; self->drops_++; return; }
  // A core notification resumes main without cancelling a pending sleep request.
  esphome::wake_loop_threadsafe();
}
bool ThreadSedTransporter::open_() {
  if (opened_) return true;
  if (shutdown_ || !ot_ || !ot_->is_lock_initialized() || !ot_->is_connected()) return false;
  auto lock = openthread_nrf52::InstanceLock::try_acquire(0);
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
bool ThreadSedTransporter::send_(const otIp6Address &address, uint16_t port, const std::string &payload) {
  if (shutdown_ || !opened_ || payload.empty() || payload.size() > 1232 ||
      !ot_ || !ot_->is_lock_initialized()) return false;
  auto lock = openthread_nrf52::InstanceLock::try_acquire(0);
  if (!lock) return false;
  auto *instance = lock.get_instance();
  bool success = false;
  if (instance && otThreadGetDeviceRole(instance) >= OT_DEVICE_ROLE_CHILD) {
    otMessageInfo info{}; info.mPeerAddr = address; info.mPeerPort = port;
    info.mSockAddr = *otThreadGetMeshLocalEid(instance); info.mHopLimit = 64;
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
  return retained.port && retained.gateway[0] && uint64_t(k_uptime_get()) < retained.expires &&
      (expected_gateway_.empty() || expected_gateway_ == retained.gateway);
}
void ThreadSedTransporter::renew_target_() {
  if (!retained.ttl_s) return;
  retained.expires = uint64_t(k_uptime_get()) + uint64_t(retained.ttl_s) * 1000ULL;
  retain();
}
void ThreadSedTransporter::discovery() {
  // Keep staged samples and frozen reports. Discovery never starts acquisition.
  retained.expires = 0; retain();
  confirmed_ = false; querying_ = false;
  query_attempts_ = 0; manifest_attempts_ = 0;
  discovery_active_ = true; discovery_started_ = millis();
  for (auto &ack : pending_acks_) {
    if (ack.result == PendingAck::Result::WAITING && !ack.recovering) {
      ack.recovering = true; ack.recovery_at = discovery_started_;
    }
  }
  ESP_LOGD(TAG, "Gateway rediscovery requested");
  if (!shutdown_ && !is_failed()) ensure_target_();
}
cJSON *ThreadSedTransporter::envelope_(const char *type) const {
  auto *j = cJSON_CreateObject();
  cJSON_AddNumberToObject(j, "v", 1); cJSON_AddStringToObject(j, "type", type);
  cJSON_AddStringToObject(j, "node_type", "sed");
  cJSON_AddStringToObject(j, "node", node_.c_str()); cJSON_AddStringToObject(j, "session", session_.c_str());
  return j;
}
bool ThreadSedTransporter::ensure_target_() {
  if (target_valid_()) return true;
  if (confirmed_) {
    ESP_LOGD(TAG, "Gateway target expired; rediscovering before the next report");
    query_attempts_ = 0;
    manifest_attempts_ = 0;
    querying_ = false;
  }
  confirmed_ = false;
  if (!open_()) return false;
  const uint32_t now = millis();
  if (querying_ && uint32_t(now - query_at_) < discovery_timeout_) return false;
  if (query_attempts_ >= 3) return false;
  query_at_ = now; querying_ = true; request_id_ = random_uint32(); query_attempts_++;
  auto *j = cJSON_CreateObject();
  cJSON_AddNumberToObject(j, "v", 1); cJSON_AddStringToObject(j, "type", "discover");
  cJSON_AddNumberToObject(j, "request_id", request_id_);
  otIp6Address group{};
  if (otIp6AddressFromString(group_.c_str(), &group) == OT_ERROR_NONE) send_(group, discovery_port_, encode(j));
  cJSON_Delete(j); return false;
}
bool ThreadSedTransporter::prepare_manifest_() {
  std::vector<std::string> descriptors;
  for (const auto *e : sensors_) {
    auto *j = e->descriptor();
    if (!j) return false;
    if (str(j, "name").empty()) cJSON_ReplaceItemInObjectCaseSensitive(j, "name", cJSON_CreateString(str(j, "id").c_str()));
    if (str(j, "name").size() > 96 || !thread_entity_bridge_nrf52::validate_capabilities(str(j, "type"), item(j, "capabilities"))) {
      cJSON_Delete(j); return false;
    }
    descriptors.push_back(encode(j)); cJSON_Delete(j);
  }
  for (const auto &c : controls_) {
    auto *j = c.descriptor();
    if (!j) return false;
    if (str(j, "name").empty()) cJSON_ReplaceItemInObjectCaseSensitive(j, "name", cJSON_CreateString(str(j, "id").c_str()));
    if (str(j, "name").size() > 96 || !thread_entity_bridge_nrf52::validate_capabilities(str(j, "type"), item(j, "capabilities"))) {
      cJSON_Delete(j); return false;
    }
    descriptors.push_back(encode(j)); cJSON_Delete(j);
  }
  auto pack = [this](const std::vector<std::string> &page, unsigned index, unsigned parts) {
    auto *j = envelope_("manifest");
    cJSON_AddStringToObject(j, "name", name_.c_str()); cJSON_AddNumberToObject(j, "revision", revision_);
    cJSON_AddNumberToObject(j, "part", index); cJSON_AddNumberToObject(j, "parts", parts);
    auto *a = cJSON_AddArrayToObject(j, "entities");
    for (const auto &d : page) cJSON_AddItemToArray(a, cJSON_Parse(d.c_str()));
    std::string payload = encode(j); cJSON_Delete(j); return payload;
  };
  std::vector<std::vector<std::string>> pages(1);
  for (const auto &d : descriptors) {
    pages.back().push_back(d);
    if (pack(pages.back(),15,16).size() > 1232) {
      pages.back().pop_back();
      if (pages.back().empty() || pages.size() >= 16) return false;
      pages.push_back({d});
      if (pack(pages.back(),15,16).size() > 1232) return false;
    }
  }
  manifest_.clear();
  for (unsigned k = 0; k < pages.size(); k++) manifest_.push_back(pack(pages[k], k, pages.size()));
  return true;
}
void ThreadSedTransporter::service_manifest_() {
  // Revalidate TTL even after the manifest was confirmed. Otherwise an expired
  // target leaves confirmed_ set forever, blocking both discovery and reports.
  if (!ensure_target_() || confirmed_ || !opened_) return;
  if (manifest_.empty() && !prepare_manifest_()) {
    ESP_LOGE(TAG, "Manifest cannot fit UDP limits or control traits are invalid");
    if (reporting_) { summary_.errors++; finish_(); }
    return;
  }
  if (manifest_attempts_ && uint32_t(millis() - manifest_at_) < 400) return;
  if (manifest_attempts_ >= 3) return;
  manifest_at_ = millis(); manifest_attempts_++;
  for (const auto &payload : manifest_) if (!send_(retained.address, retained.port, payload)) break;
}
void ThreadSedTransporter::process_(const Packet &p) {
  auto *j = cJSON_ParseWithLengthOpts(p.data, p.size + 1, nullptr, true);
  if (!cJSON_IsObject(j) || !uint_value(item(j, "v"), 1, 1)) { cJSON_Delete(j); return; }
  const std::string type = str(j, "type");
  if (type == "offer") {
    const std::string gateway = str(j, "gateway_id"), address = str(j, "address");
    const auto *request = item(j, "request_id");
    otIp6Address target{};
    bool valid_id = !gateway.empty() && gateway.size() <= 32;
    for (unsigned char c : gateway) valid_id &= std::isalnum(c) || c == '_' || c == '-';
    if (querying_ && p.port == discovery_port_ && uint_value(request) &&
        uint32_t(request->valuedouble) == request_id_ && valid_id &&
        (expected_gateway_.empty() || gateway == expected_gateway_) &&
        uint_value(item(j, "port"), 1, 65535) && uint_value(item(j, "ttl_s"), 1, 86400) &&
        otIp6AddressFromString(address.c_str(), &target) == OT_ERROR_NONE &&
        std::memcmp(&target, &p.peer, sizeof(target)) == 0) {
      if (gateway != retained.gateway) commands_.clear();
      retained.address = target; retained.port = item(j, "port")->valuedouble;
      std::memset(retained.gateway, 0, sizeof(retained.gateway));
      std::memcpy(retained.gateway, gateway.data(), gateway.size());
      retained.ttl_s = uint32_t(item(j, "ttl_s")->valuedouble);
      renew_target_();
      request_full_sync_(); retain();
      querying_ = false; confirmed_ = false; manifest_attempts_ = 0;
      ESP_LOGD(TAG, "Discovered gateway '%s', target TTL %us", gateway.c_str(), unsigned(item(j, "ttl_s")->valuedouble));
    }
    cJSON_Delete(j); return;
  }
  if (type == "report_ack") {
    receive_report_ack_(p, j);
    cJSON_Delete(j); return;
  }
  if (!target_valid_() || p.port != retained.port ||
      std::memcmp(&p.peer, &retained.address, sizeof(p.peer)) != 0 ||
      str(j, "node") != node_ || str(j, "session") != session_ || str(j, "gateway_id") != retained.gateway) {
    cJSON_Delete(j); return;
  }
  if (type == "manifest_request") {
    // Already validated the current gateway endpoint, node ID and boot session.
    confirmed_ = false; manifest_attempts_ = 0;
    request_full_sync_(); retain();
    ESP_LOGD(TAG, "Gateway requested a fresh manifest");
  } else if (type == "manifest_ack" && uint_value(item(j, "revision"), revision_, revision_)) {
    if (cJSON_IsTrue(item(j, "accepted")) && cJSON_IsTrue(item(j, "complete"))) {
      if (!confirmed_) presence_dirty_ = true;
      confirmed_ = true;
      last_gateway_reply_ = millis();
      renew_target_();
    } else if (cJSON_IsFalse(item(j, "accepted"))) {
      ESP_LOGW(TAG, "Gateway rejected manifest: %s", str(j, "error").c_str());
      if (reporting_) { summary_.errors++; finish_(); }
      manifest_attempts_ = 3;
    }
  } else if (type == "command") {
    last_gateway_reply_ = millis();
    command_(j);
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
      if (description && thread_entity_bridge_nrf52::validate_value(
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
  need_ack_ = report_ack_timeout_ != 0; ack_tracked_ = false; ack_immediate_failure_ = false;
  report_id_ = active_.id;
  query_attempts_ = 0; manifest_attempts_ = 0;
}
bool ThreadSedTransporter::is_busy() const {
  if (reporting_ || !requests_.empty() || !pending_acks_.empty() || !results_.empty() ||
      !terminal_.empty() || dispatching_ || discovery_active_ || ready_() ||
      (queue_initialized_ && k_msgq_num_used_get(const_cast<k_msgq *>(&packets_)))) return true;
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
  auto lock = openthread_nrf52::InstanceLock::try_acquire(0);
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
    if (encode(packet).size() > 1232) {
      value = cJSON_DetachItemFromObjectCaseSensitive(values, id.c_str());
      if (!values->child) { summary_.errors++; cJSON_Delete(value); return false; }
      if (!save_page()) { cJSON_Delete(value); return false; }
      cJSON_Delete(packet); packet = make_packet();
      values = cJSON_GetObjectItemCaseSensitive(packet, "values");
      commits.clear();
      cJSON_AddItemToObject(values, id.c_str(), value);
      if (encode(packet).size() > 1232) { summary_.errors++; return false; }
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
  for (size_t k = 0; k < pages.size(); k++) {
    auto *j = cJSON_Parse(pages[k].payload.c_str());
    if (!j) { summary_.errors++; encoded = false; break; }
    cJSON_SetNumberValue(cJSON_GetObjectItemCaseSensitive(j, "seq"), ++sequence_);
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
  ack.id = report_id_; ack.started = ack.attempt_at = millis(); ack.parts = payloads.size();
  ack.timeout = report_ack_timeout_; ack.resend_limit = report_ack_resend_;
  ack.payloads = std::move(payloads); ack.commits = std::move(commits);
  ack.order = active_.order; ack.full_generation = generation;
  ack.peer = retained.address; ack.port = retained.port; ack.gateway = retained.gateway;
  pending_acks_.push_back(std::move(ack));
}
void ThreadSedTransporter::receive_report_ack_(const Packet &packet, const cJSON *json) {
  if (str(json, "node") != node_ || str(json, "session") != session_ ||
      !cJSON_IsTrue(item(json, "complete")) || !uint_value(item(json, "report_id"), 1) ||
      !uint_value(item(json, "parts"), 1, 16)) return;
  for (auto &ack : pending_acks_) {
    if (ack.result != PendingAck::Result::WAITING || ack.id != uint32_t(item(json, "report_id")->valuedouble) ||
        ack.parts != item(json, "parts")->valuedouble || packet.port != ack.port ||
        std::memcmp(&packet.peer, &ack.peer, sizeof(packet.peer)) != 0 || str(json, "gateway_id") != ack.gateway)
      continue;
    // Accept this round's ACK throughout its bounded initial/retry window, once only.
    if (uint32_t(millis() - ack.started) < ack.timeout * (uint32_t(ack.resend_limit) + 1)) {
      ack.result = PendingAck::Result::SUCCESS;
      last_gateway_reply_ = millis();
      if (packet.port == retained.port && str(json, "gateway_id") == retained.gateway &&
          std::memcmp(&packet.peer, &retained.address, sizeof(packet.peer)) == 0) renew_target_();
    }
    return;
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
      retained.expires = 0; retain(); confirmed_ = false;
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
           !success ? "failed" : acknowledged ? "ACK received" : "local send/filter complete (ACK disabled)");
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
    if (!confirmed_) { retained.expires = 0; querying_ = false; }
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
  if (!confirmed_ || !target_valid_()) return;
  auto *j = envelope_("presence");
  // Solicit a bounded heartbeat reply without sending another full manifest.
  cJSON_AddBoolToObject(j, "check_manifest", true);
  cJSON_AddNumberToObject(j, "revision", revision_);
  cJSON_AddBoolToObject(j,"control_ready",ready);
  if (sleeping) cJSON_AddBoolToObject(j,"sleeping",true);
  if (send_(retained.address,retained.port,encode(j))) { heartbeat_at_ = millis(); presence_dirty_ = false; }
  cJSON_Delete(j);
}
void ThreadSedTransporter::service_presence_() {
  const bool ready = ready_();
  if (ready != last_ready_) {
    last_ready_ = ready; presence_dirty_ = true;
    if (ready) { query_attempts_ = 0; manifest_attempts_ = 0; }
  }
  if (discovery_active_ && (confirmed_ || uint32_t(millis() - discovery_started_) >= timeout_ms_)) {
    if (!confirmed_) ESP_LOGW(TAG, "Manual/recovery gateway discovery timed out");
    discovery_active_ = false;
  }
  if (!ready && !reporting_ && !discovery_active_) {
    if (presence_dirty_) presence_(false);
    return;
  }
  if (!open_()) return;
  // An online node continues discovering after bounded attempts, without a busy loop.
  if (!reporting_ && query_attempts_ >= 3 && uint32_t(millis() - query_at_) >= heartbeat_ms_) {
    query_attempts_ = 0; querying_ = false; manifest_attempts_ = 0;
  }
  if (!reporting_ && manifest_attempts_ >= 3 && !confirmed_ &&
      uint32_t(millis() - manifest_at_) >= heartbeat_ms_) {
    retained.expires = 0; retain(); query_attempts_ = 0; querying_ = false; manifest_attempts_ = 0;
  }
  service_manifest_();
  if (confirmed_ && (presence_dirty_ || (ready && uint32_t(millis() - heartbeat_at_) >= heartbeat_ms_)))
    presence_(ready);
}
void ThreadSedTransporter::loop() {
  service_results_();
  if (is_failed() || shutdown_) {
    if (reporting_) { summary_.errors++; finish_(); }
    for (const auto &r : results_) terminal_.push_back({false, r.id});
    requests_.clear(); pending_acks_.clear(); service_results_(); service_poll_(); return;
  }
  start_next_();
  service_poll_();
  Packet *p;
  for (unsigned k = 0; k < 4 && k_msgq_get(&packets_, &p, K_NO_WAIT) == 0; k++) {
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
    auto lock = openthread_nrf52::InstanceLock::try_acquire(0);
    if (!lock || !lock.get_instance()) return false;
    otUdpClose(lock.get_instance(), &socket_); opened_ = false;
  }
  Packet *packet;
  if (queue_initialized_) while (k_msgq_get(&packets_, &packet, K_NO_WAIT) == 0) delete packet;
  return true;
}
void ThreadSedTransporter::on_shutdown() {
  if (!shutdown_) presence_(false, true);
  shutdown_ = true;
  close_();
}
bool ThreadSedTransporter::teardown() { on_shutdown(); return close_(); }
void ThreadSedTransporter::dump_config() {
  ESP_LOGCONFIG(TAG, "Thread SED sender '%s': event-driven, source-managed updates, UDP only", node_.c_str());
  ESP_LOGCONFIG(TAG, "  Read-only=%u controls=%u timeout=%ums",
      unsigned(sensors_.size()),unsigned(controls_.size()),unsigned(timeout_ms_));
  for (auto *e : sensors_) e->dump_config();
}

void ThreadSedTransporter::command_(const cJSON *j) {
  if (!uint_value(item(j,"command_id"))) return;
  const uint32_t id = item(j,"command_id")->valuedouble;
  for (const auto &c : commands_) if (c.id == id) {
    send_(retained.address,retained.port,c.response); return;
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
  if (commands_.size() >= 32) commands_.erase(commands_.begin());
  commands_.push_back({id,wire});
  send_(retained.address,retained.port,wire);
}

}  // namespace esphome::thread_sed_transporter_nrf52
