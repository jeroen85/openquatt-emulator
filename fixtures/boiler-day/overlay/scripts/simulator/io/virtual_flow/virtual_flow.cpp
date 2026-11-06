#include "virtual_flow.h"

#include <cmath>

#include "driver/gpio.h"
#include "esphome/core/log.h"
#include "esphome/core/hal.h"

namespace esphome {
namespace virtual_flow {
namespace {
constexpr gpio_num_t FLOW_PIN = GPIO_NUM_15;
}  // namespace

void VirtualFlow::fail_(esp_err_t error) {
  portENTER_CRITICAL(&this->mux_);
  if (this->driver_error_ == ESP_OK) {
    this->driver_error_ = error;
    ++this->requested_generation_;
    this->emit_pending_ = true;
  }
  this->active_ = false;
  portEXIT_CRITICAL(&this->mux_);
}

void VirtualFlow::set_flow(float flow_lph) {
  // Keep the microsecond period representable even for fractional API input.
  if (!std::isfinite(flow_lph) || flow_lph < 0 || flow_lph > 3000 ||
      (flow_lph > 0 && 1500000.0 / flow_lph > UINT32_MAX)) {
    this->fail_(ESP_ERR_INVALID_ARG);
    return;
  }
  portENTER_CRITICAL(&this->mux_);
  if (this->requested_flow_lph_ != flow_lph) {
    this->requested_flow_lph_ = flow_lph;
    ++this->requested_generation_;
  }
  portEXIT_CRITICAL(&this->mux_);
}

void VirtualFlow::set_enabled(bool enabled) {
  portENTER_CRITICAL(&this->mux_);
  if (this->requested_enabled_ != enabled) {
    this->requested_enabled_ = enabled;
    ++this->requested_generation_;
  }
  portEXIT_CRITICAL(&this->mux_);
}

void VirtualFlow::control_callback_(void* context) { static_cast<VirtualFlow*>(context)->apply_settings_(); }

void VirtualFlow::timer_callback_(void* context) {
  auto* self = static_cast<VirtualFlow*>(context);
  // ESP_TIMER_TASK uses the existing persistent timer worker. No new task is
  // created for each setting change; no logging or allocation occurs here.
  portENTER_CRITICAL(&self->mux_);
  if (self->active_) {
    self->level_ = !self->level_;
    const esp_err_t error = gpio_set_level(FLOW_PIN, self->level_ ? 1 : 0);
    if (error != ESP_OK) {
      if (self->driver_error_ == ESP_OK) {
        self->driver_error_ = error;
        ++self->requested_generation_;
        self->emit_pending_ = true;
      }
      self->active_ = false;
    } else {
      if (self->level_)
        ++self->generated_rising_edges_;
      else
        ++self->generated_falling_edges_;
      if (gpio_get_level(FLOW_PIN) != static_cast<int>(self->level_)) {
        ++self->read_mismatches_;
        if (self->driver_error_ == ESP_OK) {
          self->driver_error_ = ESP_FAIL;
          ++self->requested_generation_;
          self->emit_pending_ = true;
        }
        self->active_ = false;
      }
    }
  }
  portEXIT_CRITICAL(&self->mux_);
}

void VirtualFlow::setup() {
  // pulse_meter has already installed its GPIO15 input/ISR at a higher setup
  // priority. Enable output as well, preserving that real input and ISR path.
  // gpio_config() is deliberately not used: it would replace interrupt config.
  esp_err_t error = gpio_set_direction(FLOW_PIN, GPIO_MODE_INPUT_OUTPUT);
  if (error == ESP_OK) error = gpio_set_level(FLOW_PIN, 0);
  esp_timer_create_args_t config{};
  config.callback = timer_callback_;
  config.arg = this;
  config.dispatch_method = ESP_TIMER_TASK;
  config.name = "oq-local-flow";
  if (error == ESP_OK) error = esp_timer_create(&config, &this->timer_);
  config.callback = control_callback_;
  config.name = "oq-flow-control";
  if (error == ESP_OK) error = esp_timer_create(&config, &this->control_timer_);
  if (error == ESP_OK) error = esp_timer_start_periodic(this->control_timer_, 1000);
  if (error != ESP_OK) this->fail_(error);
  this->last_emit_us_ = esp_timer_get_time();
  this->emit_();
}

void VirtualFlow::apply_settings_() {
  float flow;
  bool enabled;
  uint32_t generation;
  portENTER_CRITICAL(&this->mux_);
  generation = this->requested_generation_;
  flow = this->requested_flow_lph_;
  enabled = this->requested_enabled_;
  if (this->shutdown_ || generation == this->applied_generation_) {
    portEXIT_CRITICAL(&this->mux_);
    return;
  }
  this->reconfiguring_ = true;
  this->active_ = false;
  portEXIT_CRITICAL(&this->mux_);

  // Both callbacks run on the same ESP_TIMER_TASK dispatcher. Only this control
  // callback starts/stops/reconfigures the source timer. Unlike stopping from
  // the main loop, this cannot leave an already-dispatched old source callback
  // queued behind a reactivation. The dispatcher never overlaps its callbacks.
  const esp_err_t stopped = esp_timer_stop(this->timer_);
  if (stopped != ESP_OK && stopped != ESP_ERR_INVALID_STATE) this->fail_(stopped);
  const esp_err_t lowered = gpio_set_level(FLOW_PIN, 0);
  if (lowered != ESP_OK) this->fail_(lowered);
  const uint32_t half_period = flow > 0 ? static_cast<uint32_t>(std::llround(1500000.0 / flow)) : 0;
  portENTER_CRITICAL(&this->mux_);
  if (lowered == ESP_OK && this->level_) ++this->generated_falling_edges_;
  this->level_ = false;
  this->applied_flow_lph_ = flow;
  this->applied_enabled_ = enabled;
  this->half_period_us_ = half_period;
  this->applied_generation_ = generation;
  this->active_ = enabled && half_period > 0 && this->driver_error_ == ESP_OK && !this->shutdown_;
  const bool active = this->active_;
  portEXIT_CRITICAL(&this->mux_);
  if (active) {
    const esp_err_t error = esp_timer_start_periodic(this->timer_, half_period);
    if (error != ESP_OK) this->fail_(error);
  }
  portENTER_CRITICAL(&this->mux_);
  this->reconfiguring_ = false;
  this->emit_pending_ = true;
  portEXIT_CRITICAL(&this->mux_);
}

void VirtualFlow::emit_() {
  portENTER_CRITICAL(&this->mux_);
  if (this->reconfiguring_) {
    portEXIT_CRITICAL(&this->mux_);
    return;
  }
  const float flow = this->requested_flow_lph_;
  const bool enabled = this->requested_enabled_;
  const bool active = this->active_;
  const uint32_t generation = this->applied_generation_;
  const uint32_t requested_generation = this->requested_generation_;
  const float applied_flow = this->applied_flow_lph_;
  const bool applied_enabled = this->applied_enabled_;
  const uint32_t half_period = this->half_period_us_;
  const uint32_t rising = this->generated_rising_edges_;
  const uint32_t falling = this->generated_falling_edges_;
  const uint32_t mismatches = this->read_mismatches_;
  const esp_err_t error = this->driver_error_;
  const int gpio_level = gpio_get_level(FLOW_PIN);
  portEXIT_CRITICAL(&this->mux_);
  this->last_emit_us_ = esp_timer_get_time();
  ESP_LOGI("oq_flow",
           "OQ_LAB_FLOW {\"schema\":1,\"profile\":\"oq-local-flow-v1\",\"pin\":15,\"uptime_ms\":%u,"
           "\"requested_flow_lph\":%.9g,\"enabled\":%s,\"active\":%s,\"generation\":%u,"
           "\"requested_generation\":%u,\"applied_flow_lph\":%.9g,\"applied_enabled\":%s,"
           "\"half_period_us\":%u,\"generated_rising_edges\":%u,\"generated_falling_edges\":%u,"
           "\"read_mismatches\":%u,\"gpio_level\":%d,\"driver_error\":%d}",
           static_cast<unsigned>(millis()), flow, enabled ? "true" : "false", active ? "true" : "false",
           static_cast<unsigned>(generation), static_cast<unsigned>(requested_generation), applied_flow,
           applied_enabled ? "true" : "false", static_cast<unsigned>(half_period), static_cast<unsigned>(rising),
           static_cast<unsigned>(falling), static_cast<unsigned>(mismatches), gpio_level, static_cast<int>(error));
}

void VirtualFlow::loop() {
  portENTER_CRITICAL(&this->mux_);
  const bool pending = this->emit_pending_;
  this->emit_pending_ = false;
  portEXIT_CRITICAL(&this->mux_);
  const uint64_t now = esp_timer_get_time();
  if (pending || now - this->last_emit_us_ >= 1000000) {
    this->emit_();
  }
}

void VirtualFlow::on_shutdown() {
  portENTER_CRITICAL(&this->mux_);
  this->shutdown_ = true;
  this->active_ = false;
  portEXIT_CRITICAL(&this->mux_);
  if (this->control_timer_ != nullptr) esp_timer_stop(this->control_timer_);
  if (this->timer_ != nullptr) esp_timer_stop(this->timer_);
  gpio_set_level(FLOW_PIN, 0);
}

}  // namespace virtual_flow
}  // namespace esphome
