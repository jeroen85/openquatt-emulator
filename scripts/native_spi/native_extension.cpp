#include <fcntl.h>
#include <mach-o/dyld.h>
#include <mach/mach.h>
#include <mach/mach_vm.h>
#include <sys/mman.h>
#include <unistd.h>

#include <atomic>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <memory>
#include <mutex>
#include "device_model.h"
#include "ethernet_lan.h"
#include "rmt_peer.h"

namespace {
using Read = uint32_t (*)(void*, uint32_t);
using Write = void (*)(void*, uint32_t, uint32_t);
Read original_read = nullptr;
Write original_write = nullptr;
using Interrupt = void (*)(void*, uint32_t, bool);
Interrupt set_interrupt = nullptr;
int ethernet_port = 0;
const char* ethernet_cable = nullptr;
constexpr uint32_t kGpio9 = 1u << 9;
constexpr uint32_t kGpioIn = oq_native::kGpio + 0x3c;
constexpr uint32_t kGpioStatus = oq_native::kGpio + 0x44;
constexpr uint32_t kGpioSet = oq_native::kGpio + 0x48;
constexpr uint32_t kGpioClear = oq_native::kGpio + 0x4c;
constexpr uint32_t kGpioCpu = oq_native::kGpio + 0x5c;
constexpr uint32_t kGpioCpuHigh = oq_native::kGpio + 0x68;
constexpr uint32_t kGpioPin9 = oq_native::kGpio + 0x98;
constexpr uint32_t kGpioSource = 16;
constexpr uint32_t kSpi2Source = 21;
struct MemoryResult {
  uint64_t tag;
  uint64_t value;
};
using MemoryRead = MemoryResult (*)(void*, uint32_t);
using MemoryWrite = MemoryResult (*)(void*, uint32_t, uint32_t);
MemoryRead memory_read = nullptr;
MemoryRead memory_read8 = nullptr;
MemoryWrite memory_write = nullptr;
MemoryWrite memory_write8 = nullptr;
uint8_t* executable_base();
bool device_mode = false;
double fixture_temperature = 30.0;
int clock_fd = -1;
const char* rtd_schedule = nullptr;
bool relay_high = false;

constexpr uint32_t kSystimerValueHi = 0x60023040;
constexpr uint32_t kSystimerValueLo = 0x60023044;
constexpr uint32_t kTicksPerMicrosecond = 16;

const char* thermostat_control = nullptr;
oq_native::RmtPeers::Grow grow_rmt_rx = nullptr;

FILE* trace = nullptr;
std::atomic_uint64_t event_count{0};
uint64_t trace_bytes = 0;

[[noreturn]] void fail(const char* message) {
  std::fprintf(stderr, "[native-spi] fatal: %s\n", message);
  std::fflush(nullptr);
  _exit(78);
}

bool observed(uint32_t address) {
  return (ethernet_port && address >= oq_native::kSpi2 && address < oq_native::kSpi2 + 256) ||
         (address >= 0x60025000 && address < 0x60025100) || (address >= 0x6003f000 && address < 0x6003f600) ||
         (address >= 0x60004004 && address <= 0x60004018);
}

void record(const char* kind, void* bus, uint32_t address, uint32_t value) {
  if (!trace || !observed(address)) return;
  if (event_count++ < 20000) {
    std::fprintf(trace, "{\"kind\":\"%s\",\"bus\":\"%p\",\"address\":%u,\"value\":%u,\"chip\":%u}\n", kind, bus,
                 address, value, static_cast<unsigned>(static_cast<uint8_t*>(bus)[0xaccc]));
    std::fflush(trace);
  }
}

struct NativeMemory : oq_native::Memory {
  void* bus = nullptr;
  uint32_t read32(uint32_t address) override {
    oq_native::dram(address, 4);
    oq_native::require(address % 4 == 0, "Unaligned DMA metadata read");
    const auto result = memory_read(bus, address);
    oq_native::require(result.tag == 0, "Native memory bus rejected DMA read");
    return static_cast<uint32_t>(result.value);
  }
  uint8_t read8(uint32_t address) override {
    oq_native::dram(address, 1);
    const auto result = memory_read8(bus, address);
    oq_native::require(result.tag == 0, "Native memory bus rejected DMA byte read");
    return static_cast<uint8_t>(result.value);
  }
  void write32(uint32_t address, uint32_t value) override {
    oq_native::dram(address, 4);
    oq_native::require(address % 4 == 0, "Unaligned DMA metadata write");
    const auto result = memory_write(bus, address, value);
    oq_native::require(result.tag == 0, "Native memory bus rejected DMA metadata write");
  }
  void write8(uint32_t address, uint8_t value) override {
    oq_native::dram(address, 1);
    const auto result = memory_write8(bus, address, value);
    oq_native::require(result.tag == 0, "Native memory bus rejected DMA byte write");
  }
};

std::string hex_bytes(const std::vector<uint8_t>& bytes) {
  static constexpr char digits[] = "0123456789abcdef";
  std::string result(bytes.size() * 2, '0');
  for (size_t i = 0; i < bytes.size(); ++i) {
    result[i * 2] = digits[bytes[i] >> 4];
    result[i * 2 + 1] = digits[bytes[i] & 15];
  }
  return result;
}

struct State {
  std::mutex mutex;
  NativeMemory memory;
  std::unique_ptr<oq_native::DeviceModel> model;
  std::unique_ptr<oq_native::EthernetLan> lan;
  bool gpio_low = false;
  bool gpio_latched = false;
  bool gpio_asserted = false;
  uint64_t clock_published_us = 0;
  std::unique_ptr<oq_native::RmtPeers> rmt;

