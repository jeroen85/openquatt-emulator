"""Controlled compressor responses, startup boosts and oil-return phases."""
import ctypes
from urllib.parse import urlencode

from .dynamics import Dynamics, entity, near, value
from .lowflow import command_value
from .model import OduFleet


def command_transitions(writes):
    state, levels, modes = [False, False], [0, 0], [0, 0]
    for row in writes:
        level, mode = command_value(row, 1999), command_value(row, 3999)
        if row["exception"] or (level is None and mode is None):
            continue
        hp, now = row["unit"] - 1, row["at_ms"]
        if level is not None:
            levels[hp] = level
        if mode is not None:
            modes[hp] = mode
        active = levels[hp] > 0 and modes[hp] in (1, 2, 4)
        if active != state[hp]:
            state[hp] = active
            yield hp, now, active


def command_intervals(writes):
    start, stop, on, off = [None, None], [None, None], [], []
    for hp, now, active in command_transitions(writes):
        if active:
            if stop[hp] is not None:
                off.append({"hp": hp + 1, "ms": now - stop[hp]})
            start[hp] = now
        else:
            on.append({"hp": hp + 1, "ms": now - start[hp]})
            stop[hp] = now
    return on, off


class ResponseFleet(OduFleet):
    def __init__(self, library):
        super().__init__(library)
        self.lib.oq_odu_set_response.argtypes = [ctypes.c_void_p, ctypes.c_float, ctypes.c_float, ctypes.c_float]
        self.lib.oq_odu_set_response.restype = ctypes.c_int
        self.zero = self.oil = False
        self.set_odu({"index": 1, "profile": "V2 old"})
        self.set_environment({"outdoor_c": 8.0, "return_c": 30.0})

    def response(self, hz20, hz26):
        with self.lock:
            for handle in self.handles:
                if self.lib.oq_odu_set_response(handle, hz20, hz26, 20) != 1:
                    raise ValueError("ODU response fixture rejected")

    def read(self, index, address):
        with self.lock:
            raw = super().read(index, address)
            if raw is not None and address == 2103 and self.zero:
                return 0
            if raw is not None and address == 2119 and self.oil:
                return raw | 8
            return raw


