#pragma once

#include "esphome/core/component.h"
#include "lab_diagnostics.h"

namespace esphome {
namespace diagnostics_component {

// ESP-IDF has initialized the heap and external RAM before ESPHome setup.
// Keep the probe separate from delayed production boot automations: this
// component registers the allocation hook before the first interval can run.
class LabDiagnosticsComponent final : public Component {
 public:
  void setup() override { oq_lab_diagnostics::initialize(); }
  float get_setup_priority() const override { return 900.0f; }
};

}  // namespace diagnostics_component
}  // namespace esphome
