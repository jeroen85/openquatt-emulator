"""Verify both ODU profile intervals on an unmodified Q Duo factory image."""

import json
import struct
from urllib.parse import urlencode

from .dynamics import Dynamics, value
from .model import PROFILES
from .transport import RtuPeer


P276 = 3275
PLATE = 3236
BLOCKS = {3270, 3307, 3336, 3414}
TARGET_PLATE = {"mode": 2, "start_temperature_c": 7, "stop_delta_c": 6}
PLATE_WORDS = [2, 37, 6]
INTERVAL_MS = 600000


class ProfilePeer(RtuPeer):
    def __init__(self, fleet, clock):
        super().__init__(fleet, clock)
        self.frames = []
        self.reset = {}
        self.armed = None
        self.last_hp2_ms = 0
        self.last_hp2_regular_ms = 0

    def arm_reset(self, duration):
        with self.lock:
            self.armed = (self.fleet.now_ms, duration)

    def settings(self, index):
        with self.fleet.lock:
            return {
                "defrost": self.fleet.read(index, P276),
                "bottom-plate": [self.fleet.read(index, addr) for addr in range(PLATE, PLATE + 3)],
            }

    def advance_clock(self):
        super().advance_clock()
        with self.lock, self.fleet.lock:
            now = self.fleet.now_ms
            if self.armed:
                armed_at, duration = self.armed
                in_quiet_window = (self.last_hp2_regular_ms > armed_at
                                   and 1800 <= now - self.last_hp2_regular_ms <= 3500
                                   and now - self.last_hp2_ms >= 500)
                if in_quiet_window:
                    self.reset = {"cut_ms": now, "duration_requested_ms": duration,
                                  "before": self.settings(1), "last_hp2_request_ms": self.last_hp2_ms,
                                  "last_hp2_runtime_poll_ms": self.last_hp2_regular_ms}
                    self.fleet.set_odu({"index": 1, "online": False})
                    self.armed = None
            if (self.reset and "restore_ms" not in self.reset
                    and now >= self.reset["cut_ms"] + self.reset["duration_requested_ms"]):
                profile = PROFILES[self.fleet.profiles[1]]
                if self.fleet.lib.oq_odu_configure(self.fleet.handles[1], profile, now) != 1:
                    raise RuntimeError("Could not reset the ODU model")
                self.fleet.set_odu({"index": 1, "online": True})
                self.reset.update(restore_ms=now, after=self.settings(1))

    def _handle(self, data):
        unit, fc, address, count_or_value = struct.unpack(">BBHH", data[:6])
        now = self.fleet.now_ms
        if unit == 2:
            self.last_hp2_ms = now
            if fc == 3 and address == 2099 and count_or_value > 1:
                self.last_hp2_regular_ms = now
        reply = super()._handle(data)
        count = count_or_value if fc in (3, 16) else 1
        touches_profile = any(address <= reg < address + count for reg in (P276, PLATE, PLATE + 1, PLATE + 2))
        if touches_profile or address in BLOCKS:
            row = {"unit": unit, "fc": fc, "address": address, "count_or_value": count_or_value,
                   "at_ms": now, "ok": bool(reply and reply[1] == fc)}
            if row["ok"] and fc == 3:
                row["values"] = list(struct.unpack_from(f">{count_or_value}H", reply, 3))
            self.frames.append(row)
        return reply

    def evidence(self):
        with self.lock:
            return dict(self.reset), list(self.frames)


def p276_value(frame):
    offset = P276 - frame["address"]
    values = frame.get("values", [])
    return values[offset] if 0 <= offset < len(values) else None


