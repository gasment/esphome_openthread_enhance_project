#include "api_server.h"
#include <cerrno>
#include <cinttypes>
#include "api_connection.h"
#include "esphome/components/network/util.h"
#include "esphome/core/application.h"
#include "esphome/core/defines.h"
#include "esphome/core/hal.h"
#include "esphome/core/log.h"
#include "esphome/core/util.h"
#include "esphome/core/version.h"
#ifdef USE_THREAD_PROXY_CLIENT_HOMEASSISTANT_SERVICES
#include "homeassistant_service.h"
#endif

#ifdef USE_LOGGER
#include "esphome/components/logger/logger.h"
#endif

#include <algorithm>
#include <utility>

namespace esphome::thread_proxy_client {

static const char *const TAG = "thread_proxy_client";

// ThreadProxyClient
ThreadProxyClient *global_thread_proxy_client = nullptr;  // NOLINT(cppcoreguidelines-avoid-non-const-global-variables)



ThreadProxyClient::ThreadProxyClient() { global_thread_proxy_client = this; }

void ThreadProxyClient::socket_failed_(const LogString *msg) {
  ESP_LOGW(TAG, "Socket %s: errno %d", LOG_STR_ARG(msg), errno);
  this->destroy_socket_();
  this->mark_failed();
}

void ThreadProxyClient::setup() {
  this->subscribe_home_assistant_state("thread_sed.control", "", [](StringRef) {});
#ifdef USE_THREAD_PROXY_CLIENT_SLEEP
  auto *sleep = static_cast<deep_sleep_nrf52::DeepSleepComponent *>(this->sed_sleep_);
  if (sleep != nullptr) {
    sleep->add_sleep_guard([this]() { return this->sed_sleep_guard(); });
    auto *wake = new SedWakeTrigger(sleep);
    wake->add_callback([this](deep_sleep_nrf52::WakeupCause, int32_t) { this->sed_wake(); });
  }
#endif

#ifdef USE_LOGGER
  if (logger::global_logger != nullptr) {
    logger::global_logger->add_log_callback(
        this, [](void *self, uint8_t level, const char *tag, const char *message, size_t message_len) {
          static_cast<ThreadProxyClient *>(self)->on_log(level, tag, message, message_len);
        });
  }
#endif

#ifdef USE_CAMERA
  if (camera::Camera::instance() != nullptr && !camera::Camera::instance()->is_internal()) {
    camera::Camera::instance()->add_listener(this);
  }
#endif

  // Initialize last_connected_ for reboot timeout tracking
  this->last_connected_ = App.get_loop_component_start_time();
  // Set warning status if reboot timeout is enabled (suppressed while provisioning
  // is pending so the device waits to be onboarded instead of rebooting).
  if (this->reboot_timeout_ != 0 && !this->provisioning_pending_()) {
    this->status_set_warning(LOG_STR("waiting for client connection"));
  }

  this->udp_.setup(this->port_);
}

void ThreadProxyClient::loop() {
  this->udp_.loop();
  this->sed_tick();
  // Busy batches/normal sleep are not connection loss. Fire ready once initial
  // schema transfer has finished; disconnect only when the selected BR is lost.
  if(this->proxy_was_ready_ && !this->udp_.ready()) {
    this->proxy_was_ready_=false; this->proxy_disconnect_trigger_.trigger();
  } else if(!this->proxy_was_ready_ && this->udp_.ready() && this->udp_.schema_confirmed()) {
    this->proxy_was_ready_=true; this->proxy_ready_trigger_.trigger();
  }
}


void ThreadProxyClient::remove_client_(uint8_t client_index) {
  auto &client = this->clients_[client_index];
  if (this->sed_auto_client_ == client.get()) this->sed_auto_disconnected_ = true;
  if (this->sed_batch_done_ && this->sed_batch_client_ == client.get()) this->sed_batch_disconnected_ = true;

  ESP_LOGV(TAG, "Remove connection %s", client->get_name());

#ifdef USE_THREAD_PROXY_CLIENT_CLIENT_DISCONNECTED_TRIGGER
  // Save client info before closing socket and removal for the trigger
  char peername_buf[socket::SOCKADDR_STR_LEN];
  std::string client_name(client->get_name());
  std::string client_peername(client->get_peername_to(peername_buf));
#endif

  // Read before the swap-and-reset below destroys the connection
  const bool was_authenticated = client->is_authenticated();

  // Close socket now (was deferred from on_fatal_error to allow getpeername)
  client->helper_->close();

  // Swap-and-reset: move the removed client to the trailing slot and null it out so slots
  // [api_connection_count_, N) remain nullptr.
  const uint8_t last_index = this->api_connection_count_ - 1;
  if (client_index < last_index) {
    std::swap(this->clients_[client_index], this->clients_[last_index]);
  }
  // Drop the count before resetting the slot. reset() runs ~APIConnection(), which can reenter the
  // server (e.g. voice_assistant unsubscribes in its disconnect trigger, publishing entity state ->
  // on_*_update iterating active_clients()). Excluding the dying slot from the active range first
  // keeps that reentrant iteration from dereferencing the now-null slot.
  this->api_connection_count_--;
  this->clients_[last_index].reset();

  // Last client disconnected - set warning and start tracking for reboot timeout
  // (suppressed while provisioning is pending - see loop()).
  // Refresh on every authenticated removal, not just the last one, so an
  // unauthenticated straggler removed later (e.g. a port scan, or a dial to
  // a host that accepts TCP but never speaks the API) cannot discard a
  // healthy session's timestamp and trigger a spurious reboot
  if (was_authenticated) {
    this->last_connected_ = App.get_loop_component_start_time();
  }
  if (this->api_connection_count_ == 0 && this->reboot_timeout_ != 0 && !this->provisioning_pending_()) {
    this->status_set_warning(LOG_STR("waiting for client connection"));
  }

#ifdef USE_THREAD_PROXY_CLIENT_CLIENT_DISCONNECTED_TRIGGER
  // Fire trigger after client is removed so api.connected reflects the true state
  this->client_disconnected_trigger_.trigger(client_name, client_peername);
#endif
}



APIConnection *ThreadProxyClient::add_client_(std::unique_ptr<socket::Socket> sock) {
  auto *conn = new APIConnection(std::move(sock), this);  // NOLINT(cppcoreguidelines-owning-memory)
  this->clients_[this->api_connection_count_++].reset(conn);
  conn->start();

  // First client connected - clear warning. The reboot watchdog timestamp is
  // refreshed when an authenticated client is removed (see remove_client_),
  // never on bare TCP connects.
  if (this->api_connection_count_ == 1 && this->reboot_timeout_ != 0 && !this->provisioning_pending_()) {
    this->status_clear_warning();
  }
  return conn;
}


void ThreadProxyClient::dump_config() {
  ESP_LOGCONFIG(TAG,"Thread Proxy client: UDP %u, Thread-local discovery, BR queue ACK",this->port_);
  ESP_LOGCONFIG(TAG,"  Uplink only; no HA connection, TCP listener or SRP service");
}

void ThreadProxyClient::handle_disconnect(APIConnection *conn) {}

// Macro for controller update dispatch
#define API_DISPATCH_UPDATE(entity_type, entity_name) \
  void ThreadProxyClient::on_##entity_name##_update(entity_type *obj) { /* NOLINT(bugprone-macro-parentheses) */ \
    if (obj->is_internal() || this->sed_manual_send_) \
      return; \
    for (auto &c : this->active_clients()) { \
      if (c->flags_.state_subscription) { \
        const bool queued = c->send_##entity_name##_state(obj); \
        this->sed_auto_published(c.get(), queued); \
      } \
    } \
  }

