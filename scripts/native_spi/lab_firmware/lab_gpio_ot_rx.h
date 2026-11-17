#pragma once

#ifdef OQ_LAB_GPIO_OT_RX

#include "driver/rmt_rx.h"

namespace oq_lab_gpio_ot {

esp_err_t new_channel(const rmt_rx_channel_config_t* config, rmt_channel_handle_t* channel);
esp_err_t register_callbacks(rmt_channel_handle_t channel, const rmt_rx_event_callbacks_t* callbacks, void* user);
esp_err_t enable(rmt_channel_handle_t channel);
esp_err_t disable(rmt_channel_handle_t channel);
esp_err_t del_channel(rmt_channel_handle_t channel);
esp_err_t receive(rmt_channel_handle_t channel, void* buffer, size_t size, const rmt_receive_config_t* config);

}  // namespace oq_lab_gpio_ot

#endif
