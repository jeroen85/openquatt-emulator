#include "odu_bridge.h"

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <limits>
#include <new>

#include "../vendor/odu/quatt_odu_simulator_model.h"

namespace {

namespace odu = esphome::quatt_odu_simulator;

struct Bridge {
  odu::QuattOduSimulatorModel model;
  uint32_t now_ms{0U};
  float hz20{0.0f}, hz26{0.0f}, response_ramp{20.0f};
};

bool valid_profile(int profile) { return profile >= 0 && profile <= 4; }
bool valid_flag(int value) { return value == 0 || value == 1; }
Bridge* get(void* handle) { return static_cast<Bridge*>(handle); }

bool advance(Bridge& bridge, uint32_t now_ms) {
  const uint32_t elapsed = now_ms - bridge.now_ms;
  // RFC-1982-style half-range ordering distinguishes a forward uint32 wrap
  // from a backward clock. The supplied clock is firmware time, never wall time.
  if (elapsed > static_cast<uint32_t>(std::numeric_limits<int32_t>::max())) return false;
  uint32_t remaining = elapsed;
  while (remaining != 0U) {
    const uint32_t step = std::min(remaining, 100U);
    bridge.now_ms += step;
    bridge.model.advance_communication_watchdog(bridge.now_ms);
    auto& before = bridge.model.mutable_state();
    auto& settings = bridge.model.mutable_settings();
    const bool frozen = settings.freeze_measured_frequency;
    const float command = before.requested_table_frequency_hz;
    const float response = command == 20.0f ? bridge.hz20 : command == 26.0f ? bridge.hz26 : 0.0f;
    if (response > 0.0f && before.active_mode == odu::WorkingMode::HEATING &&
        before.requested_mode == odu::WorkingMode::HEATING && before.accepted_physical_level > 0U &&
        !before.defrost_active()) {
      const float change = bridge.response_ramp * static_cast<float>(step) / 1000.0f;
      before.measured_frequency_hz += std::clamp(response - before.measured_frequency_hz, -change, change);
      settings.freeze_measured_frequency = true;
    }
    bridge.model.update(static_cast<float>(step) / 1000.0f);
    settings.freeze_measured_frequency = frozen;
    auto& state = bridge.model.mutable_state();
    if ((state.force_no_flow || bridge.model.settings().flow_offset_lph == 0.0f) && state.flow_lph < 0.05f) {
      state.flow_lph = 0.0f;
      state.flow_switch = false;
    }
    remaining -= step;
  }
  bridge.model.advance_communication_watchdog(now_ms);
  return true;
}

const char* profile_name(odu::Profile profile) {
  switch (profile) {
    case odu::Profile::V1:
      return "V1";
    case odu::Profile::V1_5:
      return "V1.5";
    case odu::Profile::V2_OLD:
      return "V2 old";
    case odu::Profile::V2_NEW:
      return "V2 new";
    default:
      return "Disabled";
  }
}

const char* mode_name(odu::WorkingMode mode) {
  switch (mode) {
    case odu::WorkingMode::HEATING:
      return "heating";
    case odu::WorkingMode::COOLING:
      return "cooling";
    default:
      return "standby";
  }
}

bool number(char (&output)[32], double value) {
  if (!std::isfinite(value)) return false;
  const int length = std::snprintf(output, sizeof(output), "%.9g", value);
  if (length <= 0 || static_cast<size_t>(length) >= sizeof(output)) return false;
  // snprintf's decimal separator follows LC_NUMERIC. Normalize it in this one
  // number, without changing process-global locale or JSON structural commas.
  for (int i = 0; i < length; ++i)
    if (output[i] == ',') output[i] = '.';
  return true;
}

}  // namespace

