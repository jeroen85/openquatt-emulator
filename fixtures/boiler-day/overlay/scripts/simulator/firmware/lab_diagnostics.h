#pragma once

#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <limits>

// Test-only probe logic. The host-testable part owns no memory and does not
// synchronize callers; the ESP-IDF wrapper serializes allocation-hook updates.
namespace oq_lab_diagnostics {

constexpr const char* PROFILE = "oq-lab-diagnostics-v1";
constexpr uint32_t TASK_CAPACITY = 64;
constexpr size_t LINE_CAPACITY = 448;

enum class TaskError : uint32_t {
  OK = 0,
  TRACE_DISABLED = 1,
  SCRATCH_UNAVAILABLE = 2,
  NOT_INITIALIZED = 3,
  CAPACITY_OR_SNAPSHOT_FAILURE = 4,
  FORMAT_ERROR = 5,
};

struct AllocationFailures {
  uint32_t count{0};
  uint32_t largest_size{0};
  uint32_t largest_caps{0};
  uint32_t last_caps{0};
};

inline __attribute__((always_inline)) uint32_t bounded_u32(uint64_t value) {
  return value > std::numeric_limits<uint32_t>::max() ? std::numeric_limits<uint32_t>::max()
                                                      : static_cast<uint32_t>(value);
}

// Called while holding the failure lock on ESP-IDF. No allocation, formatting,
// logging, pointers to caller-owned data or unbounded work belong in this path.
inline __attribute__((always_inline)) void record_failure(AllocationFailures& failures, size_t requested_size,
                                                          uint32_t caps) {
  const uint32_t size = bounded_u32(requested_size);
  if (failures.count == 0 || size > failures.largest_size) {
    failures.largest_size = size;
    failures.largest_caps = caps;
  }
  failures.last_caps = caps;
  if (failures.count != std::numeric_limits<uint32_t>::max()) ++failures.count;
}

inline bool snapshot_complete(bool initialized, bool trace_enabled, bool scratch_available, uint32_t task_count,
                              uint32_t capacity) {
  // A full buffer is deliberately not accepted as proof of a complete snapshot.
  // Keep at least one spare entry rather than silently accepting saturation.
  return initialized && trace_enabled && scratch_available && task_count > 0 && task_count < capacity;
}

struct TaskRecord {
  uint32_t task_id{0};
  uint32_t priority{0};
  uint32_t state{0};
  uint32_t stack_hwm_bytes{0};
};

struct Summary {
  uint32_t sample_id{0};
  uint32_t uptime_ms{0};
  AllocationFailures failures{};
  bool hook_registered{false};
  bool trace_enabled{false};
  uint32_t task_count{0};
  uint32_t task_capacity{TASK_CAPACITY};
  bool task_complete{false};
  TaskError task_error{TaskError::NOT_INITIALIZED};
  uint32_t snapshot_us{0};
  uint32_t scratch_psram_bytes{0};
};

inline bool format_task(char* output, size_t capacity, uint32_t sample_id, const TaskRecord& task) {
  if (output == nullptr || capacity == 0) return false;
  const int written = std::snprintf(
      output, capacity,
      "OQ_LAB_TASK {\"schema\":1,\"sample_id\":%u,\"task_id\":%u,\"priority\":%u,\"state\":%u,"
      "\"stack_hwm_bytes\":%u}",
      static_cast<unsigned>(sample_id), static_cast<unsigned>(task.task_id), static_cast<unsigned>(task.priority),
      static_cast<unsigned>(task.state), static_cast<unsigned>(task.stack_hwm_bytes));
  return written >= 0 && static_cast<size_t>(written) < capacity;
}

constexpr const char* SUMMARY_FORMAT =
    "OQ_LAB_DIAGNOSTICS {\"schema\":1,\"profile\":\"oq-lab-diagnostics-v1\",\"sample_id\":%u,"
    "\"uptime_ms\":%u,\"allocation_failures\":%u,\"largest_failed_size\":%u,\"largest_failed_caps\":%u,"
    "\"last_failed_caps\":%u,\"hook_registered\":%s,\"trace_enabled\":%s,\"task_count\":%u,"
    "\"task_capacity\":%u,\"task_complete\":%s,\"task_error\":%u,\"snapshot_us\":%u,"
    "\"scratch_psram_bytes\":%u}";

inline bool format_summary(char* output, size_t capacity, const Summary& summary) {
  if (output == nullptr || capacity == 0) return false;
  const int written = std::snprintf(
      output, capacity, SUMMARY_FORMAT, static_cast<unsigned>(summary.sample_id),
      static_cast<unsigned>(summary.uptime_ms), static_cast<unsigned>(summary.failures.count),
      static_cast<unsigned>(summary.failures.largest_size), static_cast<unsigned>(summary.failures.largest_caps),
      static_cast<unsigned>(summary.failures.last_caps), summary.hook_registered ? "true" : "false",
      summary.trace_enabled ? "true" : "false", static_cast<unsigned>(summary.task_count),
      static_cast<unsigned>(summary.task_capacity), summary.task_complete ? "true" : "false",
      static_cast<unsigned>(summary.task_error), static_cast<unsigned>(summary.snapshot_us),
      static_cast<unsigned>(summary.scratch_psram_bytes));
  return written >= 0 && static_cast<size_t>(written) < capacity;
}

}  // namespace oq_lab_diagnostics

