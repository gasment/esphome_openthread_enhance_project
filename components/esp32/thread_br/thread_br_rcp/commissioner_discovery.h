#pragma once
#include <stdbool.h>
#include <stdint.h>
#include "openthread/link.h"
#include "openthread/message.h"
#ifdef __cplusplus
extern "C" {
#endif
typedef void (*thread_br_joiner_discovery_callback_t)(const otExtAddress *address, uint8_t version,
                                                     const otExtAddress *device_id, const char *name, void *context);
// Called only under the OpenThread API lock.
void thread_br_joiner_set_discovery_callback(thread_br_joiner_discovery_callback_t callback, void *context);
// Returns true only for a recognized project advertisement. Other Joiners keep
// the native discovery callback. The raw message never leaves the OT callback.
bool thread_br_joiner_discovery_report(const otExtAddress *address, uint8_t version, const otMessage *message,
                                     uint16_t offset, uint16_t length);
#ifdef __cplusplus
}
#endif
