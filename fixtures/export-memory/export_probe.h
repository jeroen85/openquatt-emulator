#pragma once
#include <algorithm>
#include "esp_heap_caps.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esphome/core/log.h"
#if __has_include("esphome/components/openquatt_weather/WeatherForecastRecording.h")
#include "esphome/components/openquatt_weather/WeatherForecastRecording.h"
#define OQ_HIL_FORECAST_RECORDING 1
#endif

namespace oq_virtual_export {
inline void sample() {
  const unsigned ms = esp_timer_get_time()/1000;
  static unsigned last = 0;
  const unsigned cadence = (ms >= 139000 && ms <= 167000) ? 100 : 1000;
  if (ms - last < cadence) return;
  last = ms;
  const auto httpd = xTaskGetHandle("httpd");
  if (!httpd) return;
  const auto caps = MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT;
  ESP_LOGI("hil.exp", "mem %u %u %u %u %u %u %u", ms,
           unsigned(heap_caps_get_free_size(caps)), unsigned(heap_caps_get_largest_free_block(caps)),
           unsigned(heap_caps_get_minimum_free_size(caps)), unsigned(uxTaskGetStackHighWaterMark(httpd)),
           unsigned(heap_caps_get_free_size(MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT)),
           unsigned(heap_caps_get_largest_free_block(MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT)));
}

template <class Weather> void seed(Weather& weather) {
#ifdef OQ_HIL_FORECAST_RECORDING
  using namespace esphome::openquatt_weather;
  auto& recording = weather->forecast_recording();
  oq_power_house::forecast::State state;
  constexpr uint32_t start_epoch = 1700000000;
  constexpr uint64_t start_ms = 1000;
  recording.clear();
  state.context = 1;
  for (unsigned index = 0; index <= 4320; ++index) {
    const uint32_t epoch = start_epoch + index * 10;
    const uint64_t ms = start_ms + index * 10000;
    const float heat = index % 360 < 180 ? 1900.f : 0.f;
    RecordedIssue issue{};
    if (index % 90 == 0) {
      issue.monotonic_ms = ms;
      issue.scenario.monotonic_ms = ms;
      issue.scenario.epoch = epoch;
      issue.scenario.configuration = issue.scenario.weather_generation = issue.scenario.model_context = 1;
      issue.scenario.weather_fetched_epoch = epoch - 300;
      issue.scenario.weather_start_epoch = epoch - epoch % 3600;
      issue.scenario.model_samples = 150;
      issue.scenario.room_start_c = 20.f;
      issue.scenario.held_heat_w = heat;
      issue.scenario.h_w_per_k = 220.f;
      issue.scenario.c_wh_per_k = 8000.f;
      std::fill_n(issue.scenario.temperature_c, 3, 20.12f);
      std::fill_n(issue.outside_c, 13, 6.1f);
      ++state.issued_count;
      if (!state.started_epoch) state.started_epoch = epoch;
    }
    recording.observe({ms, epoch, 1, 20.f + static_cast<float>(index % 20) / 100, 6.1f, heat},
                      state, issue.monotonic_ms ? &issue : nullptr);
  }
  ESP_LOGI("hil.exp", "seed %u %u", unsigned(recording.summary(43201000).count), state.issued_count);
#else
  (void) weather;
  ESP_LOGI("hil.exp", "seed 0 0");
#endif
}
}