#ifdef ESP_PLATFORM

#ifndef OQ_LAB_DIAGNOSTICS_TARGET
#error "lab_diagnostics.h is restricted to the explicit test-only diagnostics package"
#endif

#include "esp_attr.h"
#include "esp_heap_caps.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esphome/core/log.h"

namespace oq_lab_diagnostics {
namespace detail {

struct ProbeScratch {
  TaskStatus_t tasks[TASK_CAPACITY];
  char line[LINE_CAPACITY];
};

// Only small synchronization/counter state lives in internal DRAM. The task
// snapshot and formatting scratch are persistent, explicitly PSRAM-only.
inline DRAM_ATTR portMUX_TYPE failure_mux = portMUX_INITIALIZER_UNLOCKED;
inline DRAM_ATTR AllocationFailures failures{};
inline DRAM_ATTR portMUX_TYPE probe_mux = portMUX_INITIALIZER_UNLOCKED;
inline ProbeScratch* scratch{nullptr};
inline bool initialization_started{false};
inline bool initialized{false};
inline bool hook_registered{false};
inline bool probe_busy{false};
inline uint32_t sample_id{0};

// Keep this address-taken ISR-capable entry point out of C++ inline/COMDAT
// sections: Xtensa must place its literal pool before the IRAM instructions.
// Pure counter helpers are forced inline, so this path stays wholly in IRAM.
static void IRAM_ATTR __attribute__((noinline)) allocation_failed(size_t requested_size, uint32_t caps, const char*) {
  // portENTER_CRITICAL_SAFE selects the task/ISR-safe path on ESP-IDF Xtensa.
  // The hook never allocates, logs, waits on a scheduler primitive or retains
  // the function-name pointer provided by the allocator.
  portENTER_CRITICAL_SAFE(&failure_mux);
  record_failure(failures, requested_size, caps);
  portEXIT_CRITICAL_SAFE(&failure_mux);
}

inline AllocationFailures failure_snapshot() {
  portENTER_CRITICAL_SAFE(&failure_mux);
  const AllocationFailures result = failures;
  portEXIT_CRITICAL_SAFE(&failure_mux);
  return result;
}

struct ProbeLease {
  ProbeScratch* scratch{nullptr};
  bool initialized{false};
  bool hook_registered{false};
  uint32_t sample_id{0};
  bool acquired{false};

