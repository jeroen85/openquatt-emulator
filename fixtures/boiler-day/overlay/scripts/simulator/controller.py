"""Serialized access to the controller owned by this local emulator session."""
from __future__ import annotations

import json
import math
import threading
import time
import urllib.parse

from scripts.emulator.run import request


class Controller:
    def __init__(self, port: int, interrupted, *, ot_role=None, local_flow=False):
        self.port = port
        self.interrupted = interrupted
        self.lock = threading.RLock()
        self.next_write = 0.0
        from scripts.simulator.opentherm import Peer
        self.ot_peer = Peer(self, ot_role) if ot_role else None
        self.local_flow = local_flow
        self.flow_peer = None

    def _check(self):
        if self.interrupted.is_set():
            raise InterruptedError("Lab session stopped")

    def value(self, domain: str, name: str):
        self._check()
        with self.lock:
            data, _ = request(self.port, f"/{domain}/{urllib.parse.quote(name, safe='')}")
            payload = json.loads(data)
            if not isinstance(payload, dict) or not ({"value", "state"} & set(payload)):
                raise ValueError(f"Invalid controller entity: {name}")
            return payload.get("value", payload.get("state"))

    def values(self, fields: dict[str, tuple[str, str]], optional=False):
        self._check()
        entities = "\n".join(f"{key}\t{domain}\t{name}" for key, (domain, name) in fields.items())
        body = urllib.parse.urlencode({"detail": "state", "entities": entities}).encode()
        with self.lock:
            data, _ = request(self.port, "/openquatt/entities", body)
        payload = json.loads(data)
        if not isinstance(payload, dict) or not isinstance(payload.get("entities"), dict):
            raise ValueError("Controller bulk API response has no entity map")
        result = {}
        for key, (domain, name) in fields.items():
            item = payload["entities"].get(key)
            if not isinstance(item, dict) or key in payload.get("missing", []):
                if optional:
                    result[key] = None
                    continue
                raise ValueError(f"Controller is missing {domain}/{name}")
            result[key] = item.get("value", item.get("state"))
        return result

    def action(self, domain: str, name: str, action: str, **query):
        self._check()
        with self.lock:
            wait = self.next_write - time.monotonic()
            if wait > 0 and self.interrupted.wait(wait):
                self._check()
            self._check()
            suffix = "?" + urllib.parse.urlencode(query) if query else ""
            endpoint = f"/{domain}/{urllib.parse.quote(name, safe='')}/{action}{suffix}"
            data, _ = request(self.port, endpoint, b"")
            self.next_write = time.monotonic() + 1.5
            return data

    def _readback(self, domain, name, predicate, expected):
        deadline = time.monotonic() + 5
        actual = None
        while time.monotonic() < deadline:
            self._check()
            actual = self.value(domain, name)
            if predicate(actual):
                return
            if self.interrupted.wait(0.15):
                self._check()
        raise ValueError(f"{name} did not apply: expected {expected}, got {actual}")

    def select(self, name: str, value: str):
        self.action("select", name, "set", option=value)
        self._readback("select", name, lambda actual: actual == value, value)

    def number(self, name: str, value: float):
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError(f"Invalid numeric input for {name}")
        self.action("number", name, "set", value=value)
        def matches(actual):
            if actual is None:
                return False
            if type(actual) is bool or not math.isfinite(float(actual)):
                raise ValueError(f"Invalid numeric readback for {name}")
            return abs(float(actual) - value) <= 0.11
        self._readback("number", name, matches, value)

    def switch(self, name: str, enabled: bool):
        self.action("switch", name, "turn_on" if enabled else "turn_off")
        allowed = (True, 1, "ON", "on") if enabled else (False, 0, "OFF", "off")
        self._readback("switch", name, lambda actual: actual in allowed, enabled)

    def setup_inputs(self):
        self.select("CM Override", "Force CM0")
        self.switch("CIC - Enable polling", False)
        self.switch("Manual Cooling Enable", False)
        self.switch("api_input_cooling_enable", False)
        thermostat = self.ot_peer and self.ot_peer.role == "thermostat"
        source = "OT thermostat" if thermostat else "API input"
        for name, value in (
            ("Heating Control Mode", "Power House"),
            ("Outside Temperature Source", "API input"),
            ("Room Temperature Source", source),
            ("Room Setpoint Source", source),
            ("External Heat Demand Source", "Disabled" if thermostat else "API input"),
            ("Heating Enable Source", source),
            ("Water Supply Source", "Local"),
            ("Flow Source", "Outdoor unit"),
            ("Outdoor Unit Flow Mode", "Flowmeter HP1"),
            ("Q Flow Source", "Outdoor unit"),
            ("Flow Control Mode", "Flow Setpoint"),
        ):
            self.select(name, value)
        self.number("Flow Setpoint", 800)
        if self.ot_peer:
            self.ot_peer.setup()
        if self.local_flow:
            from scripts.simulator.flow import FlowPeer
            self.flow_peer = FlowPeer(self)
            self.flow_peer.configure()

    def sync_hydraulics(self, values: dict):
        if self.flow_peer:
            self.flow_peer.sync_hydraulics(values["flow_lph"])

    def apply_environment(self, values: dict, *, auto_mode=True):
        # API weather plus either real API inputs or Manchester/RMT thermostat ingress.
        self.number("api_input_outside_temperature", values["outdoor_c"])
        if self.ot_peer and self.ot_peer.role == "thermostat":
            self.ot_peer.apply(values)
        else:
            for name, value in (
                ("api_input_room_temperature", values["room_c"]),
                ("api_input_room_setpoint", values["setpoint_c"]),
                ("api_input_external_heat_demand", 8000 if values["heating_enable"] else 0),
            ):
                self.number(name, value)
            self.switch("api_input_heating_enable", values["heating_enable"])
        if self.flow_peer:
            self.flow_peer.apply(values)
        if auto_mode:
            self.select("CM Override", "Auto")
