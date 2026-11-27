#pragma once

#include <algorithm>
#include <array>
#include <cstdint>
#include <functional>
#include <vector>

namespace oq_native {
// Socket 0 MACRAW model for the ESP-IDF Ethernet driver.
class W5500 {
 public:
  std::function<void(const uint8_t*, size_t)> transmit;
  std::function<void()> on_interface_reset;
  uint64_t sent = 0;
  uint64_t received = 0;
  uint64_t dropped = 0;
  bool cable = false;

  W5500() { reset(); }

  bool link() const {
    const auto phy = common_[0x2e];
    return cable && (phy & 0x80) && (phy & 0x78) != 0x70;
  }

  bool interrupt() const { return (common_[0x18] & 1) && (sockets_[0][2] & sockets_[0][0x2c]); }

  bool receive(const uint8_t* frame, size_t bytes) {
    const auto length = std::max<size_t>(60, bytes) + 2;
    const auto capacity = buffer_size(sockets_[0][0x1e]);
    if (!link() || sockets_[0][4] != 0x42 || bytes < 14 || bytes > 1518 || rx_size() > capacity ||
        length > capacity - rx_size()) {
      ++dropped;
      return false;
    }
    rx_[rx_write_++ % capacity] = length >> 8;
    rx_[rx_write_++ % capacity] = length & 255;
    for (size_t i = 0; i < length - 2; ++i) rx_[rx_write_++ % capacity] = i < bytes ? frame[i] : 0;
    sockets_[0][2] |= 0x04;
    ++received;
    return true;
  }

  uint8_t read(uint8_t block, uint16_t address) const {
    if (block > 31) return 0;
    if (block == 0) {
      if (address == 0x2e) return common_[address] | (link() ? 7 : 0);
      if (address == 0x39) return 4;
      if (address == 0x17) return sockets_[0][2] != 0;
      return address < common_.size() ? common_[address] : 0;
    }
    const auto socket = (block - 1) / 4;
    const auto region = (block - 1) % 4;
    if (region == 0) {
      if (address >= sockets_[socket].size()) return 0;
      if (socket == 0) {
        if (address == 0x20 || address == 0x21)
          return octet(buffer_size(sockets_[0][0x1f]) - std::min<size_t>(tx_size(), buffer_size(sockets_[0][0x1f])),
                       address);
        if (address == 0x22 || address == 0x23) return octet(tx_read_, address);
        if (address == 0x26 || address == 0x27) return octet(rx_size(), address);
        if (address == 0x2a || address == 0x2b) return octet(rx_write_, address);
      }
      return sockets_[socket][address];
    }
    if (socket == 0 && region == 2 && buffer_size(sockets_[0][0x1e]))
      return rx_[address % buffer_size(sockets_[0][0x1e])];
    return 0;
  }

  void write(uint8_t block, uint16_t address, uint8_t value) {
    if (block > 31) return;
    if (block == 0) {
      if (address == 0 && (value & 0x80)) {
        reset();
      } else if (address == 0x2e) {
        const bool had_link = link();
        common_[address] = value & 0xf8;
        if (had_link && !link() && on_interface_reset) on_interface_reset();
      } else if (address < common_.size() && address != 0x39 && address != 0x17) {
        common_[address] = value;
      }
      return;
    }
    const auto socket = (block - 1) / 4;
    const auto region = (block - 1) % 4;
    if (region == 0 && address < sockets_[socket].size()) {
      auto& regs = sockets_[socket];
      if (address == 1) {
        if (socket == 0) command(value);
      } else if (address == 2) {
        regs[2] &= ~value;
      } else if (address == 0x1e || address == 0x1f) {
        if (regs[4] == 0 && (value == 0 || buffer_size(value))) regs[address] = value;
      } else if (address < 4 || address == 0x24 || address == 0x25 || address == 0x28 || address == 0x29 ||
                 address == 0x2c) {
        regs[address] = value;
      }
    }
    if (socket == 0 && region == 1 && buffer_size(sockets_[0][0x1f]))
      tx_[address % buffer_size(sockets_[0][0x1f])] = value;
  }

 private:
  static constexpr size_t kCapacity = 16 * 1024;
  std::array<uint8_t, 64> common_{};
  std::array<std::array<uint8_t, 0x30>, 8> sockets_{};
  std::array<uint8_t, kCapacity> tx_{}, rx_{};
  uint16_t rx_write_ = 0;
  uint16_t rx_ack_ = 0;
  uint16_t tx_read_ = 0;

  static size_t buffer_size(uint8_t kilobytes) {
    return kilobytes <= 16 && (kilobytes & (kilobytes - 1)) == 0 ? kilobytes * 1024 : 0;
  }

  static uint8_t octet(uint16_t value, uint16_t address) { return address & 1 ? value & 255 : value >> 8; }

  uint16_t pointer(size_t address) const { return (uint16_t(sockets_[0][address]) << 8) | sockets_[0][address + 1]; }
  uint16_t rx_size() const { return rx_write_ - rx_ack_; }
  uint16_t tx_size() const { return pointer(0x24) - tx_read_; }

  void reset() {
    common_.fill(0);
    common_[0x2e] = 0xb8;
    for (auto& regs : sockets_) {
      regs.fill(0);
      regs[0x1e] = regs[0x1f] = 2;
      regs[0x2c] = 0xff;
    }
    reset_socket();
    tx_.fill(0);
    rx_.fill(0);
  }

  void reset_socket() {
    auto& regs = sockets_[0];
    regs[2] = regs[4] = regs[0x24] = regs[0x25] = regs[0x28] = regs[0x29] = 0;
    rx_write_ = rx_ack_ = tx_read_ = 0;
    if (on_interface_reset) on_interface_reset();
  }

  void command(uint8_t value) {
    auto& regs = sockets_[0];
    switch (value) {
      case 0x01:  // OPEN
        reset_socket();
        if ((regs[0] & 0x0f) == 4) regs[4] = 0x42;
        break;
      case 0x10:  // CLOSE
        reset_socket();
        break;
      case 0x20: {  // SEND
        const size_t capacity = buffer_size(regs[0x1f]);
        const size_t length = tx_size();
        if (!link() || regs[4] != 0x42 || length < 14 || length > 1518 || length > capacity) break;
        std::array<uint8_t, 1518> packet{};
        for (size_t i = 0; i < length; ++i) packet[i] = tx_[(tx_read_ + i) % capacity];
        tx_read_ = pointer(0x24);
        regs[2] |= 0x10;
        ++sent;
        if (transmit) transmit(packet.data(), length);
        break;
      }
      case 0x40: {  // RECV
        const uint16_t next = pointer(0x28);
        if (uint16_t(next - rx_ack_) <= rx_size()) rx_ack_ = next;
        break;
      }
      default:
        break;
    }
  }
};
}  // namespace oq_native