#ifdef USE_BINARY_SENSOR
API_DISPATCH_UPDATE(binary_sensor::BinarySensor, binary_sensor)
#endif

#ifdef USE_COVER
API_DISPATCH_UPDATE(cover::Cover, cover)
#endif

#ifdef USE_FAN
API_DISPATCH_UPDATE(fan::Fan, fan)
#endif

#ifdef USE_LIGHT
API_DISPATCH_UPDATE(light::LightState, light)
#endif

#ifdef USE_SENSOR
API_DISPATCH_UPDATE(sensor::Sensor, sensor)
#endif

#ifdef USE_SWITCH
API_DISPATCH_UPDATE(switch_::Switch, switch)
#endif

#ifdef USE_TEXT_SENSOR
API_DISPATCH_UPDATE(text_sensor::TextSensor, text_sensor)
#endif

#ifdef USE_CLIMATE
API_DISPATCH_UPDATE(climate::Climate, climate)
#endif

#ifdef USE_NUMBER
API_DISPATCH_UPDATE(number::Number, number)
#endif

#ifdef USE_DATETIME_DATE
API_DISPATCH_UPDATE(datetime::DateEntity, date)
#endif

#ifdef USE_DATETIME_TIME
API_DISPATCH_UPDATE(datetime::TimeEntity, time)
#endif

