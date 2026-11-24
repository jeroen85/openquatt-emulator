#pragma once

#include <array>
#include <cmath>
#include <cstdint>
#include <functional>
#include <stdexcept>
#include <string>
#include <vector>

#include "w5500.h"

namespace oq_native {
constexpr uint32_t kSpi2 = 0x60024000;
constexpr uint32_t kSpi = 0x60025000;
constexpr uint32_t kGdma = 0x6003f000;
constexpr uint32_t kGpio = 0x60004000;
constexpr uint32_t kDone = 1u << 12;

struct Memory {
  virtual uint32_t read32(uint32_t address) = 0;
  virtual uint8_t read8(uint32_t address) = 0;
  virtual void write32(uint32_t address, uint32_t value) = 0;
  virtual void write8(uint32_t address, uint8_t value) = 0;
  virtual ~Memory() = default;
};

inline void require(bool ok, const char* message) {
  if (!ok) throw std::runtime_error(message);
}

inline void dram(uint32_t address, uint32_t length) {
  require(length > 0 && length <= 4096 && address >= 0x3fc88000 && uint64_t(address) + length <= 0x3fd00000,
          "DMA access outside internal DRAM");
}

struct Max31865 {
  double temperature = 30.0;
  double sampled_temperature = 30.0;
  bool temperature_valid = true;
  bool sample_valid = false;
  uint8_t physical_fault = 0;
  uint8_t config = 0;
  bool selected = false;
  int address = -1;
  bool writing = false;
  uint16_t sample = 0;
  std::array<uint8_t, 4> thresholds{0xff, 0xff, 0, 0};

  void select(bool active) {
    if (selected == active) return;
    address = -1;
    selected = active;
    if (active) {
      require(std::isfinite(temperature) && temperature >= 0 && temperature <= 100,
              "RTD temperature outside qualified range");
      sampled_temperature = temperature;
      sample_valid = temperature_valid;
      const double resistance = 1000 * (1 + 3.9083e-3 * temperature - 5.775e-7 * temperature * temperature);
      sample = static_cast<uint16_t>(std::lround(resistance / 1500.0 * 32768.0) << 1);
      if (physical_fault) sample |= 1;
    }
  }

  uint8_t exchange(uint8_t value) {
    require(selected, "SPI transfer without MAX31865 chip select");
    if (address < 0) {
      address = value & 0x7f;
      writing = value & 0x80;
      return 0;
    }
    const int index = address;
    address = (address + 1) & 0x7f;
    if (writing) {
      if (index == 0) {
        const int detect = value & 0x0c;
        require(!(detect && (value & 0x20)), "Conflicting RTD conversion/fault command");
        config = value & ~0x22;
        if (detect == 4 || detect == 12) config &= ~0x0c;
      } else if (index >= 3 && index <= 6) {
        thresholds[index - 3] = value;
      }
      return 0;
    }
    if (index == 0) return config;
    if (index == 1) return sample >> 8;
    if (index == 2) return sample & 255;
    if (index >= 3 && index <= 6) return thresholds[index - 3];
    if (index == 7) return physical_fault;
    return 0xff;
  }
};

struct Direction {
  std::array<uint32_t, 24> regs{};
  uint32_t descriptor = 0;
  uint32_t peripheral = 0x3f;
  uint32_t raw = 0;
  uint32_t enable = 0;
  bool pending = false;
  bool running = false;

  uint32_t read(uint32_t offset, bool receive) const {
    if (offset == 8) return raw;
    if (offset == 12) return raw & enable;
    if (offset == 16) return enable;
    if (offset == 20) return 0;
    if (offset == 32) return (descriptor & 0xfffff) | (!running ? 1u << (receive ? 24 : 23) : 0);
    if (offset == 72) return peripheral;
    return regs[offset / 4];
  }