extern "C" {

int oq_odu_set_response(void* handle, float hz20, float hz26, float ramp) noexcept {
  if (!handle || !std::isfinite(hz20) || !std::isfinite(hz26) || !std::isfinite(ramp) || hz20 < 0 || hz20 > 90 ||
      hz26 < 0 || hz26 > 90 || ramp <= 0 || ramp > 100)
    return 0;
  get(handle)->hz20 = hz20;
  get(handle)->hz26 = hz26;
  get(handle)->response_ramp = ramp;
  return 1;
}

void* oq_odu_create(int profile, uint32_t now_ms) noexcept {
  if (!valid_profile(profile)) return nullptr;
  auto* bridge = new (std::nothrow) Bridge;
  if (bridge == nullptr) return nullptr;
  bridge->model.configure(static_cast<odu::Profile>(profile), now_ms);
  bridge->now_ms = now_ms;
  return bridge;
}

void oq_odu_destroy(void* handle) noexcept { delete get(handle); }

int oq_odu_configure(void* handle, int profile, uint32_t now_ms) noexcept {
  if (handle == nullptr || !valid_profile(profile)) return 0;
  Bridge next;
  next.model.configure(static_cast<odu::Profile>(profile), now_ms);
  // A register-profile change does not instantaneously replace circuit water.
  const auto& previous = get(handle)->model.state();
  auto& state = next.model.mutable_state();
  state.flow_lph = previous.flow_lph;
  state.flow_switch = previous.flow_switch;
  state.pump_timer_s = previous.pump_timer_s;
  state.water_in_temperature_c = previous.water_in_temperature_c;
  state.water_out_temperature_c = previous.water_out_temperature_c;
  next.now_ms = now_ms;
  *get(handle) = next;
  return 1;
}

int oq_odu_advance(void* handle, uint32_t now_ms) noexcept {
  return handle != nullptr && advance(*get(handle), now_ms) ? 1 : 0;
}

int oq_odu_read(void* handle, uint16_t address, uint16_t* value) noexcept {
  if (handle == nullptr || value == nullptr) return 0;
  uint16_t result = 0U;
  if (!get(handle)->model.read_register(address, result)) return 0;
  *value = result;
  return 1;
}

int oq_odu_can_write(void* handle, uint16_t address, uint16_t value) noexcept {
  return handle != nullptr && get(handle)->model.can_write_register(address, value) ? 1 : 0;
}

int oq_odu_write(void* handle, uint16_t address, uint16_t value, uint32_t now_ms) noexcept {
  if (handle == nullptr) return 0;
  Bridge next = *get(handle);
  if (!advance(next, now_ms) || !next.model.write_register(address, value, now_ms)) return 0;
  *get(handle) = next;
  return 1;
}

int oq_odu_write_many(void* handle, uint16_t start, const uint16_t* values, uint16_t count, uint32_t now_ms) noexcept {
  if (handle == nullptr || values == nullptr || count == 0U || count > 123U ||
      static_cast<uint32_t>(start) + count > 65536U)
    return 0;
  Bridge next = *get(handle);
  if (!advance(next, now_ms)) return 0;
  for (uint16_t i = 0U; i < count; ++i)
    if (!next.model.can_write_register(static_cast<uint16_t>(start + i), values[i])) return 0;
  for (uint16_t i = 0U; i < count; ++i)
    if (!next.model.write_register(static_cast<uint16_t>(start + i), values[i], now_ms)) return 0;
  *get(handle) = next;
  return 1;
}

void oq_odu_request(void* handle, uint8_t unit, uint8_t requested, uint8_t function, uint16_t start, uint16_t count,
                    uint32_t now_ms, int response_dropped) noexcept {
  if (handle == nullptr || unit == 0U || unit > 247U || !valid_flag(response_dropped)) return;
  auto& bridge = *get(handle);
  if (!advance(bridge, now_ms)) return;
  bridge.model.observe_modbus_request(unit, requested, function, true, now_ms);
  if (unit != requested || !bridge.model.enabled()) return;
  bridge.model.begin_request(function, start, count, now_ms);
  auto& protocol = bridge.model.mutable_state().protocol;
  if (function == 0x03U || function == 0x04U) {
    protocol.read_count++;
    if (start <= 3069U && static_cast<uint32_t>(start) + count > 3050U &&
        bridge.model.state().profile != odu::Profile::V2_NEW)
      protocol.legacy_extension_read_violations++;
  }
  if (response_dropped != 0) protocol.dropped_response_count++;
}

int oq_odu_set_environment(void* handle, double outdoor, double inlet, double target_flow, int no_flow, int defrost,
                           uint16_t fault) noexcept {
  if (handle == nullptr || !std::isfinite(outdoor) || !std::isfinite(inlet) || !std::isfinite(target_flow) ||
      outdoor < -30.0 || outdoor > 60.0 || inlet < 0.0 || inlet > 100.0 || target_flow < 0.0 || target_flow > 5000.0 ||
      !valid_flag(no_flow) || !valid_flag(defrost))
    return 0;
  auto& model = get(handle)->model;
  auto& state = model.mutable_state();
  auto& settings = model.mutable_settings();
  state.outside_temperature_c = static_cast<float>(outdoor);
  state.water_in_temperature_c = static_cast<float>(inlet);
  state.force_flow_without_relay = target_flow > 0.0;
  state.force_no_flow = no_flow != 0;
  settings.flow_offset_lph = static_cast<float>(target_flow);
  settings.flow_gain_lph_per_ipwm = 0.0f;
  settings.flow_max_lph = std::max(1200.0f, static_cast<float>(target_flow));
  model.set_defrost(defrost != 0);
  model.set_fault_word(0U, fault);
  return 1;
}

int oq_odu_link_series(void* upstream, void* downstream) noexcept {
  if (upstream == nullptr || downstream == nullptr || upstream == downstream ||
      get(upstream)->now_ms != get(downstream)->now_ms)
    return 0;
  auto& first = get(upstream)->model.mutable_state();
  auto& second = get(downstream)->model.mutable_state();
  const bool first_enabled = get(upstream)->model.enabled();
  const bool second_enabled = get(downstream)->model.enabled();
  if (!first_enabled) first.water_out_temperature_c = first.water_in_temperature_c;
  second.water_in_temperature_c = first.water_out_temperature_c;
  if (!second_enabled) second.water_out_temperature_c = second.water_in_temperature_c;
  // Disabled profiles are passive pipe sections, never hydraulic authorities.
  auto& source = first_enabled ? first : second;
  auto& target = first_enabled ? second : first;
  if (!first_enabled && !second_enabled) {
    source.flow_lph = 0.0f;
    source.flow_switch = false;
    source.pump_timer_s = 0.0f;
  }
  target.flow_lph = source.flow_lph;
  target.flow_switch = source.flow_switch;
  target.pump_timer_s = source.pump_timer_s;
  return 1;
}

int oq_odu_snapshot(void* handle, char* buffer, size_t capacity) noexcept {
  if (buffer != nullptr && capacity != 0U) buffer[0] = '\0';
  if (handle == nullptr || buffer == nullptr || capacity == 0U) return -1;
  const auto& model = get(handle)->model;
  const auto& state = model.state();
  const auto& p = state.protocol;
  char frequency[32], flow[32], inlet[32], outlet[32], thermal[32], electrical[32], ambient[32];
  if (!number(frequency, state.measured_frequency_hz) || !number(flow, state.flow_lph) ||
      !number(inlet, state.water_in_temperature_c) || !number(outlet, state.water_out_temperature_c) ||
      !number(thermal, state.thermal_power_w) || !number(electrical, state.electrical_power_w) ||
      !number(ambient, state.outside_temperature_c))
    return -1;
  const int length = std::snprintf(
      buffer, capacity,
      "{\"profile\":\"%s\",\"profile_id\":%u,\"mode\":\"%s\",\"requested_mode\":\"%s\","
      "\"level\":%u,\"requested_level\":%u,\"frequency_hz\":%s,\"flow_lph\":%s,"
      "\"inlet_c\":%s,\"outlet_c\":%s,\"outside_c\":%s,\"power_w\":%s,\"electrical_power_w\":%s,"
      "\"pump_requested\":%s,\"pump_ipwm\":%u,\"flow_switch\":%s,\"defrost\":%s,\"fault\":%u,\"fault_words\":[%u,%u,%u]"
      ",\"watchdog_counter\":%u,"
      "\"protocol\":{\"requests\":%u,\"reads\":%u,\"writes\":%u,\"dropped\":%u,\"exceptions\":%u,"
      "\"invalid_address\":%u,\"invalid_write\":%u,\"capability_violations\":%u,"
      "\"legacy_extension_reads\":%u,\"last_function\":%u,\"last_register\":%u}}",
      profile_name(state.profile), static_cast<unsigned>(state.profile), mode_name(state.active_mode),
      mode_name(state.requested_mode), static_cast<unsigned>(state.accepted_physical_level),
      static_cast<unsigned>(state.requested_physical_level), frequency, flow, inlet, outlet, ambient, thermal,
      electrical, state.pump_request ? "true" : "false", static_cast<unsigned>(state.pump_ipwm),
      state.flow_switch ? "true" : "false", state.defrost_active() ? "true" : "false",
      static_cast<unsigned>(state.fault_words[0] | state.fault_words[1] | state.fault_words[2]),
      static_cast<unsigned>(state.fault_words[0]), static_cast<unsigned>(state.fault_words[1]),
      static_cast<unsigned>(state.fault_words[2]), static_cast<unsigned>(model.communication_watchdog_counter()),
      static_cast<unsigned>(p.request_count), static_cast<unsigned>(p.read_count), static_cast<unsigned>(p.write_count),
      static_cast<unsigned>(p.dropped_response_count), static_cast<unsigned>(p.exception_count),
      static_cast<unsigned>(p.invalid_address_count), static_cast<unsigned>(p.invalid_write_count),
      static_cast<unsigned>(p.level_capability_violations), static_cast<unsigned>(p.legacy_extension_read_violations),
      static_cast<unsigned>(p.last_function_code), static_cast<unsigned>(p.last_start_address));
  if (length < 0 || static_cast<size_t>(length) >= capacity) {
    buffer[0] = '\0';
    return -1;
  }
  return length;
}

}  // extern "C"
