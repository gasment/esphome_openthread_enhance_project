#pragma once
#include <atomic>
#include <memory>
#include <string>
#include <vector>
#include "esphome/core/component.h"
#include "esphome/components/thread_br/thread_br.h"
#include "api_frame_helper.h"
#include "udp_relay.h"
#ifdef USE_THREAD_PROXY_API_NOISE
#include "esphome/components/noise/noise.h"
#endif

namespace esphome::thread_proxy_api {

class ThreadProxyAPI;
class ProxyClient {
 public:
  ProxyClient(ThreadProxyAPI *owner, std::unique_ptr<APIFrameHelper> helper);
  ~ProxyClient();
  bool loop();
  bool subscribed() const { return subscribed_; }
 protected:
  bool send_(uint16_t type, const ManagementMessage &message);
  bool handle_(const ReadPacketBuffer &packet);
  bool metadata_(bool initial);
  bool forward_();
  bool uplink_{false};
  std::string delivery_;
  uint16_t delivery_part_{0};
  uint32_t delivery_sent_{0};
  ThreadProxyAPI *owner_;
  std::unique_ptr<APIFrameHelper> helper_;
  uint32_t created_, last_received_, last_ping_, last_status_{0}, closing_since_{0};
  bool hello_{false}, subscribed_{false}, closing_{false};
};

class ThreadProxyAPI : public Component {
 public:
  void set_parent(thread_br::ThreadBorderRouter *parent) { parent_ = parent; }
  void set_port(uint16_t port) { port_ = port; }
  void set_max_connections(uint8_t count) { max_connections_ = count; }
  void set_status_interval(uint32_t interval) { status_interval_ = interval; }
#ifdef USE_THREAD_PROXY_API_NOISE
  void set_noise_psk(const uint8_t *key) { noise_ctx_.set_psk(key); }
#endif
  UdpRelay &relay() { return relay_; }
  void setup() override;
  void loop() override;
  void dump_config() override;
  void on_shutdown() override;
  float get_setup_priority() const override { return setup_priority::AFTER_WIFI - 1.0f; }

 protected:
  friend class ProxyClient;
  friend class UdpRelay;
  UdpRelay relay_{this};
  ProxyClient *consumer_{nullptr};
  struct ThreadSnapshot {
    char network_id[17]{};
    char partition_id[9]{};
    uint8_t role{0};
    bool available{false};
  };
  static void snapshot_task_(void *arg);
  void sample_thread_();
  ThreadSnapshot snapshot_();
  bool start_listener_();
  void accept_();

  thread_br::ThreadBorderRouter *parent_{nullptr};
  std::unique_ptr<socket::ListenSocket> listener_;
  std::vector<std::unique_ptr<ProxyClient>> clients_;
#ifdef USE_THREAD_PROXY_API_NOISE
  noise::NoiseContext noise_ctx_;
#endif
  portMUX_TYPE snapshot_mux_ = portMUX_INITIALIZER_UNLOCKED;
  ThreadSnapshot thread_snapshot_;
  std::atomic<bool> stopping_{false};
  std::string proxy_id_, mac_;
  uint32_t status_interval_{30000}, last_listen_attempt_{0};
  uint16_t port_{6065};
  uint8_t max_connections_{2};
};
}  // namespace esphome::thread_proxy_api
