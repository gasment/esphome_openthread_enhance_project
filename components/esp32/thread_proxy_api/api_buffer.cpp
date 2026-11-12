// Native framing adapted from the repository's ESPHome-derived Thread SED transport.
// Isolated namespace/defines; no APIServer, entities, provisioning or global_api_server.
#include "api_buffer.h"
#ifdef ESPHOME_DEBUG_THREAD_PROXY_API
#include "esphome/core/log.h"
#endif

namespace esphome::thread_proxy_api {

#ifdef ESPHOME_DEBUG_THREAD_PROXY_API
void APIBuffer::debug_check_drop_(size_t drop) const {
  if (drop > this->size_) {
    ESP_LOGE("thread_proxy_api.buffer", "drop_front: drop=%zu size=%u", drop, this->size_);
    abort();
  }
}
#endif

bool APIBuffer::grow_(size_t n) {
  if (n > MAX_SIZE)
    return false;
  // realloc extends in place when it can, avoiding the copy
  uint8_t *grown = RAMAllocator<uint8_t>().reallocate(this->data_.get(), n);
  if (grown == nullptr)
    return false;
  (void) this->data_.release();  // realloc already freed or reused the old block
  this->data_.reset(grown);
  this->capacity_ = n;
  return true;
}

uint8_t *APIBuffer::append(size_t n) {
  const size_t old_size = this->size_;
  if (!this->resize(old_size + n))
    return nullptr;
  return this->data_.get() + old_size;
}

}  // namespace esphome::thread_proxy_api
