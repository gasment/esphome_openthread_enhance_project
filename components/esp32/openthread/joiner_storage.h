#pragma once
#include "esphome/core/defines.h"
#ifdef USE_OPENTHREAD_JOINER
#include <cstdint>
#include <openthread/instance.h>
#include <openthread/error.h>

namespace esphome::openthread::joiner_storage {
otError read(otInstance *instance, uint8_t *data, uint16_t *size);
otError write(otInstance *instance, const uint8_t *data, uint16_t size);
otError remove(otInstance *instance);
}  // namespace esphome::openthread::joiner_storage
#endif