  void write(uint32_t offset, uint32_t value, bool receive) {
    regs[offset / 4] = value;
    if (offset == 0 && (value & 1)) {
      pending = running = false;
      descriptor = raw = 0;
    }
    if (offset == 16) enable = value;
    if (offset == 20) raw &= ~value;
    if (offset == 72) {
      if (peripheral != (value & 63)) {
        pending = running = false;
        descriptor = raw = 0;
      }
      peripheral = value & 63;
    }
    if (offset == 32) {
      descriptor = 0x3fc00000 | (value & 0xfffff);
      if (value & (1u << (receive ? 21 : 20))) pending = running = false;
      require(!(value & (1u << (receive ? 23 : 22))), "GDMA restart is not qualified");
      if (value & (1u << (receive ? 22 : 21))) pending = running = true;
    }
  }
};

struct Descriptor {
  uint32_t address;
  uint32_t flags;
  uint32_t buffer;
  uint32_t count;
};

struct Event {
  std::vector<uint8_t> tx;
  std::vector<uint8_t> rx;
  uint64_t number;
  bool rtd_sample_read;
};

enum class WriteAction { kPass, kIntercept };

class DeviceModel {
 public:
  Memory& memory;
  Max31865 rtd;
  W5500 w5500;
  bool ethernet_enabled = false;
  std::array<uint32_t, 64> spi2{};
  std::array<uint32_t, 64> spi{};
  std::array<std::array<Direction, 2>, 5> dma{};
  uint64_t transactions = 0;
  uint64_t dma_reads = 0;
  uint64_t dma_writes = 0;
  uint64_t spi_reads = 0;
  uint64_t spi_writes = 0;
  std::function<void(const Event&)> event;

  explicit DeviceModel(Memory& mem) : memory(mem) { spi[0x20 / 4] = spi2[0x20 / 4] = 0x3f; }

  bool spi2_interrupt() const { return spi2[0x3c / 4] & spi2[0x34 / 4]; }

  bool spi2_address(uint32_t address) const { return ethernet_enabled && address >= kSpi2 && address < kSpi2 + 256; }

  bool read(uint32_t address, uint32_t& result) {
    const bool ethernet = spi2_address(address);
    if (ethernet || (address >= kSpi && address < kSpi + 256)) {
      auto& regs = ethernet ? spi2 : spi;
      require(address % 4 == 0, "Unaligned SPI register access");
      ++spi_reads;
      const uint32_t offset = address - (ethernet ? kSpi2 : kSpi);
      if (offset == 0x40)
        result = regs[0x3c / 4] & regs[0x34 / 4];
      else if (offset == 0x38 || offset == 0x44)
        result = 0;
      else
        result = regs[offset / 4];
      return true;
    }
    if (address >= kGdma && address < kGdma + 5 * 0xc0) {
      require(address % 4 == 0, "Unaligned GDMA register access");
      const auto relative = address - kGdma;
      const auto part = relative % 0xc0;
      auto& direction = dma[relative / 0xc0][part >= 0x60];
      if (owned(direction)) {
        result = direction.read(part % 0x60, part < 0x60);
        return true;
      }
    }
    return false;
  }

  WriteAction write(uint32_t address, uint32_t value) {
    const bool ethernet = spi2_address(address);
    if (ethernet || (address >= kSpi && address < kSpi + 256)) {
      auto& regs = ethernet ? spi2 : spi;
      require(address % 4 == 0, "Unaligned SPI register access");
      ++spi_writes;
      const auto offset = address - (ethernet ? kSpi2 : kSpi);
      if (offset == 0x38 || offset == 0x3c)
        regs[0x3c / 4] &= ~value;
      else if (offset == 0x44)
        regs[0x3c / 4] |= value;
      else if (offset == 0) {
        regs[0] = value & ~(1u << 23);
        if (value & (1u << 24)) {
          regs[0x3c / 4] &= ~kDone;
          transfer(regs, ethernet);
          regs[0] &= ~(1u << 24);
        }
      } else
        regs[offset / 4] = value;
      return WriteAction::kIntercept;
    }
    if (address >= kGdma && address < kGdma + 5 * 0xc0) {
      require(address % 4 == 0, "Unaligned GDMA register access");
      const auto relative = address - kGdma;
      const auto part = relative % 0xc0;
      auto& direction = dma[relative / 0xc0][part >= 0x60];
      const bool was_owned = owned(direction);
      if (part % 0x60 == 72 || was_owned)
        direction.write(part % 0x60, value, part < 0x60);
      else {
        const auto offset = part % 0x60;
        direction.regs[offset / 4] = value;
        if (offset == 16) direction.enable = value;
      }
      // Peripheral selection must also reach the original GDMA model, so a
      // channel can later be reused by another native peripheral.
      return (part % 0x60 != 72 && (was_owned || owned(direction))) ? WriteAction::kIntercept : WriteAction::kPass;
    }
    if (address == kGpio + 8 && (value & (1u << 5))) rtd.select(false);
    if (address == kGpio + 12 && (value & (1u << 5))) rtd.select(true);
    if (address == kGpio + 4) rtd.select(!(value & (1u << 5)));
    return WriteAction::kPass;
  }

