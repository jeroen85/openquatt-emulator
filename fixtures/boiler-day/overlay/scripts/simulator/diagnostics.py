"""Fail-closed collection of committed firmware task/allocator snapshots."""
from __future__ import annotations

import json
import threading

PROFILE = "oq-lab-diagnostics-v1"


def unsigned(value, name, maximum=0xFFFFFFFF):
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError("Invalid diagnostics field: " + name)
    return value


class Diagnostics:
    def __init__(self):
        self.lock = threading.RLock()
        self.pending_id = None
        self.pending_phase = None
        self.pending = {}
        self.samples = []
        self.last_id = -1
        self.last_uptime = -1

    def accept(self, marker, value, phase, wall_seconds):
        if not isinstance(value, dict) or type(value.get("schema")) is not int or value["schema"] != 1:
            raise ValueError("Unsupported diagnostics schema")
        sample_id = unsigned(value.get("sample_id"), "sample_id")
        with self.lock:
            if sample_id <= self.last_id:
                raise ValueError("Diagnostics sample ID did not advance")
            if self.pending_id is not None and sample_id != self.pending_id:
                raise ValueError("Uncommitted diagnostics task batch")
            if marker == "OQ_LAB_TASK":
                if self.pending_id is None:
                    self.pending_phase = phase
                self.pending_id = sample_id
                task = {key: unsigned(value.get(key), key) for key in
                        ("task_id", "priority", "state", "stack_hwm_bytes")}
                if task["task_id"] in self.pending or len(self.pending) >= 64:
                    raise ValueError("Duplicate task or diagnostics capacity exceeded")
                if task["stack_hwm_bytes"] == 0:
                    raise ValueError("Task stack high-watermark is zero")
                self.pending[task["task_id"]] = task
                return None
            if marker != "OQ_LAB_DIAGNOSTICS":
                raise ValueError("Unknown diagnostics marker")
            sample = {key: unsigned(value.get(key), key) for key in
                      ("uptime_ms", "allocation_failures", "largest_failed_size", "largest_failed_caps",
                       "last_failed_caps", "task_count", "task_capacity", "task_error", "snapshot_us", "scratch_psram_bytes")}
            if value.get("profile") != PROFILE:
                raise ValueError("Unexpected diagnostics profile")
            if value.get("hook_registered") is not True or value.get("trace_enabled") is not True:
                raise ValueError("Allocator hook or task trace is unavailable")
            if value.get("task_complete") is not True or sample["task_error"]:
                raise ValueError("Firmware task snapshot is incomplete")
            if sample["task_capacity"] != 64 or not 0 < sample["task_count"] < 64:
                raise ValueError("Invalid or saturated firmware task snapshot")
            if sample["task_count"] != len(self.pending):
                raise ValueError("Firmware task snapshot lost records")
            if sample["scratch_psram_bytes"] == 0 or sample["uptime_ms"] <= self.last_uptime:
                raise ValueError("Missing PSRAM scratch or nonmonotonic diagnostics clock")
            sample.update(schema=1, profile=PROFILE, sample_id=sample_id, phase=self.pending_phase,
                          wall_seconds=round(wall_seconds, 3), hook_registered=True, trace_enabled=True, task_complete=True, tasks=list(self.pending.values()))
            self.samples.append(sample)
            self.last_id, self.last_uptime = sample_id, sample["uptime_ms"]
            self.pending_id, self.pending_phase, self.pending = None, None, {}
            if sample["allocation_failures"]:
                raise ValueError(f"Firmware allocation failure: count={sample['allocation_failures']}, size={sample['largest_failed_size']}, caps={sample['largest_failed_caps']}")
            return dict(sample)

    def latest(self):
        with self.lock:
            return json.loads(json.dumps(self.samples[-1])) if self.samples else None

    def state(self):
        with self.lock:
            # JSON roundtrip also prevents callers mutating nested task dictionaries.
            return json.loads(json.dumps(self.samples)), self.pending_id is not None

    def summary(self):
        samples, _ = self.state()
        if not samples:
            return None
        minima = {}
        for sample in samples:
            for task in sample["tasks"]:
                task_id = task["task_id"]
                if task_id not in minima or task["stack_hwm_bytes"] < minima[task_id]["stack_hwm_bytes"]:
                    minima[task_id] = dict(task)
        return {"profile": PROFILE, "sample_count": len(samples), "allocation_failures": max(s["allocation_failures"] for s in samples),
                "min_task_stack_hwm_bytes": min(t["stack_hwm_bytes"] for t in minima.values()),
                "max_snapshot_us": max(s["snapshot_us"] for s in samples),
                "scratch_psram_bytes": max(s["scratch_psram_bytes"] for s in samples),
                "tasks": sorted(minima.values(), key=lambda task: task["stack_hwm_bytes"])}
