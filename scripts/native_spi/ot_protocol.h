#pragma once

// Adapted from OpenQuatt scripts/simulator/io/virtual_opentherm/protocol.h.

#include <array>
#include <cmath>
#include <cstddef>
#include <cstdint>

namespace esphome::virtual_opentherm::protocol {

// Independent wire framing. Do not include the production OT encoder/decoder.
struct Run {
  uint16_t duration_us;
  bool level;
};

constexpr bool even_parity(uint32_t word) {
  bool parity = false;
  for (size_t i = 0; i < 32; ++i) parity = parity != ((word >> i) & 1U);
  return !parity;
}

constexpr uint32_t frame(uint8_t type, uint8_t id, uint16_t value) {
  uint32_t word = (static_cast<uint32_t>(type & 7U) << 28U) | (static_cast<uint32_t>(id) << 16U) | value;
  return even_parity(word) ? word : word | 0x80000000U;
}

static_assert(frame(4, 25, 0x2A80) == 0x40192A80U);
static_assert(frame(1, 1, 0x2D80) == 0x90012D80U);
static_assert(!even_parity(0xC0192A80U));

struct Request {
  uint32_t word{0};
  uint8_t type{0};
  uint8_t id{0};
  uint16_t value{0};
};

// Production TX has exact 500 us Manchester halves and active-low polarity.
// RX stops at the stop bit's mandatory middle edge; its final idle half is absent.
inline bool decode_controller(const Run* runs, size_t count, Request& result, bool master = true) {
  if (runs == nullptr || count == 0 || count > 96) return false;
  std::array<bool, 68> halves{};
  size_t used = 0;
  for (size_t i = 0; i < count; ++i) {
    const auto& run = runs[i];
    if (run.duration_us == 0) continue;
    if (run.duration_us % 500 != 0 || run.duration_us > 1000) return false;
    for (size_t half = 0; half < run.duration_us / 500; ++half) {
      if (used == halves.size()) return false;
      halves[used++] = run.level;
    }
  }
  if ((used != 67 && used != 68) || halves[0] || !halves[1] || halves[66] || (used == 68 && !halves[67])) return false;
  uint32_t word = 0;
  for (size_t bit = 1; bit <= 32; ++bit) {
    if (halves[bit * 2] == halves[bit * 2 + 1]) return false;
    word = (word << 1U) | static_cast<uint32_t>(!halves[bit * 2]);
  }
  if (!even_parity(word) || (word & 0x0F000000U) != 0) return false;
  result = {word, static_cast<uint8_t>((word >> 28U) & 7U), static_cast<uint8_t>(word >> 16U),
            static_cast<uint16_t>(word)};
  return master ? (result.type == 0 || result.type == 1) : result.type >= 4;
}

inline uint16_t f88(float value) { return static_cast<uint16_t>(static_cast<int16_t>(std::lround(value * 256.0f))); }
inline float decode_f88(uint16_t value) { return static_cast<float>(static_cast<int16_t>(value)) / 256.0f; }

struct Boiler {
  float target_c{0.0f};
  float maximum_modulation{100.0f};
  bool ch_enabled{false};

  void withdraw() {
    target_c = 0.0f;
    ch_enabled = false;
  }

  // Bounded synthetic appliance fixture, not a physical boiler thermal model.
  // Unsupported Data-IDs return UNKNOWN_DATAID, never invented telemetry.
  uint32_t respond(const Request& request) {
    const uint8_t id = request.id;
    if (request.type != 0 && request.type != 1) return frame(6, id, 0);
    if (id == 0 && request.type == 0) {
      ch_enabled = (request.value & 0x0100U) != 0;
      const bool heating = ch_enabled && target_c > 0.0f;
      return frame(4, 0, static_cast<uint16_t>((request.value & 0xFF00U) | (heating ? 0x000AU : 0U)));
    }
    if (request.type == 1) {
      const float value = decode_f88(request.value);
      if (id == 1) {
        if (!std::isfinite(value) || value < 0.0f || value > 60.0f) return frame(6, id, 0);
        target_c = value;
        return frame(5, id, request.value);
      }
      if (id == 14) {
        if (!std::isfinite(value) || value < 0.0f || value > 100.0f) return frame(6, id, 0);
        maximum_modulation = value;
        return frame(5, id, request.value);
      }
      if (id == 2 || id == 124 || id == 126) return frame(5, id, request.value);
      return frame(7, id, 0);
    }
    uint16_t value = 0;
    switch (id) {
      case 1:
        value = f88(target_c);
        break;
      case 3:
        value = 0x0101;
        break;  // Synthetic DHW-present configuration, member 1.
      case 5:
        value = 0;
        break;
      case 14:
        value = f88(maximum_modulation);
        break;
      case 15:
        value = 0x1414;
        break;  // 20 kW maximum, 20% minimum modulation.
      case 17:
        value = f88(ch_enabled && target_c > 0.0f ? 30.0f : 0.0f);
        break;
      case 18:
        value = f88(1.5f);
        break;
      case 25:
        value = f88(42.5f);
        break;
      case 26:
        value = f88(48.0f);
        break;
      case 28:
        value = f88(35.0f);
        break;
      case 48:
        value = 0x3C23;
        break;  // DHW bounds 35..60 C.
      case 49:
        value = 0x3C14;
        break;  // CH bounds 20..60 C.
      case 56:
        value = f88(50.0f);
        break;
      case 57:
        value = f88(60.0f);
        break;
      case 115:
        value = 0;
        break;
      case 125:
        value = f88(2.2f);
        break;
      case 127:
        value = 0x0101;
        break;
      default:
        return frame(7, id, 0);
    }
    return frame(4, id, value);
  }
};

inline bool self_test() {
  Boiler boiler;
  const Request target{frame(1, 1, 0x2800), 1, 1, 0x2800};
  if (boiler.respond(target) != frame(5, 1, 0x2800) || boiler.target_c != 40.0f) return false;
  const Request status{frame(0, 0, 0x0100), 0, 0, 0x0100};
  if (boiler.respond(status) != frame(4, 0, 0x010A)) return false;
  const Request invalid_target{frame(1, 1, 0x4600), 1, 1, 0x4600};
  if (boiler.respond(invalid_target) != frame(6, 1, 0) || boiler.target_c != 40.0f) return false;
  const Request unknown{frame(0, 119, 0), 0, 119, 0};
  if (boiler.respond(unknown) != frame(7, 119, 0)) return false;
  boiler.withdraw();
  return !boiler.ch_enabled && boiler.target_c == 0.0f && frame(4, 25, f88(42.5f)) == 0x40192A80U;
}

}  // namespace esphome::virtual_opentherm::protocol
