#include "device_model.h"
#include "ot_protocol.h"

#include <cstdio>
#include <map>
#include <stdexcept>

using namespace oq_native;
struct FakeMemory : Memory {
  std::map<uint32_t, uint8_t> bytes;
  int writes = 0;
  int fail_after = -1;
  uint8_t read8(uint32_t address) override { return bytes[address]; }
  uint32_t read32(uint32_t address) override {
    uint32_t value = 0;
    for (int i = 0; i < 4; ++i) value |= uint32_t(read8(address + i)) << (8 * i);
    return value;
  }
  void write8(uint32_t address, uint8_t value) override {
    if (fail_after >= 0 && writes >= fail_after) throw std::runtime_error("injected write failure");
    ++writes;
    bytes[address] = value;
  }
  void write32(uint32_t address, uint32_t value) override {
    for (int i = 0; i < 4; ++i) write8(address + i, value >> (8 * i));
  }
  void seed(uint32_t address, uint32_t value) {
    for (int i = 0; i < 4; ++i) bytes[address + i] = value >> (8 * i);
  }
};

void check(bool ok) {
  if (!ok) throw std::runtime_error("assertion failed");
}
template <typename F>
void rejects(F operation) {
  bool rejected = false;
  try {
    operation();
  } catch (const std::exception&) {
    rejected = true;
  }
  check(rejected);
}
void select(DeviceModel& d, bool active) { d.write(kGpio + (active ? 12 : 8), 1u << 5); }
void fifo(DeviceModel& d, uint8_t byte, bool receive = true) {
  d.write(kSpi + 0x10, (1u << 27) | (receive ? 1u << 28 : 0) | 1);
  d.write(kSpi + 0x1c, 7);
  d.write(kSpi + 0x98, byte);
  d.write(kSpi, 1u << 24);
}
uint32_t reg(DeviceModel& d, uint32_t address) {
  uint32_t value;
  check(d.read(address, value));
  return value;
}
void submit(DeviceModel& d, FakeMemory& memory, bool receive, uint32_t descriptor, uint32_t buffer, uint32_t count,
            int channel = 0, uint32_t next = 0) {
  memory.seed(descriptor, (1u << 31) | (1u << 30) | 4 | (receive ? 0 : count << 12));
  memory.seed(descriptor + 4, buffer);
  memory.seed(descriptor + 8, next);
  const auto base = kGdma + channel * 0xc0 + (receive ? 0 : 0x60);
  d.write(base + 0x48, 1);
  d.write(base + 0x20, (descriptor & 0xfffff) | (1u << (receive ? 22 : 21)));
}
void dma_transfer(DeviceModel& d, bool receive) {
  d.write(kSpi + 0x10, (1u << 27) | (receive ? 1u << 28 : 0) | 1);
  d.write(kSpi + 0x30, (1u << 28) | (receive ? 1u << 27 : 0));
  d.write(kSpi + 0x1c, 7);
  d.write(kSpi, 1u << 24);
}

