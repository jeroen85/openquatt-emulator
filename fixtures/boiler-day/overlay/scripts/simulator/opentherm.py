"""Strict digital OpenTherm evidence and commands for the test-only RMT peers."""
from __future__ import annotations
import hashlib
import math
import re

MARKERS = ("OQ_OT_PEER_BUILD", "OQ_OT_PEER", "OQ_OT_CONTROLLER", "OQ_OT_RESOURCE")
PEER_PROFILES = {"thermostat": "ot-thermostat-ingress-v1", "boiler": "ot-boiler-duplex-v1"}


def source_fingerprint(root):
    folder = root / "scripts/simulator/io/virtual_opentherm"
    sources = sorted(p for p in folder.glob("*.*") if p.is_file())
    sources += [root / "components/opentherm" / name for name in (
        "opentherm.cpp", "opentherm.h", "opentherm_rmt_encoder.h", "opentherm_rmt_decoder.h")]
    sources += [root / "components/openquatt_ot_slave" / name for name in (
        "OpenTherm.cpp", "OpenTherm.h", "OpenThermRmtDecoder.h", "OpenQuattOTSlave.cpp",
        "OpenQuattOTSlave.h", "room_signal_freshness.h")]
    fingerprint = hashlib.sha256()
    for path in sources:
        fingerprint.update(str(path.relative_to(root)).encode() + b"\0" + path.read_bytes() + b"\0")
    return fingerprint.hexdigest()


class Evidence:
    def __init__(self, role, loopbacks, source_sha256):
        if role not in PEER_PROFILES:
            raise ValueError("Invalid OT peer role")
        self.role, self.loopbacks, self.source_sha256 = role, loopbacks, source_sha256
        self.latest = {}

    def accept(self, marker, value):
        def require(condition, text):
            if not condition:
                raise ValueError("OpenTherm evidence: " + text)
        require(marker in MARKERS and isinstance(value, dict) and type(value.get("schema")) is int and value["schema"] == 1,
                "invalid schema or marker")
        if marker != "OQ_OT_CONTROLLER":
            require(value.get("profile") == PEER_PROFILES[self.role], "wrong peer profile")
        if marker == "OQ_OT_PEER_BUILD":
            require(marker not in self.latest, "duplicate build marker")
            require(value.get("source_sha256") == self.source_sha256 and re.fullmatch(r"[0-9a-f]{64}", self.source_sha256), "source hash mismatch")
            require(value.get("loopback") == self.loopbacks and value.get("self_test") is True, "loopback or self-test mismatch")
            require(type(value.get("tx_channel")) is int and value["tx_channel"] == 1 and
                    type(value.get("rx_channel")) is int and value["rx_channel"] == (6 if self.role == "boiler" else -1), "channel mismatch")
        elif marker == "OQ_OT_RESOURCE":
            require(value.get("expected") is True, "unexpected RX memory allocation")
        else:
            require(type(value.get("uptime_ms")) is int and value["uptime_ms"] >= 0, "invalid timestamp")
            previous = self.latest.get(marker)
            require(not previous or value["uptime_ms"] >= previous["uptime_ms"], "timestamp regressed")
            counters = ("rx", "rx_invalid", "rx_overflow", "tx", "done", "tx_error", "dropped") if marker == "OQ_OT_PEER" else ("received", "invalid", "timeouts")
            for key in counters:
                require(type(value.get(key)) is int and value[key] >= 0, "invalid counter " + key)
                require(not previous or value[key] >= previous[key], "counter regressed " + key)
            booleans = ("online", "pending", "ch_enabled") if marker == "OQ_OT_PEER" else (
                "thermostat_status_valid", "ch_enable", "room_fresh", "room_setpoint_fresh", "control_setpoint_fresh", "boiler_link")
            for key in booleans:
                require(type(value.get(key)) is bool, "invalid boolean " + key)
            if marker == "OQ_OT_PEER":
                require(value["rx_overflow"] == 0 and value["tx_error"] == 0, "RMT overflow or TX error")
            else:
                for key in ("room_c", "room_setpoint_c", "control_setpoint_c"):
                    item = value.get(key)
                    require(item is None or type(item) in (int, float) and math.isfinite(item), "invalid field " + key)
        self.latest[marker] = dict(value)
        return dict(value)


class Peer:
    def __init__(self, controller, role):
        self.controller, self.role = controller, role
        self.applied = None

    def setup(self):
        self.controller.switch("Lab OT Peer Online", False)
        self.controller.switch("Lab OT Peer Bad Parity", False)
        self.controller.switch("Lab OT Peer CH Enable", False)
        if self.role == "thermostat":
            self.controller.switch("OpenTherm Enabled", True)
        if self.role == "boiler":
            self.controller.number("Lab OT Peer Response Delay", 50)
            self.controller.select("Boiler connection", "OpenTherm")
            self.controller.switch("Lab OT Peer Online", True)

    def apply(self, environment):
        if self.role != "thermostat":
            return
        if type(environment["heating_enable"]) is not bool:
            raise ValueError("OT heating enable must be boolean")
        values = (environment["room_c"], environment["setpoint_c"], bool(environment["heating_enable"]))
        if any(type(value) not in (int, float) or not math.isfinite(value) or not 5 <= value <= 35 for value in values[:2]):
            raise ValueError("OT thermostat room/setpoint must be between 5 and 35 C")
        if values == self.applied:
            return
        self.applied = None
        try:
            self.controller.switch("Lab OT Peer Online", False)
            self.controller.number("Lab OT Peer Room Temperature", values[0])
            self.controller.number("Lab OT Peer Room Setpoint", values[1])
            self.controller.number("Lab OT Peer Control Setpoint", 40 if values[2] else 0)
            self.controller.switch("Lab OT Peer CH Enable", values[2])
            self.controller.switch("Lab OT Peer Online", True)
            self.applied = values
        except Exception:
            try:
                self.controller.switch("Lab OT Peer Online", False)
            except Exception:
                pass
            raise

    def offline(self):
        self.applied = None
        self.controller.switch("Lab OT Peer Online", False)
