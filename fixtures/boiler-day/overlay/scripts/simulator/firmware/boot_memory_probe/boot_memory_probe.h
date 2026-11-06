#pragma once

#include "boot_memory_core.h"

#include "PsramBuffer.h"
#include "esphome/components/web_server_base/web_server_base.h"
#include "esphome/core/component.h"
#include "esp_app_desc.h"
#include "esp_heap_caps.h"
#include "esp_ota_ops.h"
#include "esp_random.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "lab_diagnostics.h"

namespace esphome::boot_memory_probe {

class BootMemoryProbe final : public Component, public AsyncWebHandler {
 public:
  void setup() override;
  void loop() override;
  void on_shutdown() override;
  float get_setup_priority() const override { return 850.0f; }
  bool canHandle(AsyncWebServerRequest* request) const override;
  void handleRequest(AsyncWebServerRequest* request) override;

 protected:
  struct Storage {
    oq_boot_memory::Sample samples[oq_boot_memory::CAPACITY];
    char http_line[oq_boot_memory::HTTP_LINE_BYTES];
  };
  struct BootMetadata {
    char elf_sha256[65]{};
    uint32_t boot_nonce{0}, reset_reason{0}, setup_uptime_ms{0}, setup_probe_us{0};
    uint32_t running_partition_address{0}, running_partition_subtype{0}, configured_boot_partition_address{0};
    int32_t ota_image_state{-1}, ota_state_error{-1};
    uint32_t pre_internal_free{0}, pre_internal_min_free{0}, pre_largest_block{0}, pre_psram_free{0};
  };

  void capture_();
  bool send_(httpd_req_t* request, const char* format, ...);
  bool sample_json_(httpd_req_t* request, uint32_t index, const oq_boot_memory::Sample& sample);
  bool metadata_json_(httpd_req_t* request, uint32_t available, uint32_t snapshot);
  static bool query_(AsyncWebServerRequest* request, const char* key, uint32_t maximum, uint32_t fallback,
                     uint32_t& value);

  openquatt_common::PsramBuffer<Storage> storage_;
  oq_boot_memory::Cache<> cache_;
  BootMetadata metadata_;
  std::atomic<bool> ready_{false};
  std::atomic_flag http_busy_ = ATOMIC_FLAG_INIT;
  uint32_t last_capture_ms_{0};
};

}  // namespace esphome::boot_memory_probe
