"""Local GPIO-flow fixture control and strict source-evidence validation.

The generated GPIO counters describe the source. Only the real Controller Flow
and Flow average (Selected) sensors prove the production pulse_meter input path.
"""
from __future__ import annotations

from collections import deque
import json
import math
import threading

PROFILE = "oq-local-flow-v1"
FIELDS = {
    "controller_flow_lph": ("sensor", "Controller Flow"),
    "selected_flow_lph": ("sensor", "Flow average (Selected)"),
    "local_flow_lph": ("number", "Lab Local Flow"),
    "local_flow_enabled": ("switch", "Lab Local Flow Enabled"),
    "flow_source": ("select", "Flow Source"),
    "q_flow_source": ("select", "Q Flow Source"),
    "flow_meter": ("select", "Controller Flow Meter"),
    "flow_control_mode": ("select", "Flow Control Mode"),
    "flow_setpoint_lph": ("number", "Flow Setpoint"),
}
UINT32_MAX = 0xFFFFFFFF


def _flow(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 3000:
        raise ValueError("Local flow must be finite and within 0..3000 L/h")
    if value > 0 and 1500000.0 / value > UINT32_MAX:
        raise ValueError("Local flow period exceeds the fixture counter range")
    return float(value)


class FlowPeer:
    """Cache only acknowledged controller writes, under its existing RLock."""
    def __init__(self, controller):
        self.controller = controller
        self.configured = False
        self.flow_lph = None
        self.nominal_flow_lph = None
        self.enabled = None

    def _fail_closed(self):
        self.configured = False
        self.flow_lph = None
        self.nominal_flow_lph = None
        self.enabled = None
        try:
            self.controller.switch("Lab Local Flow Enabled", False)
            self.enabled = False
        except Exception:
            # Preserve the original failure. Unknown acknowledgement remains
            # unknown; a retry must perform complete OFF/configure/ON setup.
            pass

    def configure(self, environment=None):
        flow = _flow(800 if environment is None else environment["flow_lph"])
        with self.controller.lock:
            self.configured = False
            try:
                self.controller.switch("Lab Local Flow Enabled", False)
                self.enabled = False
                # Flow Source has only Outdoor unit/CIC. Q Flow Source=Local
                # selects the physical controller meter within that first route.
                self.controller.select("Flow Source", "Outdoor unit")
                self.controller.select("Q Flow Source", "Local")
                self.controller.select("Controller Flow Meter", "Huba Control")
                self.controller.select("Flow Control Mode", "Flow Setpoint")
                self.controller.number("Flow Setpoint", 800)
                # Enabled gates the sensor independently from the hydraulics.
                # Pump-off startup must not synthesize nominal circuit flow.
                self.controller.number("Lab Local Flow", 0)
                self.flow_lph = 0.0
                self.nominal_flow_lph = flow
                self.controller.switch("Lab Local Flow Enabled", True)
                self.enabled = True
                self.configured = True
            except Exception:
                self._fail_closed()
                raise

    def apply(self, environment):
        flow = _flow(environment["flow_lph"])
        with self.controller.lock:
            if not self.configured:
                raise ValueError("Local flow fixture has not been configured")
            self.nominal_flow_lph = flow

    def sync_hydraulics(self, actual_lph):
        if type(actual_lph) not in (int, float) or not math.isfinite(actual_lph) or not 0 <= actual_lph <= 3000:
            raise ValueError("Actual hydraulic flow must be finite and within 0..3000 L/h")
        # The fixture number exposes 0.1 L/h steps. Quantizing before pulse-period
        # validation also makes an arbitrarily small coast-down settle at zero.
        flow = _flow(round(actual_lph, 1))
        with self.controller.lock:
            if not self.configured:
                raise ValueError("Local flow fixture has not been configured")
            if self.flow_lph == flow:
                return
            try:
                self.controller.number("Lab Local Flow", flow)
                self.flow_lph = flow
            except Exception:
                self._fail_closed()
                raise

    def set_enabled(self, enabled):
        if type(enabled) is not bool:
            raise ValueError("Local flow enabled must be boolean")
        with self.controller.lock:
            if enabled and not self.configured:
                raise ValueError("Configure the local flow fixture before enabling it")
            if self.enabled is enabled:
                return
            try:
                self.controller.switch("Lab Local Flow Enabled", enabled)
                self.enabled = enabled
            except Exception:
                self._fail_closed()
                raise


def _unsigned(value, field):
    if type(value) is not int or not 0 <= value <= UINT32_MAX:
        raise ValueError("Invalid local flow field: " + field)
    return value


def validate_marker(value, previous=None):
    """Validate a committed source state; return a detached known-fields dict."""
    if not isinstance(value, dict) or type(value.get("schema")) is not int or value["schema"] != 1:
        raise ValueError("Unsupported local flow schema")
    if value.get("profile") != PROFILE or type(value.get("pin")) is not int or value["pin"] != 15:
        raise ValueError("Unexpected local flow profile or GPIO")
    sample = {"schema": 1, "profile": PROFILE, "pin": 15}
    for field in ("uptime_ms", "generation", "requested_generation", "half_period_us",
                  "generated_rising_edges", "generated_falling_edges", "read_mismatches", "driver_error"):
        sample[field] = _unsigned(value.get(field), field)
    for field in ("enabled", "active", "applied_enabled"):
        if type(value.get(field)) is not bool:
            raise ValueError("Invalid local flow boolean: " + field)
        sample[field] = value[field]
    for field in ("requested_flow_lph", "applied_flow_lph"):
        sample[field] = _flow(value.get(field))
    if type(value.get("gpio_level")) is not int or value["gpio_level"] not in (0, 1):
        raise ValueError("Invalid local flow GPIO level")
    sample["gpio_level"] = value["gpio_level"]
    if sample["driver_error"] or sample["read_mismatches"]:
        raise ValueError("Local flow driver or GPIO readback failed")
    if sample["requested_generation"] < sample["generation"]:
        raise ValueError("Local flow applied generation is ahead of its intent")
    flow = sample["applied_flow_lph"]
    expected_period = 1500000.0 / flow if flow > 0 else 0
    if abs(sample["half_period_us"] - expected_period) > max(1, expected_period * 2e-7):
        raise ValueError("Local flow period does not match the applied generation")
    expected_active = sample["applied_enabled"] and flow > 0
    if sample["active"] != expected_active or (not sample["active"] and sample["gpio_level"] != 0):
        raise ValueError("Local flow source gate or idle level is inconsistent")
    if sample["requested_generation"] == sample["generation"] and (
            sample["requested_flow_lph"] != flow or sample["enabled"] != sample["applied_enabled"]):
        raise ValueError("Local flow committed intent differs from its applied state")
    rising, falling = sample["generated_rising_edges"], sample["generated_falling_edges"]
    if falling > rising or rising - falling > 1:
        raise ValueError("Local flow edge phases are inconsistent")
    if previous is not None:
        old = validate_marker(previous)
        for field in ("uptime_ms", "generation", "requested_generation", "generated_rising_edges", "generated_falling_edges"):
            if sample[field] < old[field]:
                raise ValueError("Local flow telemetry moved backwards: " + field)
        if sample["uptime_ms"] == old["uptime_ms"] and sample["generation"] == old["generation"]:
            raise ValueError("Local flow telemetry did not advance")
        if sample["generation"] == old["generation"]:
            for field in ("applied_flow_lph", "applied_enabled", "half_period_us"):
                if sample[field] != old[field]:
                    raise ValueError("Local flow settings changed without a generation commit")
            delta = rising - old["generated_rising_edges"]
            elapsed_us = (sample["uptime_ms"] - old["uptime_ms"]) * 1000
            if sample["active"] and elapsed_us >= 2 * sample["half_period_us"] and delta == 0:
                raise ValueError("Active local flow source stopped generating edges")
            if not sample["active"] and (delta or falling != old["generated_falling_edges"]):
                raise ValueError("Disabled local flow source generated edges")
    return sample


class FlowEvidence:
    """Bounded, thread-safe marker history; every accepted value is detached."""
    def __init__(self):
        self.lock = threading.RLock()
        self.samples = deque(maxlen=3600)
        self.pending = None

    def accept(self, value, phase=None, wall_seconds=None):
        with self.lock:
            sample = validate_marker(value, self.samples[-1] if self.samples else None)
            sample["phase"] = phase
            if wall_seconds is not None:
                if type(wall_seconds) not in (int, float) or not math.isfinite(wall_seconds) or wall_seconds < 0:
                    raise ValueError("Invalid local flow wall clock")
                sample["wall_seconds"] = round(wall_seconds, 3)
            if sample["requested_generation"] > sample["generation"]:
                pending = self.pending if self.pending and self.pending[0] == sample["generation"] else (sample["generation"], sample["uptime_ms"])
                if sample["uptime_ms"] - pending[1] > 2000:
                    raise ValueError("Local flow intent was not committed within 2s")
                self.pending = pending
            else:
                self.pending = None
            self.samples.append(sample)
            return dict(sample)

    def latest(self):
        with self.lock:
            return dict(self.samples[-1]) if self.samples else None

    def state(self):
        with self.lock:
            return [dict(sample) for sample in self.samples]
