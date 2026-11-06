#pragma once

#include <atomic>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <type_traits>
#include <limits>

namespace oq_boot_memory {

constexpr uint32_t CAPACITY = 96;
constexpr uint32_t TASK_CAPACITY = 64;
constexpr uint32_t INTERVAL_MS = 1000;
constexpr uint32_t SAMPLE_PAGE_LIMIT = 8;
constexpr uint32_t TASK_PAGE_LIMIT = 16;
constexpr size_t HTTP_LINE_BYTES = 2048;
constexpr uint32_t NO_SLOT = std::numeric_limits<uint32_t>::max();

enum class TaskError : uint32_t {
  OK = 0,
  TRACE_DISABLED = 1,
  SCRATCH_UNAVAILABLE = 2,
  NOT_INITIALIZED = 3,
  CAPACITY_OR_SNAPSHOT_FAILURE = 4,
  PROBE_BUSY = 5,
};

struct TaskRecord {
  uint32_t task_id{0}, priority{0}, state{0}, stack_hwm_bytes{0};
};

struct Sample {
  uint32_t uptime_ms{0};
  uint32_t internal_free{0}, internal_min_free{0}, internal_largest_block{0}, psram_free{0};
  uint32_t main_stack_hwm_bytes{0};
  uint32_t allocation_failures{0}, largest_failed_size{0}, largest_failed_caps{0}, last_failed_caps{0};
  uint32_t task_count{0}, copied_task_count{0}, snapshot_us{0}, capture_us{0}, scratch_psram_bytes{0};
  TaskError task_error{TaskError::NOT_INITIALIZED};
  bool heap_integrity{false}, hook_registered{false}, trace_enabled{false}, task_complete{false};
  // Publication completeness is separate from measurement validity. A fully
  // captured failed/incomplete diagnostic is evidence and must remain visible.
  bool build_complete{false};
  TaskRecord tasks[TASK_CAPACITY]{};
};

inline bool valid_commit(const Sample& sample) {
  if (!sample.build_complete || sample.task_count > TASK_CAPACITY || sample.copied_task_count > sample.task_count)
    return false;
  if (sample.task_complete)
    return sample.trace_enabled && sample.task_error == TaskError::OK && sample.task_count > 0 &&
           sample.task_count < TASK_CAPACITY && sample.copied_task_count == sample.task_count;
  return sample.task_error != TaskError::OK;
}

struct Page {
  bool valid{false};
  uint32_t begin{0}, end{0}, next{0};
};

inline Page page(uint32_t available, uint32_t snapshot, uint32_t offset, uint32_t limit, uint32_t maximum) {
  if (snapshot > available || offset > snapshot || limit == 0 || limit > maximum) return {};
  const uint32_t count = limit < snapshot - offset ? limit : snapshot - offset;
  return {true, offset, offset + count, offset + count};
}

inline bool parse_index(const char* raw, uint32_t maximum, uint32_t& value) {
  if (raw == nullptr || *raw == '\0') return false;
  uint32_t result = 0;
  size_t digits = 0;
  for (const char* cursor = raw; *cursor != '\0'; ++cursor) {
    if (++digits > 10 || *cursor < '0' || *cursor > '9') return false;
    const uint32_t digit = static_cast<uint32_t>(*cursor - '0');
    if (result > maximum / 10 || (result == maximum / 10 && digit > maximum % 10)) return false;
    result = result * 10 + digit;
  }
  value = result;
  return true;
}

// Both the ESPHome buffer and host fake implement this narrow ownership API.
// A broken allocator claiming success with internal storage is rejected too.
template <typename Buffer>
inline bool allocate_external_only(Buffer& buffer, size_t count) {
  if (count == 0 || !buffer.allocate_external(count)) return false;
  if (buffer.data() == nullptr || !buffer.is_external() || buffer.size() != count) {
    buffer.release();
    return false;
  }
  return true;
}

template <uint32_t Count = CAPACITY>
class Cache {
 public:
  struct Draft {
    Sample* sample{nullptr};
    uint32_t index{NO_SLOT};
  };

  // Bind exactly once, before publishing the component to readers. Storage
  // outlives every HTTP callback and is never released during shutdown/OTA.
  bool bind(Sample* records) {
    if (records == nullptr || records_ != nullptr || frozen_.load(std::memory_order_acquire)) return false;
    records_ = records;
    return true;
  }

  uint32_t committed() const { return committed_.load(std::memory_order_acquire); }
  bool frozen() const { return frozen_.load(std::memory_order_acquire); }
  void freeze() { frozen_.store(true, std::memory_order_release); }

  Draft begin() {
    if (records_ == nullptr || frozen() || writer_.test_and_set(std::memory_order_acquire)) return {};
    const uint32_t index = committed();
    if (index >= Count || frozen()) {
      writer_.clear(std::memory_order_release);
      return {};
    }
    pending_ = index;
    static_assert(std::is_trivially_copyable<Sample>::value, "Cache records must remain scalar-only");
    // Clear in PSRAM rather than materializing a full Sample on the callback stack.
    std::memset(static_cast<void*>(&records_[index]), 0, sizeof(Sample));
    records_[index].task_error = TaskError::NOT_INITIALIZED;
    return {&records_[index], index};
  }

  bool commit(uint32_t index) {
    if (index == NO_SLOT || index != pending_ || index != committed() || frozen() || !valid_commit(records_[index]))
      return false;
    // All scalar/task bytes precede this release; readers acquire the count
    // once and can only access immutable records below their snapshot boundary.
    committed_.store(index + 1, std::memory_order_release);
    pending_ = NO_SLOT;
    writer_.clear(std::memory_order_release);
    return true;
  }

  bool abort(uint32_t index) {
    if (index == NO_SLOT || index != pending_) return false;
    pending_ = NO_SLOT;
    writer_.clear(std::memory_order_release);
    return true;
  }

  const Sample* at(uint32_t index, uint32_t snapshot) const {
    const uint32_t available = committed();
    return records_ != nullptr && snapshot <= available && index < snapshot ? &records_[index] : nullptr;
  }

 private:
  Sample* records_{nullptr};
  std::atomic<uint32_t> committed_{0};
  std::atomic<bool> frozen_{false};
  std::atomic_flag writer_ = ATOMIC_FLAG_INIT;
  uint32_t pending_{NO_SLOT};  // Main-loop writer only; HTTP never touches drafts.
};

}  // namespace oq_boot_memory
