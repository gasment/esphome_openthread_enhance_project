#pragma once
#include "api_server.h"
#include "esphome/core/base_automation.h"

namespace esphome::thread_proxy_client {
template<typename... Ts> class IsReadyCondition : public Condition<Ts...> {
 public:
  explicit IsReadyCondition(ThreadProxyClient *parent) : parent_(parent) {}
  bool check(const Ts &...x) override { return this->parent_->sed_is_ready(); }
 protected:
  ThreadProxyClient *parent_;
};

template<typename... Ts> class SendAction : public Action<Ts...> {
 public:
  explicit SendAction(ThreadProxyClient *parent) : parent_(parent) {}
  TEMPLATABLE_VALUE(uint32_t, ack_timeout);
  TEMPLATABLE_VALUE(uint32_t, ack_resend);
  void set_entities(std::initializer_list<EntityBase *> entities) {
    this->entities_.assign(entities.begin(), entities.end());
  }
  void add_on_success(const std::initializer_list<Action<Ts...> *> &actions) {
    this->success_.add_actions(actions);
    // ESPHome 2026.11 removed ContinuationAction in favor of branch owners.
#if ESPHOME_VERSION_CODE >= VERSION_CODE(2026, 11, 0)
    this->success_.set_owner(this);
#else
    this->success_.add_action(&this->success_continuation_);
#endif
  }
  void add_on_error(const std::initializer_list<Action<Ts...> *> &actions) {
    this->error_.add_actions(actions);
#if ESPHOME_VERSION_CODE >= VERSION_CODE(2026, 11, 0)
    this->error_.set_owner(this);
#else
    this->error_.add_action(&this->error_continuation_);
#endif
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
  void play(const Ts &...x) override {
#if ESPHOME_VERSION_CODE >= VERSION_CODE(2026, 11, 0)
    // ActionList::set_owner() resumes here after the selected branch finishes.
    this->play_next_(x...);
#endif
  }
  void stop() override {
    this->parent_->sed_cancel_batch(this);
    this->success_.stop();
    this->error_.stop();
  }
  ThreadProxyClient *parent_;
  std::vector<EntityBase *> entities_;
  ActionList<Ts...> success_, error_;
#if ESPHOME_VERSION_CODE < VERSION_CODE(2026, 11, 0)
  // Preserve compatibility with ESPHome 2026.10.x, whose ActionList has no set_owner().
  class LegacyContinuation : public Action<Ts...> {
   public:
    explicit LegacyContinuation(SendAction *parent) : parent_(parent) {}
    void play(const Ts &...x) override { this->parent_->resume_(x...); }

   protected:
    SendAction *parent_;
  };
  void resume_(const Ts &...x) { this->play_next_(x...); }
  LegacyContinuation success_continuation_{this}, error_continuation_{this};
#endif
};
}
