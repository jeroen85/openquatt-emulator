"""ctypes adapter to the same pinned portable ODU model as the physical simulator."""
from __future__ import annotations

import ctypes
import json
import math
import threading
from pathlib import Path

PROFILES = {"Disabled": 0, "V1": 1, "V1.5": 2, "V2 old": 3, "V2 new": 4}


def checked_environment(values, base):
    if not isinstance(values, dict):
        raise ValueError("Environment must be a JSON object")
    limits = {"outdoor_c": (-25, 40), "return_c": (0, 60), "room_c": (0, 40),
              "setpoint_c": (5, 35), "flow_lph": (0, 1500)}
    if set(values) - set(base):
        raise ValueError("Unknown environment fields")
    updated = dict(base)
    for key, value in values.items():
        if key == "heating_enable":
            if type(value) is not bool:
                raise ValueError("heating_enable must be a boolean")
        elif type(value) not in (int, float) or not math.isfinite(value) or not limits[key][0] <= value <= limits[key][1]:
            raise ValueError(f"Invalid environment value: {key}")
        updated[key] = value
    return updated


def checked_odu(values):
    if not isinstance(values, dict) or set(values) - {"index", "profile", "online", "no_flow", "defrost", "fault"}:
        raise ValueError("Unknown or invalid ODU fields")
    if type(values.get("index")) is not int or values["index"] not in (0, 1):
        raise ValueError("index must be 0 or 1")
    if "profile" in values and (not isinstance(values["profile"], str) or values["profile"] not in PROFILES):
        raise ValueError("Unknown ODU profile")
    for key in ("online", "no_flow", "defrost"):
        if key in values and type(values[key]) is not bool:
            raise ValueError(f"{key} must be a boolean")
    if "fault" in values and (type(values["fault"]) is not int or not 0 <= values["fault"] <= 65535):
        raise ValueError("fault must be a 16-bit unsigned word")
    return dict(values)


