#pragma once
#include "esphome/core/defines.h"
#include "esphome/components/socket/socket.h"
#include "esphome/core/preferences.h"
#include <array>
#include <cstddef>
#include <cstdint>
#include <memory>
#include <functional>
#include <string>
#include <vector>
#ifdef USE_THREAD_PROXY_CLIENT_UDP_ENCRYPTION
#ifdef USE_THREAD_PROXY_CLIENT_NRF52
#include <psa/crypto.h>
#else
#include <mbedtls/gcm.h>
#endif
#endif

namespace esphome::thread_proxy_client {
class ThreadProxyClient;
class APIConnection;
class ProxyUdpTransport {
 public:
  struct DeliveryStats { uint32_t group{0}, bytes{0}; uint16_t rounds{0}; };
  const DeliveryStats &last_delivery() const { return last_delivery_; }
  explicit ProxyUdpTransport(ThreadProxyClient *owner) : owner_(owner) {}
  void setup(uint16_t port);
  void loop();
  bool append(uint16_t type, const uint8_t *data, size_t size);
  bool flush(const std::string &control = "", uint32_t ack_timeout = 1000, uint16_t ack_resend = 2);
  bool ready() const { return peer_verified_ && peer_len_ != 0 && socket_ != nullptr && network_connected_; }
  bool recovering() const { return recovering_; }
  bool schema_confirmed() const { return schema_confirmed_; }
  bool pending() const { return !tx_.empty() || !staging_.empty() || schema_building_ || schema_requested_; }
  void wake();
  void sleep();
  bool can_flush() const { return tx_.empty() && !schema_building_; }
  void cancel();
  void request_schema(const sockaddr_storage *reply_peer=nullptr, socklen_t reply_length=0);
  void set_key(const std::array<uint8_t, 32> &key);
 protected:
  static constexpr size_t HEADER=48, CHUNK=768, MAX_BODY=32768;
  static constexpr uint16_t PROXY_PORT=6064;
  struct SavedPeer { uint8_t address[16]; uint16_t port; uint32_t scope; };
  void bind_peer_(const sockaddr_storage &peer, socklen_t length);
  void receive_();
  void discover_();
  bool open_socket_();
  void reset_delivery_(const char *reason);
  bool send_(uint8_t kind, uint32_t group, uint16_t part, uint16_t parts,
             const uint8_t *data, size_t size, uint8_t flags=0,
             const sockaddr_storage *destination=nullptr, socklen_t destination_length=0);
  void transmit_();
  void fail_(const char *reason);
  void recover_(const char *reason);
  ThreadProxyClient *owner_;
  std::unique_ptr<socket::Socket> socket_;
  sockaddr_storage peer_{};
  socklen_t peer_len_{0};
  // Business listener and temporary onboarding replies are distinct endpoints.
  // Every in-flight group retains its own peer across PROBE/schema exchanges.
  sockaddr_storage tx_peer_{}, paused_peer_{}, requested_schema_peer_{}, schema_peer_{};
  socklen_t tx_peer_len_{0}, paused_peer_len_{0}, requested_schema_peer_len_{0}, schema_peer_len_{0};
  uint16_t port_{0};
  bool network_connected_{false}, peer_verified_{false};
  uint32_t discovery_at_{0}, address_check_at_{0};
  std::array<uint8_t,16> local_address_{};
  uint32_t socket_retry_at_{0}, receive_error_logged_at_{0};
  ESPPreferenceObject preference_;
  SavedPeer saved_{};
  std::array<uint8_t,6> mac_{};
  std::array<uint8_t,8> boot_{};
  std::array<uint8_t,16> schema_{};
  uint32_t packet_sequence_{0}, group_{0}, tx_group_{0}, last_send_{0}, tx_started_{0}, staged_at_{0}, schema_request_at_{0};
  uint16_t attempts_{0}, paused_attempts_{0}, recoveries_{0};
  bool recovering_{false};
  uint32_t recovery_started_{0}, schema_started_{0};
  uint32_t tx_ack_timeout_{1000}, paused_ack_timeout_{1000};
  uint16_t tx_ack_resend_{2}, paused_ack_resend_{2};
  uint16_t tx_part_{0};
  bool schema_building_{false}, schema_requested_{false}, schema_confirmed_{false};
  std::vector<uint8_t> staging_, tx_;
  std::vector<uint8_t> paused_tx_;
  std::string tx_control_;
  DeliveryStats last_delivery_;
  std::string paused_control_;
  uint32_t paused_group_{0};
  uint8_t tx_flags_{0};
#ifdef USE_THREAD_PROXY_CLIENT_UDP_ENCRYPTION
#ifdef USE_THREAD_PROXY_CLIENT_NRF52
  psa_key_id_t crypto_key_{0};
#else
  mbedtls_gcm_context crypto_{};
#endif
#endif
};
}  // namespace esphome::thread_proxy_client
