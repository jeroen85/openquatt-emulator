#include "lab_gpio_ot_rx.h"

#ifdef OQ_LAB_GPIO_OT_RX

#include <cstddef>
#include <cstdint>

#include "driver/gpio.h"
#include "driver/rmt_rx.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"

namespace oq_lab_gpio_ot {
namespace {

// Those two inputs are active-high after the Q controller's front end.
// This adapter emits the complete 34-bit OpenTherm frame through the same
// callback and symbol buffer as the RMT driver. Timing uses the virtual ESP clock.
struct Receiver {
  gpio_num_t pin;
  portMUX_TYPE mux = portMUX_INITIALIZER_UNLOCKED;
  BaseType_t owner = -1;
  rmt_rx_done_callback_t callback = nullptr;
  void* user = nullptr;
  rmt_symbol_word_t* buffer = nullptr;
  size_t capacity = 0;
  bool installed = false;
  bool enabled = false;
  bool armed = false;
  bool active = false;
  bool level = false;
  uint32_t last_us = 0;
  bool seen_edge = false;
  size_t pulses = 0;
  unsigned half_bits = 0;

  rmt_channel_handle_t __attribute__((always_inline)) handle() { return reinterpret_cast<rmt_channel_handle_t>(this); }

  void __attribute__((always_inline)) reset_frame() {
    active = false;
    pulses = 0;
    half_bits = 0;
  }
};

Receiver receivers[] = {{.pin = GPIO_NUM_21}, {.pin = GPIO_NUM_47}};

inline Receiver* IRAM_ATTR lookup(rmt_channel_handle_t channel) {
  for (auto& receiver : receivers) {
    if (receiver.handle() == channel) return &receiver;
  }
  return nullptr;
}

void IRAM_ATTR on_edge(void* arg) {
  auto& rx = *static_cast<Receiver*>(arg);
  if (xPortGetCoreID() != rx.owner) return;

  const auto now = static_cast<uint32_t>(esp_timer_get_time());
  const bool next = gpio_get_level(rx.pin) != 0;
  rmt_rx_done_event_data_t event{};
  rmt_rx_done_callback_t callback = nullptr;
  void* user = nullptr;

  portENTER_CRITICAL_ISR(&rx.mux);
  const uint32_t elapsed = now - rx.last_us;
  const bool after_idle = !rx.seen_edge || elapsed >= 2500;
  const bool previous = rx.level;
  rx.last_us = now;
  rx.seen_edge = true;
  rx.level = next;

  if (rx.armed && rx.enabled) {
    if (after_idle) {
      rx.reset_frame();
      rx.active = next;
    } else if (rx.active) {
      const bool short_pulse = elapsed >= 380 && elapsed <= 620;
      const bool long_pulse = elapsed >= 900 && elapsed <= 1150;
      const size_t index = rx.pulses / 2;
      if (previous == next || (!short_pulse && !long_pulse) || index >= rx.capacity) {
        rx.reset_frame();
      } else {
        auto& symbol = rx.buffer[index];
        if (rx.pulses % 2 == 0) {
          symbol.val = 0;
          symbol.duration0 = elapsed;
          symbol.level0 = previous;
        } else {
          symbol.duration1 = elapsed;
          symbol.level1 = previous;
        }
        ++rx.pulses;
        rx.half_bits += short_pulse ? 1 : 2;
        // End at the stop bit's active-to-idle transition. The decoder checks
        // the start bit, stop bit, Manchester structure and parity.
        if (rx.half_bits == 67 && !next) {
          event.received_symbols = rx.buffer;
          event.num_symbols = (rx.pulses + 1) / 2;
          callback = rx.callback;
          user = rx.user;
          rx.armed = false;
          rx.reset_frame();
        } else if (rx.half_bits >= 67) {
          rx.reset_frame();
        }
      }
    }
  }
  portEXIT_CRITICAL_ISR(&rx.mux);

  // Same-core lifecycle calls cannot run while this ISR is active. Release
  // the adapter lock before the callback, which may immediately rearm RX.
  if (callback != nullptr && callback(rx.handle(), &event, user)) portYIELD_FROM_ISR();
}

}  // namespace

esp_err_t new_channel(const rmt_rx_channel_config_t* config, rmt_channel_handle_t* channel) {
  if (config == nullptr || channel == nullptr || config->resolution_hz != 1000000 || config->flags.with_dma ||
      config->flags.invert_in)
    return ESP_ERR_INVALID_ARG;
  for (auto& rx : receivers) {
    if (rx.pin != config->gpio_num) continue;
    if (rx.installed) return ESP_ERR_INVALID_STATE;
    auto result = gpio_install_isr_service(0);
    if (result != ESP_OK && result != ESP_ERR_INVALID_STATE) return result;
    result = gpio_set_intr_type(rx.pin, GPIO_INTR_ANYEDGE);
    if (result != ESP_OK) return result;
    rx.owner = xPortGetCoreID();
    result = gpio_isr_handler_add(rx.pin, on_edge, &rx);
    if (result != ESP_OK) return result;
    gpio_intr_disable(rx.pin);
    rx.installed = true;
    *channel = rx.handle();
    return ESP_OK;
  }
  return ESP_ERR_INVALID_ARG;
}

esp_err_t register_callbacks(rmt_channel_handle_t channel, const rmt_rx_event_callbacks_t* callbacks, void* user) {
  auto* rx = lookup(channel);
  if (rx == nullptr || callbacks == nullptr || callbacks->on_recv_done == nullptr) return ESP_ERR_INVALID_ARG;
  if (!rx->installed || rx->enabled || xPortGetCoreID() != rx->owner) return ESP_ERR_INVALID_STATE;
  rx->callback = callbacks->on_recv_done;
  rx->user = user;
  return ESP_OK;
}

esp_err_t enable(rmt_channel_handle_t channel) {
  auto* rx = lookup(channel);
  if (rx == nullptr) return rmt_enable(channel);
  if (!rx->installed || rx->enabled || xPortGetCoreID() != rx->owner) return ESP_ERR_INVALID_STATE;
  const auto result = gpio_intr_enable(rx->pin);
  rx->enabled = result == ESP_OK;
  return result;
}

esp_err_t disable(rmt_channel_handle_t channel) {
  auto* rx = lookup(channel);
  if (rx == nullptr) return rmt_disable(channel);
  if (!rx->installed || !rx->enabled || xPortGetCoreID() != rx->owner) return ESP_ERR_INVALID_STATE;
  const auto result = gpio_intr_disable(rx->pin);
  if (result != ESP_OK) return result;
  rx->enabled = false;
  rx->armed = false;
  rx->seen_edge = false;
  rx->reset_frame();
  return ESP_OK;
}

esp_err_t del_channel(rmt_channel_handle_t channel) {
  auto* rx = lookup(channel);
  if (rx == nullptr) return rmt_del_channel(channel);
  if (!rx->installed || rx->enabled || xPortGetCoreID() != rx->owner) return ESP_ERR_INVALID_STATE;
  const auto result = gpio_isr_handler_remove(rx->pin);
  if (result != ESP_OK) return result;
  rx->installed = false;
  rx->callback = nullptr;
  rx->user = nullptr;
  rx->buffer = nullptr;
  rx->capacity = 0;
  return ESP_OK;
}

esp_err_t IRAM_ATTR receive(rmt_channel_handle_t channel, void* buffer, size_t size,
                            const rmt_receive_config_t* config) {
  auto* rx = lookup(channel);
  if (rx == nullptr || buffer == nullptr || size / sizeof(rmt_symbol_word_t) < 34 || config == nullptr ||
      config->flags.en_partial_rx)
    return ESP_ERR_INVALID_ARG;
  if (!rx->installed || !rx->enabled || rx->callback == nullptr || xPortGetCoreID() != rx->owner)
    return ESP_ERR_INVALID_STATE;

  portENTER_CRITICAL_SAFE(&rx->mux);
  rx->buffer = static_cast<rmt_symbol_word_t*>(buffer);
  rx->capacity = size / sizeof(rmt_symbol_word_t);
  rx->reset_frame();
  rx->armed = true;
  portEXIT_CRITICAL_SAFE(&rx->mux);
  return ESP_OK;
}

}  // namespace oq_lab_gpio_ot

#endif
