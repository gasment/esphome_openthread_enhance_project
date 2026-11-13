#pragma once
#include "api_frame_helper.h"
namespace esphome::thread_proxy_client {
class ThreadProxyClient;
class APIUDPFrameHelper final : public APIFrameHelper {
 public:
  explicit APIUDPFrameHelper(ThreadProxyClient *owner) : owner_(owner) { frame_header_padding_=UDP_PROTO_HEADER_SIZE; }
  APIError init() override { state_=State::DATA; return APIError::OK; }
  APIError loop() override { return APIError::OK; }
  APIError read_packet(ReadPacketBuffer *) override { return APIError::WOULD_BLOCK; }
  APIError write_protobuf_packet(uint16_t type, ProtoWriteBuffer buffer) override;
  APIError write_protobuf_messages(ProtoWriteBuffer buffer, std::span<const MessageInfo> messages) override;
 protected:
  ThreadProxyClient *owner_;
};
}  // namespace esphome::thread_proxy_client
