#pragma once
#include "esphome/core/defines.h"
#ifdef USE_OPENTHREAD_JOINER
#include "openthread_nrf52.h"
namespace esphome::openthread_nrf52 {
template<typename... Ts> class JoinerStartAction : public Action<Ts...> {
 public:
  explicit JoinerStartAction(OpenThreadComponent *parent) : parent_(parent) {}
  TEMPLATABLE_VALUE(std::string, pskd)
  TEMPLATABLE_VALUE(uint32_t, timeout)
  void play(const Ts &...x) override { parent_->start_joiner(pskd_.value_or(x..., std::string{}), timeout_.value(x...), pskd_.has_value()); }
 protected:
  OpenThreadComponent *parent_;
};
template<typename... Ts> class JoinerCancelAction : public Action<Ts...> {
 public:
  explicit JoinerCancelAction(OpenThreadComponent *parent) : parent_(parent) {}
  void play(const Ts &...x) override { parent_->cancel_joiner(); }
 protected:
  OpenThreadComponent *parent_;
};
template<typename... Ts> class JoinerActiveCondition : public Condition<Ts...> {
 public:
  explicit JoinerActiveCondition(OpenThreadComponent *parent) : parent_(parent) {}
  bool check(const Ts &...x) override { return parent_->is_joiner_active(); }
 protected:
  OpenThreadComponent *parent_;
};
}  // namespace esphome::openthread_nrf52
#endif
