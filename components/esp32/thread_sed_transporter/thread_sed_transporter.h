#pragma once
#include "esphome/core/defines.h"

#include <vector>
#include <atomic>
#include <functional>
#include "esphome/components/openthread/openthread.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include <deque>
#include "openthread/udp.h"
#include "cJSON.h"
#include "esphome/components/thread_entity_bridge/entity_bridge.h"
#ifdef USE_SWITCH
#include "esphome/components/switch/switch.h"
#endif
#ifdef USE_NUMBER
#include "esphome/components/number/number.h"
#endif
#ifdef USE_SELECT
#include "esphome/components/select/select.h"
#endif
#ifdef USE_BUTTON
#include "esphome/components/button/button.h"
#endif
#ifdef USE_TEXT
#include "esphome/components/text/text.h"
#endif
#include <cmath>
#include <cstring>

#include "esphome/core/component.h"
#include "esphome/core/helpers.h"
#include "esphome/components/sensor/sensor.h"
#include "esphome/components/binary_sensor/binary_sensor.h"
#include "esphome/components/text_sensor/text_sensor.h"
#include "delta.h"

namespace esphome::thread_sed_transporter {

// Baselines describe successfully delivered snapshots, not threshold crossings.
class ReportEntry {
 public:
  virtual ~ReportEntry() = default;
  ReportOutcome stage(const std::string &snapshot, bool force, uint64_t generation);
  cJSON *value() const { return cJSON_Parse(pending_.c_str()); }
  std::string snapshot() const;
  void confirm(const std::string &snapshot, uint64_t order, uint64_t generation);
  virtual bool valid_snapshot(const cJSON *value) const = 0;
  void set_identity(const std::string &id) { id_ = id; }
  const std::string &identity() const { return id_; }
  virtual cJSON *descriptor() const { return endpoint_.describe(); }
  ReportOutcome outcome{ReportOutcome::INVALID};
  void dump_config();

