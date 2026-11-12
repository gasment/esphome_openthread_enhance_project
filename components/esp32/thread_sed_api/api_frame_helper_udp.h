#pragma once
#include "api_frame_helper.h"
#ifdef USE_THREAD_SED_API_UDP
namespace esphome::thread_sed_api {
class APIServer;
class APIUDPFrameHelper final : public APIFrameHelper {
 public:
  explicit APIUDPFrameHelper(APIServer *owner) : owner_(owner) { frame_header_padding_=0; }
  APIError init() override { state_=State::DATA; return APIError::OK; }
  APIError loop() override { return APIError::OK; }
  APIError read_packet(ReadPacketBuffer *) override { return APIError::WOULD_BLOCK; }
  APIError write_protobuf_packet(uint16_t type, ProtoWriteBuffer buffer) override;
  APIError write_protobuf_messages(ProtoWriteBuffer buffer, std::span<const MessageInfo> messages) override;
 protected:
  APIServer *owner_;
};
}  // namespace esphome::thread_sed_api
#endif
