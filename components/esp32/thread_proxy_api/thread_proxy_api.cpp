#include "thread_proxy_api.h"
#include "api_frame_helper_plaintext.h"
#include "api_frame_helper_noise.h"
#include "esphome/core/application.h"
#include "esphome/core/hal.h"
#include "esphome/core/helpers.h"
#include "esphome/core/log.h"
#include "esphome/core/version.h"
#include "esp_openthread.h"
#include "esp_openthread_lock.h"
#include "openthread/thread.h"
#include <algorithm>
#include <cerrno>
#include <cstdio>

namespace esphome::thread_proxy_api {
static const char *const TAG = "thread_proxy_api";
static constexpr uint32_t HANDSHAKE_TIMEOUT = 10000;
static constexpr uint32_t IDLE_TIMEOUT = 60000;
static constexpr uint8_t MAX_PACKETS_PER_LOOP = 8;

ProxyClient::ProxyClient(ThreadProxyAPI *owner, std::unique_ptr<APIFrameHelper> helper)
    : owner_(owner), helper_(std::move(helper)), created_(millis()), last_received_(created_), last_ping_(created_) {}
ProxyClient::~ProxyClient() { helper_->close(); }

bool ProxyClient::send_(uint16_t type, const ManagementMessage &message) {
  if (!message.valid()) return false;
  APIBuffer buffer;
  const size_t padding = helper_->frame_header_padding();
  // Reserve the Noise footer too, so encryption cannot invalidate its pointers.
  if (!buffer.reserve_and_resize(padding + message.size() + helper_->frame_footer_size(),
                                 padding + message.size())) return false;
  if (message.size()) std::memcpy(buffer.data() + padding, message.data(), message.size());
  return helper_->write_protobuf_packet(type, ProtoWriteBuffer(&buffer)) == APIError::OK;
}

bool ProxyClient::metadata_(bool initial) {
  auto state = owner_->snapshot_();
  ManagementMessage message;
  message.text(1, initial ? "thread_proxy.info" : "thread_proxy.status");
  message.pair("component", "thread_proxy_api");
  message.pair("version", "1");
  message.pair("id", owner_->proxy_id_);
  message.pair("data_plane_ready", "false");
  message.pair("thread_available", state.available ? "true" : "false");
  message.pair("thread_attached", state.available && state.role >= OT_DEVICE_ROLE_CHILD ? "true" : "false");
  if (state.available) {
    message.pair("network_id", state.network_id);
    message.pair("partition_id", state.partition_id);
    const char *role = state.role == OT_DEVICE_ROLE_LEADER ? "leader" :
                       state.role == OT_DEVICE_ROLE_ROUTER ? "router" :
                       state.role == OT_DEVICE_ROLE_CHILD ? "child" :
                       state.role == OT_DEVICE_ROLE_DETACHED ? "detached" : "disabled";
    message.pair("thread_role", role);
  }
  return send_(35, message);
}

bool ProxyClient::handle_(const ReadPacketBuffer &packet) {
  if (!valid_management_message(packet.data, packet.data_len)) return false;
  ManagementMessage response;
  if (packet.type == 1) {
    if (hello_) return false;
    response.integer(1, 1);  // API major
    response.integer(2, 14); // Compatible management subset, not an entity API.
    response.text(3, "Thread Proxy 1");
    response.text(4, std::string_view(App.get_name().c_str(), App.get_name().size()));
    if (!send_(2, response)) return false;
    hello_ = true;
    return true;
  }
  if (!hello_) return false;
  switch (packet.type) {
    case 3: return send_(4, response); // Legacy login; PSK is enforced at the transport.
    case 5:
      if (!send_(6, response)) return false;
      closing_ = true; closing_since_ = millis(); return true;
    case 7: return send_(8, response);
    case 8: return true;
    case 9:
      response.text(2, std::string_view(App.get_name().c_str(), App.get_name().size()));
      response.text(3, owner_->mac_);
      response.text(4, ESPHOME_VERSION);
      response.text(6, ESPHOME_BOARD);
      response.text(12, "ESPHome");
      response.text(13, std::string_view(App.get_friendly_name().c_str(), App.get_friendly_name().size()));
#ifdef USE_THREAD_PROXY_API_NOISE
      response.integer(19, 1);
#endif
      return send_(10, response);
    case 11: return send_(19, response); // This endpoint never exposes BR entities.
    case 34:
      if (!metadata_(true)) return false;
      subscribed_ = true; last_status_ = millis();
      ESP_LOGD(TAG, "HA subscribed to proxy management");
      return true;
    default:
      // No action dispatch, state subscription, HA services or node ACK path.
      return true;
  }
}

bool ProxyClient::loop() {
  auto result = helper_->loop();
  if (result != APIError::OK && result != APIError::WOULD_BLOCK) return false;
  uint32_t now = millis(); // Fresh after accept/Noise work; never predates created_.
  if (closing_) return !helper_->can_write_without_blocking() && now - closing_since_ < 1000;
  // Draining bounded packets even without ready() handles LWIP cached bytes.
  for (uint8_t count = 0; count < MAX_PACKETS_PER_LOOP; count++) {
    ReadPacketBuffer packet{};
    result = helper_->read_packet(&packet);
    if (result == APIError::WOULD_BLOCK) break;
    if (result != APIError::OK || !handle_(packet)) return false;
    last_received_ = millis();
    if (closing_) break;
  }
  now = millis();
  if (closing_) return true;
  if ((!subscribed_ && now - created_ >= HANDSHAKE_TIMEOUT) || now - last_received_ >= IDLE_TIMEOUT) return false;
  // Frequent outbound status updates can suppress the client's idle pings.
  // Probe from this side too, so a healthy subscriber is never timed out.
  if (subscribed_ && now - last_received_ >= 15000 && now - last_ping_ >= 15000 && helper_->can_write_without_blocking()) {
    if (!send_(7, ManagementMessage{})) return false;
    last_ping_ = now;
  }
  if (subscribed_ && now - last_status_ >= owner_->status_interval_ && helper_->can_write_without_blocking()) {
    if (!metadata_(false)) return false;
    last_status_ = now;
  }
  return true;
}

void ThreadProxyAPI::setup() {
  char mac[MAC_ADDRESS_PRETTY_BUFFER_SIZE];
  get_mac_address_pretty_into_buffer(mac);
  mac_ = mac;
  proxy_id_ = get_mac_address();
  clients_.reserve(max_connections_);
  TaskHandle_t handle = nullptr;
  // RCP-backed OT getters can wait on Spinel; keep them off ESPHome's main loop.
  if (xTaskCreate(snapshot_task_, "proxy_ot_info", 4096, this, 1, &handle) != pdPASS) {
    ESP_LOGE(TAG, "Cannot create Thread metadata worker");
    mark_failed();
    return;
  }
  start_listener_();
}

bool ThreadProxyAPI::start_listener_() {
  last_listen_attempt_ = millis();
  auto listener = socket::socket_ip_loop_monitored(SOCK_STREAM, 0);
  if (listener) {
    int enable = 1;
    listener->setsockopt(SOL_SOCKET, SO_REUSEADDR, &enable, sizeof(enable));
    sockaddr_storage address{};
    socklen_t length = socket::set_sockaddr_any(reinterpret_cast<sockaddr *>(&address), sizeof(address), port_);
    if (length && listener->setblocking(false) == 0 &&
        listener->bind(reinterpret_cast<sockaddr *>(&address), length) == 0 && listener->listen(max_connections_) == 0) {
      listener_ = std::move(listener);
      status_clear_warning();
      ESP_LOGI(TAG, "Independent proxy listener on TCP %u", port_);
      return true;
    }
    listener->close();
  }
  ESP_LOGW(TAG, "Cannot listen on TCP %u (errno %d); retrying", port_, errno);
  status_set_warning();
  return false;
}

void ThreadProxyAPI::accept_() {
  // Never evict a healthy session when a port scanner or another HA connects.
  for (uint8_t count = 0; count < 4; count++) {
    sockaddr_storage address{}; socklen_t length = sizeof(address);
    auto socket = listener_->accept_loop_monitored(reinterpret_cast<sockaddr *>(&address), &length);
    if (!socket) break;
    if (clients_.size() >= max_connections_) { socket->close(); continue; }
    std::unique_ptr<APIFrameHelper> helper;
#ifdef USE_THREAD_PROXY_API_NOISE
    helper = std::make_unique<APINoiseFrameHelper>(std::move(socket), noise_ctx_);
#else
    helper = std::make_unique<APIPlaintextFrameHelper>(std::move(socket));
#endif
    if (helper->init() != APIError::OK) { helper->close(); continue; }
    clients_.push_back(std::make_unique<ProxyClient>(this, std::move(helper)));
    ESP_LOGD(TAG, "Proxy TCP client accepted (%u/%u)", unsigned(clients_.size()), max_connections_);
  }
}

void ThreadProxyAPI::loop() {
  if (stopping_) return;
  uint32_t now = millis();
  if (!listener_ && now - last_listen_attempt_ >= 5000) start_listener_();
  if (listener_ && listener_->ready()) accept_();
  for (auto it = clients_.begin(); it != clients_.end();) {
    if (!(*it)->loop()) {
      it = clients_.erase(it);
      ESP_LOGD(TAG, "Proxy TCP client removed (%u remaining)", unsigned(clients_.size()));
    } else ++it;
  }
}

void ThreadProxyAPI::snapshot_task_(void *arg) {
  auto *self = static_cast<ThreadProxyAPI *>(arg);
  while (!self->stopping_) {
    self->sample_thread_();
    const uint32_t interval = self->parent_->is_thread_ready() ? std::min(self->status_interval_, uint32_t{5000}) : 1000;
    vTaskDelay(pdMS_TO_TICKS(interval));
  }
  vTaskDelete(nullptr);
}

void ThreadProxyAPI::sample_thread_() {
  ThreadSnapshot state{};
  if (parent_->is_thread_ready()) {
    // Do not replace a valid snapshot with blank state on a brief lock stall.
    if (!esp_openthread_lock_acquire(0)) return;
    auto *instance = esp_openthread_get_instance();
    if (instance != nullptr && parent_->is_thread_ready()) {
      const auto *ext_pan = otThreadGetExtendedPanId(instance);
      if (ext_pan != nullptr) {
        for (size_t i = 0; i < 8; i++) std::snprintf(state.network_id + i * 2, 3, "%02x", ext_pan->m8[i]);
        std::snprintf(state.partition_id, sizeof(state.partition_id), "%08lx", static_cast<unsigned long>(otThreadGetPartitionId(instance)));
        state.role = otThreadGetDeviceRole(instance);
        state.available = true;
      }
    }
    esp_openthread_lock_release();
  }
  portENTER_CRITICAL(&snapshot_mux_);
  thread_snapshot_ = state;
  portEXIT_CRITICAL(&snapshot_mux_);
}

ThreadProxyAPI::ThreadSnapshot ThreadProxyAPI::snapshot_() {
  portENTER_CRITICAL(&snapshot_mux_);
  auto result = thread_snapshot_;
  portEXIT_CRITICAL(&snapshot_mux_);
  return result;
}

void ThreadProxyAPI::on_shutdown() {
  stopping_ = true;
  clients_.clear();
  if (listener_) { listener_->close(); listener_.reset(); }
  // The worker releases the OT lock and exits itself; never delete it mid-read.
}

void ThreadProxyAPI::dump_config() {
  ESP_LOGCONFIG(TAG, "Thread Proxy API (management v1):");
  ESP_LOGCONFIG(TAG, "  BR ID: %s", proxy_id_.c_str());
  ESP_LOGCONFIG(TAG, "  Independent TCP port: %u; max clients: %u", port_, max_connections_);
#ifdef USE_THREAD_PROXY_API_NOISE
  ESP_LOGCONFIG(TAG, "  Encryption: Noise (separate proxy key)");
#else
  ESP_LOGCONFIG(TAG, "  Encryption: disabled");
#endif
  ESP_LOGCONFIG(TAG, "  Announcement: _thread-proxy._tcp; status interval: %lu ms", static_cast<unsigned long>(status_interval_));
  ESP_LOGCONFIG(TAG, "  Node forwarding: not implemented (no UDP ACKs)");
}
}  // namespace esphome::thread_proxy_api