  oq_native::RmtPeers& open_therm() {
    if (!rmt) rmt = std::make_unique<oq_native::RmtPeers>(original_read, grow_rmt_rx, trace, thermostat_control);
    return *rmt;
  }

  void sample_clock(void* bus, uint32_t low) {
    const uint64_t hi = original_read(bus, kSystimerValueHi) & 0xfffff;
    const uint64_t now = ((hi << 32) | low) / kTicksPerMicrosecond;
    if (now >= clock_published_us + 2000 || now < clock_published_us) {
      if (pwrite(clock_fd, &now, sizeof(now), 0) != sizeof(now)) fail("Cannot publish S3 timer sample");
      clock_published_us = now;
      if (thermostat_control) open_therm().service(bus, now);
    }
  }

  void refresh_gpio(void* bus) {
    const bool low = model->w5500.interrupt();
    if (low || gpio_latched || gpio_asserted) {
      const uint32_t config = original_read(bus, kGpioPin9);
      const bool enabled = config & (0x1fu << 13);
      const auto type = (config >> 7) & 7;
      if (enabled && low && ((!gpio_low && (type == 2 || type == 3)) || type == 4) && !gpio_latched) {
        gpio_latched = true;
        original_write(bus, kGpioSet, kGpio9);
      }
      const bool asserted = enabled && gpio_latched;
      if (asserted || gpio_asserted) {
        const bool other = (original_read(bus, kGpioCpu) & ~kGpio9) || original_read(bus, kGpioCpuHigh);
        set_interrupt(bus, kGpioSource, asserted || other);
      }
      gpio_asserted = asserted;
    }
    gpio_low = low;
  }

  void poll_network(void* bus) {
    if (lan) {
      lan->service();
      refresh_gpio(bus);
    }
  }