 protected:
  virtual bool changed_(const cJSON *value) const = 0;
  virtual void commit_(const cJSON *value) = 0;
  std::string id_, pending_;
  thread_entity_bridge::Endpoint endpoint_{};
  uint64_t confirmed_order_{0};
  uint64_t confirmed_generation_{0};
};

class ReportSensor : public ReportEntry {
 public:
  bool valid_snapshot(const cJSON *v) const override {
    return cJSON_IsNumber(v) && finite_sample(static_cast<float>(v->valuedouble));
  }
  void set_delta(float delta) { delta_ = delta; }
  void set_source(sensor::Sensor *source) { endpoint_ = thread_entity_bridge::bind(id_, source); }
 protected:
  bool changed_(const cJSON *v) const override {
    return should_report(static_cast<float>(v->valuedouble), baseline_.value_or(0), delta_, baseline_.has_value());
  }
  void commit_(const cJSON *v) override { baseline_ = static_cast<float>(v->valuedouble); }
  optional<float> baseline_;
  float delta_{0};
};

class ReportBinarySensor : public ReportEntry {
 public:
  bool valid_snapshot(const cJSON *v) const override { return cJSON_IsBool(v); }
  void set_initial_state_metadata(bool value) { trigger_on_initial_state_ = value; }
  cJSON *descriptor() const override {
    auto *json = ReportEntry::descriptor();
    if (json) cJSON_AddBoolToObject(json, "trigger_on_initial_state", trigger_on_initial_state_);
    return json;
  }
  void set_source(binary_sensor::BinarySensor *source) { endpoint_ = thread_entity_bridge::bind(id_, source); }
 protected:
  bool changed_(const cJSON *v) const override { return !baseline_.has_value() || *baseline_ != bool(cJSON_IsTrue(v)); }
  void commit_(const cJSON *v) override { baseline_ = bool(cJSON_IsTrue(v)); }
  bool trigger_on_initial_state_{true};
  optional<bool> baseline_;
};

class ReportTextSensor : public ReportEntry {
 public:
  void set_source(text_sensor::TextSensor *source) { endpoint_ = thread_entity_bridge::bind(id_, source); }
  bool valid_snapshot(const cJSON *v) const override {
    return cJSON_IsString(v) && v->valuestring && std::strlen(v->valuestring) <= max_length_;
  }
  void set_max_length(uint32_t length) { max_length_ = length; }
  cJSON *descriptor() const override {
    auto *json = ReportEntry::descriptor();
    if (json) cJSON_SetNumberValue(cJSON_GetObjectItemCaseSensitive(json, "max_length"), max_length_);
    return json;
  }
 protected:
  bool changed_(const cJSON *v) const override { return !baseline_.has_value() || *baseline_ != v->valuestring; }
  void commit_(const cJSON *v) override { baseline_ = v->valuestring; }
  uint32_t max_length_{32};
  optional<std::string> baseline_;
};


#ifdef USE_EVENT
// Each explicit send is a new occurrence, even when its type is unchanged.
class ReportEvent : public ReportEntry {
 public:
  void set_source(event::Event *source) { source_ = source; endpoint_ = thread_entity_bridge::bind(id_, source); }
  bool valid_snapshot(const cJSON *value) const override {
    if (!cJSON_IsString(value) || !value->valuestring || !source_) return false;
    for (const auto *type : source_->get_event_types()) if (std::strcmp(type, value->valuestring) == 0) return true;
    return false;
  }
 protected:
  bool changed_(const cJSON *) const override { return true; }
  void commit_(const cJSON *) override {}  // Events have no retained state/baseline.
  event::Event *source_{nullptr};
};
#endif

class ThreadSedTransporter : public Component {
 public:
  // Application-layer gateway reachability; Thread diagnostics belong to openthread_info.
  bool is_gateway_connencted() const {
    return !shutdown_ && !is_failed() && opened_ && ot_ != nullptr && ot_->is_connected() && confirmed_ &&
           target_valid_() && uint32_t(millis() - last_gateway_reply_) < heartbeat_ms_ * 3;
  }
  void discovery();
  void setup() override;
  void loop() override;
  using ResultCallback = std::function<void(bool, uint32_t)>;
  struct SendValue {
    std::string id;
    bool overridden{false};
    std::string json;
  };
  void send_values(const std::vector<SendValue> &values, bool force = false,
                   uint32_t ack_timeout = 0, uint8_t ack_resend = 0, ResultCallback callback = {});
  void send(const std::vector<std::string> &entity_ids, bool force = false,
            uint32_t ack_timeout = 0, uint8_t ack_resend = 0, ResultCallback callback = {});
  bool is_busy() const;
  void add_result_guard(std::function<bool()> guard) { result_guards_.push_back(std::move(guard)); }
  void set_active_poll_period(uint32_t value) { active_poll_ms_ = value; }
  bool teardown() override;
  void dump_config() override;
  void on_shutdown() override;
  float get_setup_priority() const override { return setup_priority::LATE; }
  void set_openthread(openthread::OpenThreadComponent *ot) { ot_ = ot; }
  void set_node(const std::string &id, const std::string &name, uint32_t revision) {
    node_ = id; name_ = name; revision_ = revision;
  }
  void set_discovery(const std::string &id, const std::string &group, uint16_t port, uint32_t timeout) {
    expected_gateway_ = id; group_ = group; discovery_port_ = port; discovery_timeout_ = timeout;
  }
  void set_timing(uint32_t timeout, uint32_t heartbeat) { timeout_ms_ = timeout; heartbeat_ms_ = heartbeat;
  }
  void set_control_ready(bool value) { ready_reader_ = [value]() { return value; }; }
  void set_control_ready_reader(std::function<bool()> reader) { ready_reader_ = std::move(reader); }
  void add_sensor(ReportEntry *sensor) { sensors_.push_back(sensor); }
  template<class T> void add_control(const std::string &id, T *source) {
    auto endpoint = thread_entity_bridge::bind(id, source);
    Control control;
    control.id = id;
    control.type = endpoint.kind;
    control.descriptor = endpoint.describe;
    control.read = endpoint.read;
    control.execute = [endpoint](const cJSON *value) {
      auto *description = endpoint.describe();
      const bool valid = description && thread_entity_bridge::validate_value(
          endpoint.kind, thread_entity_bridge::field(description, "capabilities"), value, true);
      cJSON_Delete(description);
      return valid && endpoint.execute(value);
    };
    controls_.push_back(std::move(control));
  }
  template<typename F> void add_on_complete_callback(F &&callback) { complete_callbacks_.add(std::forward<F>(callback)); }
  template<typename F> void add_on_report_success_callback(F &&callback) { success_callbacks_.add(std::forward<F>(callback)); }
  template<typename F> void add_on_report_fail_callback(F &&callback) { fail_callbacks_.add(std::forward<F>(callback)); }

