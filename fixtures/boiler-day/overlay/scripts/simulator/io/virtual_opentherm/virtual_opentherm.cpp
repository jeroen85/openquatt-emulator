#include "virtual_opentherm.h"

#include <cmath>
#include "esp_attr.h"
#include "esphome/core/hal.h"
#include "esphome/core/log.h"

namespace esphome::virtual_opentherm {

static const char* const TAG = "virtual_ot";
DRAM_ATTR VirtualOpenTherm::WireState VirtualOpenTherm::wire_{};

bool VirtualOpenTherm::check_(esp_err_t result, const char* operation) {
  if (result == ESP_OK) return true;
  // Only setup/shutdown and explicit main-loop diagnostics use this logger.
  ESP_LOGE(TAG, "OQ_OT_PEER_ERROR operation=%s error=%s", operation, esp_err_to_name(result));
  return false;
}

void VirtualOpenTherm::record_error_(esp_err_t result, DriverOperation operation) {
  portENTER_CRITICAL(&wire_.mux);
  if (wire_.error == ESP_OK) {
    wire_.error = result;
    wire_.operation = operation;
  }
  wire_.intent.online = false;
  portEXIT_CRITICAL(&wire_.mux);
}

PeerIntent VirtualOpenTherm::intent_snapshot_() const {
  portENTER_CRITICAL(&wire_.mux);
  const PeerIntent intent = wire_.intent;
  portEXIT_CRITICAL(&wire_.mux);
  return intent;
}

void VirtualOpenTherm::setup() {
  if (this->disabled_thermostat_switch_ != nullptr) this->disabled_thermostat_switch_->turn_off();
  if (!protocol::self_test() || !this->initialize_()) {
    this->mark_failed();
    ESP_LOGE(TAG, "OQ_OT_PEER_ERROR setup failed");
    return;
  }
  this->work_();
  if (this->boiler_role_) {
    // Reuse the persistent IDF timer task. It remains runnable while the
    // production main loop executes its bounded OpenTherm OFF-flush handshake.
    esp_timer_create_args_t timer{};
    timer.callback = VirtualOpenTherm::worker_callback_;
    timer.arg = this;
    timer.dispatch_method = ESP_TIMER_TASK;
    timer.name = "oq-ot-boiler";
    if (!this->check_(esp_timer_create(&timer, &this->worker_timer_), "worker create") ||
        !this->check_(esp_timer_start_periodic(this->worker_timer_, 1000), "worker start")) {
      this->record_error_(ESP_FAIL, DriverOperation::NONE);
      this->mark_failed();
      return;
    }
  }
  ESP_LOGI(TAG,
           "OQ_OT_PEER_BUILD {\"schema\":1,\"profile\":\"%s\",\"source_sha256\":\"%s\","
           "\"tx_channel\":1,\"rx_channel\":%d,\"self_test\":true,\"loopback\":\"%s\"}",
           this->profile(), this->source_sha256(), this->boiler_role_ ? 6 : -1, this->boiler_role_ ? "0:4,1:2" : "1:4");
}

bool VirtualOpenTherm::initialize_() {
  rmt_tx_channel_config_t tx{};
  tx.gpio_num = GPIO_NUM_17;
  tx.clk_src = RMT_CLK_SRC_DEFAULT;
  tx.resolution_hz = 1000000;
  tx.mem_block_symbols = 48;
  tx.trans_queue_depth = 1;
  tx.flags.init_level = false;
  rmt_copy_encoder_config_t encoder{};
  rmt_tx_event_callbacks_t transmit_callbacks{};
  transmit_callbacks.on_trans_done = VirtualOpenTherm::tx_done_;
  if (!this->check_(rmt_new_tx_channel(&tx, &this->tx_), "TX create") ||
      !this->check_(rmt_new_copy_encoder(&encoder, &this->encoder_), "encoder create") ||
      !this->check_(rmt_tx_register_event_callbacks(this->tx_, &transmit_callbacks, &wire_), "TX callback") ||
      !this->check_(rmt_enable(this->tx_), "TX enable"))
    return false;
  if (!this->boiler_role_) return true;
  rmt_rx_channel_config_t rx{};
  rx.gpio_num = GPIO_NUM_18;
  rx.clk_src = RMT_CLK_SRC_DEFAULT;
  rx.resolution_hz = 1000000;
  rx.mem_block_symbols = CAPTURE_SYMBOLS;
  rmt_rx_event_callbacks_t receive_callbacks{};
  receive_callbacks.on_recv_done = VirtualOpenTherm::rx_done_;
  if (!this->check_(rmt_new_rx_channel(&rx, &this->rx_), "RX create") ||
      !this->check_(rmt_rx_register_event_callbacks(this->rx_, &receive_callbacks, &wire_), "RX callback") ||
      !this->check_(rmt_enable(this->rx_), "RX enable"))
    return false;
  wire_.rx_channel = this->rx_;
  rmt_receive_config_t receive{};
  receive.signal_range_min_ns = 3000;
  receive.signal_range_max_ns = 2500000;
  return this->check_(rmt_receive(this->rx_, wire_.rx_symbols.data(), sizeof(wire_.rx_symbols), &receive), "RX arm");
}

bool IRAM_ATTR VirtualOpenTherm::tx_done_(rmt_channel_handle_t, const rmt_tx_done_event_data_t*, void* context) {
  auto* wire = static_cast<WireState*>(context);
  if (wire == nullptr) return false;
  portENTER_CRITICAL_ISR(&wire->mux);
  wire->tx_busy = false;
  if (wire->tx_done_count < UINT32_MAX) ++wire->tx_done_count;
  portEXIT_CRITICAL_ISR(&wire->mux);
  return false;
}

bool IRAM_ATTR VirtualOpenTherm::rx_done_(rmt_channel_handle_t, const rmt_rx_done_event_data_t* event, void* context) {
  auto* wire = static_cast<WireState*>(context);
  if (wire == nullptr || event == nullptr) return false;
  portENTER_CRITICAL_ISR(&wire->mux);
  if (!wire->captured && event->num_symbols <= CAPTURE_SYMBOLS && wire->error == ESP_OK) {
    wire->captured_count = event->num_symbols;
    wire->captured_epoch = wire->intent.offline_epoch;
    wire->captured_online = wire->intent.online && !wire->intent.shutdown;
    for (size_t i = 0; i < event->num_symbols; ++i) wire->captured_symbols[i] = event->received_symbols[i];
    wire->captured = true;
  } else {
    if (wire->rx_overflow_count < UINT32_MAX) ++wire->rx_overflow_count;
    if (wire->error == ESP_OK) {
      wire->error = ESP_ERR_INVALID_SIZE;
      wire->operation = DriverOperation::RX_OVERFLOW;
    }
  }
  portEXIT_CRITICAL_ISR(&wire->mux);
  rmt_receive_config_t receive{};
  receive.signal_range_min_ns = 3000;
  receive.signal_range_max_ns = 2500000;
  const esp_err_t result = rmt_receive(wire->rx_channel, wire->rx_symbols.data(), sizeof(wire->rx_symbols), &receive);
  if (result != ESP_OK) {
    portENTER_CRITICAL_ISR(&wire->mux);
    if (wire->rx_overflow_count < UINT32_MAX) ++wire->rx_overflow_count;
    if (wire->error == ESP_OK) {
      wire->error = result;
      wire->operation = DriverOperation::RX_REARM;
    }
    portEXIT_CRITICAL_ISR(&wire->mux);
  }
  return false;
}

void VirtualOpenTherm::worker_callback_(void* context) {
  // No logging, JSON, allocation, component publication or task creation here.
  static_cast<VirtualOpenTherm*>(context)->work_();
}

void VirtualOpenTherm::set_online(bool online) {
  portENTER_CRITICAL(&wire_.mux);
  if (!wire_.intent.shutdown && wire_.error == ESP_OK) {
    wire_.intent.online = online;
    if (!online) ++wire_.intent.offline_epoch;
  }
  portEXIT_CRITICAL(&wire_.mux);
}

void VirtualOpenTherm::set_ch_enable(bool enabled) {
  portENTER_CRITICAL(&wire_.mux);
  if (!wire_.intent.shutdown) wire_.intent.ch_enable = enabled;
  portEXIT_CRITICAL(&wire_.mux);
}

void VirtualOpenTherm::set_bad_parity(bool enabled) {
  portENTER_CRITICAL(&wire_.mux);
  if (!wire_.intent.shutdown) wire_.intent.bad_parity = enabled;
  portEXIT_CRITICAL(&wire_.mux);
}

bool VirtualOpenTherm::set_room_temperature(float value) {
  if (!std::isfinite(value) || value < 5.0f || value > 35.0f) return false;
  portENTER_CRITICAL(&wire_.mux);
  const bool accepted = !wire_.intent.shutdown;
  if (accepted) wire_.intent.room_c = value;
  portEXIT_CRITICAL(&wire_.mux);
  return accepted;
}

bool VirtualOpenTherm::set_room_setpoint(float value) {
  if (!std::isfinite(value) || value < 5.0f || value > 35.0f) return false;
  portENTER_CRITICAL(&wire_.mux);
  const bool accepted = !wire_.intent.shutdown;
  if (accepted) wire_.intent.room_setpoint_c = value;
  portEXIT_CRITICAL(&wire_.mux);
  return accepted;
}

bool VirtualOpenTherm::set_control_setpoint(float value) {
  if (!std::isfinite(value) || value < 0.0f || value > 60.0f) return false;
  portENTER_CRITICAL(&wire_.mux);
  const bool accepted = !wire_.intent.shutdown;
  if (accepted) wire_.intent.control_setpoint_c = value;
  portEXIT_CRITICAL(&wire_.mux);
  return accepted;
}

bool VirtualOpenTherm::set_response_delay(float value) {
  if (!std::isfinite(value) || value < 20.0f || value > 1200.0f || std::floor(value) != value) return false;
  portENTER_CRITICAL(&wire_.mux);
  const bool accepted = !wire_.intent.shutdown;
  if (accepted) wire_.intent.response_delay_ms = static_cast<uint32_t>(value);
  portEXIT_CRITICAL(&wire_.mux);
  return accepted;
}

bool VirtualOpenTherm::transmit_(uint32_t word, uint32_t expected_epoch) {
  portENTER_CRITICAL(&wire_.mux);
  const bool allowed = may_transmit(wire_.intent, wire_.tx_busy, wire_.error != ESP_OK, expected_epoch);
  const bool bad_parity = wire_.intent.bad_parity;
  if (allowed) wire_.tx_busy = true;
  portEXIT_CRITICAL(&wire_.mux);
  if (!allowed) return false;
  if (bad_parity) word ^= 0x80000000U;
  for (size_t bit = 0; bit < wire_.tx_symbols.size(); ++bit) {
    const bool value = bit == 0 || bit == 33 ? true : ((word >> (32U - bit)) & 1U) != 0;
    auto& symbol = wire_.tx_symbols[bit];
    symbol.level0 = value;
    symbol.duration0 = 500;
    symbol.level1 = !value;
    symbol.duration1 = 500;
  }
  rmt_transmit_config_t transmit{};
  transmit.flags.eot_level = false;
  transmit.flags.queue_nonblocking = true;
  const esp_err_t result =
      rmt_transmit(this->tx_, this->encoder_, wire_.tx_symbols.data(), sizeof(wire_.tx_symbols), &transmit);
  if (result != ESP_OK) {
    portENTER_CRITICAL(&wire_.mux);
    wire_.tx_busy = false;
    portEXIT_CRITICAL(&wire_.mux);
    if (this->tx_error_count_ < UINT32_MAX) ++this->tx_error_count_;
    this->record_error_(result, DriverOperation::TX_SEND);
    return false;
  }
  this->last_tx_word_ = word;
  this->tx_started_ms_ = millis();
  if (this->tx_count_ < UINT32_MAX) ++this->tx_count_;
  return true;
}

void VirtualOpenTherm::process_capture_(const PeerIntent& intent) {
  std::array<rmt_symbol_word_t, CAPTURE_SYMBOLS> symbols{};
  size_t count = 0;
  uint32_t captured_epoch = 0;
  bool captured_online = false;
  portENTER_CRITICAL(&wire_.mux);
  if (wire_.captured) {
    count = wire_.captured_count;
    captured_epoch = wire_.captured_epoch;
    captured_online = wire_.captured_online;
    for (size_t i = 0; i < count; ++i) symbols[i] = wire_.captured_symbols[i];
    wire_.captured = false;
  }
  portEXIT_CRITICAL(&wire_.mux);
  if (count == 0) return;
  std::array<protocol::Run, CAPTURE_SYMBOLS * 2> runs{};
  size_t used = 0;
  for (size_t i = 0; i < count; ++i) {
    const auto& symbol = symbols[i];
    if (symbol.duration0 != 0) runs[used++] = {symbol.duration0, symbol.level0 != 0};
    if (symbol.duration1 != 0) runs[used++] = {symbol.duration1, symbol.level1 != 0};
  }
  protocol::Request request{};
  if (!protocol::decode_controller(runs.data(), used, request)) {
    if (this->rx_invalid_count_ < UINT32_MAX) ++this->rx_invalid_count_;
    return;
  }
  if (this->rx_count_ < UINT32_MAX) ++this->rx_count_;
  this->last_request_ = request;
  const uint32_t receipt_ms = millis();
  bool accepted = false;
  portENTER_CRITICAL(&wire_.mux);
  if (this->online_ && !this->pending_ && wire_.error == ESP_OK && intent.offline_epoch == wire_.intent.offline_epoch &&
      may_accept_capture(wire_.intent, captured_epoch, captured_online)) {
    // Pure bounded model arithmetic only: no allocation, logging, scheduler or
    // driver call. Model mutation and OFF-epoch intent therefore have one
    // linearization point, even when OFF/configure/ON races with decoding.
    this->pending_word_ = this->boiler_.respond(request);
    if (request.id == 0 && request.type == 0) this->last_status_ms_ = receipt_ms;
    this->response_due_ms_ = receipt_ms + wire_.intent.response_delay_ms;
    this->pending_ = true;
    accepted = true;
  }
  portEXIT_CRITICAL(&wire_.mux);
  if (!accepted && this->dropped_count_ < UINT32_MAX) ++this->dropped_count_;
}

void VirtualOpenTherm::commit_(uint32_t now, uint32_t observed_epoch) {
  portENTER_CRITICAL(&wire_.mux);
  const bool current = wire_.intent.offline_epoch == observed_epoch && !wire_.intent.shutdown && wire_.error == ESP_OK;
  wire_.snapshot = {now,
                    this->rx_count_,
                    this->rx_invalid_count_,
                    wire_.rx_overflow_count,
                    this->tx_count_,
                    wire_.tx_done_count,
                    this->tx_error_count_,
                    this->dropped_count_,
                    this->last_tx_word_,
                    this->last_request_,
                    current ? this->boiler_.target_c : 0.0f,
                    current && this->online_,
                    current && this->pending_,
                    current && this->boiler_.ch_enabled};
  portEXIT_CRITICAL(&wire_.mux);
}

void VirtualOpenTherm::work_() {
  const uint32_t now = millis();
  const PeerIntent intent = this->intent_snapshot_();
  portENTER_CRITICAL(&wire_.mux);
  const bool failed = wire_.error != ESP_OK;
  const bool busy = wire_.tx_busy;
  portEXIT_CRITICAL(&wire_.mux);
  if (failed || this->intent_epoch_.withdraw_required(intent)) {
    if (this->pending_ && this->dropped_count_ < UINT32_MAX) ++this->dropped_count_;
    this->pending_ = false;
    this->boiler_.withdraw();
    this->last_status_ms_ = 0;
    this->next_request_ms_ = now;
  }
  this->online_ = intent.online && !intent.shutdown && !failed;
  if (failed || intent.shutdown) {
    this->commit_(now, intent.offline_epoch);
    return;
  }
  if (busy && now - this->tx_started_ms_ > 200) {
    if (this->tx_error_count_ < UINT32_MAX) ++this->tx_error_count_;
    this->record_error_(ESP_ERR_TIMEOUT, DriverOperation::TX_TIMEOUT);
    this->commit_(now, intent.offline_epoch);
    return;
  }
  if (this->boiler_role_) {
    this->process_capture_(intent);
    if (this->last_status_ms_ != 0 && now - this->last_status_ms_ > 30000) {
      this->boiler_.withdraw();
      this->last_status_ms_ = 0;
    }
    if (this->online_ && this->pending_ && static_cast<int32_t>(now - this->response_due_ms_) >= 0 &&
        this->transmit_(this->pending_word_, intent.offline_epoch))
      this->pending_ = false;
  } else if (this->online_ && static_cast<int32_t>(now - this->next_request_ms_) >= 0) {
    uint32_t word = 0;
    switch (this->request_index_) {
      case 0:
        word = protocol::frame(0, 0, intent.ch_enable ? 0x0100 : 0);
        break;
      case 1:
        word = protocol::frame(1, 1, protocol::f88(intent.control_setpoint_c));
        break;
      case 2:
        word = protocol::frame(1, 16, protocol::f88(intent.room_setpoint_c));
        break;
      default:
        word = protocol::frame(1, 24, protocol::f88(intent.room_c));
        break;
    }
    if (this->transmit_(word, intent.offline_epoch)) {
      this->request_index_ = static_cast<uint8_t>((this->request_index_ + 1U) % 4U);
      this->next_request_ms_ = now + 1000;
    }
  }
  this->commit_(now, intent.offline_epoch);
}

void VirtualOpenTherm::emit_() {
  portENTER_CRITICAL(&wire_.mux);
  const Snapshot sample = wire_.snapshot;
  portEXIT_CRITICAL(&wire_.mux);
  ESP_LOGI(TAG,
           "OQ_OT_PEER {\"schema\":1,\"uptime_ms\":%lu,\"profile\":\"%s\",\"online\":%s,"
           "\"rx\":%lu,\"rx_invalid\":%lu,\"rx_overflow\":%lu,\"tx\":%lu,\"done\":%lu,\"tx_error\":%lu,"
           "\"dropped\":%lu,\"last_tx_word\":%lu,\"pending\":%s,\"last_type\":%u,\"last_id\":%u,"
           "\"last_value\":%u,\"target_c\":%.2f,\"ch_enabled\":%s}",
           static_cast<unsigned long>(sample.uptime_ms), this->profile(), sample.online ? "true" : "false",
           static_cast<unsigned long>(sample.rx), static_cast<unsigned long>(sample.rx_invalid),
           static_cast<unsigned long>(sample.rx_overflow), static_cast<unsigned long>(sample.tx),
           static_cast<unsigned long>(sample.done), static_cast<unsigned long>(sample.tx_error),
           static_cast<unsigned long>(sample.dropped), static_cast<unsigned long>(sample.last_tx_word),
           sample.pending ? "true" : "false", static_cast<unsigned>(sample.last_request.type),
           static_cast<unsigned>(sample.last_request.id), static_cast<unsigned>(sample.last_request.value),
           static_cast<double>(sample.target_c), sample.ch_enabled ? "true" : "false");
}

void VirtualOpenTherm::loop() {
  if (!this->boiler_role_) this->work_();
  portENTER_CRITICAL(&wire_.mux);
  const esp_err_t error = wire_.error;
  const DriverOperation operation = wire_.operation;
  portEXIT_CRITICAL(&wire_.mux);
  if (error != ESP_OK && !this->is_failed()) {
    ESP_LOGE(TAG, "OQ_OT_PEER_ERROR operation=%u error=%s", static_cast<unsigned>(operation), esp_err_to_name(error));
    this->mark_failed();
  }
  if (this->disabled_thermostat_switch_ != nullptr && this->disabled_thermostat_switch_->state) {
    this->disabled_thermostat_switch_->turn_off();
  }
  const uint32_t now = millis();
  if (now - this->last_emit_ms_ >= 1000) {
    this->last_emit_ms_ = now;
    this->emit_();
  }
}

void VirtualOpenTherm::probe_rx_budget() {
  if (this->is_failed()) return;
  rmt_rx_channel_config_t config{};
  config.gpio_num = GPIO_NUM_19;
  config.clk_src = RMT_CLK_SRC_DEFAULT;
  config.resolution_hz = 1000000;
  config.mem_block_symbols = 48;
  rmt_channel_handle_t temporary = nullptr;
  const esp_err_t result = rmt_new_rx_channel(&config, &temporary);
  if (temporary != nullptr) rmt_del_channel(temporary);
  const bool expected = this->boiler_role_ ? result == ESP_OK : result == ESP_ERR_NOT_FOUND;
  ESP_LOGI(TAG,
           "OQ_OT_RESOURCE {\"schema\":1,\"profile\":\"%s\",\"extra_rx_symbols\":48,"
           "\"result\":%d,\"expected\":%s}",
           this->profile(), static_cast<int>(result), expected ? "true" : "false");
}

void VirtualOpenTherm::on_shutdown() {
  portENTER_CRITICAL(&wire_.mux);
  wire_.intent.online = false;
  wire_.intent.shutdown = true;
  ++wire_.intent.offline_epoch;
  portEXIT_CRITICAL(&wire_.mux);
  if (this->worker_timer_ != nullptr) esp_timer_stop(this->worker_timer_);
  if (this->rx_ != nullptr) this->check_(rmt_disable(this->rx_), "shutdown RX disable");
  if (this->tx_ != nullptr) this->check_(rmt_disable(this->tx_), "shutdown TX disable");
  // Context, channel buffers and component live until reset. The permanent
  // gate blocks a delayed worker or ISR from claiming another transmission.
}

}  // namespace esphome::virtual_opentherm