  explicit ProbeLease(bool assign_sample_id = true) {
    portENTER_CRITICAL_SAFE(&probe_mux);
    if (!probe_busy) {
      probe_busy = true;
      this->scratch = detail::scratch;
      this->initialized = detail::initialized;
      this->hook_registered = detail::hook_registered;
      if (assign_sample_id) {
        detail::sample_id = detail::sample_id == std::numeric_limits<uint32_t>::max() ? 1 : detail::sample_id + 1;
        this->sample_id = detail::sample_id;
      }
      this->acquired = true;
    }
    portEXIT_CRITICAL_SAFE(&probe_mux);
  }
  ~ProbeLease() {
    if (!this->acquired) return;
    portENTER_CRITICAL_SAFE(&probe_mux);
    probe_busy = false;
    portEXIT_CRITICAL_SAFE(&probe_mux);
  }
  ProbeLease(const ProbeLease&) = delete;
  ProbeLease& operator=(const ProbeLease&) = delete;
};

}  // namespace detail

inline void initialize() {
  if (xPortInIsrContext()) return;
  portENTER_CRITICAL_SAFE(&detail::probe_mux);
  if (detail::initialization_started || detail::probe_busy) {
    portEXIT_CRITICAL_SAFE(&detail::probe_mux);
    return;
  }
  detail::initialization_started = true;
  detail::probe_busy = true;
  portEXIT_CRITICAL_SAFE(&detail::probe_mux);

  const bool registered = heap_caps_register_failed_alloc_callback(detail::allocation_failed) == ESP_OK;
  detail::ProbeScratch* scratch = nullptr;
#if configUSE_TRACE_FACILITY == 1
  scratch = static_cast<detail::ProbeScratch*>(
      heap_caps_calloc(1, sizeof(detail::ProbeScratch), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
#endif
  // Deliberately no retry and no internal-RAM fallback. Every subsequent probe
  // reports an incomplete snapshot when this single allocation is unavailable.
  portENTER_CRITICAL_SAFE(&detail::probe_mux);
  detail::scratch = scratch;
  detail::hook_registered = registered;
  detail::initialized = true;
  detail::probe_busy = false;
  portEXIT_CRITICAL_SAFE(&detail::probe_mux);
}

inline void emit() {
  if (xPortInIsrContext()) return;
  detail::ProbeLease lease;
  if (!lease.acquired) return;

  Summary summary{};
  summary.sample_id = lease.sample_id;
  summary.uptime_ms = static_cast<uint32_t>(esp_timer_get_time() / 1000);
  summary.hook_registered = lease.hook_registered;
#if configUSE_TRACE_FACILITY == 1
  summary.trace_enabled = true;
#endif
  if (!lease.initialized) {
    summary.task_error = TaskError::NOT_INITIALIZED;
  } else if (!summary.trace_enabled) {
    summary.task_error = TaskError::TRACE_DISABLED;
  } else if (lease.scratch == nullptr) {
    summary.task_error = TaskError::SCRATCH_UNAVAILABLE;
  } else {
#if configUSE_TRACE_FACILITY == 1
    const int64_t started_us = esp_timer_get_time();
    summary.task_count = static_cast<uint32_t>(uxTaskGetSystemState(lease.scratch->tasks, TASK_CAPACITY, nullptr));
    summary.snapshot_us = bounded_u32(static_cast<uint64_t>(esp_timer_get_time() - started_us));
    summary.scratch_psram_bytes = sizeof(detail::ProbeScratch);
    summary.task_complete = snapshot_complete(true, true, true, summary.task_count, TASK_CAPACITY);
    summary.task_error = summary.task_complete ? TaskError::OK : TaskError::CAPACITY_OR_SNAPSHOT_FAILURE;
    for (uint32_t index = 0; index < summary.task_count && index < TASK_CAPACITY; ++index) {
      const TaskStatus_t& snapshot = lease.scratch->tasks[index];
      // These scalar values were copied under the FreeRTOS kernel lock. Task
      // names, handles and stack-base pointers may already have expired: never
      // dereference them, or call another task API with a borrowed handle here.
      const TaskRecord record{static_cast<uint32_t>(snapshot.xTaskNumber),
                              static_cast<uint32_t>(snapshot.uxCurrentPriority),
                              static_cast<uint32_t>(snapshot.eCurrentState),
                              bounded_u32(static_cast<uint64_t>(snapshot.usStackHighWaterMark) * sizeof(StackType_t))};
      if (!format_task(lease.scratch->line, LINE_CAPACITY, summary.sample_id, record)) {
        summary.task_complete = false;
        summary.task_error = TaskError::FORMAT_ERROR;
        break;
      }
      ESP_LOGI("oq_lab", "%s", lease.scratch->line);
    }
#endif
  }
  summary.failures = detail::failure_snapshot();
  // The commit follows all task records and uses the existing bounded logger
  // buffer directly. No stack/internal fallback scratch is allocated when the
  // PSRAM allocation is unavailable. All fields are copied scalars.
  ESP_LOGI("oq_lab", SUMMARY_FORMAT, static_cast<unsigned>(summary.sample_id), static_cast<unsigned>(summary.uptime_ms),
           static_cast<unsigned>(summary.failures.count), static_cast<unsigned>(summary.failures.largest_size),
           static_cast<unsigned>(summary.failures.largest_caps), static_cast<unsigned>(summary.failures.last_caps),
           summary.hook_registered ? "true" : "false", summary.trace_enabled ? "true" : "false",
           static_cast<unsigned>(summary.task_count), static_cast<unsigned>(summary.task_capacity),
           summary.task_complete ? "true" : "false", static_cast<unsigned>(summary.task_error),
           static_cast<unsigned>(summary.snapshot_us), static_cast<unsigned>(summary.scratch_psram_bytes));
}

// Deliberate negative test, never called by the normal diagnostics package.
// A separate negative-test command can invoke this once, then require the
// monotonic allocation-failure counter to increase. Cumulative failures remain
// visible; the session cannot silently return to a healthy result afterwards.
inline bool inject_allocation_failure_for_test() {
  if (xPortInIsrContext()) return false;
  portENTER_CRITICAL_SAFE(&detail::probe_mux);
  const bool hook_registered = detail::hook_registered;
  portEXIT_CRITICAL_SAFE(&detail::probe_mux);
  if (!hook_registered) {
    ESP_LOGE("oq_lab", "OQ_LAB_ALLOCATION_INJECTION {\"schema\":1,\"accepted\":false,\"failed\":false}");
    return false;
  }
  constexpr size_t requested_size = 64U * 1024U * 1024U;
  constexpr uint32_t caps = MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT;
  void* allocation = heap_caps_malloc(requested_size, caps);
  const bool failed = allocation == nullptr;
  if (allocation != nullptr) heap_caps_free(allocation);
  ESP_LOGI("oq_lab", "OQ_LAB_ALLOCATION_INJECTION {\"schema\":1,\"requested_size\":%u,\"caps\":%u,\"failed\":%s}",
           static_cast<unsigned>(requested_size), static_cast<unsigned>(caps), failed ? "true" : "false");
  return failed;
}

}  // namespace oq_lab_diagnostics

#endif  // ESP_PLATFORM
