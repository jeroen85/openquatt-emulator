#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include "esphome/core/component.h"
#include "esphome/components/switch/switch.h"
#include "driver/rmt_rx.h"
#include "driver/rmt_tx.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/portmacro.h"
#include "protocol.h"
#include "worker_state.h"

namespace esphome::virtual_opentherm {

class VirtualOpenTherm : public Component {
 public:
  void set_boiler_role(bool boiler) { this->boiler_role_ = boiler; }
  void set_disabled_thermostat_switch(switch_::Switch* value) { this->disabled_thermostat_switch_ = value; }
  void setup() override;
  void loop() override;
  void on_shutdown() override;
  float get_setup_priority() const override { return setup_priority::HARDWARE - 1.0f; }
  void set_online(bool online);
  void set_ch_enable(bool enabled);
  void set_bad_parity(bool enabled);
  void probe_rx_budget();
  bool set_room_temperature(float value);
  bool set_room_setpoint(float value);
  bool set_control_setpoint(float value);
  bool set_response_delay(float value);
  float room_temperature() const { return this->intent_snapshot_().room_c; }
  float room_setpoint() const { return this->intent_snapshot_().room_setpoint_c; }
  float control_setpoint() const { return this->intent_snapshot_().control_setpoint_c; }
  float response_delay() const { return static_cast<float>(this->intent_snapshot_().response_delay_ms); }
  const char* profile() const { return this->boiler_role_ ? "ot-boiler-duplex-v1" : "ot-thermostat-ingress-v1"; }
  const char* source_sha256() const { return OQ_VIRTUAL_OT_SOURCE_SHA256; }

 protected:
  static constexpr size_t CAPTURE_SYMBOLS = 48;
  enum class DriverOperation : uint8_t { NONE, RX_OVERFLOW, RX_REARM, TX_SEND, TX_TIMEOUT };
  struct Snapshot {
    uint32_t uptime_ms{0};
    uint32_t rx{0};
    uint32_t rx_invalid{0};
    uint32_t rx_overflow{0};
    uint32_t tx{0};
    uint32_t done{0};
    uint32_t tx_error{0};
    uint32_t dropped{0};
    uint32_t last_tx_word{0};
    protocol::Request last_request{};
    float target_c{0};
    bool online{false};
    bool pending{false};
    bool ch_enabled{false};
  };
  // One instance per config. ISR state/buffers and the cross-task handoff are
  // explicitly internal DRAM, independent of malloc's PSRAM fallback policy.
  struct WireState {
    std::array<rmt_symbol_word_t, 34> tx_symbols{};
    std::array<rmt_symbol_word_t, CAPTURE_SYMBOLS> rx_symbols{};
    std::array<rmt_symbol_word_t, CAPTURE_SYMBOLS> captured_symbols{};
    portMUX_TYPE mux = portMUX_INITIALIZER_UNLOCKED;
    rmt_channel_handle_t rx_channel{nullptr};
    bool tx_busy{false};
    bool captured{false};
    size_t captured_count{0};
    uint32_t captured_epoch{0};
    bool captured_online{false};
    uint32_t tx_done_count{0};
    uint32_t rx_overflow_count{0};
    esp_err_t error{ESP_OK};
    DriverOperation operation{DriverOperation::NONE};
    PeerIntent intent{};
    Snapshot snapshot{};
  };
  static WireState wire_;
  static bool IRAM_ATTR rx_done_(rmt_channel_handle_t, const rmt_rx_done_event_data_t*, void*);
  static bool IRAM_ATTR tx_done_(rmt_channel_handle_t, const rmt_tx_done_event_data_t*, void*);
  static void worker_callback_(void* context);
  bool initialize_();
  bool check_(esp_err_t result, const char* operation);
  void record_error_(esp_err_t result, DriverOperation operation);
  PeerIntent intent_snapshot_() const;
  void work_();
  void commit_(uint32_t now, uint32_t observed_epoch);
  bool transmit_(uint32_t word, uint32_t expected_epoch);
  void process_capture_(const PeerIntent& intent);
  void emit_();
  switch_::Switch* disabled_thermostat_switch_{nullptr};
  bool boiler_role_{false};  // Immutable after component registration.
  rmt_channel_handle_t tx_{nullptr};
  rmt_encoder_handle_t encoder_{nullptr};
  rmt_channel_handle_t rx_{nullptr};
  esp_timer_handle_t worker_timer_{nullptr};
  // Boiler fields below have one writer: ESP_TIMER_TASK. The thermostat role
  // keeps its existing main-loop owner. Main-loop telemetry reads only snapshot.
  IntentEpoch intent_epoch_{};
  bool online_{false};
  uint32_t tx_count_{0};
  uint32_t tx_error_count_{0};
  uint32_t dropped_count_{0};
  uint32_t last_tx_word_{0};
  uint32_t rx_count_{0};
  uint32_t rx_invalid_count_{0};
  uint32_t next_request_ms_{0};
  uint32_t last_emit_ms_{0};  // Main-loop only.
  uint32_t response_due_ms_{0};
  uint32_t pending_word_{0};
  uint32_t last_status_ms_{0};
  uint32_t tx_started_ms_{0};
  uint8_t request_index_{0};
  bool pending_{false};
  protocol::Request last_request_{};
  protocol::Boiler boiler_{};
};

}  // namespace esphome::virtual_opentherm
