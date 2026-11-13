#pragma once

#include "api_server.h"
#ifdef USE_THREAD_PROXY_CLIENT_HOMEASSISTANT_SERVICES
#include <functional>
#include <string>
#include <type_traits>
#include <utility>
#include "api_pb2.h"
#include "esphome/core/automation.h"
#include "esphome/core/helpers.h"
#include "esphome/core/progmem.h"
#include "esphome/core/string_ref.h"

namespace esphome::thread_proxy_client {

// Converts a lambda result to the string sent to Home Assistant
template<typename T>
requires(!std::is_pointer_v<std::remove_cvref_t<T>>) std::string field_to_string(T &&val) {
  return to_string(std::forward<T>(val));  // NOLINT
}
inline std::string field_to_string(const char *val) { return val ? std::string(val) : std::string(); }
inline std::string field_to_string(std::string val) { return val; }
inline std::string field_to_string(StringRef val) { return val.str(); }

/// A key and value from codegen; on ESP8266 the table and its strings are in flash.
/// The value is the constant `value`, or the result of `fn` when it is set.
template<typename... Ts> struct HomeAssistantField {
  const char *key;
  const char *value;
  std::string (*fn)(const Ts &...);

  template<typename F> static constexpr HomeAssistantField from_lambda(const char *key, F /*lambda*/) {
    return {key, nullptr, &call_lambda<F>};
  }
  template<typename F> static std::string call_lambda(const Ts &...x) { return field_to_string(F{}(x...)); }
};


template<typename... Ts> class HomeAssistantServiceCallAction final : public Action<Ts...> {
 public:
  using Field = HomeAssistantField<Ts...>;

  /// `fields` is a codegen table: the action or event name (no key), then the data, data_template
  /// and variables entries.
  HomeAssistantServiceCallAction(ThreadProxyClient *parent, bool is_event, const Field *fields, uint8_t data_count,
                                 uint8_t data_template_count, uint8_t variables_count)
      : parent_(parent),
        fields_(fields),
        data_count_(data_count),
        data_template_count_(data_template_count),
        variables_count_(variables_count) {
    this->flags_.is_event = is_event;
  }


  void play(const Ts &...x) override {
    const Field *fields = this->fields_;
    const size_t total = 1 + this->data_count_ + this->data_template_count_ + this->variables_count_;

    // Lambda results, and on ESP8266 the RAM copies of the flash strings, must live until the send
    size_t lambda_count = 0;
#ifdef USE_ESP8266
    size_t flash_len = 0;
#endif
    for (size_t i = 0; i < total; i++) {
      lambda_count += fields[i].fn != nullptr;
#ifdef USE_ESP8266
      if (fields[i].fn == nullptr)
        flash_len += ESPHOME_strlen_P(fields[i].value);
      if (fields[i].key != nullptr)
        flash_len += ESPHOME_strlen_P(fields[i].key);
#endif
    }
    FixedVector<std::string> results;
    results.init(lambda_count);
#ifdef USE_ESP8266
    SmallBufferWithHeapFallback<128, char> flash_copy(flash_len);
    char *cursor = flash_copy.get();
#endif
    auto string_ref = [&](const char *str) {
#ifdef USE_ESP8266
      size_t len = ESPHOME_strlen_P(str);
      memcpy_P(cursor, str, len);
      StringRef ref(cursor, len);
      cursor += len;
      return ref;
#else
      return StringRef(str);
#endif
    };
    auto value_ref = [&](const Field &field) {
      if (field.fn == nullptr)
        return string_ref(field.value);
      results.push_back(field.fn(x...));
      return StringRef(results.back());
    };
    auto fill = [&](FixedVector<HomeassistantServiceMap> &dest, uint8_t count) {
      dest.init(count);
      for (uint8_t i = 0; i < count; i++, fields++) {
        auto &kv = dest.emplace_back();
        kv.key = string_ref(fields->key);
        kv.value = value_ref(*fields);
      }
    };

    HomeassistantActionRequest resp;
    resp.service = value_ref(*fields++);
    resp.is_event = this->flags_.is_event;
    fill(resp.data, this->data_count_);
    fill(resp.data_template, this->data_template_count_);
    fill(resp.variables, this->variables_count_);


    this->parent_->send_homeassistant_action(resp);
  }

 protected:
  ThreadProxyClient *parent_;
  const Field *fields_;
  uint8_t data_count_;
  uint8_t data_template_count_;
  uint8_t variables_count_;
  struct Flags {
    uint8_t is_event : 1;
    uint8_t wants_status : 1;
    uint8_t wants_response : 1;
    uint8_t has_response_template : 1;
    uint8_t reserved : 4;
  } flags_{0};
};

}  // namespace esphome::thread_proxy_client

#endif