int main() {
  int cases = 0;
  try {
    {  // Real config command, W1C and repeated CS level.
      FakeMemory memory;
      DeviceModel d(memory);
      select(d, true);
      fifo(d, 0x80);
      select(d, true);
      fifo(d, 0x87);
      check(d.rtd.config == 0x81 && d.transactions == 2 && reg(d, kSpi + 0x3c) == kDone);
      d.write(kSpi + 0x38, kDone);
      check(reg(d, kSpi + 0x3c) == 0);
      ++cases;
    }
    {  // PT1000 sample and address sequencing.
      Max31865 rtd;
      rtd.select(true);
      rtd.exchange(1);
      const uint16_t raw = uint16_t(rtd.exchange(0)) << 8 | rtd.exchange(0);
      check(std::abs((raw >> 1) * 1500.0 / 32768.0 - 1116.73025) < .03);
      rtd.select(false);
      rejects([&] { rtd.exchange(0); });
      ++cases;
    }
    {  // Report the fixture latched for completed MAX31865 sample reads.
      FakeMemory memory;
      DeviceModel d(memory);
      int samples = 0;
      d.event = [&](const Event& value) { samples += value.rtd_sample_read; };
      d.rtd.temperature = 34;
      select(d, true);
      fifo(d, 1, false);
      d.rtd.temperature = 38;
      fifo(d, 0);
      check(samples == 0);
      fifo(d, 0);
      check(samples == 1 && d.rtd.sampled_temperature == 34 && d.rtd.sample_valid);
      select(d, false);
      d.rtd.temperature_valid = false;
      select(d, true);
      d.write(kSpi + 0x10, (1u << 27) | (1u << 28) | 1);
      d.write(kSpi + 0x1c, 23);
      d.write(kSpi + 0x98, 1);
      d.write(kSpi, 1u << 24);
      check(samples == 2 && d.rtd.sampled_temperature == 38 && !d.rtd.sample_valid);
      ++cases;
    }
    {  // Native GDMA transmit and receive, preserving neighbouring bytes.
      FakeMemory memory;
      DeviceModel d(memory);
      const uint32_t tx = 0x3fca0000, rx = tx + 16, tx_buffer = tx + 64, rx_buffer = tx + 80;
      select(d, true);
      memory.seed(tx_buffer, 0x80);
      submit(d, memory, false, tx, tx_buffer, 1);
      dma_transfer(d, false);
      check(!(memory.read32(tx) & (1u << 31)) && reg(d, kGdma + 0x68) == 11);
      memory.seed(tx_buffer, 0x03);
      submit(d, memory, false, tx, tx_buffer, 1);
      dma_transfer(d, false);
      check(d.rtd.config == 1);
      select(d, false);
      select(d, true);
      memory.seed(tx_buffer, 0);
      submit(d, memory, false, tx, tx_buffer, 1);
      dma_transfer(d, false);
      memory.seed(tx_buffer, 0);
      memory.seed(rx_buffer, 0xaabbcc00);
      submit(d, memory, false, tx, tx_buffer, 1);
      submit(d, memory, true, rx, rx_buffer, 1);
      // Reused RX descriptor: the previous transfer's output length exceeds
      // the new buffer size. The controller must overwrite that length.
      memory.seed(rx, memory.read32(rx) | (590u << 12));
      dma_transfer(d, true);
      check(memory.read32(rx_buffer) == 0xaabbcc01);
      check(((memory.read32(rx) >> 12) & 4095) == 1 && !(memory.read32(rx) & (1u << 31)));
      check(reg(d, kGdma + 8) == 3 && reg(d, kSpi + 0x3c) == kDone);
      ++cases;
    }
    for (uint32_t bad : {0x42000000u, 0x3fc87fffu, 0x3fd00000u}) {
      FakeMemory memory;
      DeviceModel d(memory);
      select(d, true);
      submit(d, memory, false, 0x3fca0000, bad, 1);
      const int before = memory.writes;
      rejects([&] { dma_transfer(d, false); });
      check(memory.writes == before && reg(d, kSpi + 0x3c) == 0);
      ++cases;
    }
    {  // Reject cyclic descriptors before modifying RAM.
      FakeMemory memory;
      DeviceModel d(memory);
      select(d, true);
      submit(d, memory, false, 0x3fca0000, 0x3fca0040, 1, 0, 0x3fca0000);
      d.write(kSpi + 0x10, 1u << 27);
      d.write(kSpi + 0x30, 1u << 28);
      d.write(kSpi + 0x1c, 39);
      rejects([&] { d.write(kSpi, 1u << 24); });
      check(memory.writes == 0);
      ++cases;
    }
    {  // Multiple submissions must not select an arbitrary channel.
      FakeMemory memory;
      DeviceModel d(memory);
      select(d, true);
      submit(d, memory, false, 0x3fca0000, 0x3fca0040, 1, 0);
      submit(d, memory, false, 0x3fca0010, 0x3fca0050, 1, 1);
      rejects([&] { dma_transfer(d, false); });
      check(memory.writes == 0);
      ++cases;
    }
    {  // Non-SPI3 GDMA and channel reuse retain native handling.
      FakeMemory memory;
      DeviceModel d(memory);
      check(d.write(kGdma + 0xa8, 6) == WriteAction::kPass);
      check(d.write(kGdma + 0x80, 1u << 22) == WriteAction::kPass);
      uint32_t result;
      check(!d.read(kGdma + 0xa8, result));
      check(d.write(kGdma + 0xa8, 1) == WriteAction::kPass);
      check(d.read(kGdma + 0xa8, result) && result == 1);
      check(d.write(kGdma + 0xa8, 0) == WriteAction::kPass);
      check(!d.read(kGdma + 0xa8, result));
      ++cases;
    }
    for (int failure = 0; failure < 4; ++failure) {
      FakeMemory memory;
      DeviceModel d(memory);
      select(d, true);
      submit(d, memory, false, 0x3fca0000, 0x3fca0040, 1);
      memory.seed(0x3fca0040, 0x80);
      memory.fail_after = failure;
      rejects([&] { dma_transfer(d, false); });
      check(reg(d, kSpi + 0x3c) == 0 && d.transactions == 0);
      ++cases;
    }
    {  // Ownership and reset boundaries.
      FakeMemory memory;
      DeviceModel d(memory);
      select(d, true);
      submit(d, memory, false, 0x3fca0000, 0x3fca0040, 1);
      d.write(kGdma + 0x60, 1);
      rejects([&] { dma_transfer(d, false); });
      check(memory.writes == 0);
      rejects([&] { d.write(kSpi + 1, 0); });
      ++cases;
    }
    {  // A real two-descriptor transfer must preserve chain pointers and EOF boundaries.
      FakeMemory memory;
      DeviceModel d(memory);
      select(d, true);
      fifo(d, 1, false);
      const uint32_t tx = 0x3fca0000, tx2 = tx + 16, rx = tx + 32, rx2 = tx + 48;
      const uint32_t tx_buffer = tx + 128, rx_buffer = tx + 160;
      submit(d, memory, false, tx, tx_buffer, 1, 0, tx2);
      memory.seed(tx, (1u << 31) | 4 | (1u << 12));
      submit(d, memory, false, tx2, tx_buffer + 4, 1, 0);
      submit(d, memory, true, rx, rx_buffer, 1, 0, rx2);
      memory.seed(rx, (1u << 31) | 1);
      submit(d, memory, true, rx2, rx_buffer + 4, 1, 0);
      memory.seed(rx2, (1u << 31) | 1);
      d.write(kGdma + 0x80, (tx & 0xfffff) | (1u << 21));
      d.write(kGdma + 0x20, (rx & 0xfffff) | (1u << 22));
      memory.seed(rx_buffer, 0xaabbcc00);
      memory.seed(rx_buffer + 4, 0xddeeff00);
      d.write(kSpi + 0x10, (1u << 27) | (1u << 28) | 1);
      d.write(kSpi + 0x30, (1u << 28) | (1u << 27));
      d.write(kSpi + 0x1c, 15);
      d.write(kSpi, 1u << 24);
      const auto sample = (uint16_t(memory.read8(rx_buffer)) << 8) | memory.read8(rx_buffer + 4);
      check(sample == d.rtd.sample && sample != 0);
      check(memory.read32(tx + 8) == tx2 && memory.read32(rx + 8) == rx2);
      check(memory.read32(rx) == (1u << 12 | 1));
      check(memory.read32(rx2) == (1u << 30 | 1u << 12 | 1));
      check((memory.read32(rx_buffer) & 0xffffff00) == 0xaabbcc00);
      check((memory.read32(rx_buffer + 4) & 0xffffff00) == 0xddeeff00);
      check(reg(d, kSpi + 0x3c) == kDone && d.transactions == 2);
      ++cases;
    }
    for (int failure = 0; failure < 9; ++failure) {
      FakeMemory memory;
      DeviceModel d(memory);
      select(d, true);
      fifo(d, 0, false);
      d.rtd.config = 0x81;
      submit(d, memory, false, 0x3fca0000, 0x3fca0040, 1);
      submit(d, memory, true, 0x3fca0010, 0x3fca0050, 1);
      memory.writes = 0;
      memory.fail_after = failure;
      rejects([&] { dma_transfer(d, true); });
      check(reg(d, kSpi + 0x3c) == 0 && d.transactions == 1 && d.rtd.address == 0);
      ++cases;
    }
    {  // MACRAW packet boundaries survive the 16-bit pointer wrap.
      W5500 ethernet;
      ethernet.cable = true;
      ethernet.write(1, 0x1e, 3);
      check(ethernet.read(1, 0x1e) == 2);
      ethernet.write(1, 0, 4);
      ethernet.write(1, 1, 1);
      ethernet.write(1, 0x1e, 16);
      check(ethernet.read(1, 0x1e) == 2 && ethernet.read(1, 4) == 0x42);
      ethernet.write(0, 0x18, 1);
      ethernet.write(1, 0x2c, 4);
      std::array<uint8_t, 128> frame{};
      uint16_t pointer = 0;
      for (unsigned sequence = 0; sequence < 520; ++sequence) {
        frame[0] = sequence & 255;
        frame.back() = (sequence + 37) & 255;
        check(ethernet.receive(frame.data(), frame.size()));
        check(ethernet.interrupt() && ethernet.read(1, 0x27) == 130);
        check(ethernet.read(3, pointer) == 0 && ethernet.read(3, pointer + 1) == 130);
        check(ethernet.read(3, pointer + 2) == frame[0] && ethernet.read(3, pointer + 129) == frame.back());
        pointer += 130;
        ethernet.write(1, 0x28, pointer >> 8);
        ethernet.write(1, 0x29, pointer & 255);
        ethernet.write(1, 1, 0x40);
        ethernet.write(1, 2, 4);
        check(!ethernet.interrupt() && ethernet.read(1, 0x26) == 0 && ethernet.read(1, 0x27) == 0);
      }
      ethernet.cable = false;
      check(!ethernet.link() && !ethernet.receive(frame.data(), frame.size()));
      ++cases;
    }
    {  // Reset, close/reopen and PHY power-down invalidate forwarded TCP state.
      W5500 ethernet;
      unsigned resets = 0;
      ethernet.on_interface_reset = [&] { ++resets; };
      ethernet.cable = true;
      ethernet.write(1, 0, 4);
      ethernet.write(1, 1, 0x01);
      check(resets == 1 && ethernet.link());
      ethernet.write(1, 1, 0x10);
      check(resets == 2);
      ethernet.write(0, 0x2e, 0xf0);
      check(resets == 3 && !ethernet.link());
      ethernet.write(0, 0x2e, 0xf0);
      ethernet.write(0, 0x2e, 0xb8);
      check(resets == 3 && ethernet.link());
      ethernet.write(0, 0, 0x80);
      check(resets == 4);
      ++cases;
    }
    {
      namespace ot = esphome::virtual_opentherm::protocol;
      check(ot::self_test());
      std::array<ot::Run, 68> runs{};
      const uint32_t answer = 0x40192a80;
      for (unsigned bit = 0; bit < 34; ++bit) {
        const bool value = bit == 0 || bit == 33 || (answer & (1u << (32u - bit)));
        runs[bit * 2] = {500, !value};
        runs[bit * 2 + 1] = {500, value};
      }
      ot::Request frame;
      check(ot::decode_controller(runs.data(), runs.size(), frame, false) && frame.word == answer);
      check(!ot::decode_controller(runs.data(), runs.size(), frame));
      runs[2].level = !runs[2].level;
      runs[3].level = !runs[3].level;
      check(!ot::decode_controller(runs.data(), runs.size(), frame, false));
      ++cases;
    }
    std::printf("PASS %d native peripheral boundary cases\n", cases);
    return 0;
  } catch (const std::exception& error) {
    std::fprintf(stderr, "FAIL after %d cases: %s\n", cases, error.what());
    return 1;
  }
}
