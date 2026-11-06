#pragma once

#include <cstdint>

namespace esphome::virtual_opentherm {

// Copied under the DRAM mutex; callers never share mutable model references.
struct PeerIntent {
  bool online{false};
  bool ch_enable{false};
  bool bad_parity{false};
  bool shutdown{false};
  float room_c{19.0f};
  float room_setpoint_c{21.0f};
  float control_setpoint_c{40.0f};
  uint32_t response_delay_ms{50};
  uint32_t offline_epoch{0};
};

class IntentEpoch {
 public:
  // Remember OFF even when OFF/configure/ON completes between worker ticks.
  bool withdraw_required(const PeerIntent& intent) {
    const bool changed = !this->seen_ || intent.offline_epoch != this->offline_epoch_;
    this->seen_ = true;
    this->offline_epoch_ = intent.offline_epoch;
    return changed || !intent.online || intent.shutdown;
  }

 private:
  bool seen_{false};
  uint32_t offline_epoch_{0};
};

inline bool may_transmit(const PeerIntent& intent, bool busy, bool failed, uint32_t expected_offline_epoch) {
  return intent.online && !intent.shutdown && !busy && !failed && intent.offline_epoch == expected_offline_epoch;
}

inline bool may_accept_capture(const PeerIntent& intent, uint32_t captured_epoch, bool captured_online) {
  return intent.online && !intent.shutdown && captured_online && captured_epoch == intent.offline_epoch;
}

}  // namespace esphome::virtual_opentherm