class OduFleet:
    def __init__(self, library: Path):
        self.lock = threading.RLock()
        self.lib = ctypes.CDLL(str(library))
        pointer = ctypes.c_void_p
        self.lib.oq_odu_create.argtypes = [ctypes.c_int, ctypes.c_uint32]
        self.lib.oq_odu_create.restype = pointer
        self.lib.oq_odu_destroy.argtypes = [pointer]
        self.lib.oq_odu_destroy.restype = None
        for name, types in {
            "configure": [pointer, ctypes.c_int, ctypes.c_uint32],
            "advance": [pointer, ctypes.c_uint32],
            "link_series": [pointer, pointer],
            "read": [pointer, ctypes.c_uint16, ctypes.POINTER(ctypes.c_uint16)],
            "can_write": [pointer, ctypes.c_uint16, ctypes.c_uint16],
            "write": [pointer, ctypes.c_uint16, ctypes.c_uint16, ctypes.c_uint32],
            "write_many": [pointer, ctypes.c_uint16, ctypes.POINTER(ctypes.c_uint16), ctypes.c_uint16, ctypes.c_uint32],
            "set_environment": [pointer, ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_int, ctypes.c_int, ctypes.c_uint16],
            "snapshot": [pointer, ctypes.c_void_p, ctypes.c_size_t],
        }.items():
            function = getattr(self.lib, "oq_odu_" + name)
            function.argtypes = types
            function.restype = ctypes.c_int
        self.lib.oq_odu_request.argtypes = [pointer, ctypes.c_uint8, ctypes.c_uint8, ctypes.c_uint8,
                                           ctypes.c_uint16, ctypes.c_uint16, ctypes.c_uint32, ctypes.c_int]
        self.lib.oq_odu_request.restype = None
        self.profiles = ["V2 old", "V2 new"]
        self.online = [True, True]
        self.no_flow = [False, False]
        self.defrost = [False, False]
        self.faults = [0, 0]
        self.now_ms = 0
        self.environment = {"outdoor_c": 8.0, "return_c": 28.0, "room_c": 19.0,
                            "setpoint_c": 21.0, "flow_lph": 800.0, "heating_enable": False}
        self.handles = [self.lib.oq_odu_create(PROFILES[p], 0) for p in self.profiles]
        if not all(self.handles):
            self.close()
            raise RuntimeError("Unable to create native ODU models")
        self._apply_environment()

    def close(self):
        with self.lock:
            for handle in getattr(self, "handles", []):
                if handle:
                    self.lib.oq_odu_destroy(handle)
            self.handles = []

    def _pump_commands(self):
        requested = [self.read(index, 2010) == 4096 for index in (0, 1)]
        ipwm = [self.read(index, 2015) for index in (0, 1)]
        # R2010 enables the relay; the production CM0 stop command R2015=1000
        # stops rotation even with an energized relay. Other PWM values retain
        # the explicitly nominal pump-on fixture; no calibrated pump curve.
        running = [relay and value is not None and value < 1000
                   for relay, value in zip(requested, ipwm)]
        return requested, ipwm, running

    def _link_series(self):
        if self.lib.oq_odu_link_series(*self.handles) != 1:
            raise RuntimeError("Unable to synchronize the Duo series circuit")

    def _apply_environment(self):
        _, _, running = self._pump_commands()
        target_flow = self.environment["flow_lph"] if any(running) else 0.0
        # no_flow represents a physical obstruction in this one series circuit.
        # Independent local sensor failure only disables the GPIO source.
        blocked = any(self.no_flow)
        for index, handle in enumerate(self.handles):
            if self.lib.oq_odu_set_environment(handle, self.environment["outdoor_c"],
                    self.environment["return_c"], target_flow,
                    blocked, self.defrost[index], self.faults[index]) != 1:
                raise ValueError("ODU environment rejected by native model")
        self._link_series()

    def set_environment(self, values: dict):
        with self.lock:
            self.environment = checked_environment(values, self.environment)
            self._apply_environment()

    def set_odu(self, values: dict):
        values = checked_odu(values)
        if set(values) - {"index", "profile", "online", "no_flow", "defrost", "fault"}:
            raise ValueError("Unknown ODU fields")
        index = values.get("index")
        if type(index) is not int or index not in (0, 1):
            raise ValueError("index must be 0 or 1")
        with self.lock:
            profile = values.get("profile", self.profiles[index])
            if profile not in PROFILES:
                raise ValueError("Unknown ODU profile")
            for key in ("online", "no_flow", "defrost"):
                if key in values and type(values[key]) is not bool:
                    raise ValueError(f"{key} must be a boolean")
            fault = values.get("fault", self.faults[index])
            if type(fault) is not int or not 0 <= fault <= 65535:
                raise ValueError("fault must be a 16-bit unsigned word")
            if profile != self.profiles[index]:
                if self.lib.oq_odu_configure(self.handles[index], PROFILES[profile], self.now_ms) != 1:
                    raise ValueError("ODU profile rejected")
                self.profiles[index] = profile
            for key, attr in (("online", self.online), ("no_flow", self.no_flow), ("defrost", self.defrost)):
                if key in values:
                    attr[index] = values[key]
            self.faults[index] = fault
            self._apply_environment()

    def advance(self, uptime_ms: int):
        with self.lock:
            if type(uptime_ms) is not int:
                raise ValueError("ODU firmware clock must be an integer")
            target = uptime_ms & 0xFFFFFFFF
            remaining = (target - self.now_ms) & 0xFFFFFFFF
            if remaining > 0x7FFFFFFF:
                raise RuntimeError("ODU firmware clock regressed")
            # Advance both units together. HP2 receives HP1's outlet and the same
            # circuit flow, including startup/coast-down and profile changes.
            while remaining:
                step = min(remaining, 100)
                now = (self.now_ms + step) & 0xFFFFFFFF
                self._link_series()
                for handle in self.handles:
                    if self.lib.oq_odu_advance(handle, now) != 1:
                        raise RuntimeError("ODU firmware clock regressed")
                self.now_ms = now
                self._link_series()
                remaining -= step

    def read(self, index: int, address: int) -> int | None:
        with self.lock:
            result = ctypes.c_uint16()
            if self.lib.oq_odu_read(self.handles[index], address, ctypes.byref(result)) != 1:
                return None
            return result.value

    def write(self, index: int, address: int, value: int) -> bool:
        with self.lock:
            success = self.lib.oq_odu_write(self.handles[index], address, value, self.now_ms) == 1
            if success:
                self._apply_environment()
            return success

    def write_many(self, index: int, address: int, values: list[int]) -> bool:
        with self.lock:
            array = (ctypes.c_uint16 * len(values))(*values)
            success = self.lib.oq_odu_write_many(self.handles[index], address, array, len(values), self.now_ms) == 1
            if success:
                self._apply_environment()
            return success

    def observe_request(self, index: int, address: int, fc: int, start: int, count: int, dropped: bool):
        with self.lock:
            self.lib.oq_odu_request(self.handles[index], index + 1, address, fc, start, count, self.now_ms, dropped)

    def hydraulics(self) -> dict:
        with self.lock:
            units = self.snapshot()
            requested, ipwm, running = self._pump_commands()
            return {"flow_lph": units[0]["flow_lph"],
                    "target_flow_lph": self.environment["flow_lph"] if any(running) and not any(self.no_flow) else 0.0,
                    "nominal_flow_lph": self.environment["flow_lph"],
                    "pump_requested": requested, "pump_ipwm": ipwm, "pump_running": running,
                    "blocked": any(self.no_flow), "return_c": self.environment["return_c"],
                    "interstage_c": units[0]["outlet_c"], "supply_c": units[1]["outlet_c"]}

    def snapshot(self) -> list[dict]:
        with self.lock:
            result = []
            for index, handle in enumerate(self.handles):
                buffer = ctypes.create_string_buffer(8192)
                length = self.lib.oq_odu_snapshot(handle, buffer, len(buffer))
                if length <= 0 or length >= len(buffer):
                    raise RuntimeError("Unable to read native ODU model snapshot")
                item = json.loads(buffer.raw[:length])
                item.update(address=index + 1, profile=self.profiles[index], online=self.online[index],
                            no_flow=self.no_flow[index], defrost=self.defrost[index])
                result.append(item)
            return result
