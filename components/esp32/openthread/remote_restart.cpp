#include "openthread.h"
#ifdef USE_ESP32
#include "esphome/core/application.h"
#include "esphome/core/hal.h"
#include "esphome/core/log.h"
#include "esphome/core/wake.h"
#include <openthread/message.h>
#include <openthread/link.h>
#include <cstring>

namespace esphome::openthread {
namespace {
constexpr uint16_t RESTART_PORT = 18514;
constexpr uint8_t REQUEST_MAGIC[4] = {'E', 'H', 'R', '1'};
constexpr size_t PACKET_SIZE = 21;
}

void OpenThreadComponent::receive_remote_restart_(void *context, otMessage *message, const otMessageInfo *info) {
  auto *self = static_cast<OpenThreadComponent *>(context);
  if (self->restart_request_pending_.load() || self->restart_scheduled_ ||
      otMessageGetLength(message) - otMessageGetOffset(message) != PACKET_SIZE) return;
  RestartRequest request{};
  if (otMessageRead(message, otMessageGetOffset(message), request.bytes, PACKET_SIZE) != PACKET_SIZE ||
      std::memcmp(request.bytes, REQUEST_MAGIC, sizeof(REQUEST_MAGIC)) != 0 || request.bytes[4] != 1) return;
  request.peer = info->mPeerAddr;
  request.port = info->mPeerPort;
  self->restart_request_ = request;
  self->restart_request_pending_.store(true);
  wake_loop_threadsafe();
}

void OpenThreadComponent::service_remote_restart_() {
  if (this->restart_scheduled_) {
    if (static_cast<int32_t>(millis() - this->restart_due_) >= 0) {
      ESP_LOGW("openthread", "Remote restart requested by BR");
      App.safe_reboot();
    }
    return;
  }
  if (!this->restart_socket_open_ || this->restart_request_pending_.load()) {
    auto lock = InstanceLock::try_acquire(0);
    if (!lock) return;
    auto *instance = lock.get_instance();
    if (!instance || otThreadGetDeviceRole(instance) < OT_DEVICE_ROLE_CHILD) return;
    if (!this->restart_socket_open_) {
      if (otUdpOpen(instance, &this->restart_socket_, receive_remote_restart_, this) != OT_ERROR_NONE) return;
      otSockAddr local{};
      local.mPort = RESTART_PORT;
      if (otUdpBind(instance, &this->restart_socket_, &local, OT_NETIF_THREAD_INTERNAL) != OT_ERROR_NONE) {
        otUdpClose(instance, &this->restart_socket_);
        return;
      }
      this->restart_socket_open_ = true;
    }
    if (!this->restart_request_pending_.load()) return;
    const RestartRequest request = this->restart_request_;
    this->restart_request_pending_.store(false);
    if (std::memcmp(request.bytes + 5, otLinkGetExtendedAddress(instance)->m8, OT_EXT_ADDRESS_SIZE) != 0) return;
    uint8_t response[PACKET_SIZE];
    std::memcpy(response, request.bytes, PACKET_SIZE);
    response[4] = 2;
    otMessageInfo reply{};
    reply.mPeerAddr = request.peer;
    reply.mPeerPort = request.port;
    reply.mSockAddr = *otThreadGetMeshLocalEid(instance);
    reply.mHopLimit = 64;
    auto *out = otUdpNewMessage(instance, nullptr);
    if (!out) return;
    const auto append = otMessageAppend(out, response, sizeof(response));
    const auto sent = append == OT_ERROR_NONE ? otUdpSend(instance, &this->restart_socket_, out, &reply) : append;
    if (sent != OT_ERROR_NONE) { otMessageFree(out); return; }
    this->restart_scheduled_ = true;
    this->restart_due_ = millis() + 500;
  }
}
}  // namespace esphome::openthread
#endif  // USE_ESP32