#ifdef USE_DATETIME_DATETIME
API_DISPATCH_UPDATE(datetime::DateTimeEntity, datetime)
#endif

#ifdef USE_TEXT
API_DISPATCH_UPDATE(text::Text, text)
#endif

#ifdef USE_SELECT
API_DISPATCH_UPDATE(select::Select, select)
#endif

#ifdef USE_LOCK
API_DISPATCH_UPDATE(lock::Lock, lock)
#endif

#ifdef USE_VALVE
API_DISPATCH_UPDATE(valve::Valve, valve)
#endif

#ifdef USE_MEDIA_PLAYER
API_DISPATCH_UPDATE(media_player::MediaPlayer, media_player)
#endif

#ifdef USE_WATER_HEATER
API_DISPATCH_UPDATE(water_heater::WaterHeater, water_heater)
#endif

#ifdef USE_EVENT
void ThreadProxyClient::on_event(event::Event *obj) {
  if (obj->is_internal())
    return;
  this->sed_queue_event(obj, obj->get_last_event_type());
}
#endif

#ifdef USE_UPDATE
// Update is a special case - the method is called on_update, not on_update_update
void ThreadProxyClient::on_update(update::UpdateEntity *obj) {
  if (obj->is_internal() || this->sed_manual_send_)
    return;
  for (auto &c : this->active_clients()) {
    if (c->flags_.state_subscription) {
      const bool queued = c->send_update_state(obj);
      this->sed_auto_published(c.get(), queued);
    }
  }
}
#endif

#ifdef USE_ZWAVE_PROXY
void ThreadProxyClient::on_zwave_proxy_request(const ZWaveProxyRequest &msg) {
  // We could add code to manage a second subscription type, but, since this message type is
  //  very infrequent and small, we simply send it to all clients
  for (auto &c : this->active_clients()) {
    if (!c->send_message(msg)) {
      API_LOG_MSG_DROPPED(TAG, "Home ID notification");
    }
  }
}
#endif

#ifdef USE_SERIAL_PROXY_USB_IDENTITY
void ThreadProxyClient::send_serial_proxy_identity(const SerialProxyIdentity &msg) {
  for (auto &c : this->active_clients()) {
    if (c->flags_.serial_proxy_identity_subscription)
      c->send_serial_proxy_identity(msg);
  }
}
#endif

#ifdef USE_IR_RF
void ThreadProxyClient::send_infrared_rf_receive_event([[maybe_unused]] uint32_t device_id, uint32_t key,
                                               const std::vector<int32_t> *timings) {
  InfraredRFReceiveEvent resp{};
#ifdef USE_DEVICES
  resp.device_id = device_id;
#endif
  resp.key = key;
  resp.timings = timings;

  for (auto &c : this->active_clients())
    c->send_infrared_rf_receive_event(resp);
}

