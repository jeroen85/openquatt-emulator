#pragma once

#include "esphome/core/component.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"

namespace esphome {
namespace virtual_flow {

// Only an emulator fixture: this drives the real controller flow-input pad.
// No sensor publication, flow-API substitution or host counter injection occurs.
class VirtualFlow final : public Component {
 public:
  void setup() override;
  void loop() override;
  void on_shutdown() override;
  float get_setup_priority() const override { return -200.0f; }
  void set_flow(float flow_lph);
  void set_enabled(bool enabled);

 protected:
  static void control_callback_(void* context);
  static void timer_callback_(void* context);
  void apply_settings_();
  void emit_();
  void fail_(esp_err_t error);

  portMUX_TYPE mux_ = portMUX_INITIALIZER_UNLOCKED;
  esp_timer_handle_t timer_{nullptr};
  esp_timer_handle_t control_timer_{nullptr};
  float requested_flow_lph_{800.0f};
  bool requested_enabled_{false};
  float applied_flow_lph_{0.0f};
  bool applied_enabled_{false};
  bool active_{false};
  bool level_{false};
  uint32_t requested_generation_{1};
  uint32_t applied_generation_{0};
  uint32_t half_period_us_{0};
  uint32_t generated_rising_edges_{0};
  uint32_t generated_falling_edges_{0};
  uint32_t read_mismatches_{0};
  esp_err_t driver_error_{ESP_OK};
  bool shutdown_{false};
  bool reconfiguring_{false};
  bool emit_pending_{false};
  uint64_t last_emit_us_{0};
};

}  // namespace virtual_flow
}  // namespace esphome
