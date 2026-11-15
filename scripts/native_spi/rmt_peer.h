#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <stdexcept>
#include "ot_protocol.h"

namespace oq_native {
namespace ot = esphome::virtual_opentherm::protocol;

class RmtPeers {
 public:
  using Read = uint32_t (*)(void*, uint32_t);
  using Grow = void (*)(void*);

  RmtPeers(Read read, Grow grow, FILE* trace, const char* thermostat)
      : read_(read), grow_(grow), trace_(trace), thermostat_(thermostat) {}

  static bool is_tx_start(uint32_t address, uint32_t value) {
    return address >= kRmt + 0x20 && address <= kRmt + 0x2c && (address & 3u) == 0 && (value & 1u);
  }

  static bool matrix_address(uint32_t address) {
    return address == kMatrixOut + 48 * 4 || address == kMatrixOut + 14 * 4 ||
           (address >= kMatrixIn && address < kMatrixIn + 16 && (address & 3u) == 0);
  }

  void on_matrix(uint32_t address, uint32_t value) {
    if (address == kMatrixOut + 48 * 4) {
      boiler_out_ = value & 0x1ffu;
    } else if (address == kMatrixOut + 14 * 4) {
      thermostat_out_ = value & 0x1ffu;
    } else {
      input_routes_[(address - kMatrixIn) / 4] = value;
    }
  }

  void on_tx(void* bus, uint32_t address, uint64_t now) {
    const unsigned channel = (address - (kRmt + 0x20)) / 4;
    const bool boiler = boiler_out_ == kRmtSignal + channel;
    if (!boiler && thermostat_out_ != kRmtSignal + channel) return;
    // Both production encoders start with a 500 us active-low start bit.
    // The driver's short idle-settle transmission is outside this protocol.
    constexpr uint32_t start_symbol = (0x8000u | 500u) << 16u | 500u;
    if (read_(bus, kRmtMem + channel * 48 * 4) != start_symbol) return;
    std::array<ot::Run, 68> runs{};
    for (unsigned i = 0; i < 34; ++i) {
      const uint32_t symbol = read_(bus, kRmtMem + (channel * 48 + i) * 4);
      runs[i * 2] = {static_cast<uint16_t>(symbol & 0x7fffu), (symbol & 0x8000u) != 0};
      runs[i * 2 + 1] = {static_cast<uint16_t>((symbol >> 16u) & 0x7fffu), (symbol >> 31u) != 0};
    }
    ot::Request decoded;
    if (!ot::decode_controller(runs.data(), runs.size(), decoded, boiler)) {
      event("invalid-tx", boiler ? "otb" : "ott", channel, 0, now);
      return;
    }
    event("controller-tx", boiler ? "otb" : "ott", channel, decoded.word, now);
    if (boiler && !std::getenv("OQ_NATIVE_OTB_DISCONNECTED")) {
      if (decoded.id == 25) {
        if (const char* path = std::getenv("OQ_NATIVE_OTB_WATER_CONTROL")) {
          if (FILE* file = std::fopen(path, "r")) {
            float next;
            if (std::fscanf(file, "%f", &next) == 1 && std::isfinite(next) && next >= 0 && next <= 80)
              boiler_.boiler_water_c = next;
            std::fclose(file);
          }
        }
      }
      boiler_answer_ = boiler_.respond(decoded);
      boiler_due_ = now + 110000;  // 34 ms TX followed by 76 ms response delay.
      boiler_expires_ = now + 400000;
    }
  }

  void service(void* bus, uint64_t now) {
    if (now < last_sample_) {
      boiler_.withdraw();
      boiler_due_ = thermostat_due_ = 0;
      thermostat_index_ = 0;
    }
    if (now == last_sample_) return;
    last_sample_ = now;
    if (boiler_due_ && now >= boiler_due_) {
      if (now >= boiler_expires_) {
        boiler_due_ = 0;
      } else if (inject(bus, 47, boiler_answer_, "otb", now)) {
        boiler_due_ = 0;
      }
    }
    if (now >= thermostat_due_) {
      const int thermostat_state = read_thermostat();
      if (thermostat_state == 'x') {
        thermostat_due_ = now + 100000;
        return;
      }
      const std::array<uint32_t, 4> frames = {
          ot::frame(0, 0, thermostat_state == '1' ? 0x0100u : 0u),
          ot::frame(1, 1, ot::f88(40.0f)),
          ot::frame(1, 16, ot::f88(thermostat_input("OQ_NATIVE_OTT_SETPOINT", 21.0f))),
          ot::frame(1, 24, ot::f88(thermostat_input("OQ_NATIVE_OTT_ROOM", 19.0f))),
      };
      if (inject(bus, 21, frames[thermostat_index_], "ott", now)) {
        thermostat_index_ = (thermostat_index_ + 1) % frames.size();
        thermostat_due_ = now + 1000000;
      } else {
        thermostat_due_ = now + 50000;
      }
    }
  }

