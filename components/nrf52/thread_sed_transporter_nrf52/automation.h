#pragma once

#include "esphome/core/automation.h"
#include "thread_sed_transporter_nrf52.h"

namespace esphome::thread_sed_transporter_nrf52 {

template<typename... Ts> class SendAction : public Action<Ts...> {
 public:
  explicit SendAction(ThreadSedTransporter *parent) : parent_(parent) {
    parent_->add_result_guard([this]() { return !success_.is_action_running() && !fail_.is_action_running(); });
  }
  void set_ack_timeout(uint32_t value) { ack_timeout_ = value; }
  void set_ack_resend(uint8_t value) { ack_resend_ = value; }
  void add_entity_id(const std::string &id) { entities_.push_back({id, {}}); }
  template<typename V> void add_text_state(const std::string &id, V value) {
    TemplatableValue<std::string, Ts...> state(value);
    entities_.push_back({id, [state](Ts... x) mutable {
      auto text = state.value(x...);
      if (text.find('\0') != std::string::npos) return std::string{};
      return serialize_(cJSON_CreateString(text.c_str()));
    }});
  }
  template<typename V> void add_numeric_state(const std::string &id, V value) {
    TemplatableValue<float, Ts...> state(value);
    entities_.push_back({id, [state](Ts... x) mutable {
      float number = state.value(x...);
      return std::isfinite(number) ? serialize_(cJSON_CreateNumber(number)) : std::string{};
    }});
  }
  template<typename V> void add_binary_state(const std::string &id, V value) {
    TemplatableValue<bool, Ts...> state(value);
    entities_.push_back({id, [state](Ts... x) mutable { return serialize_(cJSON_CreateBool(state.value(x...))); }});
  }
  TEMPLATABLE_VALUE(bool, force)
  Trigger<uint32_t> *get_success_trigger() { return &success_; }
  Trigger<uint32_t> *get_fail_trigger() { return &fail_; }
  void play(const Ts &...x) override {
    std::vector<ThreadSedTransporter::SendValue> values;
    for (const auto &entry : entities_)
      values.push_back({entry.id, bool(entry.evaluate), entry.evaluate ? entry.evaluate(x...) : std::string{}});
    parent_->send_values(values, this->force_.value(x...), ack_timeout_, ack_resend_, callback_());
  }

 protected:
  ThreadSedTransporter::ResultCallback callback_() {
    return [this](bool success, uint32_t id) {
      if (success) success_.trigger(id); else fail_.trigger(id);
    };
  }
  ThreadSedTransporter *parent_;
  static std::string serialize_(cJSON *value) {
    if (!value) return {};
    char *text = cJSON_PrintUnformatted(value);
    std::string result = text ? text : "";
    cJSON_free(text); cJSON_Delete(value);
    return result;
  }
  struct Entity {
    std::string id;
    std::function<std::string(Ts...)> evaluate;
  };
  std::vector<Entity> entities_;
  uint32_t ack_timeout_{0};
  uint8_t ack_resend_{0};
  Trigger<uint32_t> success_, fail_;
};

class ReportCompleteTrigger : public Trigger<bool, uint32_t, uint32_t, uint32_t, uint32_t> {
 public:
  explicit ReportCompleteTrigger(ThreadSedTransporter *parent) {
    parent->add_result_guard([this]() { return !this->is_action_running(); });
    parent->add_on_complete_callback([this](bool success, uint32_t sent, uint32_t unchanged,
                                          uint32_t invalid, uint32_t errors) {
      this->trigger(success, sent, unchanged, invalid, errors);
    });
  }
};

class ReportSuccessTrigger : public Trigger<uint32_t> {
 public:
  explicit ReportSuccessTrigger(ThreadSedTransporter *parent) {
    parent->add_result_guard([this]() { return !this->is_action_running(); });
    parent->add_on_report_success_callback([this](uint32_t report_id) { this->trigger(report_id); });
  }
};

class ReportFailTrigger : public Trigger<uint32_t> {
 public:
  explicit ReportFailTrigger(ThreadSedTransporter *parent) {
    parent->add_result_guard([this]() { return !this->is_action_running(); });
    parent->add_on_report_fail_callback([this](uint32_t report_id) { this->trigger(report_id); });
  }
};

template<typename... Ts> class DiscoveryAction : public Action<Ts...> {
 public:
  explicit DiscoveryAction(ThreadSedTransporter *parent) : parent_(parent) {}
  void play(const Ts &...x) override { parent_->discovery(); }
 protected:
  ThreadSedTransporter *parent_;
};

}  // namespace esphome::thread_sed_transporter_nrf52
