#include "openthread_nrf52.h"
#ifdef USE_OPENTHREAD_NRF52
#include "esphome/core/helpers.h"
#include "esphome/core/log.h"
#include "ip6_native.h"
#include <openthread/platform/radio.h>
#include <zephyr/net/net_if.h>
#include <cstring>
#include <cerrno>
#include <array>

namespace esphome::openthread_nrf52 {
static const char *const TAG = "openthread_nrf52";

namespace {
static const char *const IP6_TAG = "openthread_nrf52.ip6";

bool is_native_address(const otNetifAddress &address) {
  const auto *bytes = address.mAddress.mFields.m8;
  // Match NCS 2.9.2's RLOC/ALOC exclusion in openthread_utils.c.
  const bool aloc = bytes[8] == 0 && bytes[9] == 0 && bytes[10] == 0 &&
                    bytes[11] == 0xff && bytes[12] == 0xfe && bytes[13] == 0 && bytes[14] == 0xfc;
  return address.mValid && !address.mRloc && !aloc;
}

struct DesiredAddress {
  struct in6_addr address{};
  enum net_addr_type type{NET_ADDR_AUTOCONF};
  enum net_addr_state state{NET_ADDR_PREFERRED};
  bool mesh_local{false};
};

struct AddressSnapshot {
  std::array<nrf52_ip6_native_address, CONFIG_NET_IF_UNICAST_IPV6_ADDR_COUNT> entries{};
  size_t count{0};
  bool valid{false};
};

AddressSnapshot snapshot_addresses(struct net_if *iface) {
  AddressSnapshot snapshot;
  const int count = nrf52_ip6_native_snapshot(iface, snapshot.entries.data(), snapshot.entries.size());
  snapshot.valid = count >= 0;
  if (snapshot.valid) snapshot.count = count;
  return snapshot;
}
}  // namespace

void OpenThreadComponent::setup() {
  auto *context = openthread_get_default_context();
  if (context == nullptr || context->instance == nullptr) {
    ESP_LOGE(TAG, "Zephyr OpenThread context is unavailable");
    this->mark_failed();
    return;
  }
  // The L2 interface must be up for native IPv6 sockets. MANUAL_START prevents
  // net_if_up() from joining before the configured dataset has been applied.
  int result = net_if_up(context->iface);
  if (result < 0 && result != -EALREADY) {
    ESP_LOGE(TAG, "Failed to bring Thread interface up: %d", result);
    this->mark_failed();
    return;
  }
  this->lock_initialized_ = true;
  {
    auto lock = InstanceLock::acquire();
    if (!lock) {
      this->mark_failed();
      return;
    }
    auto *instance = lock.get_instance();
#ifdef USE_OPENTHREAD_JOINER
    if (!this->joiner_.boot_locked(instance) || otIp6SetEnabled(instance, true) != OT_ERROR_NONE) {
      this->mark_failed();
      return;
    }
#else
    otOperationalDatasetTlvs dataset{};
    bool apply_dataset = true;
#ifndef USE_OPENTHREAD_NRF52_FORCE_DATASET
    if (otDatasetGetActiveTlvs(instance, &dataset) == OT_ERROR_NONE && dataset.mLength > 0 &&
        otDatasetIsCommissioned(instance)) {
      apply_dataset = false;
      ESP_LOGI(TAG, "Using stored dataset (force_dataset: true to override)");
    }
#endif
    if (apply_dataset) {
      constexpr size_t chars = sizeof(USE_OPENTHREAD_NRF52_TLVS) - 1;
      static_assert(chars % 2 == 0 && chars / 2 <= sizeof(dataset.mTlvs));
      if (parse_hex(USE_OPENTHREAD_NRF52_TLVS, chars, dataset.mTlvs, chars / 2) != chars) {
        ESP_LOGE(TAG, "Invalid configured dataset encoding");
        this->mark_failed();
        return;
      }
      dataset.mLength = chars / 2;
      // The interface can already have a persistent dataset, but must not have
      // Thread enabled while replacing it. Do not erase settings on failure.
      auto error = otThreadSetEnabled(instance, false);
      if (error == OT_ERROR_NONE) error = otDatasetSetActiveTlvs(instance, &dataset);
      if (error != OT_ERROR_NONE || !otDatasetIsCommissioned(instance)) {
        ESP_LOGE(TAG, "Cannot apply complete dataset: %s", otThreadErrorToString(error));
        this->mark_failed();
        return;
      }
    }
#endif
#if CONFIG_OPENTHREAD_MTD
    if (this->child_timeout_.has_value()) {
      otThreadSetChildTimeout(instance, *this->child_timeout_);
      ESP_LOGI(TAG, "Child timeout: requested=%" PRIu32 "s, effective=%" PRIu32 "s",
               *this->child_timeout_, otThreadGetChildTimeout(instance));
    }
#endif
    if (!this->apply_linkmode_(instance)) {
      this->mark_failed();
      return;
    }
    if (this->output_power_.has_value()) {
      const auto error = otPlatRadioSetTransmitPower(instance, *this->output_power_);
      if (error != OT_ERROR_NONE) {
        ESP_LOGE(TAG, "Cannot set transmit power: %s", otThreadErrorToString(error));
        this->mark_failed();
        return;
      }
    }
    this->state_callback_.state_changed_cb = [](otChangedFlags flags, struct openthread_context *, void *user) {
      OpenThreadComponent::on_state_changed(flags, user);
    };
    this->state_callback_.user_data = this;
    result = openthread_state_changed_cb_register(context, &this->state_callback_);
    if (result != 0) {
      ESP_LOGE(TAG, "Cannot register state callback: %d", result);
      this->mark_failed();
      return;
    }
    this->callback_registered_ = true;
  }
  // openthread_start() takes the API mutex itself. MTD_SED is deliberately
  // disabled in Kconfig so it cannot override our runtime poll_period/link mode.
#ifdef USE_OPENTHREAD_JOINER
  bool commissioned = false;
  {
    auto lock = InstanceLock::acquire();
    if (lock) commissioned = otDatasetIsCommissioned(lock.get_instance());
  }
  result = commissioned ? openthread_start(context) : 0;
  ESP_LOGI(TAG, "Joiner mode: %s", commissioned ? "attaching with stored Dataset" : "waiting for joiner.start");
#else
  result = openthread_start(context);
#endif
  if (result != 0) {
    ESP_LOGE(TAG, "Cannot start Thread: %d", result);
    auto lock = InstanceLock::acquire();
    if (lock) {
      openthread_state_changed_cb_unregister(context, &this->state_callback_);
      this->callback_registered_ = false;
    }
    this->connected_ = false;
    this->mark_failed();
    return;
  }
  auto lock = InstanceLock::acquire();
  if (lock) this->connected_ = otThreadGetDeviceRole(lock.get_instance()) >= OT_DEVICE_ROLE_CHILD;
#ifdef USE_OPENTHREAD_JOINER
  this->joiner_.finish_boot();
#endif
}

void OpenThreadComponent::ot_main() {}  // Zephyr owns the OpenThread work queue.

otInstance *OpenThreadComponent::get_openthread_instance_() { return openthread_get_default_instance(); }

int OpenThreadComponent::openthread_stop_() {
  // Called with the OT lock held, after common teardown disables Thread/IPv6.
  if (this->callback_registered_) {
    openthread_state_changed_cb_unregister(openthread_get_default_context(), &this->state_callback_);
    this->callback_registered_ = false;
  }
  this->connected_ = false;
  this->lock_initialized_ = false;
  this->teardown_stage_ = TeardownStage::TEARDOWN_STAGE_COMPLETED;
  return 0;
}

void OpenThreadComponent::sync_ip6_addresses_() {
  this->ip6_sync_retry_ = true;
  auto lock = InstanceLock::try_acquire(0);
  if (!lock) return;
  auto *context = openthread_get_default_context();
  auto *instance = lock.get_instance();
  if (context == nullptr || context->iface == nullptr || !otIp6IsEnabled(instance) ||
      otThreadGetDeviceRole(instance) < OT_DEVICE_ROLE_CHILD) return;
  auto *iface = context->iface;
  const uint32_t generation = this->ip6_generation_.load();
  std::vector<DesiredAddress> desired;
  const auto *mesh_prefix = otThreadGetMeshLocalPrefix(instance);
  for (auto *address = otIp6GetUnicastAddresses(instance); address != nullptr; address = address->mNext) {
    if (!is_native_address(*address)) continue;
    DesiredAddress entry;
    std::memcpy(&entry.address, address->mAddress.mFields.m8, sizeof(entry.address));
    entry.state = address->mPreferred ? NET_ADDR_PREFERRED : NET_ADDR_DEPRECATED;
    entry.mesh_local = std::memcmp(address->mAddress.mFields.m8, mesh_prefix->m8, sizeof(mesh_prefix->m8)) == 0;
    switch (address->mAddressOrigin) {
      case OT_ADDRESS_ORIGIN_THREAD:
      case OT_ADDRESS_ORIGIN_SLAAC: entry.type = NET_ADDR_AUTOCONF; break;
      case OT_ADDRESS_ORIGIN_DHCPV6: entry.type = NET_ADDR_DHCP; break;
      case OT_ADDRESS_ORIGIN_MANUAL: entry.type = NET_ADDR_MANUAL; break;
      default: continue;
    }
    desired.push_back(entry);
  }

  // Zephyr's OT state handler already removes stale native addresses BEFORE
  // calling component callbacks. Do not repeat net_if_ipv6_addr_rm here: it
  // decrements a reference on each call, even if the remaining owner is a socket.
  // The native socket close path reclaims those slots once its references drain.
  size_t repaired = 0;
  bool success = true;
  for (auto &entry : desired) {
    const int result = nrf52_ip6_native_ensure(iface, reinterpret_cast<const uint8_t *>(&entry.address),
                                              entry.type, entry.state, entry.mesh_local);
    if (result < 0) success = false;
    else if (result > 0) ++repaired;
  }
  // Re-read after repair. No address pointers escape the interface lock.
  const auto actual = snapshot_addresses(iface);
  if (!actual.valid) return;
  size_t stale = 0;
  for (size_t i = 0; i < actual.count; ++i) {
    bool found = false;
    for (const auto &entry : desired)
      if (std::memcmp(&entry.address, actual.entries[i].address, sizeof(entry.address)) == 0) { found = true; break; }
    if (!found) ++stale;
  }
  for (const auto &entry : desired) {
    bool found = false;
    for (size_t i = 0; i < actual.count; ++i)
      if (std::memcmp(&entry.address, actual.entries[i].address, sizeof(entry.address)) == 0 &&
          entry.state == actual.entries[i].state) { found = true; break; }
    success = success && found;
  }
  success = success && stale == 0;
  if (repaired)
    ESP_LOGI(IP6_TAG, "Repaired %u missing/address-state entries after native cleanup", unsigned(repaired));
  if (success) {
    if (!this->ip6_sync_logged_ || this->ip6_warned_)
      ESP_LOGI(IP6_TAG, "IPv6 address tables synchronized: OT=%u Zephyr=%u capacity=%u",
               unsigned(desired.size()), unsigned(actual.count), unsigned(CONFIG_NET_IF_UNICAST_IPV6_ADDR_COUNT));
    this->ip6_sync_logged_ = true;
    this->ip6_synced_generation_ = generation;
    this->ip6_sync_retry_ = false;
    this->ip6_warned_ = false;
  } else {
    // Log first failure and changes, not an unchanged condition every 30 seconds.
    uint32_t signature = 2166136261U;
    auto hash = [&signature](const void *data, size_t size) {
      const auto *bytes = static_cast<const uint8_t *>(data);
      for (size_t i = 0; i < size; ++i) signature = (signature ^ bytes[i]) * 16777619U;
    };
    for (const auto &entry : desired) { hash(&entry.address, sizeof(entry.address)); hash(&entry.state, sizeof(entry.state)); }
    for (size_t i = 0; i < actual.count; ++i) {
      hash(actual.entries[i].address, sizeof(actual.entries[i].address));
      hash(&actual.entries[i].state, sizeof(actual.entries[i].state));
    }
    if (!this->ip6_warned_ || this->ip6_warning_signature_ != signature) {
      this->ip6_warned_ = true;
      this->ip6_warning_signature_ = signature;
      ESP_LOGW(IP6_TAG, "IPv6 tables differ: OT=%u Zephyr=%u stale/pending=%u capacity=%u; retrying, preserving socket references",
               unsigned(desired.size()), unsigned(actual.count), unsigned(stale), unsigned(CONFIG_NET_IF_UNICAST_IPV6_ADDR_COUNT));
    }
  }
}

network::IPAddresses OpenThreadComponent::get_ip_addresses() {
  network::IPAddresses addresses{};
  auto lock = InstanceLock::try_acquire(10);
  if (!lock) return addresses;
  size_t count = 1;  // Index zero is reserved for IPv4 by ESPHome.
  // Keep application/global addresses visible when OT has more entries than
  // ESPHome's fixed-size array. RLOC/ALOC are internal transport locators.
  const auto *mesh_prefix = otThreadGetMeshLocalPrefix(lock.get_instance());
  for (bool external_first : {true, false})
    for (const auto *addr = otIp6GetUnicastAddresses(lock.get_instance());
         addr != nullptr && count < addresses.size(); addr = addr->mNext) {
      if (!is_native_address(*addr)) continue;
      const auto *bytes = addr->mAddress.mFields.m8;
      const bool external = addr->mPreferred && !(bytes[0] == 0xfe && (bytes[1] & 0xc0) == 0x80) &&
                            std::memcmp(bytes, mesh_prefix->m8, sizeof(mesh_prefix->m8)) != 0;
      if (external != external_first) continue;
      struct in6_addr ip6{};
      std::memcpy(&ip6, bytes, sizeof(ip6));
      addresses[count++] = network::IPAddress(&ip6);
    }
  return addresses;
}

InstanceLock InstanceLock::try_acquire(int delay) {
  auto *component = global_openthread_component;
  auto *context = openthread_get_default_context();
  if (component == nullptr || !component->is_lock_initialized() || context == nullptr || context->instance == nullptr)
    return InstanceLock(false);
  return InstanceLock(k_mutex_lock(&context->api_lock, K_MSEC(delay)) == 0);
}

InstanceLock InstanceLock::acquire() {
  auto *component = global_openthread_component;
  auto *context = openthread_get_default_context();
  if (component == nullptr || !component->is_lock_initialized() || context == nullptr || context->instance == nullptr)
    return InstanceLock(false);
  return InstanceLock(k_mutex_lock(&context->api_lock, K_FOREVER) == 0);
}

otInstance *InstanceLock::get_instance() { return this->owns_ ? openthread_get_default_instance() : nullptr; }

InstanceLock::~InstanceLock() {
  if (this->owns_) k_mutex_unlock(&openthread_get_default_context()->api_lock);
}
}  // namespace esphome::openthread_nrf52
#endif