 private:
  static constexpr uint32_t kRmt = 0x60016000;
  static constexpr uint32_t kRmtMem = 0x60016800;
  static constexpr uint32_t kMatrixIn = 0x60004298;   // RMT_SIG_IN0_IDX (81)
  static constexpr uint32_t kMatrixOut = 0x60004554;  // GPIO_FUNC0_OUT_SEL_CFG
  static constexpr uint32_t kRmtSignal = 81;
  static constexpr size_t kNativeRmt = 0x60f0;
  static constexpr size_t kRxPointer = 0x38;
  static constexpr size_t kRxCount = 0x40;
  static constexpr size_t kRxStride = 0x68;
  static constexpr size_t kRxArmed = 0x5e;

  struct Pulse {
    uint64_t ticks;
    uint8_t level;
    uint8_t padding[7]{};
  };
  static_assert(sizeof(Pulse) == 16, "native RMT pulse stride");

  // The pinned 0.51.0 receive channel starts with a VecDeque<Pulse>.
  // Use esp-emu's own grow routine, so its normal tick and reset own the allocation.
  template <typename T>
  static T load(const uint8_t* bytes, size_t offset) {
    T value;
    std::memcpy(&value, bytes + offset, sizeof(value));
    return value;
  }

  static float thermostat_input(const char* key, float fallback) {
    const char* path = std::getenv(key);
    if (!path) return fallback;
    if (FILE* file = std::fopen(path, "r")) {
      float next;
      if (std::fscanf(file, "%f", &next) == 1 && std::isfinite(next) && next >= 0 && next <= 40)
        fallback = next;
      std::fclose(file);
    }
    return fallback;
  }

  int read_thermostat() const {
    FILE* file = std::fopen(thermostat_, "r");
    if (!file) return EOF;
    const int value = std::fgetc(file);
    std::fclose(file);
    return value;
  }

  bool inject(void* bus, unsigned pin, uint32_t word, const char* link, uint64_t now) {
    auto* rmt = static_cast<uint8_t*>(bus) + kNativeRmt;
    if (load<size_t>(rmt, kRxCount) != 4) return false;
    auto* channels = load<uint8_t*>(rmt, kRxPointer);
    if (!channels) return false;
    for (unsigned i = 0; i < 4; ++i) {
      const uint32_t config = read_(bus, kRmt + 0x30 + i * 8);
      const uint32_t route = input_routes_[i];
      auto* rx = channels + i * kRxStride;
      // Active-high from the HCQ front end, 1 MHz capture and at least 48 words.
      if ((route & 0xffu) != (0x80u | pin) || (config & 0xffu) != 80u || ((config >> 24u) & 0xfu) == 0 ||
          rx[kRxArmed] != 1 || load<size_t>(rx, 24) != 0)
        continue;

      std::array<Pulse, 68> pulses{};
      size_t count = 0;
      auto append = [&](bool level) {
        if (count && pulses[count - 1].level == level) {
          pulses[count - 1].ticks += 500;
        } else {
          pulses[count++] = {500, static_cast<uint8_t>(level)};
        }
      };
      for (unsigned bit = 0; bit < 34; ++bit) {
        const bool value = bit == 0 || bit == 33 || (word & (1u << (32u - bit)));
        append(value);
        append(!value);
      }
      for (size_t index = 0; index < count; ++index) {
        size_t capacity = load<size_t>(rx, 0);
        if (index == capacity) {
          grow_(rx);
          capacity = load<size_t>(rx, 0);
        }
        auto* queue = load<Pulse*>(rx, 8);
        const size_t head = load<size_t>(rx, 16);
        if (!queue || capacity <= index || head >= capacity)
          throw std::runtime_error("Unexpected native RMT receive queue layout");
        queue[(head + index) % capacity] = pulses[index];
        const size_t length = index + 1;
        std::memcpy(rx + 24, &length, sizeof(length));
      }
      event("peer-tx", link, i + 4, word, now);
      return true;
    }
    return false;
  }

  void event(const char* direction, const char* link, unsigned channel, uint32_t word, uint64_t now) {
    if (events_++ >= 32768) throw std::runtime_error("OpenTherm trace budget exceeded");
    const int written = std::fprintf(
        trace_,
        "{\"kind\":\"opentherm\",\"direction\":\"%s\",\"link\":\"%s\",\"channel\":%u,\"word\":%u,\"guest_us\":%llu}\n",
        direction, link, channel, word, static_cast<unsigned long long>(now));
    if (written <= 0 || std::fflush(trace_) != 0) throw std::runtime_error("OpenTherm trace write failed");
  }

  Read read_;
  Grow grow_;
  FILE* trace_;
  const char* thermostat_;
  ot::Boiler boiler_;
  uint32_t boiler_answer_ = 0;
  uint64_t boiler_due_ = 0;
  uint64_t boiler_expires_ = 0;
  uint64_t thermostat_due_ = 0;
  uint64_t last_sample_ = 0;
  std::array<uint32_t, 4> input_routes_{};
  uint32_t boiler_out_ = 0;
  uint32_t thermostat_out_ = 0;
  unsigned thermostat_index_ = 0;
  unsigned events_ = 0;
};
}  // namespace oq_native
