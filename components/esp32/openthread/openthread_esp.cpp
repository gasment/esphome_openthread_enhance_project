#include "esphome/core/defines.h"
#if defined(USE_OPENTHREAD) && defined(USE_ESP32)
#include <openthread/logging.h>
#include <openthread/ip6.h>
#include "openthread.h"

#include "esp_log.h"
#include "esp_openthread.h"
#include "esp_openthread_lock.h"

#include "esp_task_wdt.h"
#include "esphome/core/hal.h"
#include "esphome/core/helpers.h"
#include "esphome/core/log.h"

#include "esp_err.h"
#include "esp_event.h"
#include "esp_heap_caps.h"
#include "esp_netif.h"
#include "esp_netif_types.h"
#include "esp_netif_net_stack.h"
#include "esp_openthread_cli.h"
#include "esp_openthread_netif_glue.h"
#include "esp_vfs_eventfd.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs_flash.h"
#include "lwip/netif.h"
#include <array>
#include <cstring>

static const char *const TAG = "openthread";

namespace esphome::openthread {

namespace {
static const char *const IP6_TAG = "openthread.ip6";
struct StackAddress {
  esp_ip6_addr_t address{};
  bool preferred{false};
  bool valid{true};
};
struct AddressSnapshot {
  esp_netif_t *interface{nullptr};
  std::array<StackAddress, CONFIG_LWIP_IPV6_NUM_ADDRESSES> addresses{};
  size_t count{0};
};

bool same_address(const esp_ip6_addr_t &a, const esp_ip6_addr_t &b) {
  return std::memcmp(a.addr, b.addr, sizeof(a.addr)) == 0;
}

// Read the table in TCP/IP context, not concurrently with IDF's address events.
esp_err_t snapshot_lwip_addresses(void *context) {
  auto *snapshot = static_cast<AddressSnapshot *>(context);
  auto *interface = static_cast<struct netif *>(esp_netif_get_netif_impl(snapshot->interface));
  if (interface == nullptr || !netif_is_up(interface)) return ESP_ERR_INVALID_STATE;
  for (int i = 0; i < CONFIG_LWIP_IPV6_NUM_ADDRESSES; ++i) {
    const auto state = netif_ip6_addr_state(interface, i);
    // Tentative/duplicate entries also occupy slots and may be stale. INVALID
    // slots are already reusable; getters below only expose valid addresses.
    if (state == IP6_ADDR_INVALID) continue;
    auto &entry = snapshot->addresses[snapshot->count++];
    std::memcpy(entry.address.addr, netif_ip6_addr(interface, i)->addr, sizeof(entry.address.addr));
    entry.address.zone = ip6_addr_zone(netif_ip6_addr(interface, i));
    entry.preferred = ip6_addr_ispreferred(state);
    entry.valid = ip6_addr_isvalid(state);
  }
  return ESP_OK;
}
}  // namespace

void OpenThreadComponent::setup() {
  ESP_LOGI(TAG, "Setup: NVS/eventfd, free internal heap=%u, largest block=%u",
           static_cast<unsigned>(heap_caps_get_free_size(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT)),
           static_cast<unsigned>(heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT)));
  // Used eventfds:
  // * netif
  // * ot task queue
  // * radio driver
  esp_vfs_eventfd_config_t eventfd_config = {
      .max_fds = 3,
  };
  // Network interface setup handled by network component
  ESP_ERROR_CHECK(nvs_flash_init());
  ESP_ERROR_CHECK(esp_vfs_eventfd_register(&eventfd_config));

  ESP_LOGI(TAG, "Setup: creating ot_main task (10240-byte stack)");
  const BaseType_t task_result = xTaskCreate(
      [](void *arg) {
        static_cast<OpenThreadComponent *>(arg)->ot_main();
        vTaskDelete(nullptr);
      },
      "ot_main", 10240, this, 5, nullptr);
  if (task_result != pdPASS) {
    ESP_LOGE(TAG, "Failed to create ot_main task: insufficient task memory");
    esp_vfs_eventfd_unregister();
    this->teardown_stage_ = TeardownStage::TEARDOWN_STAGE_COMPLETED;
    this->mark_failed();
    return;
  }
}

