#include "esphome/core/defines.h"
#ifdef USE_OPENTHREAD_NRF52
#include "openthread_nrf52.h"

#include <openthread/cli.h>
#include <openthread/child_supervision.h>
#include <openthread/instance.h>
#include <openthread/ip6.h>
#include <openthread/logging.h>
#include <openthread/netdata.h>
#include <openthread/tasklet.h>

#include <cstring>
#include <algorithm>
#include <utility>

#include "esphome/core/application.h"
#include "esphome/core/helpers.h"
#include "esphome/core/log.h"

static const char *const TAG = "openthread_nrf52";

namespace esphome::openthread_nrf52 {

#if CONFIG_OPENTHREAD_MTD
bool OpenThreadComponent::supervision_timeout_for_poll_(uint32_t period, uint16_t &timeout) const {
  const uint32_t minimum = uint32_t(this->supervision_interval_) + (period + 999) / 1000;
  uint32_t selected = this->supervision_timeout_;
  if (selected == 0) selected = std::max<uint32_t>(190, minimum + 30);
  if (selected <= minimum || selected > UINT16_MAX) return false;
  timeout = static_cast<uint16_t>(selected);
  return true;
}

bool OpenThreadComponent::set_poll_period(uint32_t period) {
  if (period != 0 && (period < 10 || period > 0x3FFFFFF)) {
    ESP_LOGW(TAG, "Invalid poll_period: %" PRIu32 "ms; previous configuration retained", period);
    return false;
  }
  uint16_t timeout;
  if (!this->supervision_timeout_for_poll_(period, timeout)) {
    ESP_LOGW(TAG, "Poll period incompatible with Child Supervision timeout; previous policy retained");
    return false;
  }
  this->poll_period_ = period;
  return true;
}

void OpenThreadComponent::log_supervision_failures_(otInstance *instance) {
  const auto count = otChildSupervisionGetCheckFailureCounter(instance);
  if (count == this->supervision_failures_) return;
  ESP_LOGW(TAG, "Child Supervision checks timed out: +%u (total=%u); MLE parent validation requested",
           unsigned(static_cast<uint16_t>(count - this->supervision_failures_)), unsigned(count));
  this->supervision_failures_ = count;
}
#endif

static bool is_link_local_or_multicast(const otIp6Address &address) {
  const auto *bytes = address.mFields.m8;
  // IPv6 addresses are stored in network byte order: fe80::/10 and ff00::/8.
  // These predicates are not exposed by the ESP-IDF 5.5 OpenThread C API.
  return bytes[0] == 0xff || (bytes[0] == 0xfe && (bytes[1] & 0xc0) == 0x80);
}

OpenThreadComponent *global_openthread_component =  // NOLINT(cppcoreguidelines-avoid-non-const-global-variables)
    nullptr;                                        // NOLINT(cppcoreguidelines-avoid-non-const-global-variables)

OpenThreadComponent::OpenThreadComponent() { global_openthread_component = this; }

bool OpenThreadComponent::keep_alive() {
  auto lock = InstanceLock::try_acquire(100);
  if (!lock) {
    ESP_LOGW(TAG, "Keep-alive skipped: OpenThread unavailable or API lock busy");
    return false;
  }
  auto *instance = lock.get_instance();
  if (otThreadGetDeviceRole(instance) != OT_DEVICE_ROLE_CHILD) {
    ESP_LOGW(TAG, "Keep-alive skipped: device is not attached as a child");
    return false;
  }
  if (otThreadGetLinkMode(instance).mRxOnWhenIdle) {
    ESP_LOGW(TAG, "Keep-alive skipped: Data Poll requires SED mode (positive poll_period); MED receives continuously");
    return false;
  }
  const otError error = otLinkSendDataRequest(instance);
  if (error != OT_ERROR_NONE) {
    ESP_LOGW(TAG, "Cannot queue keep-alive Data Poll: %s", otThreadErrorToString(error));
    return false;
  }
  ESP_LOGD(TAG, "Keep-alive Data Poll queued (parent reception not yet confirmed)");
  return true;
}

void OpenThreadComponent::dump_config() {
  ESP_LOGCONFIG(TAG, "Open Thread:");
  ESP_LOGCONFIG(TAG, "  Independent nRF52/Zephyr fork: 1.0.0 (ESPHome 2026.9.1 baseline)");
  ESP_LOGCONFIG(TAG, "  Stack Log Level: %s", USE_OPENTHREAD_NRF52_LOG_LEVEL);
#if CONFIG_OPENTHREAD_FTD
  ESP_LOGCONFIG(TAG, "  Device Type: FTD");
#elif CONFIG_OPENTHREAD_MTD
  ESP_LOGCONFIG(TAG, "  Device Type: MTD");
  // TBD: Synchronized Sleepy End Device
  if (this->poll_period_ > 0) {
    ESP_LOGCONFIG(TAG, "  Device is configured as Sleepy End Device (SED)");
    ESP_LOGCONFIG(TAG, "  Configured Poll Period: %" PRIu32 "ms", this->poll_period_);
  } else {
    ESP_LOGCONFIG(TAG, "  Device is configured as Minimal End Device (MED)");
  }
  auto lock = InstanceLock::try_acquire(10);
  if (lock) {
    ESP_LOGCONFIG(TAG, "  Effective Child Timeout: %" PRIu32 "s", otThreadGetChildTimeout(lock.get_instance()));
    ESP_LOGCONFIG(TAG, "  Child Supervision: interval=%us check_timeout=%us (%s), failures=%u",
                  unsigned(otChildSupervisionGetInterval(lock.get_instance())),
                  unsigned(otChildSupervisionGetCheckTimeout(lock.get_instance())),
                  this->supervision_timeout_ == 0 ? "automatic" : "explicit",
                  unsigned(otChildSupervisionGetCheckFailureCounter(lock.get_instance())));
    if (this->poll_period_ > 0)
      ESP_LOGCONFIG(TAG, "  Effective Keep-alive Poll Period: %" PRIu32 "ms", otLinkGetPollPeriod(lock.get_instance()));
  }
#endif
  if (this->output_power_.has_value()) {
    ESP_LOGCONFIG(TAG, "  Output power: %" PRId8 "dBm", *this->output_power_);
  }
}

void OpenThreadComponent::on_state_changed(otChangedFlags flags, void *context) {
  auto *self = static_cast<OpenThreadComponent *>(context);
  if (flags & OT_CHANGED_THREAD_ROLE) {
    // This runs on the OpenThread task thread with the OT lock held,
    // so we can safely call otThreadGetDeviceRole directly.
    otInstance *instance = self->get_openthread_instance_();
    if (instance == nullptr) return;
    otDeviceRole role = otThreadGetDeviceRole(instance);
    self->connected_ = role >= OT_DEVICE_ROLE_CHILD;
  }
  if (flags & (OT_CHANGED_THREAD_ROLE | OT_CHANGED_THREAD_NETDATA | OT_CHANGED_IP6_ADDRESS_ADDED |
               OT_CHANGED_IP6_ADDRESS_REMOVED | OT_CHANGED_THREAD_ML_ADDR | OT_CHANGED_THREAD_LL_ADDR)) {
    self->ip6_generation_.fetch_add(1);
    self->network_diagnostics_pending_ = true;
    self->enable_loop_soon_any_context();
  }
}

void OpenThreadComponent::loop() {
  if (!this->lock_initialized_ || this->teardown_stage_ != TeardownStage::TEARDOWN_STAGE_NOT_STARTED) return;
  this->service_remote_restart_();
#ifdef USE_OPENTHREAD_JOINER
  this->loop_joiner_();
#endif
  const uint32_t now = millis();
  const bool dirty = this->ip6_sync_retry_ || this->ip6_generation_.load() != this->ip6_synced_generation_;
  if (uint32_t(now - this->ip6_sync_at_) >= (dirty ? 1000U : 5000U)) {
    this->ip6_sync_at_ = now;
    this->sync_ip6_addresses_();
  }
  if (!this->network_diagnostics_pending_.load())
    return;
  auto lock = InstanceLock::try_acquire(10);
  if (!lock)
    return;
  this->network_diagnostics_pending_ = false;
  this->log_network_diagnostics_(lock.get_instance());
}

void OpenThreadComponent::log_network_diagnostics_(otInstance *instance) {
  // Run on the application thread with the OT lock held. Dump actual OT
  // addresses and network data, independently of the OMR sensor's filtering.
  const auto role = otThreadGetDeviceRole(instance);
  ESP_LOGI(TAG, "IPv6 diagnostics: role=%s, SLAAC=%s", otThreadDeviceRoleToString(role),
           otIp6IsSlaacEnabled(instance) ? "enabled" : "disabled");
  unsigned addresses = 0;
  char text[OT_IP6_ADDRESS_STRING_SIZE];
  for (const auto *address = otIp6GetUnicastAddresses(instance); address != nullptr; address = address->mNext) {
    otIp6AddressToString(&address->mAddress, text, sizeof(text));
    ESP_LOGI(TAG, "  OT address: %s valid=%u preferred=%u mesh_local=%u rloc=%u", text,
             unsigned(address->mValid), unsigned(address->mPreferred), unsigned(address->mMeshLocal),
             unsigned(address->mRloc));
    addresses++;
  }
  otNetworkDataIterator iterator = OT_NETWORK_DATA_ITERATOR_INIT;
  otBorderRouterConfig prefix{};
  unsigned prefixes = 0;
  while (otNetDataGetNextOnMeshPrefix(instance, &iterator, &prefix) == OT_ERROR_NONE) {
    otIp6AddressToString(&prefix.mPrefix.mPrefix, text, sizeof(text));
    ESP_LOGI(TAG, "  Network prefix: %s/%u stable=%u on_mesh=%u slaac=%u preferred=%u", text,
             unsigned(prefix.mPrefix.mLength), unsigned(prefix.mStable), unsigned(prefix.mOnMesh),
             unsigned(prefix.mSlaac), unsigned(prefix.mPreferred));
    prefixes++;
  }
  ESP_LOGI(TAG, "IPv6 diagnostics complete: %u addresses, %u on-mesh prefix entries", addresses, prefixes);
}

// Gets the off-mesh routable address
std::optional<otIp6Address> OpenThreadComponent::get_omr_address() {
  auto lock = InstanceLock::try_acquire(10);
  if (!lock) {
    return {};
  }
  return this->get_omr_address(lock);
}

std::optional<otIp6Address> OpenThreadComponent::get_omr_address(InstanceLock &lock) {
  if (!lock) {
    return {};
  }
  otNetworkDataIterator iterator = OT_NETWORK_DATA_ITERATOR_INIT;
  otInstance *instance = lock.get_instance();
  if (otThreadGetDeviceRole(instance) < OT_DEVICE_ROLE_CHILD) {
    return {};
  }
  const otNetifAddress *addresses = otIp6GetUnicastAddresses(instance);
  const auto *mesh_prefix = otThreadGetMeshLocalPrefix(instance);
  std::optional<otIp6Address> selected;
  int selected_preference = -2;

  otBorderRouterConfig config{};
  while (otNetDataGetNextOnMeshPrefix(instance, &iterator, &config) == OT_ERROR_NONE) {
    const auto &prefix = config.mPrefix;
    // OMR prefixes are stable, on-mesh SLAAC /64s. Do not assume the first
    // Network Data prefix is the one usable from the infrastructure network.
    if (prefix.mLength != 64 || !config.mStable || !config.mOnMesh || !config.mSlaac || !config.mPreferred ||
        is_link_local_or_multicast(prefix.mPrefix) ||
        std::memcmp(prefix.mPrefix.mFields.m8, mesh_prefix->m8, 8) == 0) {
      continue;
    }
    for (const auto *addr = addresses; addr != nullptr; addr = addr->mNext) {
      const auto &ip = addr->mAddress;
      if (!addr->mValid || !addr->mPreferred || addr->mRloc || addr->mMeshLocal ||
          is_link_local_or_multicast(ip) ||
          std::memcmp(ip.mFields.m8, mesh_prefix->m8, 8) == 0 ||
          otIp6PrefixMatch(&prefix.mPrefix, &ip) < prefix.mLength) {
        continue;
      }
      // Prefer higher route preference; use a stable address order for ties.
      if (!selected || config.mPreference > selected_preference ||
          (config.mPreference == selected_preference && std::memcmp(ip.mFields.m8, selected->mFields.m8, 16) < 0)) {
        selected = ip;
        selected_preference = config.mPreference;
      }
    }
  }
  return selected;
}

void OpenThreadComponent::defer_factory_reset_external_callback() {
  ESP_LOGD(TAG, "Defer factory_reset_external_callback_");
  this->defer([this]() { if (this->factory_reset_external_callback_) this->factory_reset_external_callback_(); });
}

void OpenThreadSrpComponent::srp_callback(otError err, const otSrpClientHostInfo *host_info,
                                          const otSrpClientService *services,
                                          const otSrpClientService *removed_services, void *context) {
  if (err != 0) {
    ESP_LOGW(TAG, "SRP client reported an error: %s", otThreadErrorToString(err));
    for (const otSrpClientHostInfo *host = host_info; host; host = nullptr) {
      ESP_LOGW(TAG, "  Host: %s", host->mName);
    }
    for (const otSrpClientService *service = services; service; service = service->mNext) {
      ESP_LOGW(TAG, "  Service: %s", service->mName);
    }
  }
}

void OpenThreadSrpComponent::srp_start_callback(const otSockAddr *server_socket_address, void *context) {
  ESP_LOGI(TAG, "SRP client has started");
}

void OpenThreadSrpComponent::srp_factory_reset_callback(otError err, const otSrpClientHostInfo *host_info,
                                                        const otSrpClientService *services,
                                                        const otSrpClientService *removed_services, void *context) {
  OpenThreadComponent *obj = (OpenThreadComponent *) context;
  if (err == OT_ERROR_NONE && removed_services != NULL && host_info != NULL &&
      host_info->mState == OT_SRP_CLIENT_ITEM_STATE_REMOVED) {
    ESP_LOGD(TAG, "Successful Removal SRP Host and Services");
  } else if (err != OT_ERROR_NONE) {
    // Handle other SRP client events or errors
    ESP_LOGW(TAG, "SRP client event/error: %s", otThreadErrorToString(err));
  }
  obj->defer_factory_reset_external_callback();
}

void OpenThreadSrpComponent::setup() {
  if (global_openthread_component == nullptr || global_openthread_component->is_failed()) {
    this->mark_failed();
    return;
  }
  otError error;
  InstanceLock lock = InstanceLock::acquire();
  if (!lock) {
    ESP_LOGW(TAG, "OpenThread is unavailable");
    return;
  }
  otInstance *instance = lock.get_instance();

  otSrpClientSetCallback(instance, OpenThreadSrpComponent::srp_callback, nullptr);

  // set the host name
  uint16_t size;
  char *existing_host_name = otSrpClientBuffersGetHostNameString(instance, &size);
  const auto &host_name = App.get_name();
  uint16_t host_name_len = host_name.size();
  if (host_name_len >= size) {
    ESP_LOGW(TAG, "Hostname is too long, choose a shorter project name");
    return;
  }
  memset(existing_host_name, 0, size);
  memcpy(existing_host_name, host_name.c_str(), host_name_len);

  error = otSrpClientSetHostName(instance, existing_host_name);
  if (error != 0) {
    ESP_LOGW(TAG, "Could not set host name");
    return;
  }

  error = otSrpClientEnableAutoHostAddress(instance);
  if (error != 0) {
    ESP_LOGW(TAG, "Could not enable auto host address");
    return;
  }

  // OpenThread retains these pointers; store TXT data for the service lifetime.
  const auto &mdns_services = this->mdns_->get_services();
  ESP_LOGD(TAG, "Setting up SRP services. count = %u", unsigned(mdns_services.size()));
  for (const auto &service : mdns_services) {
    otSrpClientBuffersServiceEntry *entry = otSrpClientBuffersAllocateService(instance);
    if (!entry) {
      ESP_LOGW(TAG, "Failed to allocate service entry");
      continue;
    }

    // Set service name
    char *string = otSrpClientBuffersGetServiceEntryServiceNameString(entry, &size);
    std::string full_service = std::string(MDNS_STR_ARG(service.service_type)) + "." + MDNS_STR_ARG(service.proto);
    if (full_service.size() >= size) {
      ESP_LOGW(TAG, "Service name too long: %s", full_service.c_str());
      otSrpClientBuffersFreeService(instance, entry);
      continue;
    }
    memcpy(string, full_service.c_str(), full_service.size() + 1);

    // Set instance name (using host_name)
    string = otSrpClientBuffersGetServiceEntryInstanceNameString(entry, &size);
    if (host_name_len >= size) {
      ESP_LOGW(TAG, "Instance name too long: %s", host_name.c_str());
      otSrpClientBuffersFreeService(instance, entry);
      continue;
    }
    memset(string, 0, size);
    memcpy(string, host_name.c_str(), host_name_len);

    // Set port
    entry->mService.mPort = service.port.value();

    otDnsTxtEntry *txt_entries =
        reinterpret_cast<otDnsTxtEntry *>(this->pool_alloc_(sizeof(otDnsTxtEntry) * service.txt_records.size()));
    // Set TXT records
    entry->mService.mNumTxtEntries = service.txt_records.size();
    for (size_t i = 0; i < service.txt_records.size(); i++) {
      const auto &txt = service.txt_records[i];
      // Value is either a compile-time string literal in flash or a pointer to dynamic_txt_values_
      // OpenThread SRP client expects the data to persist, so we copy it
      const char *value_str = MDNS_STR_ARG(txt.value);
      txt_entries[i].mKey = MDNS_STR_ARG(txt.key);
      size_t value_len = strlen(value_str);
      char *value_copy = reinterpret_cast<char *>(this->pool_alloc_(value_len + 1));
      memcpy(value_copy, value_str, value_len + 1);
      txt_entries[i].mValue = reinterpret_cast<const uint8_t *>(value_copy);
      txt_entries[i].mValueLength = value_len;
    }
    entry->mService.mTxtEntries = txt_entries;
    entry->mService.mNumTxtEntries = service.txt_records.size();

    // Add service
    error = otSrpClientAddService(instance, &entry->mService);
    if (error != OT_ERROR_NONE) {
      ESP_LOGW(TAG, "Failed to add service: %s", otThreadErrorToString(error));
      otSrpClientBuffersFreeService(instance, entry);
      continue;
    }
    ESP_LOGD(TAG, "Added service: %s", full_service.c_str());
  }

  otSrpClientEnableAutoStartMode(instance, OpenThreadSrpComponent::srp_start_callback, nullptr);
  ESP_LOGD(TAG, "Finished SRP setup");
}

void *OpenThreadSrpComponent::pool_alloc_(size_t size) {
  uint8_t *ptr = new uint8_t[size];
  this->memory_pool_.emplace_back(std::unique_ptr<uint8_t[]>(ptr));
  return ptr;
}

void OpenThreadSrpComponent::set_mdns(esphome::mdns::MDNSComponent *mdns) { this->mdns_ = mdns; }

bool OpenThreadComponent::teardown() {
  if (!this->lock_initialized_) return true;
  switch (this->teardown_stage_) {
    case TeardownStage::TEARDOWN_STAGE_NOT_STARTED: {
      auto lock = InstanceLock::try_acquire(100);
      if (!lock) {
        // Try again on next teardown loop
        ESP_LOGV(TAG, "Failed to acquire OpenThread lock during teardown");
        return false;
      }
      // Start tearing down
      this->teardown_stage_ = TeardownStage::TEARDOWN_STAGE_STOP_IN_PROCESS;
      ESP_LOGV(TAG, "Clear SRP");
      otInstance *instance = lock.get_instance();
      if (this->restart_socket_open_) {
        otUdpClose(instance, &this->restart_socket_);
        this->restart_socket_open_ = false;
      }
      otSrpClientClearHostAndServices(instance);
      otSrpClientBuffersFreeAllServices(instance);
      if (otThreadSetEnabled(instance, false) != OT_ERROR_NONE) {
        ESP_LOGW(TAG, "Failed to disable Thread during teardown");
      }
      if (otIp6SetEnabled(instance, false) != OT_ERROR_NONE) {
        ESP_LOGW(TAG, "Failed to disable IPv6 during teardown");
      }
      // Stop OpenThread
      this->connected_ = false;
      ESP_LOGV(TAG, "Stop OpenThread");
      int error = this->openthread_stop_();
      if (error != 0) {
        ESP_LOGW(TAG, "Failed attempt to stop OpenThread %d", error);
        this->teardown_stage_ = TeardownStage::TEARDOWN_STAGE_COMPLETED;
      }
    } break;
    case TeardownStage::TEARDOWN_STAGE_STOP_IN_PROCESS:
      // Waiting on OpenThread stop
      break;
    case TeardownStage::TEARDOWN_STAGE_COMPLETED:
      ESP_LOGV(TAG, "OpenThreadComponent Teardown Complete");
      break;
  }
  return this->teardown_stage_ == TeardownStage::TEARDOWN_STAGE_COMPLETED;
}

void OpenThreadComponent::on_factory_reset(std::function<void()> callback) {
  this->factory_reset_external_callback_ = std::move(callback);
  ESP_LOGD(TAG, "Start Removal SRP Host and Services");
  otError error;
  InstanceLock lock = InstanceLock::acquire();
  if (!lock) {
    ESP_LOGW(TAG, "OpenThread is unavailable");
    return;
  }
  otInstance *instance = lock.get_instance();
  otSrpClientSetCallback(instance, OpenThreadSrpComponent::srp_factory_reset_callback, this);
  error = otSrpClientRemoveHostAndServices(instance, true, true);
  if (error != OT_ERROR_NONE) {
    ESP_LOGW(TAG, "Failed to Remove SRP Host and Services");
    return;
  }
  ESP_LOGD(TAG, "Waiting on Confirmation Removal SRP Host and Services");
}

bool OpenThreadComponent::apply_linkmode_(otInstance *instance) {
#if CONFIG_OPENTHREAD_MTD
  uint16_t supervision_timeout;
  if (!this->supervision_timeout_for_poll_(this->poll_period_, supervision_timeout)) {
    ESP_LOGE(TAG, "Invalid Child Supervision policy; link mode retained");
    return false;
  }
#endif
  otLinkModeConfig link_mode_config{};
#if CONFIG_OPENTHREAD_FTD
  link_mode_config.mRxOnWhenIdle = true;
  link_mode_config.mDeviceType = true;
  link_mode_config.mNetworkData = true;
#elif CONFIG_OPENTHREAD_MTD
  link_mode_config.mRxOnWhenIdle = this->poll_period_ == 0;
  link_mode_config.mDeviceType = false;
  link_mode_config.mNetworkData = false;
#endif

  if (otThreadSetLinkMode(instance, link_mode_config) != OT_ERROR_NONE) {
    ESP_LOGE(TAG, "Failed to set linkmode");
    return false;
  }
#if CONFIG_OPENTHREAD_MTD
  if (otLinkSetPollPeriod(instance, this->poll_period_) != OT_ERROR_NONE) {
    ESP_LOGE(TAG, "Failed to set poll period");
    return false;
  }
  otChildSupervisionSetInterval(instance, this->supervision_interval_);
  otChildSupervisionSetCheckTimeout(instance, supervision_timeout);
  ESP_LOGD(TAG, "Polling policy: %s", this->poll_period_ > 0 ? "periodic SED" : "MED (continuous reception)");
#endif
#ifdef ESPHOME_LOG_HAS_DEBUG  // Fetch link mode from OT only when DEBUG
  link_mode_config = otThreadGetLinkMode(instance);
  ESP_LOGD(TAG, "Link Mode Device Type: %s, Network Data: %s, RX On When Idle: %s",
           TRUEFALSE(link_mode_config.mDeviceType), TRUEFALSE(link_mode_config.mNetworkData),
           TRUEFALSE(link_mode_config.mRxOnWhenIdle));
#endif
  return true;
}

}  // namespace esphome::openthread_nrf52
#endif
