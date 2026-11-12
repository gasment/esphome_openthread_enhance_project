#pragma once
#include "esphome/core/defines.h"
#ifdef USE_THREAD_SED_API_UDP
#include "esphome/components/socket/socket.h"
#include "esphome/core/preferences.h"
#include <array>
#include <cstddef>
#include <cstdint>
#include <memory>
#include <functional>
#include <string>
#include <vector>
#ifdef USE_THREAD_SED_UDP_ENCRYPTION
#ifdef USE_THREAD_SED_NRF52
#include <psa/crypto.h>
#else
#include <mbedtls/gcm.h>
#endif
#endif

namespace esphome::thread_sed_api {
class APIServer;
class APIConnection;
class SedUdpTransport {
 public:
  explicit SedUdpTransport(APIServer *owner) : owner_(owner) {}
  void setup(uint16_t port);
  void loop();
  bool append(uint16_t type, const uint8_t *data, size_t size);
  bool flush(const std::string &control = "", uint32_t ack_timeout = 1000, uint16_t ack_resend = 2);
  bool ready() const { return peer_len_ != 0 && socket_ != nullptr; }
  bool pending() const { return !tx_.empty() || !staging_.empty() || schema_building_ || schema_requested_; }
  void wake();
  void sleep();
  bool can_flush() const { return tx_.empty() && !schema_building_; }
  void cancel();
  void request_schema();
  void set_key(const std::array<uint8_t, 32> &key);
 protected:
  static constexpr size_t HEADER=48, CHUNK=768, MAX_BODY=32768;
  struct SavedPeer { uint8_t address[16]; uint16_t port; uint32_t scope; };
  void bind_peer_(const sockaddr_storage &peer, socklen_t length);
  void receive_();
  bool send_(uint8_t kind, uint32_t group, uint16_t part, uint16_t parts,
             const uint8_t *data, size_t size, uint8_t flags=0);
  void transmit_();
  void fail_(const char *reason);
  APIServer *owner_;
  std::unique_ptr<socket::Socket> socket_;
  sockaddr_storage peer_{};
  socklen_t peer_len_{0};
  ESPPreferenceObject preference_;
  SavedPeer saved_{};
  std::array<uint8_t,6> mac_{};
  std::array<uint8_t,8> boot_{};
  std::array<uint8_t,16> schema_{};
  uint32_t packet_sequence_{0}, group_{0}, tx_group_{0}, last_send_{0}, tx_started_{0}, staged_at_{0}, schema_request_at_{0};
  uint16_t attempts_{0};
  uint32_t tx_ack_timeout_{1000}, paused_ack_timeout_{1000};
  uint16_t tx_ack_resend_{2}, paused_ack_resend_{2};
  uint16_t tx_part_{0};
  bool schema_building_{false}, schema_requested_{false};
  std::vector<uint8_t> staging_, tx_;
  std::vector<uint8_t> paused_tx_;
  std::string tx_control_;
  std::string paused_control_;
  uint32_t paused_group_{0};
  uint8_t tx_flags_{0};
  std::array<std::array<uint8_t,16>,16> commands_{};
  size_t command_cursor_{0};
#ifdef USE_THREAD_SED_UDP_ENCRYPTION
#ifdef USE_THREAD_SED_NRF52
  psa_key_id_t crypto_key_{0};
#else
  mbedtls_gcm_context crypto_{};
#endif
#endif
};
}  // namespace esphome::thread_sed_api
#endif