static esp_netif_t *init_openthread_netif(const esp_openthread_platform_config_t *config) {
  esp_netif_config_t cfg = ESP_NETIF_DEFAULT_OPENTHREAD();
  esp_netif_t *netif = esp_netif_new(&cfg);
  assert(netif != nullptr);
  ESP_ERROR_CHECK(esp_netif_attach(netif, esp_openthread_netif_glue_init(config)));

  return netif;
}

void OpenThreadComponent::ot_main() {
  ESP_LOGI(TAG, "ot_main started, free internal heap=%u, largest block=%u",
           static_cast<unsigned>(heap_caps_get_free_size(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT)),
           static_cast<unsigned>(heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT)));
  esp_openthread_platform_config_t config = {
      .radio_config =
          {
              .radio_mode = RADIO_MODE_NATIVE,
              .radio_uart_config = {},
          },
      .host_config =
          {
              // There is a conflict between esphome's logger which also
              // claims the usb serial jtag device.
              // .host_connection_mode = HOST_CONNECTION_MODE_CLI_USB,
              // .host_usb_config = USB_SERIAL_JTAG_DRIVER_CONFIG_DEFAULT(),
          },
      .port_config =
          {
              .storage_partition_name = "nvs",
              .netif_queue_size = 10,
              .task_queue_size = 10,
          },
  };

  // Initialize the OpenThread stack
  // otLoggingSetLevel(OT_LOG_LEVEL_DEBG);
  ESP_LOGI(TAG, "Initializing OpenThread platform/radio...");
  ESP_ERROR_CHECK(esp_openthread_init(&config));
  ESP_LOGI(TAG, "OpenThread platform/radio initialized");
  // Mark lock as initialized so InstanceLock callers know it's safe to acquire.
  // Must be set after esp_openthread_init() which creates the internal semaphore.
  this->lock_initialized_ = true;
  // Fetch OT instance once to avoid repeated call into OT stack
  otInstance *instance = esp_openthread_get_instance();

#if CONFIG_OPENTHREAD_STATE_INDICATOR_ENABLE
  ESP_ERROR_CHECK(esp_openthread_state_indicator_init(instance));
#endif

#if CONFIG_OPENTHREAD_LOG_LEVEL_DYNAMIC
  // The OpenThread log level directly matches ESP log level
  (void) otLoggingSetLevel(CONFIG_LOG_DEFAULT_LEVEL);
#endif
  // Initialize the OpenThread cli
#if CONFIG_OPENTHREAD_CLI
  esp_openthread_cli_init();
#endif

  esp_netif_t *openthread_netif;
  // Initialize the esp_netif bindings
  ESP_LOGI(TAG, "Attaching OpenThread network interface...");
  openthread_netif = init_openthread_netif(&config);
  esp_netif_set_default_netif(openthread_netif);
  ESP_LOGI(TAG, "OpenThread network interface attached");

#if CONFIG_OPENTHREAD_CLI_ESP_EXTENSION
  esp_cli_custom_command_init();
#endif  // CONFIG_OPENTHREAD_CLI_ESP_EXTENSION

  ESP_LOGD(TAG, "Thread Version: %" PRIu16, otThreadGetVersion());

  this->apply_linkmode_(instance);

  if (this->output_power_.has_value()) {
    if (const auto err = otPlatRadioSetTransmitPower(instance, *this->output_power_); err != OT_ERROR_NONE) {
      ESP_LOGE(TAG, "Failed to set power: %s", otThreadErrorToString(err));
    }
  }

  // Run the main loop
#if CONFIG_OPENTHREAD_CLI
  esp_openthread_cli_create_task();
