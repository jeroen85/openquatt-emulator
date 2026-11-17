#include "../device_model.h"
#include "../ethernet_lan.h"

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

namespace {
struct CosimLan {
  std::unique_ptr<FILE, oq_native::CloseLanFile> trace;
  oq_native::W5500 chip;
  oq_native::EthernetLan lan;

  CosimLan(int port, const char* cable, const char* path, unsigned long epoch)
      : trace(std::fopen(path, "w")),
        chip(),
        lan(chip, port, cable, require_trace(), {0, 0, epoch, 0}, std::chrono::steady_clock::now()) {}

  FILE* require_trace() const {
    if (!trace) throw std::runtime_error("Cannot open co-simulation Ethernet trace");
    return trace.get();
  }
};

}  // namespace

extern "C" {
void* oq_cosim_lan_create(int port, const char* cable, const char* path, unsigned long epoch) {
  try {
    return new CosimLan(port, cable, path, epoch);
  } catch (const std::exception&) {
    return nullptr;
  }
}

int oq_cosim_lan_service(void* device) {
  try {
    auto& model = *static_cast<CosimLan*>(device);
    model.lan.service();
    return model.chip.interrupt();
  } catch (const std::exception&) {
    return -1;
  }
}

int oq_cosim_lan_transfer(void* device, uint16_t address, uint8_t control, uint8_t* data, size_t count) {
  try {
    auto& chip = static_cast<CosimLan*>(device)->chip;
    const uint8_t block = control >> 3;
    for (size_t i = 0; i < count; ++i, ++address) {
      if (control & 4)
        chip.write(block, address, data[i]);
      else
        data[i] = chip.read(block, address);
    }
    return chip.interrupt();
  } catch (const std::exception&) {
    return -1;
  }
}

void oq_cosim_lan_destroy(void* device) { delete static_cast<CosimLan*>(device); }
}