  oq_native::DeviceModel& get(void* bus) {
    oq_native::require(static_cast<uint8_t*>(bus)[0xaccc] == 5, "Native extension requires S3 bus");
    if (!model || memory.bus != bus) {
      lan.reset();
      gpio_low = gpio_latched = gpio_asserted = false;
      memory.bus = bus;
      model = std::make_unique<oq_native::DeviceModel>(memory);
      model->rtd.temperature = fixture_temperature;
      model->rtd.temperature_valid = rtd_schedule == nullptr;
      if (ethernet_port) {
        model->ethernet_enabled = true;
        lan = std::make_unique<oq_native::EthernetLan>(model->w5500, ethernet_port, ethernet_cable, trace);
      }
      model->event = [this](const oq_native::Event& value) {
        const std::string tx = hex_bytes(value.tx);
        const std::string rx = hex_bytes(value.rx);
        oq_native::require(event_count++ < 100000 && trace_bytes + tx.size() + rx.size() + 256 <= 32 * 1024 * 1024,
                           "Native SPI trace budget exceeded");
        trace_bytes += tx.size() + rx.size() + 256;
        const int written = std::fprintf(
            trace,
            "{\"kind\":\"spi-transfer\",\"number\":%llu,\"tx\":\"%s\",\"rx\":\"%s\",\"dma_reads\":%llu,\"dma_"
            "writes\":%llu}\n",
            value.number, tx.c_str(), rx.c_str(), model->dma_reads, model->dma_writes);
        oq_native::require(written > 0, "Native SPI trace write failed");
        if (rtd_schedule && value.rtd_sample_read) {
          const auto& rtd = model->rtd;
          const auto guest_us = static_cast<unsigned long long>(clock_published_us);
          const int result = rtd.sample_valid && rtd.temperature_valid && !(rtd.sample & 1)
              ? std::fprintf(trace, "{\"kind\":\"pt1000-sample\",\"guest_us\":%llu,\"fixture_c\":%.9g}\n",
                             guest_us, rtd.sampled_temperature)
              : std::fprintf(trace, "{\"kind\":\"pt1000-sample\",\"guest_us\":%llu,\"fixture_c\":null}\n", guest_us);
          oq_native::require(result > 0, "Native RTD trace write failed");
        }
        oq_native::require(std::fflush(trace) == 0, "Native SPI trace write failed");
      };
    }
    return *model;
  }
};

State& state() {
  static State instance;
  return instance;
}

[[noreturn]] void model_failure(const std::exception& error) {
  std::fprintf(trace, "{\"kind\":\"model-error\",\"message\":\"%s\"}\n", error.what());
  std::fflush(trace);
  fail(error.what());
}

uint32_t read_hook(void* bus, uint32_t address) {
  const bool peripheral = (address >= oq_native::kSpi && address < oq_native::kSpi + 256) ||
                          (address >= oq_native::kGdma && address < oq_native::kGdma + 5 * 0xc0) ||
                          (ethernet_port && address >= oq_native::kSpi2 && address < oq_native::kSpi2 + 256);
  const bool gpio = ethernet_port && (address == kGpioIn || address == kGpioStatus || address == kGpioCpu);
  static thread_local unsigned reads = 0;
  const bool network_tick = ethernet_port && (++reads % 16 == 0);
  if (device_mode && (peripheral || gpio || network_tick)) {
    try {
      auto& holder = state();
      std::lock_guard<std::mutex> guard(holder.mutex);
      auto& model = holder.get(bus);
      if (network_tick || gpio) holder.poll_network(bus);
      uint32_t result;
      if (peripheral && model.read(address, result)) return result;
      if (gpio) {
        result = original_read(bus, address) & ~kGpio9;
        if (address == kGpioIn) return result | (model.w5500.interrupt() ? 0 : kGpio9);
        return result | ((address == kGpioStatus ? holder.gpio_latched : holder.gpio_asserted) ? kGpio9 : 0);
      }
    } catch (const std::exception& error) {
      model_failure(error);
    }
  }
  if (clock_fd >= 0 && address == kSystimerValueLo) {
    try {
      auto& holder = state();
      std::lock_guard<std::mutex> guard(holder.mutex);
      const uint32_t low = original_read(bus, address);
      holder.sample_clock(bus, low);
      return low;
    } catch (const std::exception& error) {
      model_failure(error);
    }
  }
  const uint32_t value = original_read(bus, address);
  if (!device_mode) record("read", bus, address, value);
  return value;
}

void write_hook(void* bus, uint32_t address, uint32_t value) {
  if (thermostat_control &&
      (oq_native::RmtPeers::matrix_address(address) || oq_native::RmtPeers::is_tx_start(address, value))) {
    try {
      auto& holder = state();
      std::lock_guard<std::mutex> guard(holder.mutex);
      auto& peer = holder.open_therm();
      if (oq_native::RmtPeers::matrix_address(address)) {
        peer.on_matrix(address, value);
      } else {
        peer.on_tx(bus, address, holder.clock_published_us);
      }
      original_write(bus, address, value);
      return;
    } catch (const std::exception& error) {
      model_failure(error);
    }
  }
  if (rtd_schedule && (address == oq_native::kGpio + 4 ||
                       address == oq_native::kGpio + 8 || address == oq_native::kGpio + 12)) {
    auto& holder = state();
    std::lock_guard<std::mutex> guard(holder.mutex);
    constexpr uint32_t relay = 1u << 16;
    const bool next = address == oq_native::kGpio + 4 ? (value & relay) != 0 :
        (value & relay) == 0 ? relay_high : address == oq_native::kGpio + 8;
    if (relay_high != next) {
      relay_high = next;
      std::fprintf(trace, "{\"kind\":\"r1\",\"guest_us\":%llu,\"energized\":%s}\n",
                   static_cast<unsigned long long>(holder.clock_published_us), next ? "true" : "false");
      std::fflush(trace);
    }
  }
  const bool gpio = ethernet_port && (address == kGpioClear || address == kGpioPin9);
  if (device_mode && (observed(address) || gpio)) {
    try {
      auto& holder = state();
      std::lock_guard<std::mutex> guard(holder.mutex);
      auto& model = holder.get(bus);
      if (gpio) {
        original_write(bus, address, value);
        if (address == kGpioClear && (value & kGpio9)) holder.gpio_latched = false;
        holder.refresh_gpio(bus);
        return;
      }
      if (rtd_schedule && address == oq_native::kSpi && (value & (1u << 24))) {
        model.rtd.temperature_valid = false;
        if (FILE* file = std::fopen(rtd_schedule, "r")) {
          double next;
          if (std::fscanf(file, "%lf", &next) == 1 && std::isfinite(next) && next >= 0 && next <= 100) {
            model.rtd.temperature = next;
            model.rtd.temperature_valid = true;
          }
          std::fclose(file);
        }
      }
      const auto action = model.write(address, value);
      if (model.spi2_address(address)) {
        // The native S3 tick recomputes source 21 from its SPI2 raw/mask words.
        // Keep those words in sync so completion survives the next tick.
        auto* bytes = static_cast<uint8_t*>(bus);
        const uint32_t enabled = model.spi2[0x34 / 4];
        const uint32_t raw = model.spi2[0x3c / 4];
        std::memcpy(bytes + 0x8778, &enabled, sizeof(enabled));
        std::memcpy(bytes + 0x877c, &raw, sizeof(raw));
        set_interrupt(bus, kSpi2Source, model.spi2_interrupt());
        holder.poll_network(bus);
      }
      if (action == oq_native::WriteAction::kIntercept) return;
    } catch (const std::exception& error) {
      model_failure(error);
    }
  }
  if (!device_mode) record("write", bus, address, value);
  original_write(bus, address, value);
}

void jump(uint8_t* target, const void* destination) {
  const uint32_t instructions[] = {0x58000050, 0xd61f0200};  // ldr x16, #8; br x16
  std::memcpy(target, instructions, sizeof(instructions));
  const uintptr_t pointer = reinterpret_cast<uintptr_t>(destination);
  std::memcpy(target + 8, &pointer, sizeof(pointer));
}

uint8_t* executable_base() {
  for (uint32_t index = 0; index < _dyld_image_count(); ++index) {
    const auto* header = _dyld_get_image_header(index);
    if (header && header->filetype == MH_EXECUTE) return reinterpret_cast<uint8_t*>(const_cast<mach_header*>(header));
  }
  fail("cannot identify the main emulator image");
}

void* install(uintptr_t offset, const uint8_t expected[16], void* hook) {
  auto* base = executable_base();
  const auto* header = reinterpret_cast<const mach_header_64*>(base);
  if (header->magic != MH_MAGIC_64 || header->ncmds > 256) fail("invalid native emulator Mach-O header");
  const auto* command = reinterpret_cast<const load_command*>(base + sizeof(mach_header_64));
  bool mapped = false;
  for (uint32_t i = 0; i < header->ncmds; ++i) {
    if (command->cmdsize < sizeof(load_command)) fail("invalid native load command");
    if (command->cmd == LC_SEGMENT_64) {
      const auto* segment = reinterpret_cast<const segment_command_64*>(command);
      if (std::strncmp(segment->segname, "__TEXT", 16) == 0 && segment->fileoff == 0)
        mapped = offset + 16 <= segment->vmsize;
    }
    command = reinterpret_cast<const load_command*>(reinterpret_cast<const uint8_t*>(command) + command->cmdsize);
  }
  if (!mapped) fail("native hook outside executable text");
  auto* address = base + offset;
  if (std::memcmp(address, expected, 16) != 0) fail("emulator instruction signature mismatch");
  void* allocation = mmap(nullptr, 4096, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANON, -1, 0);
  if (allocation == MAP_FAILED) fail("cannot allocate emulator trampoline");
  auto* trampoline = static_cast<uint8_t*>(allocation);
  std::memcpy(trampoline, address, 16);
  jump(trampoline + 16, address + 16);
  if (mprotect(allocation, 4096, PROT_READ | PROT_EXEC) != 0) fail("cannot seal emulator trampoline");
  __builtin___clear_cache(reinterpret_cast<char*>(trampoline), reinterpret_cast<char*>(trampoline + 32));

  const auto page_size = static_cast<uintptr_t>(getpagesize());
  auto page = reinterpret_cast<mach_vm_address_t>(address) & ~(page_size - 1);
  const auto protection =
      mach_vm_protect(mach_task_self(), page, page_size, false, VM_PROT_READ | VM_PROT_WRITE | VM_PROT_COPY);
  if (protection != KERN_SUCCESS) fail("cannot prepare private emulator code page");
  jump(address, hook);
  __builtin___clear_cache(reinterpret_cast<char*>(address), reinterpret_cast<char*>(address + 16));
  if (mach_vm_protect(mach_task_self(), page, page_size, false, VM_PROT_READ | VM_PROT_EXECUTE) != KERN_SUCCESS)
    fail("cannot reseal private emulator code page");
  return allocation;
}

__attribute__((constructor)) void initialize() {
  const char* path = std::getenv("OQ_NATIVE_SPI_TRACE");
  if (!path || !*path) fail("explicit trace output required during interface qualification");
  trace = std::fopen(path, "w");
  if (!trace) fail("cannot open private trace output");
  const uint8_t read_bytes[16] = {0xff, 0x83, 0x01, 0xd1, 0xf4, 0x4f, 0x04, 0xa9,
                                  0xfd, 0x7b, 0x05, 0xa9, 0xfd, 0x43, 0x01, 0x91};
  const uint8_t write_bytes[16] = {0xff, 0x43, 0x02, 0xd1, 0xf6, 0x57, 0x06, 0xa9,
                                   0xf4, 0x4f, 0x07, 0xa9, 0xfd, 0x7b, 0x08, 0xa9};
  const auto base = executable_base();
  static_assert(sizeof(MemoryResult) == 16, "native memory ABI requires two return registers");
  const uint8_t read_signature[16] = {0xff, 0x43, 0x03, 0xd1, 0xf8, 0x5f, 0x09, 0xa9,
                                      0xf6, 0x57, 0x0a, 0xa9, 0xf4, 0x4f, 0x0b, 0xa9};
  const uint8_t read8_signature[16] = {0xff, 0x03, 0x03, 0xd1, 0xf8, 0x5f, 0x08, 0xa9,
                                       0xf6, 0x57, 0x09, 0xa9, 0xf4, 0x4f, 0x0a, 0xa9};
  const uint8_t write_signature[16] = {0xff, 0xc3, 0x03, 0xd1, 0xfa, 0x67, 0x0a, 0xa9,
                                       0xf8, 0x5f, 0x0b, 0xa9, 0xf6, 0x57, 0x0c, 0xa9};
  const uint8_t write8_signature[16] = {0xff, 0x83, 0x03, 0xd1, 0xfa, 0x67, 0x09, 0xa9,
                                        0xf8, 0x5f, 0x0a, 0xa9, 0xf6, 0x57, 0x0b, 0xa9};
  if (std::memcmp(base + 0xde094, read_signature, 16) || std::memcmp(base + 0xdd65c, read8_signature, 16) ||
      std::memcmp(base + 0xdfae4, write_signature, 16) || std::memcmp(base + 0xde608, write8_signature, 16))
    fail("native memory function signature mismatch");
  memory_read = reinterpret_cast<MemoryRead>(base + 0xde094);
  memory_read8 = reinterpret_cast<MemoryRead>(base + 0xdd65c);
  memory_write = reinterpret_cast<MemoryWrite>(base + 0xdfae4);
  memory_write8 = reinterpret_cast<MemoryWrite>(base + 0xde608);
  const char* mode = std::getenv("OQ_NATIVE_SPI_MODE");
  device_mode = mode && std::strcmp(mode, "device") == 0;
  if (const char* path = std::getenv("OQ_NATIVE_GUEST_CLOCK")) {
    clock_fd = open(path, O_WRONLY | O_CLOEXEC);
    if (!device_mode || clock_fd < 0) fail("Cannot open S3 timer sample file");
  }
  if ((thermostat_control = std::getenv("OQ_NATIVE_OT_CONTROL"))) {
    const uint8_t signature[16] = {0xf4, 0x4f, 0xbe, 0xa9, 0xfd, 0x7b, 0x01, 0xa9,
                                   0xfd, 0x43, 0x00, 0x91, 0xf3, 0x03, 0x00, 0xaa};
    if (!device_mode || clock_fd < 0 || !*thermostat_control || std::memcmp(base + 0x2ab448, signature, 16))
      fail("Native RMT function signature or configuration mismatch");
    grow_rmt_rx = reinterpret_cast<oq_native::RmtPeers::Grow>(base + 0x2ab448);
  }
  if (const char* port = std::getenv("OQ_W5500_PORT")) {
    char* end = nullptr;
    const long value = std::strtol(port, &end, 10);
    ethernet_cable = std::getenv("OQ_W5500_CABLE");
    if (!device_mode || end == port || *end || value < 1024 || value > 65535 || !ethernet_cable || !*ethernet_cable)
      fail("Invalid W5500 fixture configuration");
    const uint8_t signature[16] = {0xf6, 0x57, 0xbd, 0xa9, 0xf4, 0x4f, 0x01, 0xa9,
                                   0xfd, 0x7b, 0x02, 0xa9, 0xfd, 0x83, 0x00, 0x91};
    if (std::memcmp(base + 0x104414, signature, 16)) fail("Native interrupt function signature mismatch");
    set_interrupt = reinterpret_cast<Interrupt>(base + 0x104414);
    ethernet_port = static_cast<int>(value);
  }
  rtd_schedule = std::getenv("OQ_NATIVE_RTD_CONTROL");
  if (const char* temperature = std::getenv("OQ_NATIVE_RTD_C")) {
    char* end = nullptr;
    fixture_temperature = std::strtod(temperature, &end);
    if (!end || *end || !std::isfinite(fixture_temperature) || fixture_temperature < 0 || fixture_temperature > 100)
      fail("invalid native RTD fixture temperature");
  }
  original_read = reinterpret_cast<Read>(install(0x1a7d40, read_bytes, reinterpret_cast<void*>(read_hook)));
  original_write = reinterpret_cast<Write>(install(0x1a8680, write_bytes, reinterpret_cast<void*>(write_hook)));
  std::fprintf(stderr, "[native-spi] native S3 MMIO extension ready; mode=%s; no debugger\n",
               device_mode ? "device" : "trace");
}
}  // namespace