#endif
#ifdef USE_OPENTHREAD_JOINER
  if (!this->joiner_.boot_locked(instance)) {
    this->mark_failed();
  } else {
    ESP_ERROR_CHECK(otIp6SetEnabled(instance, true));
    if (otDatasetIsCommissioned(instance)) {
      ESP_ERROR_CHECK(otThreadSetEnabled(instance, true));
      ESP_LOGI(TAG, "Joiner mode: attaching with stored Dataset");
    } else {
      ESP_LOGI(TAG, "Joiner mode: waiting for joiner.start");
    }
  }
#else
  ESP_LOGI(TAG, "Activating dataset...");
  otOperationalDatasetTlvs dataset = {};

#ifndef USE_OPENTHREAD_FORCE_DATASET
  // Check if openthread has a valid dataset from a previous execution
  otError error = otDatasetGetActiveTlvs(instance, &dataset);
  if (error != OT_ERROR_NONE) {
    // Make sure the length is 0 so we fallback to the configuration
    dataset.mLength = 0;
  } else {
    ESP_LOGI(TAG, "Found existing dataset, ignoring config (force_dataset: true to override)");
  }
#endif

#ifdef USE_OPENTHREAD_TLVS
  if (dataset.mLength == 0) {
    // If we didn't have an active dataset, and we have tlvs, parse it and pass it to esp_openthread_auto_start
    size_t len = (sizeof(USE_OPENTHREAD_TLVS) - 1) / 2;
    if (len > sizeof(dataset.mTlvs)) {
      ESP_LOGW(TAG, "TLV buffer too small, truncating");
      len = sizeof(dataset.mTlvs);
    }
    parse_hex(USE_OPENTHREAD_TLVS, sizeof(USE_OPENTHREAD_TLVS) - 1, dataset.mTlvs, len);
    dataset.mLength = len;
  }
#endif

  // Pass the existing dataset, or NULL which will use the preprocessor definitions
  ESP_ERROR_CHECK(esp_openthread_auto_start(dataset.mLength > 0 ? &dataset : nullptr));
  ESP_LOGI(TAG, "OpenThread auto-start completed");

#endif

  // Register state change callback to update connected_ reactively instead of polling
  otError ot_err = otSetStateChangedCallback(instance, OpenThreadComponent::on_state_changed, this);
  if (ot_err != OT_ERROR_NONE) {
    ESP_LOGW(TAG, "Failed to register state change callback: %d", ot_err);
  }

#ifdef USE_OPENTHREAD_JOINER
  this->joiner_.finish_boot();
#endif
  esp_openthread_launch_mainloop();

  // Clean up - reset lock flag before deinit destroys the semaphore
  this->lock_initialized_ = false;
  esp_openthread_deinit();
  esp_openthread_netif_glue_deinit();
  esp_netif_destroy(openthread_netif);

  esp_vfs_eventfd_unregister();
  this->teardown_stage_ = TeardownStage::TEARDOWN_STAGE_COMPLETED;
  vTaskDelete(NULL);
}

int OpenThreadComponent::openthread_stop_() { return esp_openthread_mainloop_exit(); }

void OpenThreadComponent::loop() {
  if (!this->lock_initialized_ || this->teardown_stage_ != TeardownStage::TEARDOWN_STAGE_NOT_STARTED) return;
  this->service_remote_restart_();
#ifdef USE_OPENTHREAD_JOINER
  this->loop_joiner_();
#endif
  const uint32_t now = millis();
  // Rate-limit event bursts/retries; periodically audit even if an IDF event was lost.
  if (uint32_t(now - this->ip6_sync_at_) < 1000) return;
  if (this->ip6_generation_.load() == this->ip6_synced_generation_ &&
      uint32_t(now - this->ip6_sync_at_) < 5000) return;
  this->ip6_sync_at_ = now;
  this->sync_ip6_addresses_();
}

