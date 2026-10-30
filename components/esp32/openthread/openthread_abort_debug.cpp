// Retain assertion/abort text that ESPHome's generic crash backtrace cannot recover.
#include "esphome/core/defines.h"
#if defined(USE_ESP32) && defined(USE_OPENTHREAD)
#include "esphome/core/log.h"
#include "esp_attr.h"
#include "esp_memory_utils.h"
#include "esp_task_wdt.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include <cstddef>
#include <cstdint>

namespace esphome::openthread {
namespace {
constexpr uint32_t ABORT_MAGIC = 0x4F544142;
constexpr size_t ABORT_TEXT_SIZE = 384;
struct AbortRecord {
  uint32_t magic;
  uint32_t checksum;
  uint32_t length;
  char text[ABORT_TEXT_SIZE];
};
// Survives software reset; deliberately no initializer/global constructor.
RTC_NOINIT_ATTR AbortRecord abort_record;
RTC_NOINIT_ATTR AbortRecord wdt_record;
AbortRecord saved_abort{};
AbortRecord saved_wdt{};
bool records_loaded{false};

uint32_t IRAM_ATTR text_checksum(const char *text, size_t length) {
  uint32_t checksum = 2166136261U;
  for (size_t i = 0; i < length; i++) {
    checksum = (checksum ^ static_cast<uint8_t>(text[i])) * 16777619U;
  }
  return checksum;
}

void IRAM_ATTR append_wdt_message(void *, const char *message) {
  if (message == nullptr)
    return;
  size_t length = wdt_record.length;
  while (*message != '\0' && length < ABORT_TEXT_SIZE - 1) {
    wdt_record.text[length++] = *message++;
  }
  wdt_record.text[length] = '\0';
  wdt_record.length = length;
}

void load_record(AbortRecord &source, AbortRecord &destination) {
  if (source.magic != ABORT_MAGIC)
    return;
  source.magic = 0;
  const size_t length = source.length;
  if (length == 0 || length >= ABORT_TEXT_SIZE || source.text[length] != '\0' ||
      source.checksum != text_checksum(source.text, length))
    return;
  destination = source;
  destination.magic = ABORT_MAGIC;
}

void print_record(const char *tag, const char *prefix, const AbortRecord &record) {
  if (record.magic != ABORT_MAGIC)
    return;
  // Separate lines/chunks survive small logger buffers and API log transport.
  size_t start = 0;
  while (start < record.length) {
    size_t end = start;
    while (end < record.length && record.text[end] != '\n' && end - start < 160)
      end++;
    if (end > start)
      ESP_LOGE(tag, "%s: %.*s", prefix, static_cast<int>(end - start), record.text + start);
    start = end < record.length && record.text[end] == '\n' ? end + 1 : end;
  }
}
}  // namespace

#if CONFIG_ESP_TASK_WDT_EN
void IRAM_ATTR capture_task_watchdog() {
  wdt_record.magic = 0;
  wdt_record.length = 0;
  wdt_record.text[0] = '\0';
  // IDF explicitly provides this callback API for the task-watchdog ISR path.
  // No ESP_LOG calls, allocation, Flash writes or watchdog reset in this hook.
  if (esp_task_wdt_print_triggered_tasks(append_wdt_message, nullptr, nullptr) != ESP_OK)
    return;
  append_wdt_message(nullptr, DRAM_STR("\nInterrupted task: "));
  append_wdt_message(nullptr, pcTaskGetName(nullptr));
  wdt_record.checksum = text_checksum(wdt_record.text, wdt_record.length);
  wdt_record.magic = ABORT_MAGIC;
}
#endif

void IRAM_ATTR capture_abort_details(const char *details) {
  abort_record.magic = 0;
  // IDF __assert_func builds its message on the DRAM stack. Do not touch flash
  // strings here: the abort may have occurred while the flash cache was disabled.
  if (details == nullptr || !esp_ptr_in_dram(details)) {
    return;
  }
  size_t length = 0;
  while (length < ABORT_TEXT_SIZE - 1 && esp_ptr_in_dram(details + length)) {
    const char value = details[length];
    if (value == '\0')
      break;
    abort_record.text[length++] = value;
  }
  if (length == 0)
    return;
  abort_record.text[length] = '\0';
  abort_record.length = length;
  abort_record.checksum = text_checksum(abort_record.text, length);
  abort_record.magic = ABORT_MAGIC;  // Commit only after the complete record is written.
}

void report_previous_abort() {
  if (!records_loaded) {
    load_record(abort_record, saved_abort);
    load_record(wdt_record, saved_wdt);
    records_loaded = true;
  }
  print_record("openthread.abort", "Previous software abort", saved_abort);
  print_record("openthread.wdt", "Previous task watchdog", saved_wdt);
}
}  // namespace esphome::openthread

extern "C" {
#if CONFIG_ESP_TASK_WDT_EN
void IRAM_ATTR esp_task_wdt_isr_user_handler() {
  esphome::openthread::capture_task_watchdog();
}
#endif
void __real_esp_system_abort(const char *details) __attribute__((noreturn));

void IRAM_ATTR __attribute__((noreturn)) __wrap_esp_system_abort(const char *details) {
  esphome::openthread::capture_abort_details(details);
  // Preserve IDF's normal panic logging, ESPHome crash recorder and reset behaviour.
  __real_esp_system_abort(details);
}
}
#endif
