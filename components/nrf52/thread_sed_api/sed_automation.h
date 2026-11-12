#pragma once
#include "api_server.h"
#include "esphome/core/base_automation.h"

namespace esphome::thread_sed_api {
template<typename... Ts> class IsReadyCondition : public Condition<Ts...> {
 public:
  explicit IsReadyCondition(APIServer *parent) : parent_(parent) {}
  bool check(const Ts &...x) override { return this->parent_->sed_is_ready(); }
 protected:
  APIServer *parent_;
};

template<typename... Ts> class SendAction : public Action<Ts...> {
 public:
  explicit SendAction(APIServer *parent) : parent_(parent) {}
  TEMPLATABLE_VALUE(uint32_t, ack_timeout);
  TEMPLATABLE_VALUE(uint32_t, ack_resend);
  void set_entities(std::initializer_list<EntityBase *> entities) {
    this->entities_.assign(entities.begin(), entities.end());
  }
  void add_on_success(const std::initializer_list<Action<Ts...> *> &actions) {
    this->success_.add_actions(actions);
    this->success_.add_action(&this->success_continuation_);
  }
  void add_on_error(const std::initializer_list<Action<Ts...> *> &actions) {
    this->error_.add_actions(actions);
    this->error_.add_action(&this->error_continuation_);
  }
  void play_complex(const Ts &...x) override {
    this->num_running_++;
    // Like ESPHome's asynchronous actions, preserve trigger arguments by value.
    auto done = [this, x...](bool success) mutable {
      if (this->num_running_ == 0) return;
      auto &branch = success ? this->success_ : this->error_;
      if (branch.empty()) this->play_next_(x...);
      else branch.play(x...);
    };
    if (!this->parent_->sed_begin_batch(this, this->ack_timeout_.value(x...), this->ack_resend_.value(x...), this->entities_, done)) done(false);
  }
 protected:
  void play(const Ts &...x) override {}
  void stop() override {
    this->parent_->sed_cancel_batch(this);
    this->success_.stop();
    this->error_.stop();
  }
  APIServer *parent_;
  std::vector<EntityBase *> entities_;
  ActionList<Ts...> success_, error_;
  ContinuationAction<Ts...> success_continuation_{this}, error_continuation_{this};
};
}
