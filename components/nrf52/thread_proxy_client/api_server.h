#pragma once

#include "esphome/core/defines.h"
#include "api_buffer.h"
#include "udp_transport.h"
// Must precede clients_ so APIConnection is complete for default_delete (libc++).
#include "api_connection.h"
#include "api_pb2.h"
#include "api_pb2_service.h"
#include "esphome/components/socket/socket.h"
#include "esphome/core/automation.h"
#include "esphome/core/component.h"
#include "esphome/core/entity_includes.h"
#include "esphome/core/log.h"
#include "esphome/core/string_ref.h"
#ifdef USE_PROVISIONING
#include "esphome/components/provisioning/provisioning.h"
#endif
#ifdef USE_LOGGER
#include "esphome/components/logger/logger.h"
#endif
#ifdef USE_CAMERA
#include "esphome/components/camera/camera.h"
#endif

#include <array>
#include <memory>
#include <vector>
#include "sed_session.h"

namespace esphome::thread_proxy_client {



class ThreadProxyClient final : public Component
#ifdef USE_CAMERA
    ,
                        public camera::CameraListener
#endif
{
 public:
  ThreadProxyClient();
  friend class ProxyUdpTransport;
  friend class APIUDPFrameHelper;
  ProxyUdpTransport udp_{this};
  void set_udp_key(const std::array<uint8_t,32> &key) { udp_.set_key(key); }
  Trigger<> *get_ready_trigger() { return &proxy_ready_trigger_; }
  Trigger<> *get_disconnect_trigger() { return &proxy_disconnect_trigger_; }
  Trigger<> proxy_ready_trigger_, proxy_disconnect_trigger_;
  bool proxy_was_ready_{false};
  void set_sleep_budget(uint32_t ms) { sed_sleep_budget_ = ms; }
  bool sed_transport_ready() const {
    return true;
  }
  bool sed_has_sleep() const { return sed_sleep_ != nullptr || sed_expected_sleep_seconds_ != 0; }
#ifdef USE_THREAD_PROXY_CLIENT_SLEEP
  void set_deep_sleep(deep_sleep_nrf52::DeepSleepComponent *sleep) { sed_sleep_ = sleep; }
#endif
  uint32_t sed_sleep_seconds() const;
  void set_sleep_seconds(uint32_t seconds) { sed_expected_sleep_seconds_ = seconds; }
  bool sed_sleep_guard();
  void sed_poll(bool fast);
  void sed_queue_event(EntityBase *entity, StringRef type);
  void sed_wake();
  void sed_tick();
  bool sed_send(const char *service, const std::string &value);
  void sed_control(APIConnection *client, StringRef value);
  void set_manual_send(bool value) { sed_manual_send_ = value; }
  bool sed_manual_send() const { return sed_manual_send_; }
  bool sed_is_ready() const;
  bool sed_begin_batch(const void *owner, uint32_t timeout, uint32_t resend, const std::vector<EntityBase *> &entities, std::function<void(bool)> done);
  bool sed_batch_wants(EntityBase *entity) const;
  void enable_auto_ack(uint32_t timeout, uint32_t resend) { sed_auto_enabled_ = true; sed_auto_timeout_ = timeout; sed_auto_resend_ = resend; }
  Trigger<std::string, uint32_t, bool> *get_auto_ack_trigger() { return &sed_auto_ack_trigger_; }
  Trigger<std::string, std::string> *get_auto_ack_error_trigger() { return &sed_auto_error_trigger_; }
  bool sed_auto_pending() const { return sed_auto_enabled_ && sed_auto_generation_ != sed_auto_confirmed_; }
  bool sed_auto_callbacks_running() {
    return sed_auto_ack_trigger_.is_action_running() || sed_auto_error_trigger_.is_action_running();
  }
  void sed_auto_published(APIConnection *client, bool queued = true);
  void sed_auto_tick();
  void sed_auto_finish(bool success, const char *reason);
  void sed_cancel_batch(const void *owner);
  void sed_batch_tick();
  void sed_finish_batch(bool success, const char *reason);
  bool sed_send_to(APIConnection *client, const char *service, const std::string &value);

  void setup() override;
  uint16_t get_port() const { return this->port_; }
  float get_setup_priority() const override { return setup_priority::AFTER_WIFI; }
  void loop() override;
  void dump_config() override;
  void on_shutdown() override;
  bool teardown() override;
#ifdef USE_LOGGER
  void on_log(uint8_t level, const char *tag, const char *message, size_t message_len);
#endif
#ifdef USE_CAMERA
  void on_camera_image(const std::shared_ptr<camera::CameraImage> &image) override;
#endif
  void set_port(uint16_t port) { this->port_ = port; }
  void set_reboot_timeout(uint32_t reboot_timeout) { this->reboot_timeout_ = reboot_timeout; }
  void set_batch_delay(uint16_t batch_delay) { this->batch_delay_ = batch_delay; }
  uint16_t get_batch_delay() const { return batch_delay_; }
  void set_listen_backlog(uint8_t listen_backlog) { this->listen_backlog_ = listen_backlog; }

  // Get reference to shared buffer for API connections
  APIBuffer &get_shared_buffer_ref() { return shared_write_buffer_; }


  void handle_disconnect(APIConnection *conn);
#ifdef USE_BINARY_SENSOR
  void on_binary_sensor_update(binary_sensor::BinarySensor *obj);
#endif
#ifdef USE_COVER
  void on_cover_update(cover::Cover *obj);
#endif
#ifdef USE_FAN
  void on_fan_update(fan::Fan *obj);
#endif
#ifdef USE_LIGHT
  void on_light_update(light::LightState *obj);
#endif
#ifdef USE_SENSOR
  void on_sensor_update(sensor::Sensor *obj);
#endif
#ifdef USE_SWITCH
  void on_switch_update(switch_::Switch *obj);
#endif
#ifdef USE_TEXT_SENSOR
  void on_text_sensor_update(text_sensor::TextSensor *obj);
#endif
#ifdef USE_CLIMATE
  void on_climate_update(climate::Climate *obj);
#endif
#ifdef USE_NUMBER
  void on_number_update(number::Number *obj);
#endif
#ifdef USE_DATETIME_DATE
  void on_date_update(datetime::DateEntity *obj);
#endif
#ifdef USE_DATETIME_TIME
  void on_time_update(datetime::TimeEntity *obj);
#endif
#ifdef USE_DATETIME_DATETIME
  void on_datetime_update(datetime::DateTimeEntity *obj);
#endif
#ifdef USE_TEXT
  void on_text_update(text::Text *obj);
#endif
#ifdef USE_SELECT
  void on_select_update(select::Select *obj);
#endif
#ifdef USE_LOCK
  void on_lock_update(lock::Lock *obj);
#endif
#ifdef USE_VALVE
  void on_valve_update(valve::Valve *obj);
#endif
#ifdef USE_MEDIA_PLAYER
  void on_media_player_update(media_player::MediaPlayer *obj);
#endif
#ifdef USE_WATER_HEATER
  void on_water_heater_update(water_heater::WaterHeater *obj);
#endif
#ifdef USE_THREAD_PROXY_CLIENT_HOMEASSISTANT_SERVICES
  void send_homeassistant_action(const HomeassistantActionRequest &call);

#endif  // USE_THREAD_PROXY_CLIENT_HOMEASSISTANT_SERVICES
#ifdef USE_HOMEASSISTANT_TIME
  void request_time();
#endif

#ifdef USE_ALARM_CONTROL_PANEL
  void on_alarm_control_panel_update(alarm_control_panel::AlarmControlPanel *obj);
#endif
#ifdef USE_EVENT
  void on_event(event::Event *obj);
#endif
#ifdef USE_UPDATE
  void on_update(update::UpdateEntity *obj);
#endif
#ifdef USE_ZWAVE_PROXY
  void on_zwave_proxy_request(const ZWaveProxyRequest &msg);
#endif
#ifdef USE_SERIAL_PROXY_USB_IDENTITY
  /// Tell every subscribed client that a serial proxy port's identity changed
  void send_serial_proxy_identity(const SerialProxyIdentity &msg);
#endif
#ifdef USE_IR_RF
  void send_infrared_rf_receive_event(uint32_t device_id, uint32_t key, const std::vector<int32_t> *timings);
#endif

  bool is_connected() const { return this->api_connection_count_ != 0; }
  bool is_connected_with_state_subscription() const;

  // Range-for view over the populated slice [0, api_connection_count_). Read-only with respect
  // to ownership; callers get `const unique_ptr&` so they can invoke non-const methods on the
  // APIConnection but cannot reset/move the slot and break the count invariant.
  using APIConnectionPtr = std::unique_ptr<APIConnection>;
  class ActiveClientsView {
    const APIConnectionPtr *begin_;
    const APIConnectionPtr *end_;

   public:
    ActiveClientsView(const APIConnectionPtr *b, const APIConnectionPtr *e) : begin_(b), end_(e) {}
    const APIConnectionPtr *begin() const { return this->begin_; }
    const APIConnectionPtr *end() const { return this->end_; }
  };
  ActiveClientsView active_clients() const {
    return {this->clients_.data(), this->clients_.data() + this->api_connection_count_};
  }

#ifdef USE_THREAD_PROXY_CLIENT_HOMEASSISTANT_STATES
  struct HomeAssistantStateSubscription {
    const char *entity_id;  // Pointer to flash (internal) or heap (external)
    const char *attribute;  // Pointer to flash or nullptr (nullptr means no attribute)
    std::function<void(StringRef)> callback;
    bool once;

    // Dynamic storage for external components using std::string API (custom_api_device.h)
    // These are only allocated when using the std::string overload (nullptr for const char* overload)
    std::unique_ptr<std::string> entity_id_dynamic_storage;
    std::unique_ptr<std::string> attribute_dynamic_storage;
  };

  // New const char* overload (for internal components - zero allocation)
  void subscribe_home_assistant_state(const char *entity_id, const char *attribute, std::function<void(StringRef)> &&f);
  void get_home_assistant_state(const char *entity_id, const char *attribute, std::function<void(StringRef)> &&f);

  // std::string overload with StringRef callback (for custom_api_device.h with zero-allocation callback)
  void subscribe_home_assistant_state(std::string entity_id, optional<std::string> attribute,
                                      std::function<void(StringRef)> &&f);
  void get_home_assistant_state(std::string entity_id, optional<std::string> attribute,
                                std::function<void(StringRef)> &&f);

  // Legacy std::string overload (for custom_api_device.h - converts StringRef to std::string for callback)
  void subscribe_home_assistant_state(std::string entity_id, optional<std::string> attribute,
                                      std::function<void(const std::string &)> &&f);
  void get_home_assistant_state(std::string entity_id, optional<std::string> attribute,
                                std::function<void(const std::string &)> &&f);

  const std::vector<HomeAssistantStateSubscription> &get_state_subs() const;
#endif

#ifdef USE_THREAD_PROXY_CLIENT_CLIENT_CONNECTED_TRIGGER
  Trigger<std::string, std::string> *get_client_connected_trigger() { return &this->client_connected_trigger_; }
#endif
#ifdef USE_THREAD_PROXY_CLIENT_CLIENT_DISCONNECTED_TRIGGER
  Trigger<std::string, std::string> *get_client_disconnected_trigger() { return &this->client_disconnected_trigger_; }
#endif

 protected:
  // Accept incoming socket connections. Only called when socket has pending connections.
  void __attribute__((noinline)) accept_new_connections_();
  /// Takes the socket into a new connection and starts it; callers must have
  /// checked at_client_limit_() first
  APIConnection *add_client_(std::unique_ptr<socket::Socket> sock);
  bool at_client_limit_() const { return this->api_connection_count_ >= MAX_THREAD_PROXY_CLIENT_CONNECTIONS; }
  // Remove a disconnected client by index. Swaps with the last populated slot and resets it.
  void __attribute__((noinline)) remove_client_(uint8_t client_index);

#ifdef USE_PROVISIONING
  // True while a configured provisioning window is still pending (the device is
  // unprovisioned). Suppresses the reboot timeout and its warning so the device is
  // not auto-rebooted while waiting to be provisioned. False when no provisioning
  // window is configured.
  bool provisioning_pending_() const {
    return provisioning::global_provisioning_manager != nullptr &&
           provisioning::global_provisioning_manager->window_pending();
  }
#else
  bool provisioning_pending_() const { return false; }
#endif

#ifdef USE_THREAD_PROXY_CLIENT_HOMEASSISTANT_STATES
  // Helper methods to reduce code duplication
  void add_state_subscription_(const char *entity_id, const char *attribute, std::function<void(StringRef)> &&f,
                               bool once);
  void add_state_subscription_(std::string entity_id, optional<std::string> attribute,
                               std::function<void(StringRef)> &&f, bool once);
  // Legacy helper: wraps std::string callback and delegates to StringRef version
  void add_state_subscription_(std::string entity_id, optional<std::string> attribute,
                               std::function<void(const std::string &)> &&f, bool once);
#endif  // USE_THREAD_PROXY_CLIENT_HOMEASSISTANT_STATES
  // No explicit close() needed — listen sockets have no active connections on
  // failure/shutdown. Destructor handles fd cleanup (close or abort per platform).
  inline void destroy_socket_() {
    delete this->socket_;
    this->socket_ = nullptr;
  }
  /// Log the failure, drop the listen socket, and mark the component failed
  /// unless this build can still dial out
  void socket_failed_(const LogString *msg);
  // Pointers and pointer-like types first (4 bytes each)
  socket::ListenSocket *socket_{nullptr};
#ifdef USE_THREAD_PROXY_CLIENT_CLIENT_CONNECTED_TRIGGER
  Trigger<std::string, std::string> client_connected_trigger_;
#endif
#ifdef USE_THREAD_PROXY_CLIENT_CLIENT_DISCONNECTED_TRIGGER
  Trigger<std::string, std::string> client_disconnected_trigger_;
#endif

  // 4-byte aligned types
  uint32_t reboot_timeout_{900000};  // Keep in sync with DEFAULT_REBOOT_TIMEOUT in __init__.py
  uint32_t last_connected_{0};

  // Slots [0, api_connection_count_) are populated; trailing slots are always nullptr.
  std::array<APIConnectionPtr, MAX_THREAD_PROXY_CLIENT_CONNECTIONS> clients_{};
  // Vectors and strings (12 bytes each on 32-bit)
  // Shared proto write buffer for all connections.
  // Not pre-allocated: all send paths call prepare_first_message_buffer() which
  // reserves the exact needed size. Pre-allocating here would cause heap fragmentation
  // since the buffer would almost always reallocate on first use.
  APIBuffer shared_write_buffer_;
#ifdef USE_THREAD_PROXY_CLIENT_HOMEASSISTANT_STATES
  std::vector<HomeAssistantStateSubscription> state_subs_;
#endif

  // Group smaller types together
  uint16_t port_{6064};        // Keep in sync with DEFAULT_PORT in __init__.py
  uint16_t batch_delay_{100};  // Keep in sync with DEFAULT_BATCH_DELAY in __init__.py
  uint8_t listen_backlog_{4};  // Keep in sync with DEFAULT_LISTEN_BACKLOG in __init__.py
  bool sed_auto_enabled_{false}, sed_auto_waiting_{false}, sed_auto_acked_{false}, sed_auto_disconnected_{false};
  APIConnection *sed_auto_client_{nullptr};
  uint32_t sed_auto_generation_{0}, sed_auto_confirmed_{0}, sed_auto_marker_generation_{0};
  uint32_t sed_auto_sequence_{0}, sed_auto_started_{0}, sed_auto_timeout_{500};
  uint32_t sed_auto_ack_started_{0}, sed_auto_event_cursor_{0}, sed_auto_marker_event_cutoff_{0};
  uint16_t sed_auto_resend_{2}, sed_auto_retries_left_{0};
  std::string sed_auto_token_;
  const char *sed_auto_error_{nullptr};
  Trigger<std::string, uint32_t, bool> sed_auto_ack_trigger_;
  Trigger<std::string, std::string> sed_auto_error_trigger_;
  bool sed_manual_send_{false};
  APIConnection *sed_batch_client_{nullptr};
  const void *sed_batch_owner_{nullptr};
  std::function<void(bool)> sed_batch_done_;
  std::string sed_batch_token_;
  std::vector<EntityBase *> sed_batch_entities_;
  uint32_t sed_batch_sequence_{0}, sed_batch_started_{0}, sed_batch_timeout_{0};
  uint32_t sed_batch_ack_started_{0};
  uint16_t sed_batch_resend_{0}, sed_batch_retries_left_{0};
  const char *sed_batch_delivery_error_{nullptr};
  uint32_t sed_batch_cutoff_{0}, sed_batch_event_cursor_{0};
  bool sed_batch_snapshot_started_{false};
  bool sed_batch_marker_sent_{false}, sed_batch_acked_{false}, sed_batch_disconnected_{false};
  struct SedEvent { uint32_t sequence; uint32_t key; std::string type; EntityBase *entity; };
  std::vector<SedEvent> sed_events_;
  uint32_t sed_expected_sleep_seconds_{0};
  uint32_t sed_event_sequence_{0}, sed_boot_{0}, sed_event_sent_{0}, sed_saved_poll_{0};
  bool sed_fast_poll_{false};
  void *sed_sleep_{nullptr};
  bool sed_sleep_pending_{false}, sed_sleep_ack_{false}, sed_sleep_finishing_{false};
  uint32_t sed_sleep_started_{0}, sed_sleep_budget_{10000}, sed_token_{0};
  uint32_t sed_marker_sent_{0};
  bool shutting_down_ = false;
  uint8_t api_connection_count_{0};

};

extern ThreadProxyClient *global_thread_proxy_client;  // NOLINT(cppcoreguidelines-avoid-non-const-global-variables)

template<typename... Ts> class CanSleepCondition : public Condition<Ts...> {
 public:
  explicit CanSleepCondition(ThreadProxyClient *parent) : parent_(parent) {}
  bool check(const Ts &...x) override { return this->parent_->sed_sleep_guard(); }
 protected:
  ThreadProxyClient *parent_;
};

template<typename... Ts> class APIConnectedCondition final : public Condition<Ts...> {
  TEMPLATABLE_VALUE(bool, state_subscription_only)
 public:
  bool check(const Ts &...x) override {
    if (this->state_subscription_only_.value(x...)) {
      return global_thread_proxy_client->is_connected_with_state_subscription();
    }
    return global_thread_proxy_client->is_connected();
  }
};

}  // namespace esphome::thread_proxy_client
#include "sed_automation.h"
