#include "ip6_native.h"
#include <zephyr/net/net_if.h>
#include <errno.h>
#include <string.h>

/* Compile these accesses as C, just like Zephyr. With DAD/ACD disabled,
 * net_if_addr contains an empty GNU C union whose C++ layout differs. */
int nrf52_ip6_native_snapshot(struct net_if *iface, struct nrf52_ip6_native_address *entries, size_t capacity) {
  if (iface == NULL || entries == NULL) return -EINVAL;
  int count = 0;
  net_if_lock(iface);
  const struct net_if_ipv6 *ipv6 = iface->config.ip.ipv6;
  if (ipv6 != NULL) {
    for (size_t i = 0; i < NET_IF_MAX_IPV6_ADDR; ++i) {
      const struct net_if_addr *address = &ipv6->unicast[i];
      if (!address->is_used || address->address.family != AF_INET6) continue;
      if ((size_t) count >= capacity) { count = -ENOSPC; break; }
      memcpy(entries[count].address, &address->address.in6_addr, sizeof(entries[count].address));
      entries[count].state = address->addr_state;
      ++count;
    }
  }
  net_if_unlock(iface);
  return count;
}

int nrf52_ip6_native_ensure(struct net_if *iface, const uint8_t address[16], int type, int state, int mesh_local) {
  if (iface == NULL || address == NULL) return -EINVAL;
  struct in6_addr ip6;
  memcpy(&ip6, address, sizeof(ip6));
  net_if_lock(iface);
  struct net_if_addr *native = net_if_ipv6_addr_lookup_by_iface(iface, &ip6);
  const int missing = native == NULL;
  if (missing) native = net_if_ipv6_addr_add(iface, &ip6, (enum net_addr_type) type, 0);
  int result = 0;
  if (native == NULL) {
    result = -ENOMEM;
  } else if (missing || native->addr_state != (enum net_addr_state) state || native->is_mesh_local != !!mesh_local) {
    native->addr_state = (enum net_addr_state) state;
    native->is_mesh_local = !!mesh_local;
    result = 1;
  }
  net_if_unlock(iface);
  return result;
}
