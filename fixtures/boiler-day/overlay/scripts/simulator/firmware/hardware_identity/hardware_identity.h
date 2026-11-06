#pragma once

#include "esphome/core/component.h"
#include "esphome/core/log.h"
#include "esp_app_desc.h"
#include "esp_random.h"
#include "esp_system.h"
#include "esp_timer.h"

namespace esphome::hardware_identity {

// A boot marker is emitted once, after normal component setup and logging.
// Periodic identity lets a read-only collector attach without requesting reset.
class HardwareIdentity final : public PollingComponent {
 public:
  void setup() override {
    this->boot_id_ = esp_random();
    this->emit_("OQ_HARDWARE_BOOT");
  }
  void update() override { this->emit_("OQ_HARDWARE_PROFILE"); }
  float get_setup_priority() const override { return -200.0f; }

 protected:
  void emit_(const char* marker) {
    const auto* descriptor = esp_app_get_description();
    static constexpr char HEX[] = "0123456789abcdef";
    char hash[65];
    for (size_t index = 0; index < 32; index++) {
      hash[index * 2] = HEX[descriptor->app_elf_sha256[index] >> 4];
      hash[index * 2 + 1] = HEX[descriptor->app_elf_sha256[index] & 0x0f];
    }
    hash[64] = '\0';
    ESP_LOGI("oq_hardware",
             "%s {\"schema\":1,\"profile\":\"q-duo-hardware-memory-v1\","
             "\"instrumentation_schema\":\"oq-lab-diagnostics-v1\",\"uptime_ms\":%lu,"
             "\"elf_sha256\":\"%s\",\"boot_id\":%lu,\"reset_reason\":%d}",
             marker, static_cast<unsigned long>(esp_timer_get_time() / 1000), hash,
             static_cast<unsigned long>(this->boot_id_), static_cast<int>(esp_reset_reason()));
  }
  uint32_t boot_id_{0};
};

}  // namespace esphome::hardware_identity