#endif

#ifdef USE_ALARM_CONTROL_PANEL
API_DISPATCH_UPDATE(alarm_control_panel::AlarmControlPanel, alarm_control_panel)
#endif

#ifdef USE_THREAD_PROXY_CLIENT_HOMEASSISTANT_SERVICES
void ThreadProxyClient::send_homeassistant_action(const HomeassistantActionRequest &call) {
  bool has_subscriber = false;
  for (auto &client : this->active_clients()) {
    has_subscriber |= client->send_homeassistant_action(call);
  }
  if (!has_subscriber) {
    // Home Assistant subscribes to actions shortly *after* authenticating, so actions
    // fired right at connection time (on_client_connected, on_time_sync, ...) can
    // arrive before the subscription and are lost - warn instead of failing silently.
    ESP_LOGW(TAG, "Home Assistant %s '%.*s' dropped; %s",
             call.is_event ? LOG_STR_LITERAL("event") : LOG_STR_LITERAL("action"),
             static_cast<int>(call.service.size()), call.service.empty() ? "" : call.service.c_str(),
             this->is_connected() ? LOG_STR_LITERAL("client has not subscribed to actions (yet)")
                                  : LOG_STR_LITERAL("no client connected"));
  }
}
#endif  // USE_THREAD_PROXY_CLIENT_HOMEASSISTANT_SERVICES

#ifdef USE_THREAD_PROXY_CLIENT_HOMEASSISTANT_STATES
// Helper to add subscription (reduces duplication)
void ThreadProxyClient::add_state_subscription_(const char *entity_id, const char *attribute,
                                        std::function<void(StringRef)> &&f, bool once) {
  this->state_subs_.push_back(HomeAssistantStateSubscription{
      .entity_id = entity_id, .attribute = attribute, .callback = std::move(f), .once = once,
      // entity_id_dynamic_storage and attribute_dynamic_storage remain nullptr (no heap allocation)
  });
}

// Helper to add subscription with heap-allocated strings (reduces duplication)
void ThreadProxyClient::add_state_subscription_(std::string entity_id, optional<std::string> attribute,
                                        std::function<void(StringRef)> &&f, bool once) {
  HomeAssistantStateSubscription sub;
  // Allocate heap storage for the strings
  sub.entity_id_dynamic_storage = std::make_unique<std::string>(std::move(entity_id));
  sub.entity_id = sub.entity_id_dynamic_storage->c_str();

  if (attribute.has_value()) {
    sub.attribute_dynamic_storage = std::make_unique<std::string>(std::move(attribute.value()));
    sub.attribute = sub.attribute_dynamic_storage->c_str();
  } else {
    sub.attribute = nullptr;
  }

  sub.callback = std::move(f);
  sub.once = once;
  this->state_subs_.push_back(std::move(sub));
}

// New const char* overload (for internal components - zero allocation)
void ThreadProxyClient::subscribe_home_assistant_state(const char *entity_id, const char *attribute,
                                               std::function<void(StringRef)> &&f) {
  this->add_state_subscription_(entity_id, attribute, std::move(f), false);
}

void ThreadProxyClient::get_home_assistant_state(const char *entity_id, const char *attribute,
                                         std::function<void(StringRef)> &&f) {
  this->add_state_subscription_(entity_id, attribute, std::move(f), true);
}

// std::string overload with StringRef callback (zero-allocation callback)
void ThreadProxyClient::subscribe_home_assistant_state(std::string entity_id, optional<std::string> attribute,
                                               std::function<void(StringRef)> &&f) {
  this->add_state_subscription_(std::move(entity_id), std::move(attribute), std::move(f), false);
}