 protected:
  // Per-component RAM survives light sleep. Deep sleep/reboot starts a full sync.
  struct RetainedTransport {
    uint32_t ttl_s{0};
    uint64_t expires{0};
    otIp6Address address{};
    uint16_t port{0};
    char gateway[33]{};
  } retained;
  // No flash writes: these baselines and transport state live as long as the component.
  static void retain() {}
  struct Snapshot {
    std::string id, value;
    ReportEntry *entry{nullptr};
    size_t control{SIZE_MAX};
  };
  struct Request {
    uint32_t id{0}, ack_timeout{0};
    uint8_t ack_resend{0};
    std::vector<Snapshot> snapshots;
    bool force{false};
    uint64_t order{0};
  };
  struct Result { uint32_t id; bool notify_global; ResultCallback callback; };
  struct Terminal { bool success; uint32_t id; };
  void enqueue_(Request request, ResultCallback callback);
  void start_next_();
  void service_results_();
  void service_poll_();
  bool close_();
  std::deque<Request> requests_;
  Request active_;
  std::vector<Result> results_;
  std::vector<Terminal> terminal_;
  std::vector<std::function<bool()>> result_guards_;
  bool dispatching_{false};
  uint32_t active_poll_ms_{100}, saved_poll_ms_{0};
  bool poll_overridden_{false};
  struct Control {
    std::string id, type, previous;
    uint64_t confirmed_order{0};
    uint64_t confirmed_generation{0};
    std::function<cJSON *()> descriptor, read;
    std::function<bool(const cJSON *)> execute;
  };
  struct Packet { otIp6Address peer; uint16_t port; size_t size; char data[1233]; };
  struct CompletedCommand { uint32_t id; std::string response; };
  struct Commit {
    ReportEntry *entry;
    size_t control;
    std::string snapshot;
  };
  void confirm_(const std::vector<Commit> &commits, uint64_t order, uint64_t generation);
  void request_full_sync_() { ++full_generation_; }
  struct PendingAck {
    uint32_t id, started, attempt_at, timeout;
    uint8_t resend_limit{0}, resends{0};
    bool recovered{false}, recovering{false};
    uint32_t recovery_at{0};
    std::vector<std::string> payloads;
    std::vector<Commit> commits;
    uint64_t order{0}, full_generation{0};
    uint16_t parts, port;
    otIp6Address peer;
    std::string gateway;
    enum class Result { WAITING, SUCCESS } result{Result::WAITING};
  };
  enum class Phase { IDLE, SEND };
  static void receive_(void *, otMessage *, const otMessageInfo *);
  bool open_();
  bool send_(const otIp6Address &, uint16_t, const std::string &);
  bool target_valid_() const;
  void renew_target_();
  bool ensure_target_();
  void process_(const Packet &);
  cJSON *envelope_(const char *type) const;
  bool prepare_manifest_();
  void service_manifest_();
  void stage_();
  void transmit_();
  void service_report_ack_();
  void receive_report_ack_(const Packet &packet, const cJSON *json);
  void track_report_ack_(std::vector<std::string> payloads, std::vector<Commit> commits = {},
                         uint64_t generation = 0);
  void report_result_(bool success, uint32_t report_id);
  void finish_();
  void presence_(bool ready, bool sleeping = false);
  void command_(const cJSON *);
  void service_presence_();
  bool ready_() const { return ready_reader_ && ready_reader_(); }
  std::function<bool()> ready_reader_;
  std::vector<ReportEntry *> sensors_;
  std::vector<Control> controls_;
  std::vector<std::string> manifest_;
  std::vector<CompletedCommand> commands_;
  std::vector<PendingAck> pending_acks_;
  uint64_t next_order_{0}, full_generation_{1};
  uint32_t next_report_id_{0}, report_id_{0}, report_ack_timeout_{0};
  uint8_t report_ack_resend_{0};
  bool need_ack_{false}, ack_tracked_{false}, ack_immediate_failure_{false};
  openthread::OpenThreadComponent *ot_{nullptr};
  otUdpSocket socket_{};
  static constexpr size_t RX_QUEUE_SIZE = 6;
  QueueHandle_t packets_{nullptr};
  bool queue_initialized_{false};
  std::atomic<bool> shutdown_{false};
  std::atomic<uint32_t> drops_{0};
  std::string node_, name_, session_, expected_gateway_, group_{"ff13::8000:8562"};
  uint32_t revision_{1}, sequence_{0}, request_id_{0};
  uint16_t discovery_port_{18512};
  uint32_t discovery_timeout_{1000}, timeout_ms_{5000}, heartbeat_ms_{10000};
  uint32_t started_{0}, query_at_{0}, manifest_at_{0}, heartbeat_at_{0};
  uint32_t last_gateway_reply_{0}, discovery_started_{0};
  bool discovery_active_{false};
  uint8_t query_attempts_{0}, manifest_attempts_{0};
  bool opened_{false}, querying_{false}, confirmed_{false}, reporting_{false}, staged_{false};
  bool last_ready_{false}, presence_dirty_{true};
  Phase phase_{Phase::IDLE};
  ReportSummary summary_;
  CallbackManager<void(bool, uint32_t, uint32_t, uint32_t, uint32_t)> complete_callbacks_;
  CallbackManager<void(uint32_t)> success_callbacks_, fail_callbacks_;
};
}  // namespace esphome::thread_sed_transporter