void OpenThreadComponent::sync_ip6_addresses_() {
  auto *interface = esp_openthread_get_netif();
  if (interface == nullptr) return;
  std::vector<StackAddress> desired;
  uint32_t generation;
  {
    auto lock = InstanceLock::try_acquire(0);
    if (!lock) return;
    auto *instance = lock.get_instance();
    // Do not reconcile a transient detached/disabled address table.
    if (!otIp6IsEnabled(instance) || otThreadGetDeviceRole(instance) < OT_DEVICE_ROLE_CHILD) return;
    generation = this->ip6_generation_.load();
    const auto *mesh_prefix = otThreadGetMeshLocalPrefix(instance);
    for (auto *address = otIp6GetUnicastAddresses(instance); address != nullptr; address = address->mNext) {
      if (!address->mValid) continue;
      const auto *bytes = address->mAddress.mFields.m8;
      const bool mesh_local = std::memcmp(bytes, mesh_prefix->m8, sizeof(mesh_prefix->m8)) == 0;
      const bool link_local = bytes[0] == 0xfe && (bytes[1] & 0xc0) == 0x80;
      const bool locator = bytes[8] == 0 && bytes[9] == 0 && bytes[10] == 0 &&
                           bytes[11] == 0xff && bytes[12] == 0xfe && bytes[13] == 0;
      // Mirror IDF glue: RLOC/ALOC are handled by OT itself, not lwIP.
      if (mesh_local && locator) continue;
      StackAddress entry;
      std::memcpy(entry.address.addr, bytes, sizeof(entry.address.addr));
      entry.preferred = address->mPreferred && !mesh_local && !link_local;
      desired.push_back(entry);
    }
  }
  // No OT lock while calling into TCP/IP: lwIP output can call back into OT.
  AddressSnapshot actual;
  actual.interface = interface;
  if (esp_netif_tcpip_exec(snapshot_lwip_addresses, &actual) != ESP_OK) return;
  auto unchanged = [this, generation]() {
    return this->ip6_generation_.load() == generation && this->lock_initialized_.load() &&
           this->teardown_stage_ == TeardownStage::TEARDOWN_STAGE_NOT_STARTED;
  };
  size_t removed = 0, repaired = 0;
  bool success = true;
  // Reclaim stale slots first. Valid deprecated OT addresses remain present so
  // old connections survive prefix transitions; lack of preference is not expiry.
  for (size_t i = 0; i < actual.count; ++i) {
    if (!unchanged()) return;
    const auto &entry = actual.addresses[i];
    bool keep = false;
    for (const auto &source : desired) if (same_address(entry.address, source.address)) { keep = true; break; }
    if (!keep) {
      if (esp_netif_remove_ip6_address(interface, &entry.address) == ESP_OK) ++removed;
      else success = false;
    }
  }
  // A dropped GOT_IP6 event or a formerly full table must not lose an address forever.
  for (const auto &entry : desired) {
    if (!unchanged()) return;
    bool matches = false;
    for (size_t i = 0; i < actual.count; ++i) {
      const auto &previous = actual.addresses[i];
      if (previous.valid && same_address(entry.address, previous.address) && entry.preferred == previous.preferred) {
        matches = true; break;
      }
    }
    if (matches) continue;
    if (esp_netif_add_ip6_address(interface, entry.address, entry.preferred) == ESP_OK) ++repaired;
    else success = false;
  }
  if (!unchanged()) return;
  // A queued old IDF event can race with this pass; verify against fresh lwIP data.
  AddressSnapshot verified;
  verified.interface = interface;
  if (esp_netif_tcpip_exec(snapshot_lwip_addresses, &verified) != ESP_OK) return;
  success = success && verified.count == desired.size();
  for (const auto &entry : desired) {
    bool matches = false;
    for (size_t i = 0; i < verified.count; ++i)
      if (verified.addresses[i].valid && same_address(entry.address, verified.addresses[i].address) &&
          entry.preferred == verified.addresses[i].preferred) { matches = true; break; }
    success = success && matches;
  }
  if (!unchanged()) return;
  const bool first_or_recovered = !this->ip6_checked_ || !this->ip6_sync_ok_;
  this->ip6_checked_ = true;
  this->ip6_sync_ok_ = success;
  this->ip6_ot_count_ = desired.size();
  this->ip6_lwip_count_ = verified.count;
  if (removed || repaired)
    ESP_LOGCONFIG(IP6_TAG, "Address reconciliation: removed %u stale, repaired %u missing/state entries",
             unsigned(removed), unsigned(repaired));
  if (success) {
    if (first_or_recovered)
      ESP_LOGCONFIG(IP6_TAG, "IPv6 address tables synchronized: OT=%u lwIP=%u capacity=%u",
                    unsigned(desired.size()), unsigned(verified.count), unsigned(CONFIG_LWIP_IPV6_NUM_ADDRESSES));
    this->ip6_synced_generation_ = generation;
    this->ip6_sync_warned_ = false;
  } else {
    // Leave dirty and retry after 1s. Never evict a still-valid OT address to make room.
    this->ip6_synced_generation_ = generation - 1;
    if (!this->ip6_sync_warned_ || uint32_t(millis() - this->ip6_sync_warning_at_) >= 30000) {
      this->ip6_sync_warned_ = true;
      this->ip6_sync_warning_at_ = millis();
      ESP_LOGW(IP6_TAG, "IPv6 address tables differ: OT=%u lwIP=%u capacity=%u; retrying without evicting valid addresses",
               unsigned(desired.size()), unsigned(verified.count), unsigned(CONFIG_LWIP_IPV6_NUM_ADDRESSES));
    }
  }
}

