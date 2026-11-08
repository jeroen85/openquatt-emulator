#pragma once

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
#define OQ_ODU_NOEXCEPT noexcept
extern "C" {
#else
#define OQ_ODU_NOEXCEPT
#endif

// Profiles: 0 Disabled, 1 V1, 2 V1.5, 3 V2 old, 4 V2 new.
// Mutators/readers return 1 on success and 0 on invalid input. The caller owns
// synchronization; one model must never be used concurrently without a lock.
void* oq_odu_create(int profile, uint32_t now_ms) OQ_ODU_NOEXCEPT;
void oq_odu_destroy(void* handle) OQ_ODU_NOEXCEPT;
int oq_odu_configure(void* handle, int profile, uint32_t now_ms) OQ_ODU_NOEXCEPT;
int oq_odu_advance(void* handle, uint32_t now_ms) OQ_ODU_NOEXCEPT;
int oq_odu_read(void* handle, uint16_t address, uint16_t* value) OQ_ODU_NOEXCEPT;
int oq_odu_can_write(void* handle, uint16_t address, uint16_t value) OQ_ODU_NOEXCEPT;
int oq_odu_write(void* handle, uint16_t address, uint16_t value, uint32_t now_ms) OQ_ODU_NOEXCEPT;
int oq_odu_write_many(void* handle, uint16_t start, const uint16_t* values, uint16_t count,
                      uint32_t now_ms) OQ_ODU_NOEXCEPT;
// Call once for a CRC-valid bus frame, before address/range validation or reply
// fault injection. Wrong-address receipts advance time but never feed this ODU.
void oq_odu_request(void* handle, uint8_t unit, uint8_t requested, uint8_t function, uint16_t start, uint16_t count,
                    uint32_t now_ms, int response_dropped) OQ_ODU_NOEXCEPT;
// target_flow is the caller's shared circuit target in L/h, derived from
// received pump commands by OduFleet. This is no calibrated pump/PWM curve.
int oq_odu_set_environment(void* handle, double outdoor, double inlet, double target_flow, int no_flow, int defrost,
                           uint16_t fault) OQ_ODU_NOEXCEPT;
// Couple HP1 outlet to HP2 inlet and synchronize series hydraulic state. Both
// models must already have the same firmware clock; relay commands stay separate.
int oq_odu_link_series(void* upstream, void* downstream) OQ_ODU_NOEXCEPT;
// Explicit fault fixture for low-command frequency responses. Zero disables
// injection. The requested table/registers stay unchanged; thermodynamics uses
// the injected measured frequency. These are test phases, not ODU firmware.
int oq_odu_set_response(void* handle, float hz20, float hz26, float ramp) OQ_ODU_NOEXCEPT;
// Returns JSON byte count excluding NUL, or -1. On failure a nonempty buffer is
// cleared rather than exposing truncated JSON. power_w is thermal power.
int oq_odu_snapshot(void* handle, char* buffer, size_t capacity) OQ_ODU_NOEXCEPT;

#ifdef __cplusplus
}
#endif
#undef OQ_ODU_NOEXCEPT
