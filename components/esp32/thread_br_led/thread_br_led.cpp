#include "thread_br_led.h"
#include "esphome/core/hal.h"
#include "esphome/core/log.h"

#ifdef USE_THREAD_BR_NATIVE
namespace esphome::thread_br_led {
static const char *const TAG = "thread_br_led";

void ThreadBRLEDSwitch::setup() {
  // Preload the inactive level before switching the pad to output.
  pin_->digital_write(false);
  pin_->setup();
  enabled_ = get_initial_state_with_restore_mode().value_or(true);
  publish_state(enabled_);
  loop();
}

void ThreadBRLEDSwitch::write_state(bool enabled) {
  enabled_ = enabled;
  // Publish only the user's enable setting; fault indication never changes it.
  publish_state(enabled);
  loop();
}

void ThreadBRLEDSwitch::drive_(bool lit) {
  if (lit == lit_) return;
  lit_ = lit;
  pin_->digital_write(lit);
}

void ThreadBRLEDSwitch::loop() {
  const uint32_t now = millis();
  const bool healthy = parent_->is_radio_healthy();
  if (!activity_) {
    // Failure overrides OFF. Removing Ethernet or losing attachment does not
    // indicate a local radio failure and does not change the saved setting.
    drive_(healthy ? enabled_ : (now / 200) % 2 == 0);
    return;
  }
  const uint32_t sequence = parent_->get_radio_activity_sequence();
  if (sequence != last_sequence_) {
    if (!seen_activity_ || uint32_t(now - last_activity_at_) >= 200)
      activity_started_at_ = now;
    last_activity_at_ = now;
    last_sequence_ = sequence;
    seen_activity_ = true;
  }
  const bool active = seen_activity_ && uint32_t(now - last_activity_at_) < 200;
  drive_(healthy && enabled_ && active && (uint32_t(now - activity_started_at_) / 100) % 2 == 0);
}

void ThreadBRLEDSwitch::on_shutdown() { drive_(false); }

void ThreadBRLEDSwitch::dump_config() {
  LOG_SWITCH("", "Thread BR LED", this);
  LOG_PIN("  Pin: ", pin_);
  ESP_LOGCONFIG(TAG, "  Mode: %s", activity_ ? "Thread MAC activity" : "Local radio health (fault overrides OFF)");
}
}  // namespace esphome::thread_br_led
#endif
