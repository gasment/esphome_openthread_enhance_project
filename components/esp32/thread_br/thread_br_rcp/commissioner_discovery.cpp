#include "commissioner_discovery.h"
#include <cstring>

namespace {
thread_br_joiner_discovery_callback_t callback = nullptr;
void *callback_context = nullptr;

bool valid_name(const uint8_t *data, size_t length) {
  // Reject malformed/overlong UTF-8 and embedded control characters before JSON.
  size_t i = 0;
  while (i < length) {
    uint32_t value = data[i++], minimum = 0;
    unsigned extra = 0;
    if (value < 0x80) {
      if (value < 32 || value == 127) return false;
      continue;
    }
    if ((value & 0xe0) == 0xc0) { value &= 0x1f; extra = 1; minimum = 0x80; }
    else if ((value & 0xf0) == 0xe0) { value &= 0x0f; extra = 2; minimum = 0x800; }
    else if ((value & 0xf8) == 0xf0) { value &= 7; extra = 3; minimum = 0x10000; }
    else return false;
    if (i + extra > length) return false;
    while (extra--) {
      const uint8_t c = data[i++];
      if ((c & 0xc0) != 0x80) return false;
      value = (value << 6) | (c & 0x3f);
    }
    if (value < minimum || (value >= 0x7f && value <= 0x9f) ||
        value > 0x10ffff || (value >= 0xd800 && value <= 0xdfff)) return false;
  }
  return length >= 1 && length <= 48;
}
}

extern "C" void thread_br_joiner_set_discovery_callback(thread_br_joiner_discovery_callback_t cb, void *context) {
  callback = cb;
  callback_context = context;
}

extern "C" bool thread_br_joiner_discovery_report(const otExtAddress *address, uint8_t version,
                                                  const otMessage *message, uint16_t offset, uint16_t length) {
  if (!callback || !address || !message || uint32_t(offset) + length > otMessageGetLength(message)) return false;
  const uint32_t end = uint32_t(offset) + length;
  while (uint32_t(offset) + 2 <= end) {
    uint8_t header[4];
    if (otMessageRead(message, offset, header, 2) != 2) return false;
    uint16_t size = header[1], header_size = 2;
    if (size == 255) {
      if (uint32_t(offset) + 4 > end || otMessageRead(message, offset + 2, header + 2, 2) != 2) return false;
      size = (uint16_t(header[2]) << 8) | header[3];
      header_size = 4;
    }
    if (uint32_t(offset) + header_size + size > end) return false;
    // Experimental Joiner Advertisement TLV 241, private project OUI FFFFFF.
    if (header[0] == 241 && header_size == 2 && size >= 16 && size <= 63) {
      uint8_t data[63];
      if (otMessageRead(message, offset + header_size, data, size) != size) return false;
      static const uint8_t PREFIX[]{0xff, 0xff, 0xff, 'E', 'S', 'J', 1};
      if (std::memcmp(data, PREFIX, sizeof(PREFIX)) == 0 && valid_name(data + 15, size - 15)) {
        otExtAddress id;
        char name[49]{};
        std::memcpy(id.m8, data + 7, 8);
        std::memcpy(name, data + 15, size - 15);
        callback(address, version, &id, name, callback_context);
        return true;
      }
    }
    const uint32_t next = uint32_t(offset) + header_size + size;
    if (next > UINT16_MAX) return false;
    offset = static_cast<uint16_t>(next);
  }
  return false;
}