void ThreadProxyClient::get_home_assistant_state(std::string entity_id, optional<std::string> attribute,
                                         std::function<void(StringRef)> &&f) {
  this->add_state_subscription_(std::move(entity_id), std::move(attribute), std::move(f), true);
}

// Legacy helper: wraps std::string callback and delegates to StringRef version
void ThreadProxyClient::add_state_subscription_(std::string entity_id, optional<std::string> attribute,
                                        std::function<void(const std::string &)> &&f, bool once) {
  // Wrap callback to convert StringRef -> std::string, then delegate
  this->add_state_subscription_(std::move(entity_id), std::move(attribute),
                                std::function<void(StringRef)>([f = std::move(f)](StringRef state) { f(state.str()); }),
                                once);
}

// Legacy std::string overload (for custom_api_device.h - converts StringRef to std::string)
void ThreadProxyClient::subscribe_home_assistant_state(std::string entity_id, optional<std::string> attribute,
                                               std::function<void(const std::string &)> &&f) {
  this->add_state_subscription_(std::move(entity_id), std::move(attribute), std::move(f), false);
}

void ThreadProxyClient::get_home_assistant_state(std::string entity_id, optional<std::string> attribute,
                                         std::function<void(const std::string &)> &&f) {
  this->add_state_subscription_(std::move(entity_id), std::move(attribute), std::move(f), true);
}

const std::vector<ThreadProxyClient::HomeAssistantStateSubscription> &ThreadProxyClient::get_state_subs() const {
  return this->state_subs_;
}
#endif


#ifdef USE_HOMEASSISTANT_TIME
void ThreadProxyClient::request_time() {
  for (auto &client : this->active_clients()) {
    if (!client->flags_.remove && client->is_authenticated()) {
      client->send_time_request();
      return;  // Only request from one client to avoid clock conflicts
    }
  }
}
#endif

bool ThreadProxyClient::is_connected_with_state_subscription() const {
  for (uint8_t i = 0; i < this->api_connection_count_; i++) {
    if (this->clients_[i]->flags_.state_subscription) {
      return true;
    }
  }
  return false;
}

#ifdef USE_LOGGER
void ThreadProxyClient::on_log(uint8_t level, const char *tag, const char *message, size_t message_len) {
  if (this->shutting_down_) {
    // Don't try to send logs during shutdown
    // as it could result in a recursion and
    // we would be filling a buffer we are trying to clear
    return;
  }
  for (auto &c : this->active_clients()) {
    if (!c->flags_.remove && c->get_log_subscription_level() >= level)
      c->try_send_log_message(level, tag, message, message_len);
  }
}
#endif

#ifdef USE_CAMERA
void ThreadProxyClient::on_camera_image(const std::shared_ptr<camera::CameraImage> &image) {
  for (auto &c : this->active_clients()) {
    if (!c->flags_.remove)
      c->set_camera_state(image);
  }
}
#endif

void ThreadProxyClient::on_shutdown() {
  this->shutting_down_ = true;

  // Close the listening socket to prevent new connections
  this->destroy_socket_();

  // Change batch delay to 5ms for quick flushing during shutdown
  this->batch_delay_ = 5;

  // Send disconnect requests to all connected clients
  for (auto &c : this->active_clients()) {
    DisconnectRequest req;
    if (!c->send_message(req)) {
      // If we can't send the disconnect request directly (tx_buffer full),
      // schedule it at the front of the batch so it will be sent with priority
      c->schedule_message_front_(nullptr, DisconnectRequest::MESSAGE_TYPE, DisconnectRequest::ESTIMATED_SIZE);
    }
  }
}

bool ThreadProxyClient::teardown() {
  // If network is disconnected, no point trying to flush buffers
  if (!network::is_connected()) {
    return true;
  }
  this->loop();

  // Return true only when all clients have been torn down
  return this->api_connection_count_ == 0;
}


}  // namespace esphome::thread_proxy_client
