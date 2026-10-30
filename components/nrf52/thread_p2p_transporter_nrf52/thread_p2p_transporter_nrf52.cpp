#include "thread_p2p_transporter_nrf52.h"
#include <algorithm>
#include <cstring>
#include <new>
#include <utility>
#include "esphome/core/log.h"
#include "esphome/core/hal.h"
#include "openthread/message.h"
#include "openthread/thread.h"

namespace esphome::thread_p2p_transporter_nrf52 {
static const char *const TAG = "thread_p2p_transporter_nrf52";

// Version 1: P2P1 | kind | type | send_flag | options | session(8) |
// message_id(4) | payload_length(2) | reserved(2). Integers are network byte order.
static uint64_t read_be(const uint8_t *p, size_t count) {
  uint64_t value = 0;
  for (size_t i = 0; i < count; i++) value = (value << 8) | p[i];
  return value;
}
static void write_be(std::string &s, size_t pos, uint64_t value, size_t count) {
  for (size_t i = 0; i < count; i++) { s[pos + count - 1 - i] = char(value & 0xff); value >>= 8; }
}
static std::string header(uint8_t kind, uint8_t type, uint8_t flag, bool ack, uint64_t session,
                          uint32_t id, size_t size) {
  std::string wire(24, '\0');
  wire.replace(0, 4, "P2P1");
  wire[4] = kind; wire[5] = type; wire[6] = flag; wire[7] = ack ? 1 : 0;
  write_be(wire, 8, session, 8); write_be(wire, 16, id, 4); write_be(wire, 20, size, 2);
  return wire;
}
static bool valid_utf8(const std::string &s) {
  size_t i = 0;
  while (i < s.size()) {
    uint8_t c = uint8_t(s[i++]);
    if (c < 0x80) continue;
    unsigned n; uint32_t code, minimum;
    if (c >= 0xc2 && c <= 0xdf) { n = 1; code = c & 0x1f; minimum = 0x80; }
    else if (c >= 0xe0 && c <= 0xef) { n = 2; code = c & 0x0f; minimum = 0x800; }
    else if (c >= 0xf0 && c <= 0xf4) { n = 3; code = c & 0x07; minimum = 0x10000; }
    else return false;
    if (i + n > s.size()) return false;
    while (n--) {
      uint8_t next = uint8_t(s[i++]);
      if ((next & 0xc0) != 0x80) return false;
      code = (code << 6) | (next & 0x3f);
    }
    if (code < minimum || code > 0x10ffff || (code >= 0xd800 && code <= 0xdfff)) return false;
  }
  return true;
}
static bool valid_target(const otIp6Address &target, const otMeshLocalPrefix &prefix) {
  const uint8_t *p = target.mFields.m8;
  static const uint8_t reserved[] = {0, 0, 0, 0xff, 0xfe, 0};
  static const uint8_t anycast[] = {0xfd, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff};
  uint8_t nonzero = 0;
  for (size_t i = 8; i < 16; i++) nonzero |= p[i];
  return std::memcmp(p, prefix.m8, 8) == 0 && nonzero &&
         std::memcmp(p + 8, reserved, 6) != 0 &&
         !(std::memcmp(p + 8, anycast, 7) == 0 && p[15] >= 0x80);
}

void ThreadP2PTransporter::setup() {
  ot_ = openthread_nrf52::global_openthread_component;
  if (!renew_session_()) { mark_failed(); return; }
  if (ot_ == nullptr || (!sender_only_ && otIp6AddressFromString(address_text_.c_str(), &address_) != OT_ERROR_NONE)) {
    ESP_LOGE(TAG, "OpenThread component or local address is invalid");
    mark_failed();
    return;
  }
  for (auto &target : targets_) {
    if (otIp6AddressFromString(target.address_text.c_str(), &target.address) != OT_ERROR_NONE) {
      mark_failed();
      return;
    }
  }
  // MTD also receives receipt ACKs on its ephemeral sending socket.
  k_msgq_init(&queue_, queue_buffer_, sizeof(Packet *), RX_QUEUE_SIZE);
  queue_initialized_ = true;
}

bool ThreadP2PTransporter::open_() {
  if (shutdown_ || is_failed() || ot_ == nullptr || !ot_->is_lock_initialized() || !ot_->is_connected()) {
    ready_ = false;
    return false;
  }
  if (retry_pending_ && uint32_t(millis() - retry_at_) < 1000) return false;
  auto lock = openthread_nrf52::InstanceLock::try_acquire(0);
  if (!lock) return false;
  auto *instance = lock.get_instance();
  if (instance == nullptr || otThreadGetDeviceRole(instance) < OT_DEVICE_ROLE_CHILD) {
    ready_ = false;
    return false;
  }
  const auto *prefix = otThreadGetMeshLocalPrefix(instance);
  prefix_ = *prefix;
  if (!sender_only_ && std::memcmp(address_.mFields.m8, prefix->m8, 8) != 0) {
    ESP_LOGE(TAG, "Configured address does not match the ACTIVE Mesh-Local /64; check persisted dataset/force_dataset");
    ready_ = false;
    mark_failed();
    return false;
  }
  for (const auto &target : targets_) {
    if (std::memcmp(target.address.mFields.m8, prefix->m8, 8) != 0) {
      ESP_LOGE(TAG, "Target '%s' is outside the ACTIVE Mesh-Local /64", target.id.c_str());
      ready_ = false;
      mark_failed();
      return false;
    }
  }
  if (address_added_ && !otIp6HasUnicastAddress(instance, &address_)) {
    ESP_LOGE(TAG, "Application address was removed; check for an address conflict");
    ready_ = false;
    mark_failed();
    return false;
  }
  if (!sender_only_ && !address_added_) {
    if (otIp6HasUnicastAddress(instance, &address_)) {
      ESP_LOGE(TAG, "Application address is already owned by the local stack");
      mark_failed();
      return false;
    }
    otNetifAddress address{};
    address.mAddress = address_;
    address.mPrefixLength = 64;
    address.mPreferred = true;
    address.mValid = true;
    const auto error = otIp6AddUnicastAddress(instance, &address);
    if (error != OT_ERROR_NONE) {
      ESP_LOGW(TAG, "Adding application address failed: %s; retry in 1s", otThreadErrorToString(error));
      retry_pending_ = true;
      retry_at_ = millis();
      return false;
    }
    address_added_ = true;
  }
  if (socket_open_) {
    ready_ = true;
    return true;
  }
  auto error = otUdpOpen(instance, &socket_, receive_, this);
  if (error == OT_ERROR_NONE) {
    otSockAddr local{};
    if (!sender_only_) {
      local.mAddress = address_;
      local.mPort = port_;
    }
    // MTD uses a wildcard bind and an ephemeral source port; it adds no address.
    // NCS 2.9.2 uses OT_NETIF_THREAD for its internal Thread UDP stack.
    error = otUdpBind(instance, &socket_, &local, OT_NETIF_THREAD);
    if (error != OT_ERROR_NONE) otUdpClose(instance, &socket_);
  }
  if (error != OT_ERROR_NONE) {
    ESP_LOGW(TAG, "Opening peer UDP socket failed: %s; retry in 1s", otThreadErrorToString(error));
    retry_pending_ = true;
    retry_at_ = millis();
    return false;
  }
  socket_open_ = ready_ = true;
  retry_pending_ = false;
  if (sender_only_) {
    ESP_LOGI(TAG, "MTD peer sender ready; using native Mesh-Local EID and an ephemeral port");
  } else {
    ESP_LOGI(TAG, "Target UDP ready on [%s]:%u", address_text_.c_str(), port_);
  }
  return true;
}


void ThreadP2PTransporter::receive_(void *context, otMessage *message, const otMessageInfo *info) {
  auto *self = static_cast<ThreadP2PTransporter *>(context);
  if (self->shutdown_ || !self->queue_initialized_) return;
  const int size = otMessageGetLength(message) - otMessageGetOffset(message);
  if (size < int(HEADER_SIZE) || size > int(MAX_DATAGRAM) ||
      (!self->sender_only_ && std::memcmp(info->mSockAddr.mFields.m8, self->address_.mFields.m8, 16) != 0)) {
    self->dropped_++;
    return;
  }
  auto *packet = new (std::nothrow) Packet;
  if (packet == nullptr) { self->dropped_++; return; }
  packet->source = info->mPeerAddr;
  packet->port = info->mPeerPort;
  packet->size = size;
  if (otMessageRead(message, otMessageGetOffset(message), packet->data, size) != size ||
      k_msgq_put(&self->queue_, &packet, K_NO_WAIT) != 0) {
    delete packet;
    self->dropped_++;
  }
}

bool ThreadP2PTransporter::transmit_(const otIp6Address &target, uint16_t port, const std::string &wire) {
  if (!open_() || std::memcmp(target.mFields.m8, prefix_.m8, 8) != 0) return false;
  auto lock = openthread_nrf52::InstanceLock::try_acquire(0);
  if (!lock) return false;
  auto *instance = lock.get_instance();
  if (!instance || otThreadGetDeviceRole(instance) < OT_DEVICE_ROLE_CHILD) return false;
  auto *message = otUdpNewMessage(instance, nullptr);
  if (!message) return false;
  otMessageInfo info{};
  info.mPeerAddr = target;
  info.mPeerPort = port;
  info.mSockAddr = sender_only_ ? *otThreadGetMeshLocalEid(instance) : address_;
  info.mHopLimit = 64;
  auto error = otMessageAppend(message, wire.data(), wire.size());
  if (error == OT_ERROR_NONE) error = otUdpSend(instance, &socket_, message, &info);
  if (error != OT_ERROR_NONE) { otMessageFree(message); return false; }
  sent_++;
  return true;
}

void ThreadP2PTransporter::complete_(bool success, uint8_t flag, uint32_t id, const SendCompletion &completion) {
  if (shutdown_) return;
  if (success) {
    ESP_LOGD(TAG, "Message %u flag=0x%02X acknowledged", unsigned(id), flag);
    success_callbacks_.call(flag, id);
  } else {
    ESP_LOGW(TAG, "Message %u flag=0x%02X failed", unsigned(id), flag);
    fail_callbacks_.call(flag, id);
  }
  if (!shutdown_ && completion) completion(success, flag, id);
}

void ThreadP2PTransporter::notify_fail_(uint8_t flag, uint32_t id, SendCompletion completion) {
  // Defer local rejection too: a send action must finish before its completion callback runs.
  defer([this, flag, id, completion = std::move(completion)]() {
    this->complete_(false, flag, id, completion);
  });
}

bool ThreadP2PTransporter::send(const std::string &target, bool direct_address, std::optional<uint16_t> port_override, uint8_t flag,
                              const Payload &payload, uint32_t timeout, uint8_t resend, SendCompletion completion) {
  if (++sequence_ == 0) {
    sequence_ = 1;
    if (!renew_session_()) {
      if (timeout) notify_fail_(flag, sequence_, std::move(completion));
      return false;
    }
  }
  const uint32_t id = sequence_;
  auto reject = [this, flag, id, timeout, &completion](const char *reason) {
    ESP_LOGW(TAG, "Message %u rejected: %s", unsigned(id), reason);
    if (timeout) notify_fail_(flag, id, std::move(completion));
    return false;
  };
  std::string body;
  if (payload.is_bool()) body.assign(1, payload.bool_value ? 1 : 0);
  else if (payload.is_float()) {
    if (!std::isfinite(payload.float_value)) return reject("float must be finite");
    static_assert(sizeof(float) == 4, "Protocol requires float32");
    uint32_t bits;
    std::memcpy(&bits, &payload.float_value, 4);
    body.resize(4); write_be(body, 0, bits, 4);
  } else if (payload.is_string()) {
    if (payload.string_value.size() > MAX_PAYLOAD || !valid_utf8(payload.string_value))
      return reject("string exceeds limit or is not UTF-8");
    body = payload.string_value;
  } else return reject("unsupported payload type");
  if ((port_override.has_value() && *port_override == 0) || resend > 10 ||
      (timeout && (timeout < 10 || timeout > 60000)))
    return reject("invalid port or ACK options");
  if (timeout && pending_.size() >= MAX_PENDING) return reject("ACK pending queue is full");
  otIp6Address destination{};
  uint16_t port = port_override.value_or(18520);
  bool found = false;
  if (direct_address) found = otIp6AddressFromString(target.c_str(), &destination) == OT_ERROR_NONE;
  else {
    for (const auto &entry : targets_) {
      if (entry.id == target) {
        destination = entry.address;
        port = port_override.value_or(entry.port);
        found = true;
        break;
      }
    }
  }
  if (!found) return reject("unknown target or invalid address");
  if (!open_()) return reject("Thread transport is not ready");
  if (!valid_target(destination, prefix_) ||
      (!sender_only_ && std::memcmp(destination.mFields.m8, address_.mFields.m8, 16) == 0))
    return reject("target must be a nonlocal unicast address in the active Mesh-Local /64");
  std::string wire = header(1, uint8_t(payload.type), flag, timeout != 0, session_, id, body.size()) + body;
  if (timeout) pending_.push_back({destination, port, flag, resend, id, timeout, millis(), wire, std::move(completion)});
  bool submitted = transmit_(destination, port, wire);
  if (!submitted) ESP_LOGW(TAG, "Message %u UDP submission failed%s", unsigned(id), timeout ? "; ACK retry remains active" : "");
  return submitted;
}

void ThreadP2PTransporter::process_(const Packet &packet) {
  const auto *p = packet.data;
  if (std::memcmp(p, "P2P1", 4) != 0 || p[22] || p[23] ||
      std::memcmp(packet.source.mFields.m8, prefix_.m8, 8) != 0) { rejected_++; return; }
  const uint8_t kind = p[4], type = p[5], flag = p[6], options = p[7];
  const uint64_t session = read_be(p + 8, 8);
  const uint32_t id = read_be(p + 16, 4);
  const size_t size = read_be(p + 20, 2);
  if (size + HEADER_SIZE != packet.size || id == 0) { rejected_++; return; }
  if (kind == 2) {
    if (size || type || options) { rejected_++; return; }
    for (size_t i = 0; i < pending_.size(); i++) {
      const auto &entry = pending_[i];
      if (session == session_ && id == entry.id && flag == entry.flag && packet.port == entry.port &&
          std::memcmp(packet.source.mFields.m8, entry.destination.mFields.m8, 16) == 0) {
        auto completion = std::move(pending_[i].completion);
        pending_.erase(pending_.begin() + i);
        complete_(true, flag, id, completion);
        return;
      }
    }
    return;  // Late, duplicate or unsolicited ACK.
  }
  if (kind != 1 || options > 1 || sender_only_) { rejected_++; return; }
  Payload payload;
  if (type == uint8_t(PayloadType::BOOL) && size == 1 && p[HEADER_SIZE] <= 1)
    payload = Payload::from_bool(p[HEADER_SIZE] != 0);
  else if (type == uint8_t(PayloadType::FLOAT) && size == 4) {
    uint32_t bits = read_be(p + HEADER_SIZE, 4);
    float value; std::memcpy(&value, &bits, 4);
    if (!std::isfinite(value)) { rejected_++; return; }
    payload = Payload::from_float(value);
  } else if (type == uint8_t(PayloadType::STRING)) {
    payload = Payload::from_string(std::string(reinterpret_cast<const char *>(p + HEADER_SIZE), size));
    if (!valid_utf8(payload.string_value)) { rejected_++; return; }
  } else { rejected_++; return; }
  const uint32_t now = millis();
  seen_.erase(std::remove_if(seen_.begin(), seen_.end(), [now](const Seen &entry) {
    return uint32_t(now - entry.at) >= DEDUP_TTL;
  }), seen_.end());
  bool duplicate = false;
  for (const auto &entry : seen_) {
    if (entry.session == session && entry.id == id && entry.port == packet.port &&
        std::memcmp(entry.source.mFields.m8, packet.source.mFields.m8, 16) == 0) { duplicate = true; break; }
  }
  // ACK receipt before calling automations; retransmissions receive ACK again.
  if (options & 1) transmit_(packet.source, packet.port, header(2, 0, flag, false, session, id, 0));
  if (duplicate) return;
  if (seen_.size() >= MAX_SEEN) seen_.erase(seen_.begin());
  seen_.push_back({packet.source, packet.port, session, id, now});
  received_++;
  receive_callbacks_.call(flag, payload);
}

void ThreadP2PTransporter::loop() {
  if (shutdown_ || is_failed()) return;
  open_();
  Packet *packet;
  for (unsigned count = 0; queue_initialized_ && count < RX_QUEUE_SIZE && k_msgq_get(&queue_, &packet, K_NO_WAIT) == 0; count++) {
    process_(*packet);
    delete packet;
    if (shutdown_) return;
  }
  // Remove before callbacks, so callbacks can enqueue new messages safely.
  for (size_t i = 0; i < pending_.size();) {
    auto &entry = pending_[i];
    if (uint32_t(millis() - entry.started) < entry.timeout) { i++; continue; }
    if (entry.retries) {
      entry.retries--; entry.started = millis();
      transmit_(entry.destination, entry.port, entry.wire);
      i++;
    } else {
      const uint8_t flag = entry.flag;
      const uint32_t id = entry.id;
      auto completion = std::move(entry.completion);
      pending_.erase(pending_.begin() + i);
      notify_fail_(flag, id, std::move(completion));
    }
  }
}

bool ThreadP2PTransporter::renew_session_() {
  if (random_bytes(reinterpret_cast<uint8_t *>(&session_), sizeof(session_))) return true;
  ESP_LOGE(TAG, "Cannot generate P2P session");
  return false;
}

void ThreadP2PTransporter::on_shutdown() {
  shutdown_ = true;
  ready_ = false;
  pending_.clear();
  close_();
}

bool ThreadP2PTransporter::teardown() {
  shutdown_ = true;
  ready_ = false;
  pending_.clear();
  return this->close_();
}

bool ThreadP2PTransporter::close_() {
  // Taking the OT lock is also a barrier against a receive callback already
  // copying a packet when shutdown_ was set. Do not drain before that barrier.
  if (ot_ != nullptr && ot_->is_lock_initialized()) {
    auto lock = openthread_nrf52::InstanceLock::try_acquire(0);
    if (!lock) return false;  // ESPHome retries teardown; never block the loop.
    auto *instance = lock.get_instance();
    if (instance == nullptr) return false;
    if (socket_open_ && otUdpClose(instance, &socket_) != OT_ERROR_NONE) return false;
    socket_open_ = false;
    if (address_added_) otIp6RemoveUnicastAddress(instance, &address_);
    address_added_ = false;
  }
  // If the OT component already stopped, its teardown has disabled Thread/IP6
  // under the same mutex, so packet producers have also stopped.
  socket_open_ = address_added_ = false;
  Packet *packet;
  while (queue_initialized_ && k_msgq_get(&queue_, &packet, K_NO_WAIT) == 0) delete packet;
  return true;
}

void ThreadP2PTransporter::dump_config() {
  ESP_LOGCONFIG(TAG, "Thread P2P: %s, %u address aliases", sender_only_ ? "MTD sender with ACK reception" : "FTD sender/receiver", unsigned(targets_.size()));
  ESP_LOGCONFIG(TAG, "  Typed protocol v1; payload <= %u bytes; pending ACK <= %u; dedup <= %u messages / 15min",
                unsigned(MAX_PAYLOAD), unsigned(MAX_PENDING), unsigned(MAX_SEEN));
  ESP_LOGCONFIG(TAG, "  Submitted=%u received=%u rejected=%u dropped=%u", unsigned(sent_), unsigned(received_),
                unsigned(rejected_), unsigned(dropped_.load()));
}

}  // namespace esphome::thread_p2p_transporter_nrf52