class OduProfileRecovery(Dynamics):
    def __init__(self, thermostat, fleet, peer):
        super().__init__(thermostat, fleet)
        self.peer = peer
        self.units = (1, 2)
        self.outside = -0.3
        self.refresh_ms = -20000
        self.last_sample_ms = -3000
        self.profiles = [(hp, name) for hp in self.units for name in ("defrost", "settings")]
        self.seed_stage = "load"
        self.result.update(version=1, checks={}, samples=[])
        self.settings_pending = [row for row in self.settings_pending
                                 if row[1] not in ("api_input_outside_temperature", "Boiler connection")]
        self.settings_pending.extend([
            ("select", "Quatt Hybrid version", "V1.5"),
            ("number", "api_input_outside_temperature", self.outside),
            ("select", "Boiler connection", "R1"),
        ])
        for index in (0, 1):
            fleet.set_odu({"index": index, "profile": "V1.5"})
        fleet.set_environment({"outdoor_c": self.outside, "return_c": 23.0})

    @staticmethod
    def status(get, hp, name):
        body = get(f"/openquatt/odu-{name}/hp{hp}/status")
        return json.loads(body) if body else {}

    @staticmethod
    def action(get, hp, name, action, status, **values):
        query = urlencode({**values, "csrf_token": status["csrf_token"]})
        return get(f"/openquatt/odu-{name}/hp{hp}/{action}?{query}", method="POST") is not None

    @staticmethod
    def in_sync(defrost, settings):
        return (defrost.get("profile_state") == "IN_SYNC" and defrost.get("defrost_mode") == 4
                and defrost.get("auto_reapply") is True and not defrost.get("busy")
                and settings.get("status") == "IN_SYNC" and settings.get("actual") == TARGET_PLATE
                and settings.get("auto_reapply") is True and not settings.get("busy"))

    def seed(self, get):
        hp, name = self.profiles[0]
        status = self.status(get, hp, name)
        if not status.get("identity_ready") or status.get("busy"):
            return
        if self.seed_stage == "load":
            if self.action(get, hp, name, "load", status):
                self.seed_stage = "save"
        elif self.seed_stage == "save" and status.get("loaded"):
            desired = ({"mode": 4, "expected_mode": status["defrost_mode"]}
                       if name == "defrost" else TARGET_PLATE)
            if self.action(get, hp, name, "save", status, **desired, auto_reapply="true"):
                self.seed_stage = "confirm"
        elif self.seed_stage == "confirm" and status.get("auto_reapply") is True:
            synced = (status.get("profile_state") == "IN_SYNC" and status.get("defrost_mode") == 4
                      if name == "defrost" else
                      status.get("status") == "IN_SYNC" and status.get("actual") == TARGET_PLATE)
            if synced:
                self.profiles.pop(0)
                self.seed_stage = "load"
                if not self.profiles:
                    self.result["stage"] = "arm"

    def observe(self, get):
        stage = self.result["stage"]
        if stage == "complete":
            return
        if stage == "configure":
            if self.configure(get):
                self.result["stage"] = "seed"
            return
        now = self.fleet.now_ms
        if now - self.refresh_ms >= 18000:
            if get(f"/number/api_input_outside_temperature/set?value={self.outside}", method="POST") is not None:
                self.refresh_ms = now
        if stage == "seed":
            self.seed(get)
            return
        if now - self.last_sample_ms < 2000:
            return
        self.last_sample_ms = now
        defrost = self.status(get, 2, "defrost")
        settings = self.status(get, 2, "settings")
        if not defrost or not settings:
            return
        cm = value(get, "text_sensor", "Control Mode")
        hp2 = self.peer.settings(1)
        self.result["current"] = row = {
            "at_ms": now, "cm": cm, "hp2_online": defrost.get("online"),
            "operating_mode": defrost.get("operation_mode"), "defrost_state": defrost.get("profile_state"),
            "bottom_plate_state": settings.get("status"), "registers": hp2,
        }
        if stage == "arm":
            if self.in_sync(defrost, settings) and cm == "CM98":
                self.result["armed_ms"] = now
                self.peer.arm_reset(400)
                self.result["stage"] = "observe-reset"
        elif stage == "observe-reset":
            self.result["samples"].append(row)
            reset, frames = self.peer.evidence()
            if "restore_ms" not in reset or now <= reset["restore_ms"]:
                return
            if not self.in_sync(defrost, settings) or hp2 != {"defrost": 4, "bottom-plate": PLATE_WORDS}:
                return
            elapsed = now - self.result["armed_ms"]
            stable_reads = {f["address"] for f in frames if f["unit"] == 1 and f["fc"] == 3
                            and f["at_ms"] > self.result["armed_ms"] and f["ok"]}
            if elapsed < INTERVAL_MS - 45000 or not {P276, PLATE} <= stable_reads:
                return
            self.result["restored_ms"] = now
            self.result["stage"] = "complete"

    def finalize(self, report):
        reset, frames = self.peer.evidence()
        self.result["odu_reset"] = reset
        self.result["profile_frames"] = frames
        cut, restored = reset.get("cut_ms", -1), reset.get("restore_ms", -1)
        seeds, automatic = {}, {}
        for hp in self.units:
            received = [f for f in frames if f["unit"] == hp and f["fc"] == 3 and f["ok"]]
            seeds[hp] = {
                "defrost": next((f for f in reversed(received)
                                 if f["at_ms"] < cut and p276_value(f) == 4), None),
                "bottom-plate": next((f for f in reversed(received) if f["at_ms"] < cut
                                      and f["address"] == PLATE and f["values"] == PLATE_WORDS), None),
            }
            automatic[hp] = [f for f in frames if f["unit"] == hp and restored <= f["at_ms"]]
        self.result["last_confirmed_seed"] = seeds
        hp2_frames = automatic[2]
        d_reads = [f for f in hp2_frames if f["ok"] and f["fc"] == 3 and p276_value(f) is not None]
        b_reads = [f for f in hp2_frames if f["ok"] and f["fc"] == 3 and f["address"] == PLATE]
        d_writes = [f for f in hp2_frames if f["ok"] and f["fc"] == 6 and f["address"] == P276]
        b_writes = [f for f in hp2_frames if f["ok"] and f["fc"] == 6 and PLATE <= f["address"] < PLATE + 3]
        after = reset.get("after", {})
        expected_plate_writes = {PLATE + i: target for i, target in enumerate(PLATE_WORDS)
                                 if i < len(after.get("bottom-plate", [])) and after["bottom-plate"][i] != target}
        checks = {
            "profile_writes_are_single_register": restored > cut >= 0 and all(
                row["fc"] != 16 for frames in automatic.values() for row in frames),
            "same_odu_model_reset_both_profiles": reset.get("before") == {"defrost": 4, "bottom-plate": PLATE_WORDS}
            and after.get("defrost") != 4 and bool(expected_plate_writes) and restored > cut >= 0,
            "defrost_restored_once_and_verified": len(d_writes) == 1 and len(d_reads) >= 2
            and d_writes[0]["count_or_value"] == 4 and p276_value(d_reads[0]) == after.get("defrost")
            and d_reads[0]["at_ms"] < d_writes[0]["at_ms"] < d_reads[-1]["at_ms"] and p276_value(d_reads[-1]) == 4,
            "bottom_plate_writes_only_changed_values_and_verifies": bool(expected_plate_writes)
            and len(b_writes) == len(expected_plate_writes)
            and dict((f["address"], f["count_or_value"]) for f in b_writes) == expected_plate_writes
            and len(b_reads) >= 2 and all(f["count_or_value"] == 3 for f in b_reads)
            and b_reads[0]["values"] == after.get("bottom-plate") and b_reads[-1]["values"] == PLATE_WORDS
            and b_reads[0]["at_ms"] < min(f["at_ms"] for f in b_writes)
            and max(f["at_ms"] for f in b_writes) < b_reads[-1]["at_ms"],
        }
        stable = automatic[1]
        last_armed = self.result.get("armed_ms", -1)
        deltas = {}
        for hp, reads in ((1, stable), (2, d_reads + b_reads)):
            for name, address in (("defrost", P276), ("bottom-plate", PLATE)):
                seed = seeds[hp][name]
                first = next((f for f in reads if f["address"] == address and f["at_ms"] > last_armed), None)
                if seed and first:
                    deltas[f"hp{hp}_{name}"] = first["at_ms"] - seed["at_ms"]
        self.result["interval_ms"] = deltas
        self.result["recovery_reads"] = {"defrost": d_reads, "bottom-plate": b_reads}
        before_write = min((f["at_ms"] for f in d_writes + b_writes), default=0)
        steady_samples = [row for row in self.result["samples"] if row["at_ms"] < before_write]
        checks.update(
            short_reset_without_offline=400 <= restored - cut < 1000
            and not any(event["guest_ms"] >= last_armed for event in report["modbus_link_events"])
            and not report["peer"]["offline_frames"]
            and all(row["hp2_online"] is True for row in self.result["samples"]),
            four_checks_near_600_seconds=len(deltas) == 4
            and all(598000 <= delta <= 640000 for delta in deltas.values()),
            unchanged_unit_exactly_two_reads_and_no_write=len(stable) == 2
            and {(f["fc"], f["address"], f["count_or_value"]) for f in stable} == {(3, P276, 1), (3, PLATE, 3)}
            and all(f["ok"] and f["values"] == ([4] if f["address"] == P276 else PLATE_WORDS) for f in stable),
            first_defrost_check_is_single_register=bool(d_reads) and d_reads[0]["address"] == P276
            and d_reads[0]["count_or_value"] == 1,
            stable_cm98=bool(steady_samples) and all(row["cm"] == "CM98" and row["operating_mode"] == 0
                                                      for row in steady_samples),
        )
        self.result["checks"] = checks