network::IPAddresses OpenThreadComponent::get_ip_addresses() {
  network::IPAddresses addresses{};
  if (!this->lock_initialized_ || this->teardown_stage_ != TeardownStage::TEARDOWN_STAGE_NOT_STARTED) return addresses;
  AddressSnapshot snapshot;
  snapshot.interface = esp_openthread_get_netif();
  if (snapshot.interface == nullptr || esp_netif_tcpip_exec(snapshot_lwip_addresses, &snapshot) != ESP_OK) return addresses;
  size_t output = 1;  // Index zero is reserved for IPv4 by ESPHome.
  // Prefer the externally usable addresses when the fixed ESPHome array is smaller.
  for (bool preferred : {true, false})
    for (size_t i = 0; i < snapshot.count && output < addresses.size(); ++i)
      if (snapshot.addresses[i].valid && snapshot.addresses[i].preferred == preferred)
        addresses[output++] = network::IPAddress(&snapshot.addresses[i].address);
  return addresses;
}

// not thread safe, only use in read-only use cases
otInstance *OpenThreadComponent::get_openthread_instance_() { return esp_openthread_get_instance(); }

InstanceLock InstanceLock::try_acquire(int delay) {
  if (global_openthread_component == nullptr || !global_openthread_component->is_lock_initialized()) {
    return InstanceLock(false);
  }
  return InstanceLock(esp_openthread_lock_acquire(delay));
}

InstanceLock InstanceLock::acquire() {
  // Wait for the lock to be created by ot_main() before attempting to acquire it.
  // esp_openthread_lock_acquire() will assert-crash if called before esp_openthread_init().
  constexpr uint32_t lock_init_timeout_ms = 10000;
  uint32_t start = millis();
  while (!global_openthread_component->is_lock_initialized()) {
    if (millis() - start > lock_init_timeout_ms) {
      ESP_LOGE(TAG, "OpenThread lock not initialized after %" PRIu32 "ms, aborting", lock_init_timeout_ms);
      abort();
    }
    delay(10);
    esp_task_wdt_reset();
  }
  while (!esp_openthread_lock_acquire(100)) {
    esp_task_wdt_reset();
  }
  return InstanceLock(true);
}

otInstance *InstanceLock::get_instance() { return esp_openthread_get_instance(); }

InstanceLock::~InstanceLock() {
  if (this->owns_) {
    esp_openthread_lock_release();
  }
}

}  // namespace esphome::openthread
#endif
