#include "boot_memory_probe.h"

#include <cstdarg>
#include <cstdio>
#include <cstring>
#include <new>
#include <sys/socket.h>

namespace esphome::boot_memory_probe {
namespace {
constexpr const char* PATH = "/openquatt/lab/boot-memory";
constexpr const char* TASK_PATH = "/openquatt/lab/boot-memory/tasks";
const char* json_bool(bool value) { return value ? "true" : "false"; }
void abort_response(httpd_req_t* request) {
  // Abort synchronously while this request still owns the descriptor. A queued
  // bare-fd close could run after HTTPD reused that descriptor for another client.
  const int fd = httpd_req_to_sockfd(request);
  if (fd >= 0) (void)shutdown(fd, SHUT_RDWR);
}
void error_response(AsyncWebServerRequest* request, const char* status, const char* body) {
  // ESPHome's integer adapter maps 405/416/503 to 500; use native IDF status strings.
  httpd_resp_set_status(*request, status);
  httpd_resp_set_type(*request, "application/json; charset=utf-8");
  httpd_resp_set_hdr(*request, "Cache-Control", "no-store");
  if (httpd_resp_send(*request, body, HTTPD_RESP_USE_STRLEN) != ESP_OK) abort_response(*request);
}

struct HttpLease {
  std::atomic_flag& flag;
  explicit HttpLease(std::atomic_flag& value) : flag(value) {}
  ~HttpLease() { flag.clear(std::memory_order_release); }
};
}  // namespace

void BootMemoryProbe::setup() {
  const int64_t started = esp_timer_get_time();
  metadata_.setup_uptime_ms = static_cast<uint32_t>(started / 1000);
  metadata_.boot_nonce = esp_random();
  metadata_.reset_reason = static_cast<uint32_t>(esp_reset_reason());
  multi_heap_info_t before{};
  heap_caps_get_info(&before, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
  metadata_.pre_internal_free = before.total_free_bytes;
  metadata_.pre_internal_min_free = before.minimum_free_bytes;
  metadata_.pre_largest_block = before.largest_free_block;
  metadata_.pre_psram_free = heap_caps_get_free_size(MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
  const auto* descriptor = esp_app_get_description();
  static constexpr char HEX[] = "0123456789abcdef";
  for (size_t index = 0; index < 32; ++index) {
    metadata_.elf_sha256[index * 2] = HEX[descriptor->app_elf_sha256[index] >> 4];
    metadata_.elf_sha256[index * 2 + 1] = HEX[descriptor->app_elf_sha256[index] & 15];
  }
  const auto* running = esp_ota_get_running_partition();
  const auto* configured = esp_ota_get_boot_partition();
  if (configured != nullptr) metadata_.configured_boot_partition_address = configured->address;
  if (running != nullptr) {
    metadata_.running_partition_address = running->address;
    metadata_.running_partition_subtype = static_cast<uint32_t>(running->subtype);
    esp_ota_img_states_t image_state{};
    metadata_.ota_state_error = esp_ota_get_state_partition(running, &image_state);
    if (metadata_.ota_state_error == ESP_OK) metadata_.ota_image_state = static_cast<int32_t>(image_state);
  }
  if (oq_boot_memory::allocate_external_only(storage_, 1)) {
    new (storage_.data()) Storage{};
    cache_.bind(storage_.data()->samples);
    capture_();  // Priority 850: after diagnostics initialization, before WiFi/network setup.
  } else {
    this->status_set_error(LOG_STR("boot memory PSRAM allocation failed; no internal fallback"));
  }
  metadata_.setup_probe_us = oq_lab_diagnostics::bounded_u32(esp_timer_get_time() - started);
  ready_.store(true, std::memory_order_release);
  auto* server = web_server_base::global_web_server_base;
  if (server != nullptr) server->add_handler(this);  // Existing authentication middleware, never a second listener.
}

void BootMemoryProbe::capture_() {
  auto draft = cache_.begin();
  if (draft.sample == nullptr) return;
  auto& sample = *draft.sample;
  const int64_t started = esp_timer_get_time();
  sample.uptime_ms = static_cast<uint32_t>(started / 1000);
  multi_heap_info_t heap{};
  heap_caps_get_info(&heap, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
  sample.internal_free = heap.total_free_bytes;
  sample.internal_min_free = heap.minimum_free_bytes;
  sample.internal_largest_block = heap.largest_free_block;
  sample.psram_free = heap_caps_get_free_size(MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
  sample.main_stack_hwm_bytes = uxTaskGetStackHighWaterMark(nullptr);
  sample.heap_integrity = heap_caps_check_integrity_all(false);
  // Reuse the diagnostics profile's single allocation hook and serialized PSRAM
  // task scratch. Never register a competing hook or allocate task scratch here.
  oq_lab_diagnostics::detail::ProbeLease lease(false);
  sample.hook_registered = lease.hook_registered;
#if configUSE_TRACE_FACILITY == 1
  sample.trace_enabled = true;
#endif
  if (!lease.acquired) {
    sample.task_error = oq_boot_memory::TaskError::PROBE_BUSY;
  } else if (!lease.initialized) {
    sample.task_error = oq_boot_memory::TaskError::NOT_INITIALIZED;
  } else if (!sample.trace_enabled) {
    sample.task_error = oq_boot_memory::TaskError::TRACE_DISABLED;
  } else if (lease.scratch == nullptr) {
    sample.task_error = oq_boot_memory::TaskError::SCRATCH_UNAVAILABLE;
  } else {
#if configUSE_TRACE_FACILITY == 1
    const int64_t task_started = esp_timer_get_time();
    sample.task_count = uxTaskGetSystemState(lease.scratch->tasks, oq_boot_memory::TASK_CAPACITY, nullptr);
    sample.snapshot_us = oq_lab_diagnostics::bounded_u32(esp_timer_get_time() - task_started);
    sample.scratch_psram_bytes = sizeof(oq_lab_diagnostics::detail::ProbeScratch);
    sample.task_complete =
        oq_lab_diagnostics::snapshot_complete(true, true, true, sample.task_count, oq_boot_memory::TASK_CAPACITY);
    sample.task_error =
        sample.task_complete ? oq_boot_memory::TaskError::OK : oq_boot_memory::TaskError::CAPACITY_OR_SNAPSHOT_FAILURE;
    for (uint32_t index = 0; index < sample.task_count && index < oq_boot_memory::TASK_CAPACITY; ++index) {
      const TaskStatus_t& task = lease.scratch->tasks[index];
      // Only kernel-copied scalars: task names, handles and stack pointers can
      // expire immediately after uxTaskGetSystemState releases the kernel lock.
      sample.tasks[index] = {
          static_cast<uint32_t>(task.xTaskNumber), static_cast<uint32_t>(task.uxCurrentPriority),
          static_cast<uint32_t>(task.eCurrentState),
          oq_lab_diagnostics::bounded_u32(static_cast<uint64_t>(task.usStackHighWaterMark) * sizeof(StackType_t))};
      ++sample.copied_task_count;
    }
#endif
  }
  const auto failures = oq_lab_diagnostics::detail::failure_snapshot();
  sample.allocation_failures = failures.count;
  sample.largest_failed_size = failures.largest_size;
  sample.largest_failed_caps = failures.largest_caps;
  sample.last_failed_caps = failures.last_caps;
  sample.capture_us = oq_lab_diagnostics::bounded_u32(esp_timer_get_time() - started);
  sample.build_complete = true;
  if (!cache_.commit(draft.index)) cache_.abort(draft.index);
  last_capture_ms_ = sample.uptime_ms;
}

void BootMemoryProbe::loop() {
  if (!storage_ || cache_.frozen() || cache_.committed() >= oq_boot_memory::CAPACITY) {
    this->disable_loop();
    return;
  }
  const uint32_t now = static_cast<uint32_t>(esp_timer_get_time() / 1000);
  if (static_cast<uint32_t>(now - last_capture_ms_) >= oq_boot_memory::INTERVAL_MS) capture_();
}

void BootMemoryProbe::on_shutdown() {
  cache_.freeze();
  // Intentionally no free/reset: in-flight HTTP readers retain immutable data
  // through OTA/shutdown. A new application boot constructs a new empty cache.
  this->disable_loop();
}

bool BootMemoryProbe::canHandle(AsyncWebServerRequest* request) const {
  char url[AsyncWebServerRequest::URL_BUF_SIZE];
  request->url_to(url);
  return std::strcmp(url, PATH) == 0 || std::strcmp(url, TASK_PATH) == 0;
}

bool BootMemoryProbe::query_(AsyncWebServerRequest* request, const char* key, uint32_t maximum, uint32_t fallback,
                             uint32_t& value) {
  if (!request->hasArg(key)) {
    value = fallback;
    return true;
  }
  const auto raw = request->arg(key);
  return oq_boot_memory::parse_index(raw.c_str(), maximum, value);
}

bool BootMemoryProbe::send_(httpd_req_t* request, const char* format, ...) {
  char* line = storage_.data()->http_line;
  va_list args;
  va_start(args, format);
  const int length = std::vsnprintf(line, oq_boot_memory::HTTP_LINE_BYTES, format, args);
  va_end(args);
  return length >= 0 && static_cast<size_t>(length) < oq_boot_memory::HTTP_LINE_BYTES &&
         httpd_resp_send_chunk(request, line, length) == ESP_OK;
}

bool BootMemoryProbe::metadata_json_(httpd_req_t* request, uint32_t available, uint32_t snapshot) {
  const auto& meta = metadata_;
  return send_(
      request,
      "{\"schema\":1,\"profile\":\"oq-boot-memory-v1\",\"instrumentation_schema\":\"oq-lab-diagnostics-v1\","
      "\"boot_nonce\":%u,\"reset_reason\":%u,\"elf_sha256\":\"%s\",\"setup_uptime_ms\":%u,\"uptime_ms_now\":%llu,"
      "\"running_partition_address\":%u,\"running_partition_subtype\":%u,\"configured_boot_partition_address\":%u,"
      "\"ota_image_state\":%ld,\"ota_state_error\":%ld,\"first_boot_after_ota_verified\":false,"
      "\"capacity\":%u,\"committed_count\":%u,\"snapshot_count\":%u,\"complete\":%s,\"frozen\":%s,"
      "\"interval_ms\":%u,\"cache_psram_bytes\":%u,\"http_scratch_psram_bytes\":%u,\"component_bytes\":%u,"
      "\"setup_probe_us\":%u,\"pre_probe\":{\"internal_free\":%u,\"internal_min_free\":%u,"
      "\"internal_largest_block\":%u,\"psram_free\":%u}",
      meta.boot_nonce, meta.reset_reason, meta.elf_sha256, meta.setup_uptime_ms,
      static_cast<unsigned long long>(esp_timer_get_time() / 1000), meta.running_partition_address,
      meta.running_partition_subtype, meta.configured_boot_partition_address, static_cast<long>(meta.ota_image_state),
      static_cast<long>(meta.ota_state_error), oq_boot_memory::CAPACITY, available, snapshot,
      json_bool(available == oq_boot_memory::CAPACITY), json_bool(cache_.frozen()), oq_boot_memory::INTERVAL_MS,
      static_cast<unsigned>(sizeof(Storage)), static_cast<unsigned>(oq_boot_memory::HTTP_LINE_BYTES),
      static_cast<unsigned>(sizeof(*this)), meta.setup_probe_us, meta.pre_internal_free, meta.pre_internal_min_free,
      meta.pre_largest_block, meta.pre_psram_free);
}

bool BootMemoryProbe::sample_json_(httpd_req_t* request, uint32_t index, const oq_boot_memory::Sample& sample) {
  uint32_t minimum_stack = 0;
  for (uint32_t task = 0; task < sample.copied_task_count; ++task)
    if (task == 0 || sample.tasks[task].stack_hwm_bytes < minimum_stack)
      minimum_stack = sample.tasks[task].stack_hwm_bytes;
  return send_(
      request,
      "{\"index\":%u,\"uptime_ms\":%u,\"internal_free\":%u,\"internal_min_free\":%u,"
      "\"internal_largest_block\":%u,\"psram_free\":%u,\"main_stack_hwm_bytes\":%u,\"heap_integrity\":%s,"
      "\"allocation_failures\":%u,\"largest_failed_size\":%u,\"largest_failed_caps\":%u,\"last_failed_caps\":%u,"
      "\"hook_registered\":%s,\"trace_enabled\":%s,\"task_count\":%u,\"copied_task_count\":%u,"
      "\"task_complete\":%s,\"task_error\":%u,\"min_task_stack_hwm_bytes\":%u,"
      "\"snapshot_us\":%u,\"capture_us\":%u,\"shared_scratch_psram_bytes\":%u}",
      index, sample.uptime_ms, sample.internal_free, sample.internal_min_free, sample.internal_largest_block,
      sample.psram_free, sample.main_stack_hwm_bytes, json_bool(sample.heap_integrity), sample.allocation_failures,
      sample.largest_failed_size, sample.largest_failed_caps, sample.last_failed_caps,
      json_bool(sample.hook_registered), json_bool(sample.trace_enabled), sample.task_count, sample.copied_task_count,
      json_bool(sample.task_complete), static_cast<uint32_t>(sample.task_error), minimum_stack, sample.snapshot_us,
      sample.capture_us, sample.scratch_psram_bytes);
}

void BootMemoryProbe::handleRequest(AsyncWebServerRequest* request) {
  if (request->method() != HTTP_GET) {
    httpd_resp_set_hdr(*request, "Allow", "GET");
    error_response(request, "405 Method Not Allowed", R"({"error":"read_only"})");
    return;
  }
  if (!ready_.load(std::memory_order_acquire) || !storage_) {
    error_response(request, "503 Service Unavailable", R"({"error":"startup_cache_unavailable_no_internal_fallback"})");
    return;
  }
  auto* server = web_server_base::global_web_server_base;
  if ((server != nullptr && server->is_recovery_active()) || http_busy_.test_and_set(std::memory_order_acquire)) {
    error_response(request, "503 Service Unavailable", R"({"error":"busy_or_recovery"})");
    return;
  }
  HttpLease lease(http_busy_);
  char url[AsyncWebServerRequest::URL_BUF_SIZE];
  request->url_to(url);
  const bool tasks = std::strcmp(url, TASK_PATH) == 0;
  const uint32_t available = cache_.committed();
  uint32_t snapshot, offset, limit, sample_index = 0;
  if (!query_(request, "snapshot", available, available, snapshot) ||
      !query_(request, "offset", tasks ? oq_boot_memory::TASK_CAPACITY : oq_boot_memory::CAPACITY, 0, offset) ||
      !query_(request, "limit", tasks ? oq_boot_memory::TASK_PAGE_LIMIT : oq_boot_memory::SAMPLE_PAGE_LIMIT,
              tasks ? oq_boot_memory::TASK_PAGE_LIMIT : 4, limit) ||
      limit == 0 ||
      (tasks &&
       (!request->hasArg("sample") || !query_(request, "sample", oq_boot_memory::CAPACITY - 1, 0, sample_index)))) {
    error_response(request, "400 Bad Request", R"({"error":"invalid_page"})");
    return;
  }
  const auto* sample = tasks ? cache_.at(sample_index, snapshot) : nullptr;
  const uint32_t rows = tasks ? (sample != nullptr ? sample->copied_task_count : 0) : snapshot;
  const auto selected = oq_boot_memory::page(
      rows, rows, offset, limit, tasks ? oq_boot_memory::TASK_PAGE_LIMIT : oq_boot_memory::SAMPLE_PAGE_LIMIT);
  if (!selected.valid || (tasks && sample == nullptr)) {
    error_response(request, "416 Range Not Satisfiable", R"({"error":"page_out_of_range"})");
    return;
  }
  httpd_req_t* raw = *request;
  httpd_resp_set_type(raw, "application/json; charset=utf-8");
  httpd_resp_set_hdr(raw, "Cache-Control", "no-store");
  bool ok =
      metadata_json_(raw, available, snapshot) && send_(raw, ",\"offset\":%u,\"next_offset\":%u,\"%s\":[",
                                                        selected.begin, selected.next, tasks ? "tasks" : "samples");
  for (uint32_t index = selected.begin; ok && index < selected.end; ++index) {
    if (index > selected.begin) ok = send_(raw, ",");
    if (!ok) break;
    if (tasks) {
      const auto& task = sample->tasks[index];
      ok = send_(raw, "{\"sample\":%u,\"task_id\":%u,\"priority\":%u,\"state\":%u,\"stack_hwm_bytes\":%u}",
                 sample_index, task.task_id, task.priority, task.state, task.stack_hwm_bytes);
    } else {
      ok = sample_json_(raw, index, *cache_.at(index, snapshot));
    }
  }
  if (ok) ok = send_(raw, "]}");
  if (ok) ok = httpd_resp_send_chunk(raw, nullptr, 0) == ESP_OK;
  if (!ok) abort_response(raw);
}

}  // namespace esphome::boot_memory_probe