 private:
  bool owned(const Direction& direction) const {
    return direction.peripheral == 1 || (ethernet_enabled && direction.peripheral == 0);
  }

  Direction& channel(bool receive, uint32_t peripheral) {
    Direction* found = nullptr;
    for (auto& pair : dma) {
      auto& direction = pair[receive ? 0 : 1];
      if (direction.peripheral == peripheral && direction.pending) {
        require(found == nullptr, "Multiple active DMA channels for SPI direction");
        found = &direction;
      }
    }
    require(found != nullptr, "SPI DMA enabled without a submitted descriptor");
    return *found;
  }

  std::vector<Descriptor> chain(Direction& direction, uint32_t count, bool receive) {
    require(direction.pending && direction.running, "Inactive DMA channel");
    std::vector<Descriptor> descriptors;
    uint32_t address = direction.descriptor;
    while (count) {
      require(address % 4 == 0 && descriptors.size() < 16, "Unaligned/excessive DMA descriptor chain");
      for (const auto& prior : descriptors) require(prior.address != address, "Cyclic DMA descriptor chain");
      dram(address, 12);
      const uint32_t flags = memory.read32(address);
      const uint32_t buffer = memory.read32(address + 4);
      const uint32_t next = memory.read32(address + 8);
      dma_reads += 3;
      const uint32_t size = flags & 4095;
      const uint32_t length = (flags >> 12) & 4095;
      // GDMA writes RX length on completion. ESP-IDF leaves its previous
      // value in place when a descriptor is reused for a shorter read.
      require((flags & (1u << 31)) && size && size <= 4092 && (receive || length <= size),
              "Invalid DMA ownership/size");
      const uint32_t available = receive ? size : length;
      require(available > 0, "Empty DMA descriptor");
      const uint32_t amount = count < available ? count : available;
      dram(buffer, amount);
      descriptors.push_back({address, flags, buffer, amount});
      count -= amount;
      require(!count || next != 0, "Short DMA descriptor chain");
      address = next;
    }
    return descriptors;
  }

  void checked32(uint32_t address, uint32_t value) {
    memory.write32(address, value);
    require(memory.read32(address) == value, "DMA metadata write did not persist");
    ++dma_writes;
  }

