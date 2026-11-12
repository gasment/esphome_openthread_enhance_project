#pragma once
// Small wire primitives for management only. Never include api/api_pb2.h.
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <string_view>
#include <vector>
#include "api_buffer.h"

namespace esphome::thread_proxy_api {

inline constexpr uint16_t MAX_MESSAGE_TYPE = 16383;
struct ProtoVarIntResult {
  uint32_t value;
  uint32_t consumed;
  bool has_value() const { return consumed != 0; }
};
class ProtoVarInt {
 public:
  static ProtoVarIntResult parse(const uint8_t *data, uint32_t length) {
    uint32_t value = 0;
    for (uint32_t i = 0; i < length && i < 5; i++) {
      if (i == 4 && data[i] > 15) return {0, 0};
      value |= uint32_t(data[i] & 127) << (i * 7);
      if (!(data[i] & 128)) return {value, i + 1};
    }
    return {0, 0};
  }
  static ProtoVarIntResult parse_non_empty(const uint8_t *data, uint32_t length) { return parse(data, length); }
};
class ProtoSize {
 public:
  static constexpr uint8_t varint16(uint16_t value) { return value < 128 ? 1 : value < 16384 ? 2 : 3; }
};
class ProtoWriteBuffer {
 public:
  explicit ProtoWriteBuffer(APIBuffer *buffer) : buffer_(buffer) {}
  APIBuffer *get_buffer() const { return buffer_; }
 protected:
  APIBuffer *buffer_;
};

// Bounded builder; input is read only and strings are copied before returning.
class ManagementMessage {
 public:
  static constexpr size_t MAX_SIZE = 1024;
  void integer(uint32_t field, uint32_t value) { varint_(field << 3); varint_(value); }
  void text(uint32_t field, std::string_view value) {
    bytes(field, reinterpret_cast<const uint8_t *>(value.data()), value.size());
  }
  void bytes(uint32_t field, const uint8_t *data, size_t length) {
    if (length > MAX_SIZE) { valid_ = false; return; }
    varint_((field << 3) | 2);
    varint_(length);
    if (data_.size() + length > MAX_SIZE) { valid_ = false; return; }
    if (length) data_.insert(data_.end(), data, data + length);
  }
  void pair(std::string_view key, std::string_view value) {
    ManagementMessage map;
    map.text(1, key); map.text(2, value);
    if (!map.valid()) { valid_ = false; return; }
    bytes(2, map.data(), map.size());
  }
  bool valid() const { return valid_; }
  const uint8_t *data() const { return data_.data(); }
  size_t size() const { return data_.size(); }
 protected:
  void varint_(uint32_t value) {
    do {
      if (data_.size() == MAX_SIZE) { valid_ = false; return; }
      data_.push_back((value & 127) | (value > 127 ? 128 : 0));
      value >>= 7;
    } while (value);
  }
  std::vector<uint8_t> data_;
  bool valid_{true};
};

// Validate the envelope before accepting even requests whose fields we ignore.
// Groups are unsupported. Fixed64/varint values can occupy up to 10 bytes.
inline bool valid_management_message(const uint8_t *data, size_t size) {
  size_t offset = 0;
  while (offset < size) {
    auto tag = ProtoVarInt::parse(data + offset, size - offset);
    if (!tag.has_value() || (tag.value >> 3) == 0) return false;
    offset += tag.consumed;
    size_t length = 0;
    switch (tag.value & 7) {
      case 0: {
        bool finished = false;
        for (uint8_t i = 0; i < 10 && offset < size; i++) {
          uint8_t byte = data[offset++];
          if (i == 9 && byte > 1) return false;
          if (!(byte & 128)) { finished = true; break; }
        }
        if (!finished) return false;
        continue;
      }
      case 1: length = 8; break;
      case 2: {
        auto value = ProtoVarInt::parse(data + offset, size - offset);
        if (!value.has_value()) return false;
        offset += value.consumed;
        length = value.value;
        break;
      }
      case 5: length = 4; break;
      default: return false;
    }
    if (length > size - offset) return false;
    offset += length;
  }
  return true;
}
}  // namespace esphome::thread_proxy_api
