#pragma once
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

struct net_if;

/* Plain transfer record: never expose Zephyr's net_if_addr layout to C++. */
struct nrf52_ip6_native_address {
  uint8_t address[16];
  int32_t state;
};

int nrf52_ip6_native_snapshot(struct net_if *iface, struct nrf52_ip6_native_address *entries, size_t capacity);
/* 0=unchanged, 1=repaired, negative errno=failure. Never removes/unrefs addresses. */
int nrf52_ip6_native_ensure(struct net_if *iface, const uint8_t address[16], int type, int state, int mesh_local);

#ifdef __cplusplus
}
#endif