class OduResponse(Dynamics):
    settings = Dynamics.settings + (
        ("select", "Quatt Hybrid version", "V2"),
        ("select", "Heating Control Mode", "Power House"),
        ("select", "External Heat Demand Source", "API input"),
        ("number", "Rated maximum house power", 15000),
        ("number", "Power House temperature reaction", 0),
        ("number", "api_input_external_heat_demand", 9000),
        ("number", "Minimum runtime", 300),
        ("number", "HP1 - Excluded frequency minimum", 30),
        ("number", "HP1 - Excluded frequency maximum", 120),
        ("number", "HP2 - Excluded frequency minimum", 30),
        ("number", "HP2 - Excluded frequency maximum", 120),
    )

    transient = False

    def __init__(self, thermostat, fleet):
        super().__init__(thermostat, fleet)
        self.power, self.phase_start, self.stable = 9000, 0, None
        self.last_sample = -5000
        self.result.update(version=1, samples=[], checks={},
                           contract={"minimum_runtime_ms": 300000, "minimum_off_ms": 240000,
                                     "stable_ms": 90000, "floor_demand_w": 5000,
                                     "transient_demand_w": 4500, "restart_demand_w": 9000,
                                     "scope": "Injected responses; no STM8 firmware or calibrated capacity proof."})
        fleet.response(20, 26)

    def transition(self, stage, sample):
        self.result["checkpoints"][stage] = sample
        self.result["stage"] = stage
        self.phase_start, self.stable = sample["guest_ms"], None

    def observe(self, get):
        stage = self.result["stage"]
        if stage in ("complete", "failed"):
            return
        if stage == "configure":
            if self.configure(get):
                self.result["stage"] = "standby"
            return
        if self.fleet.now_ms - self.last_sample < 5000:
            return
        self.last_sample = self.fleet.now_ms
        if get(entity("number", "api_input_external_heat_demand") + "/set?" + urlencode({"value": self.power}),
               method="POST", timeout=2) is None:
            raise RuntimeError("External heat demand refresh failed")
        sample = self.sample(get)
        with self.fleet.lock:
            sample.update(nominal_hz=[self.fleet.read(i, 2102) for i in (0, 1)],
                          actual_hz=[self.fleet.read(i, 2103) for i in (0, 1)],
                          physical_w=[u["power_w"] for u in self.fleet.snapshot()])
        if self.fleet.oil:
            sample["oil_received"] = [value(get, "binary_sensor", f"HP{i} - Protection - Compressor oil return")
                                      for i in (1, 2)]
        sample.update(stage=stage, demand_w=self.power)
        self.result["samples"].append(sample)
        self.result["current"] = sample
        now, fw = sample["guest_ms"], sample["firmware"]
        if len(self.result["samples"]) > 1 and now - self.result["samples"][-2]["guest_ms"] > 12000:
            self.stable = None
        age = now - self.phase_start
        commands = [u["requested_level"] for u in sample["odus"]]
        duo = all(v > 0 for v in commands)
        actual = sample["actual_hz"]
        received = all(type(a) in (int, float) and near(b, a, 1) for a, b in zip(actual, fw["frequency_hz"]))
        if stage == "standby":
            if (fw["ott_valid"] is True and fw["heat"] is False and not any(commands) and self.no_flow(sample)
                    and all(near(hz, 0) for hz in fw["frequency_hz"])):
                self.set_heat(True)
                self.transition("normal", sample)
        elif stage == "normal":
            normal = duo and actual == [26, 26] and received and self.has_flow(sample)
            self.stable = (self.stable or now) if normal else None
            if self.stable is not None and now - self.stable >= 200000:
                if self.transient:
                    self.power, self.fleet.zero = 4500, True
                    self.transition("zero", sample)
                else:
                    self.fleet.response(48, 48)
                    self.transition("floor_seed", sample)
        elif stage == "zero":
            if all(near(hz, 0) for hz in fw["frequency_hz"]):
                self.result["checkpoints"].setdefault("zero_received", sample)
            if age >= 65000:
                self.fleet.response(45, 45)
                self.fleet.zero = False
                self.transition("startup", sample)
        elif stage == "startup":
            if all(near(hz, 45, 1) for hz in actual) and received:
                self.result["checkpoints"].setdefault("startup_received", sample)
            if age >= 210000:
                self.fleet.response(20, 26)
                self.transition("normal_recovery", sample)
        elif stage == "normal_recovery":
            if age >= 90000:
                self.fleet.response(50, 50)
                self.fleet.oil = True
                self.transition("oil", sample)
        elif stage == "oil":
            if all(v is True for v in sample.get("oil_received", [])) and len(sample.get("oil_received", [])) == 2:
                self.result["checkpoints"].setdefault("oil_received", sample)
            if age >= 120000:
                self.fleet.response(20, 26)
                self.fleet.oil = False
                self.transition("oil_recovery", sample)
        elif stage == "oil_recovery":
            normal = duo and all(near(a, b, 1) for a, b in zip(actual, sample["nominal_hz"])) and received and self.has_flow(sample)
            self.stable = (self.stable or now) if normal else None
            if age >= 180000 and self.stable is not None and now - self.stable >= 60000:
                self.set_heat(False)
                self.transition("stop", sample)
        elif stage == "floor_seed":
            exact = duo and sample["nominal_hz"] == [26, 26] and actual == [48, 48] and received
            self.stable = (self.stable or now) if exact else None
            if self.stable is not None and now - self.stable >= 45000:
                self.power = 5000
                self.transition("floor", sample)
        elif stage == "floor":
            single = sum(v > 0 for v in commands) == 1 and sum(v > 1 for v in actual) == 1
            adequate = sum(sample["physical_w"]) >= self.power * .95
            self.stable = (self.stable or now) if single and adequate and received else None
            if self.stable is not None and now - self.stable >= 90000:
                self.set_heat(False)
                self.transition("stop", sample)
        elif stage == "stop":
            if not any(commands) and all(near(hz, 0, .5) for hz in fw["frequency_hz"]):
                self.transition("stopped", sample)
        elif stage == "stopped":
            if age >= 30000:
                self.power = 9000
                self.set_heat(True)
                self.transition("restart", sample)
        elif stage == "restart":
            if (all(v > 0 for v in commands) and all(type(hz) in (int,float) and hz > 3 for hz in fw["frequency_hz"])
                    and all(u["requested_mode"] == "heating" for u in sample["odus"]) and self.has_flow(sample)):
                self.stable = self.stable or now
                if now - self.stable >= 60000:
                    self.transition("complete", sample)
            else:
                self.stable = None

    def finalize(self, report):
        points = self.result["checkpoints"]
        on, off = command_intervals(report["writes"])
        checks = {"normal_received": "zero" in points if self.transient else "floor_seed" in points,
                  "recovery_complete": "complete" in points,
                  "minimum_runtime": all(x["ms"] >= 300000 for x in on),
                  "minimum_off": bool(off) and all(x["ms"] >= 240000 for x in off),
                  "both_units_restarted": {x["hp"] for x in off} == {1,2},
                  "stop_interval_observed": bool(on)}
        samples = self.result["samples"]
        recovery = []
        for sample in samples:
            units, fw = sample["odus"], sample["firmware"]
            valid = (sample.get("stage") == "restart" and fw.get("heat") is True and self.has_flow(sample)
                     and all(u["requested_mode"] == "heating" and u["requested_level"] > 0
                             and self.heating(sample, i) for i, u in enumerate(units)))
            if valid and (not recovery or sample["guest_ms"] - recovery[-1]["guest_ms"] <= 12000):
                recovery.append(sample)
            else:
                recovery = [sample] if valid else []
        checks["both_running_recovery"] = (len(recovery) >= 2
            and recovery[-1]["guest_ms"] - recovery[0]["guest_ms"] >= 60000
            and points.get("complete", {}).get("guest_ms") == recovery[-1]["guest_ms"])
        checks["sample_coverage"] = len(samples) >= 2 and all(
            0 < b["guest_ms"]-a["guest_ms"] <= 12000 for a,b in zip(samples,samples[1:])) and all(
            0 <= s["guest_ms"]-s["observed_from_ms"] <= 12000 for s in samples)
        if self.transient:
            for key in ("zero_received", "startup_received", "oil_received"):
                checks[key] = key in points
            sustained = [s for s in self.result["samples"] if s["stage"] in
                         ("startup", "normal_recovery", "oil", "oil_recovery")]
            checks["needed_peer_retained"] = bool(sustained) and all(
                all(u["requested_level"] > 0 and u["requested_mode"] == "heating" and u["frequency_hz"] > 3
                    for u in s["odus"]) for s in sustained)
            observed = [s["guest_ms"] for s in sustained if s["stage"] == "startup" and
                        all(near(hz, 45, 1) for hz in s["firmware"]["frequency_hz"])]
            checks["startup_past_120s"] = len(observed) > 1 and max(observed) - min(observed) >= 150000
        else:
            checks["exact_floor_received"] = "floor" in points
            checks["stop_interval_observed"] = bool(on)
        self.result.update(checks=checks, on_intervals=on, off_intervals=off,
                           timing_coverage={"completed_on": len(on), "completed_off": len(off),
                                            "source": "successful_R1999_R3999_command_transitions",
                                            "confirmed_standby_margin": "not_qualified"})


class OduTransients(OduResponse):
    transient = True