  void transfer(std::array<uint32_t, 64>& regs, bool ethernet) {
    const uint32_t user = regs[0x10 / 4];
    const uint32_t bits = (regs[0x1c / 4] & 0x3ffff) + 1;
    require(bits % 8 == 0 && bits <= 32768, "Unsupported SPI transfer bit length");
    if (ethernet) {
      require((user & 0xe0000000) == 0xc0000000 && (regs[0x18 / 4] >> 28) == 15 && (regs[0x14 / 4] >> 27) == 7,
              "Unsupported W5500 SPI header");
    } else {
      require(!(user & 0xe0000000), "SPI command/address/dummy phases are not qualified");
    }
    const uint32_t count = bits / 8;
    const uint8_t control = regs[0x04 / 4] >> 24;
    const bool mosi = ethernet ? (control & 4) != 0 : (user & (1u << 27)) != 0;
    const bool miso = ethernet ? !mosi : (user & (1u << 28)) != 0;
    const uint32_t peripheral = ethernet ? 0 : 1;
    const bool tx_dma = mosi && (regs[0x30 / 4] & (1u << 28));
    const bool rx_dma = miso && (regs[0x30 / 4] & (1u << 27));
    Direction* tx_channel = tx_dma ? &channel(false, peripheral) : nullptr;
    Direction* rx_channel = rx_dma ? &channel(true, peripheral) : nullptr;
    const auto tx_descriptors = tx_dma ? chain(*tx_channel, count, false) : std::vector<Descriptor>{};
    const auto rx_descriptors = rx_dma ? chain(*rx_channel, count, true) : std::vector<Descriptor>{};
    std::vector<uint8_t> tx(count, 0), rx(count, 0);
    uint32_t offset = 0;
    if (tx_dma) {
      for (const auto& descriptor : tx_descriptors) {
        for (uint32_t i = 0; i < descriptor.count; ++i) {
          tx[offset++] = memory.read8(descriptor.buffer + i);
          ++dma_reads;
        }
      }
    } else if (mosi) {
      const uint32_t base = (user & (1u << 25)) ? 32 : 0;
      require(base + count <= 64, "SPI FIFO overflow");
      for (uint32_t i = 0; i < count; ++i) tx[i] = (regs[(0x98 + base + i) / 4] >> (((base + i) % 4) * 8)) & 255;
    }
    auto candidate = rtd;
    bool rtd_sample_read = false;
    if (ethernet) {
      const uint16_t command = regs[0x18 / 4] & 0xffff;
      const uint16_t address = (command << 8) | (command >> 8);
      for (uint32_t i = 0; i < count; ++i) {
        if (mosi)
          w5500.write(control >> 3, address + i, tx[i]);
        else
          rx[i] = w5500.read(control >> 3, address + i);
      }
    } else {
      const bool has_command = candidate.address < 0;
      const bool reads_registers = has_command ? !(tx.front() & 0x80) : !candidate.writing;
      const int first_register = has_command ? tx.front() & 0x7f : candidate.address;
      // Completing data register 2 marks a sample even when the command and
      // the two data bytes travel in separate SPI transfers under one CS.
      rtd_sample_read = miso && reads_registers && first_register <= 2 &&
                        count > static_cast<uint32_t>(2 - first_register + has_command);
      for (uint32_t i = 0; i < count; ++i) rx[i] = candidate.exchange(tx[i]);
    }
    offset = 0;
    for (const auto& descriptor : rx_descriptors) {
      for (uint32_t i = 0; i < descriptor.count; ++i) {
        memory.write8(descriptor.buffer + i, rx[offset++]);
        require(memory.read8(descriptor.buffer + i) == rx[offset - 1], "DMA receive byte did not persist");
        ++dma_writes;
      }
    }
    if (miso && !rx_dma) {
      const uint32_t base = (user & (1u << 24)) ? 32 : 0;
      require(base + count <= 64, "SPI receive FIFO overflow");
      for (uint32_t i = 0; i < count; ++i) {
        auto& word = regs[(0x98 + base + i) / 4];
        const auto shift = ((base + i) % 4) * 8;
        word = (word & ~(255u << shift)) | (uint32_t(rx[i]) << shift);
      }
    }
    for (const auto& descriptor : tx_descriptors) checked32(descriptor.address, descriptor.flags & ~(1u << 31));
    for (size_t i = 0; i < rx_descriptors.size(); ++i) {
      const auto& descriptor = rx_descriptors[i];
      uint32_t flags = (descriptor.flags & ~((1u << 31) | (4095u << 12) | (1u << 30))) | (descriptor.count << 12);
      if (i + 1 == rx_descriptors.size()) flags |= 1u << 30;
      checked32(descriptor.address, flags);
    }
    if (tx_channel) {
      tx_channel->pending = tx_channel->running = false;
      tx_channel->raw |= 11;
    }
    if (rx_channel) {
      rx_channel->pending = rx_channel->running = false;
      rx_channel->raw |= 3;
    }
    rtd = candidate;
    regs[0x3c / 4] |= kDone;
    ++transactions;
    if (!ethernet && event) event({tx, rx, transactions, rtd_sample_read});
  }
};
}  // namespace oq_native
