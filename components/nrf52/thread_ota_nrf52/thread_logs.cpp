#include "thread_logs.h"

#include <cstdint>
#include <cstring>
#include <zephyr/kernel.h>
#include <zephyr/logging/log_backend.h>
#include <zephyr/logging/log_backend_std.h>
#include <zephyr/logging/log_output.h>
#include <zephyr/mgmt/mcumgr/mgmt/handlers.h>
#include <zephyr/mgmt/mcumgr/mgmt/mgmt.h>
#include <zephyr/random/random.h>
#include <zephyr/sys/printk.h>
#include <zcbor_decode.h>
#include <zcbor_encode.h>
#include <mgmt/mcumgr/util/zcbor_bulk.h>

namespace esphome::thread_ota_nrf52 {
namespace {

constexpr size_t LOG_CAPACITY = 4096;
constexpr size_t LOG_CHUNK = 160;
constexpr uint16_t LOG_GROUP = 0xA652;
static uint8_t log_bytes[LOG_CAPACITY];
static uint64_t log_head;
static uint32_t boot_id;
static struct k_spinlock log_lock;

// The logger never waits for a network client. Oldest bytes are overwritten.
static int capture_output(uint8_t *data, size_t length, void *) {
  if (length == 0)
    return 0;
  const auto key = k_spin_lock(&log_lock);
  size_t kept = length > LOG_CAPACITY ? LOG_CAPACITY : length;
  uint64_t start = log_head + length - kept;
  size_t offset = static_cast<size_t>(start % LOG_CAPACITY);
  size_t first = kept < LOG_CAPACITY - offset ? kept : LOG_CAPACITY - offset;
  std::memcpy(log_bytes + offset, data + length - kept, first);
  std::memcpy(log_bytes, data + length - kept + first, kept - first);
  log_head += length;
  k_spin_unlock(&log_lock, key);
  return static_cast<int>(length);
}

static uint8_t format_buffer[128];
LOG_OUTPUT_DEFINE(thread_log_output, capture_output, format_buffer, sizeof(format_buffer));

static void process_log(const struct log_backend *, union log_msg_generic *message) {
  log_output_msg_process(&thread_log_output, &message->log, log_backend_std_get_flags());
}

static void dropped_logs(const struct log_backend *, uint32_t count) {
  log_backend_std_dropped(&thread_log_output, count);
}

static void panic_logs(const struct log_backend *) { log_backend_std_panic(&thread_log_output); }

static bool decode_cursor(zcbor_state_t *state, void *value) {
  return zcbor_uint64_decode(state, static_cast<uint64_t *>(value));
}

static bool decode_boot(zcbor_state_t *state, void *value) {
  return zcbor_uint32_decode(state, static_cast<uint32_t *>(value));
}

static const struct log_backend_api thread_log_api = {
    .process = process_log,
    .dropped = dropped_logs,
    .panic = panic_logs,
    .init = nullptr,
    .is_ready = nullptr,
    .format_set = nullptr,
    .notify = nullptr,
};
LOG_BACKEND_DEFINE(thread_log_backend, thread_log_api, true);

static int read_logs(struct smp_streamer *context) {
  uint64_t cursor = UINT64_MAX;
  uint32_t requested_boot = 0;
  size_t decoded = 0;
  struct zcbor_map_decode_key_val fields[] = {
      {.key = {.value = reinterpret_cast<const uint8_t *>("cursor"), .len = 6},
       .decoder = decode_cursor,
       .value_ptr = &cursor,
       .found = false},
      {.key = {.value = reinterpret_cast<const uint8_t *>("boot"), .len = 4},
       .decoder = decode_boot,
       .value_ptr = &requested_boot,
       .found = false},
  };
  if (zcbor_map_decode_bulk(context->reader->zs, fields, ARRAY_SIZE(fields), &decoded) != 0)
    return MGMT_ERR_EINVAL;

  uint8_t chunk[LOG_CHUNK];
  uint64_t lost = 0;
  const auto key = k_spin_lock(&log_lock);
  uint64_t oldest = log_head > LOG_CAPACITY ? log_head - LOG_CAPACITY : 0;
  if (cursor == UINT64_MAX || requested_boot != boot_id || cursor > log_head) {
    cursor = oldest;
  } else if (cursor < oldest) {
    lost = oldest - cursor;
    cursor = oldest;
  }
  size_t length = static_cast<size_t>(log_head - cursor);
  if (length > LOG_CHUNK)
    length = LOG_CHUNK;
  size_t offset = static_cast<size_t>(cursor % LOG_CAPACITY);
  size_t first = length < LOG_CAPACITY - offset ? length : LOG_CAPACITY - offset;
  std::memcpy(chunk, log_bytes + offset, first);
  std::memcpy(chunk + first, log_bytes, length - first);
  uint64_t next = cursor + length;
  k_spin_unlock(&log_lock, key);

  // Encode outside the lock. Reads are non-destructive, so retries and multiple
  // viewers have independent cursors and cannot steal each other's log data.
  auto *encoder = context->writer->zs;
  const struct zcbor_string payload = {.value = chunk, .len = length};
  bool ok = zcbor_tstr_put_lit(encoder, "v") && zcbor_uint32_put(encoder, 1) &&
            zcbor_tstr_put_lit(encoder, "boot") && zcbor_uint32_put(encoder, boot_id) &&
            zcbor_tstr_put_lit(encoder, "next") && zcbor_uint64_put(encoder, next) &&
            zcbor_tstr_put_lit(encoder, "lost") && zcbor_uint64_put(encoder, lost) &&
            zcbor_tstr_put_lit(encoder, "data") && zcbor_bstr_encode(encoder, &payload);
  return ok ? MGMT_ERR_EOK : MGMT_ERR_EMSGSIZE;
}

static const struct mgmt_handler log_handlers[] = {{.mh_read = read_logs, .mh_write = nullptr}};
static struct mgmt_group log_group;

static void register_log_group() {
  boot_id = sys_rand32_get() | 1U;
  log_group.mg_handlers = log_handlers;
  log_group.mg_handlers_count = ARRAY_SIZE(log_handlers);
  log_group.mg_group_id = LOG_GROUP;
  mgmt_register_group(&log_group);
}
MCUMGR_HANDLER_DEFINE(thread_log_mgmt, register_log_group);

}  // namespace

void install_log_bridge(logger::Logger *log) {
  log->add_log_callback(log, [](void *, uint8_t, const char *, const char *message, size_t length) {
    // LOG_PRINTK forwards this to Zephyr backends. ESP_LOG here would recurse.
    printk("%.*s\n", static_cast<int>(length), message);
  });
}

}  // namespace esphome::thread_ota_nrf52
