#pragma once
#include <array>
#include <atomic>
#include <deque>
#include <memory>
#include <string>
#include <vector>
#include "api_buffer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "openthread/udp.h"
#ifdef USE_THREAD_PROXY_UDP_ENCRYPTION
#include "mbedtls/gcm.h"
#endif

namespace esphome::thread_proxy_api {
class ThreadProxyAPI;
// Owns the radio-side socket. No TCP, crypto or allocations in the OT callback.
class UdpRelay {
 public:
  struct Group {
    std::array<uint8_t, 48> header{};
    APIBuffer body;
    std::string delivery;
  };
  explicit UdpRelay(ThreadProxyAPI *owner) : owner_(owner) {}
  void set_limits(uint16_t groups, uint32_t bytes) { max_groups_ = groups; max_bytes_ = bytes; }
#ifdef USE_THREAD_PROXY_UDP_ENCRYPTION
  void set_key(const std::array<uint8_t,32> &key);
#endif
  bool request_schema(const std::string &mac);
  bool setup();
  void stop() { stopping_ = true; }
  bool ready() const { return ready_; }
  uint16_t queued() const { return queued_; }
  uint16_t nodes() const { return node_count_; }
  std::shared_ptr<const Group> front();
  bool acknowledge(const std::string &delivery);
 protected:
  struct Datagram { otMessageInfo peer; uint16_t size; uint8_t data[832]; };
  struct Assembly {
    std::array<uint8_t,48> header{};
    otMessageInfo peer{};
    APIBuffer body;
    uint64_t mask{0};
    uint32_t touched{0};
    uint16_t final_size{0};
  };
  struct Receipt { std::array<uint8_t,48> header; uint64_t hash; uint32_t at; };
  struct Node { otIp6Address address{}; std::array<uint8_t,6> mac{}; std::array<uint8_t,16> schema{}; uint32_t last_seen{0}, last_probe{0}; uint16_t port{6064}; };
  static void task_(void *arg);
  static void receive_(void *arg, otMessage *message, const otMessageInfo *peer);
  void run_();
  void process_(Datagram &packet);
  bool accept_(const uint8_t *header, const uint8_t *body, size_t size);
  bool send_(uint8_t kind, const uint8_t *header, const uint8_t *body, size_t size, const otMessageInfo &peer);
  void schema_requests_();
  Node *node_(const otIp6Address &address, uint16_t port);
  ThreadProxyAPI *owner_;
  QueueHandle_t incoming_{nullptr}, requests_{nullptr};
  SemaphoreHandle_t mutex_{nullptr};
  otUdpSocket socket_{};
  bool opened_{false}, encrypted_{false};
#ifdef USE_THREAD_PROXY_UDP_ENCRYPTION
  mbedtls_gcm_context crypto_{};
#endif
  std::atomic<bool> stopping_{false}, ready_{false};
  std::atomic<uint16_t> queued_{0}, node_count_{0};
  std::deque<std::shared_ptr<Group>> queue_;
  std::vector<Assembly> assemblies_;
  std::deque<Receipt> receipts_;
  std::vector<Node> nodes_;
  std::array<uint8_t,8> boot_{};
  uint32_t sequence_{0}, delivery_{0}, bytes_{0};
  uint32_t max_bytes_{131072};
  uint16_t max_groups_{16};
};
}  // namespace esphome::thread_proxy_api
