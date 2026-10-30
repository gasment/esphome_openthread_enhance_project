#pragma once
// ESPHome 2026.9.1 network and factory-reset consumers have a fixed include path.
// This alias exposes our independent implementation without loading upstream OT.
#include "esphome/components/openthread_nrf52/openthread_nrf52.h"
namespace esphome {
namespace openthread = openthread_nrf52;
}
