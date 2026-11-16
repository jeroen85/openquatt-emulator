#include "../device_model.h"

#include <new>

extern "C" {
void* oq_cosim_rtd_create(double temperature) {
  if (!std::isfinite(temperature) || temperature < 0 || temperature > 100) return nullptr;
  auto* rtd = new (std::nothrow) oq_native::Max31865;
  if (rtd) rtd->temperature = temperature;
  return rtd;
}

void oq_cosim_rtd_select(void* device, int active) { static_cast<oq_native::Max31865*>(device)->select(active != 0); }

int oq_cosim_rtd_exchange(void* device, uint8_t value) {
  try {
    return static_cast<oq_native::Max31865*>(device)->exchange(value);
  } catch (const std::exception&) {
    return -1;
  }
}

void oq_cosim_rtd_destroy(void* device) { delete static_cast<oq_native::Max31865*>(device); }
}
