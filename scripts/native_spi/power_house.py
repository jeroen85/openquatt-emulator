"""Normal Power House demand ramps with the unmodified Duo ODU model."""
import math
from statistics import median
from urllib.parse import urlencode

from .dynamics import Dynamics, entity, near, value
from .odu_scenarios import command_intervals
from .lowflow import command_value


CLIMATES = {"mild": {"outdoor_c": 8.0, "return_c": 30.0},
            "cold": {"outdoor_c": -5.0, "return_c": 30.0},
            "warm-water": {"outdoor_c": 8.0, "return_c": 40.0}}
PHASES = (("low_up", 2600, "single", 90000), ("mid_up", 5000, "either", 90000),
          ("high", 10000, "duo", 300000), ("mid_down", 5000, "either", 90000),
          ("low_down", 2600, "single", 90000), ("stop", 0, "off", 30000),
          ("restart", 2600, "single", 90000))
MAX_GAP_MS = 12000
REQUEST_PUBLICATION_MS = 30000


def finite(number):
    return type(number) in (int, float) and math.isfinite(number)


def active_units(sample):
    """Require coherent commands, model activity and firmware feedback."""
    active = []
    for i, unit in enumerate(sample["odus"]):
        hz, measured = unit["frequency_hz"], sample["firmware"]["frequency_hz"][i]
        if not finite(hz) or not finite(measured):
            return None
        if unit["requested_level"] > 0 and unit["requested_mode"] == "heating" and hz > 3 and near(measured, hz, 1):
            active.append(i)
        elif not (unit["requested_level"] == 0 and unit["requested_mode"] == "standby"
                  and near(hz, 0, .5) and near(measured, 0, .5)):
            return None
    return active


def slew_evidence(samples):
    rates = {"mid_up": 15000 / 480, "high": 15000 / 480,
             "mid_down": -15000 / 180, "low_down": -15000 / 180}
    evidence = {name: {"pairs": 0, "plausible": True} for name in rates}
    for a, b in zip(samples, samples[1:]):
        name = b.get("stage")
        if name not in rates:
            continue
        row = evidence[name]
        first, last = a["firmware"].get("request_w"), b["firmware"].get("request_w")
        if not finite(first) or not finite(last):
            row["plausible"] = False
            continue
        row["pairs"] += 1
        # The operator-facing P_req sensor publishes every 30s. Its previous
        # value can be one publication old, in addition to HTTP sample skew.
        span = b["guest_ms"] - a["observed_from_ms"] + REQUEST_PUBLICATION_MS
        change = (last - first) * (1 if rates[name] > 0 else -1)
        row["plausible"] &= span > 0 and -50 <= change <= abs(rates[name]) * span / 1000 + 50
    return evidence


class PowerHouse(Dynamics):
    settings = tuple(row for row in Dynamics.settings if row[1] not in (
        "Room Temperature Source", "Room Setpoint Source", "api_input_outside_temperature")) + (
        ("select", "Quatt Hybrid version", "V2"),
        ("select", "Heating Control Mode", "Power House"),
        ("select", "External Heat Demand Source", "API input"),
        ("select", "Room Temperature Source", "API input"),
        ("select", "Room Setpoint Source", "API input"),
        ("select", "Water Supply Source", "Local"),
        ("select", "Power House response profile", "Balanced"),
        ("number", "Rated maximum house power", 15000),
        ("number", "Minimum runtime", 300),
        ("number", "api_input_room_temperature", 21),
        ("number", "api_input_room_setpoint", 21),
        ("number", "api_input_external_heat_demand", 0),
        ("number", "HP1 - Excluded frequency minimum", 0),
        ("number", "HP1 - Excluded frequency maximum", 0),
        ("number", "HP2 - Excluded frequency minimum", 0),
        ("number", "HP2 - Excluded frequency maximum", 0),
    )

    def __init__(self, thermostat, fleet, climate="mild"):
        if climate not in CLIMATES:
            raise ValueError("Unknown Power House climate profile")
        super().__init__(thermostat, fleet)
        self.climate = dict(CLIMATES[climate])
        self.settings_pending.append(("number", "api_input_outside_temperature", self.climate["outdoor_c"]))
        for i in (0, 1):
            fleet.set_odu({"index": i, "profile": "V2 old"})
        fleet.set_environment(self.climate)
        self.power, self.index, self.last_sample = 0, -1, -5000
        self.hold = []
        self.result.update(version=1, samples=[], checks={}, phase_requests={}, phase_results={},
                           climate_profile=climate, climate=self.climate,
                           contract={"demand_steps_w": [0] + [p[1] for p in PHASES],
                                     "minimum_runtime_ms": 300000, "minimum_off_ms": 240000,
                                     "demand_rise_min": 8, "demand_fall_min": 3,
                                     "room_c": 21, "setpoint_c": 21, "rated_power_w": 15000,
                                     "response_injection": False, "frequency_exclusion": False,
                                     "scope": "Normal model response; PT1000 fixed, no calibrated capacity or building model."})

    def write_input(self, get, name, expected):
        if get(entity("number", name) + "/set?" + urlencode({"value": expected}), method="POST", timeout=2) is None:
            raise RuntimeError(f"Power House input refresh failed: {name}")

    def refresh(self, get):
        for name, expected in (("api_input_external_heat_demand", self.power),
                               ("api_input_outside_temperature", self.climate["outdoor_c"]),
                               ("api_input_room_temperature", 21), ("api_input_room_setpoint", 21)):
            self.write_input(get, name, expected)

    def sample(self, get):
        sample = super().sample(get)
        fw = sample["firmware"]
        for key, domain, name in (
            ("control", "select", "Heating Control Mode"),
            ("room_selected_c", "sensor", "Room Temperature (Selected)"),
            ("setpoint_selected_c", "sensor", "Room Setpoint (Selected)"),
            ("outside_c", "sensor", "Outside Temperature (Selected)"),
            ("external_w", "sensor", "External Heat Demand (Selected)"),
            ("request_w", "sensor", "Power House – P_req"),
            ("demand_source", "text_sensor", "Power House – demand source"),
        ):
            fw[key] = value(get, domain, name)
        fw["water_in_c"] = [value(get, "sensor", f"HP{i} - Water in temperature") for i in (1, 2)]
        fw["water_out_c"] = [value(get, "sensor", f"HP{i} - Water out temperature") for i in (1, 2)]
        with self.fleet.lock:
            full = self.fleet.snapshot()
            sample["odus"] = full
            sample["physical_w"] = [u["power_w"] for u in full]
            sample["outside_c"] = [u["outside_c"] for u in full]
            sample["nominal_hz"] = [self.fleet.read(i, 2102) for i in (0, 1)]
            sample["guest_ms"] = self.fleet.now_ms
        sample.update(stage=self.result["stage"], demand_w=self.power)
        return sample

    def inputs_match(self, sample, power=None):
        power = self.power if power is None else power
        fw = sample["firmware"]
        return (fw["control"] == "Power House"
                and near(fw["external_w"], power, 5) and near(fw["room_selected_c"], 21)
                and near(fw["setpoint_selected_c"], 21) and near(fw["outside_c"], self.climate["outdoor_c"], .2)
                and all(near(t, self.climate["outdoor_c"], .2) for t in sample["outside_c"])
                and near(sample["odus"][0]["inlet_c"], self.climate["return_c"], .2))

    def phase_valid(self, sample, expected, power=None):
        power = self.power if power is None else power
        if not self.inputs_match(sample, power) or sample["firmware"]["lowflow_fault"] is not False:
            return False
        active = active_units(sample)
        if expected == "off":
            mode = str(sample["firmware"]["mode"])
            idle = (mode.startswith("CM0") and self.no_flow(sample)) or (
                self.climate["outdoor_c"] < 0 and mode.startswith("CM98") and self.has_flow(sample))
            return active == [] and sample["firmware"]["heat"] is False and idle
        if active is None or len(active) not in ({1} if expected == "single" else {2} if expected == "duo" else {1, 2}):
            return False
        fw = sample["firmware"]
        return (fw["heat"] is True and str(fw["mode"]).startswith("CM2") and self.has_flow(sample)
                and fw["demand_source"] == "external" and near(fw["request_w"], power, 100)
                and all(near(fw["water_in_c"][i], unit["inlet_c"], .7)
                        and near(fw["water_out_c"][i], unit["outlet_c"], .7)
                        and (not unit["requested_level"] or finite(sample["nominal_hz"][i])
                             and near(unit["frequency_hz"], sample["nominal_hz"][i], 1))
                        for i, unit in enumerate(sample["odus"]))
                and all(finite(p) and p >= 0 for p in sample["physical_w"]))

    def held(self, sample, valid, duration):
        if (not valid or sample["guest_ms"] - sample["observed_from_ms"] > MAX_GAP_MS
                or self.hold and sample["observed_from_ms"] - self.hold[-1]["guest_ms"] > MAX_GAP_MS):
            self.hold = []
        if valid:
            self.hold.append(sample)
        return bool(self.hold) and sample["guest_ms"] - self.hold[0]["guest_ms"] >= duration

    def begin_next(self, get, sample):
        self.index += 1
        if self.index == len(PHASES):
            self.result["stage"] = "complete"
            self.result["post_complete"] = {"begin_ms": sample["guest_ms"],
                "last_valid_ms": sample["guest_ms"], "invalid_since_ms": None}
            return
        name, self.power, _, _ = PHASES[self.index]
        self.result["phase_requests"][name] = {"guest_ms": self.fleet.now_ms, "power_w": self.power}
        self.write_input(get, "api_input_external_heat_demand", self.power)
        self.set_heat(self.power > 0)
        self.result["stage"], self.hold = name, []

    def observe(self, get):
        stage = self.result["stage"]
        if stage == "failed":
            return
        if stage == "configure":
            if self.configure(get):
                self.result["stage"] = "standby"
            return
        if self.fleet.now_ms - self.last_sample < 5000:
            return
        self.last_sample = self.fleet.now_ms
        self.refresh(get)
        sample = self.sample(get)
        self.result["samples"].append(sample)
        self.result["current"] = sample
        if stage == "complete":
            tail = self.result["post_complete"]
            if self.phase_valid(sample, "single"):
                tail.update(last_valid_ms=sample["guest_ms"], invalid_since_ms=None)
            else:
                if tail["invalid_since_ms"] is None:
                    tail["invalid_since_ms"] = sample["observed_from_ms"]
                if sample["guest_ms"] - tail["invalid_since_ms"] >= 60000:
                    self.result.update(stage="failed", violation="Normaal Single-bedrijf viel na herstel langer dan 60 s weg.")
            return
        if stage == "standby":
            if self.held(sample, self.phase_valid(sample, "off"), 5000):
                self.result["checkpoints"]["standby"] = sample
                self.begin_next(get, sample)
            return
        name, _, expected, duration = PHASES[self.index]
        valid = self.phase_valid(sample, expected)
        if self.held(sample, valid, duration):
            self.result["checkpoints"][name] = sample
            self.result["phase_results"][name] = {
                "start_ms": self.hold[0]["guest_ms"], "end_ms": sample["guest_ms"],
                "hold_ms": sample["guest_ms"] - self.hold[0]["guest_ms"], "samples": len(self.hold),
                "model_power_median_w": median(sum(s["physical_w"]) for s in self.hold[-5:]),
                "owners": sorted({i + 1 for s in self.hold for i in (active_units(s) or [])}),
            }
            self.begin_next(get, sample)

    def finalize(self, report):
        results, samples = self.result["phase_results"], self.result["samples"]
        checks = {"standby": "standby" in self.result["checkpoints"]}
        for name, power, expected, duration in PHASES:
            rows = [s for s in samples if s.get("stage") == name]
            end = results.get(name, {})
            hold = [s for s in rows if end.get("start_ms", float("inf")) <= s["guest_ms"] <= end.get("end_ms", -1)]
            checks[name] = (bool(hold) and end.get("hold_ms", 0) >= duration
                            and hold[-1]["guest_ms"] - hold[0]["guest_ms"] >= duration
                            and all(self.phase_valid(s, expected, power) for s in hold))
        on, off = command_intervals(report["writes"])
        checks["command_minimum_runtime"] = len(on) >= 2 and all(x["ms"] >= 300000 for x in on)
        checks["command_minimum_off"] = bool(off) and all(x["ms"] >= 240000 for x in off)
        checks["sample_coverage"] = len(samples) >= 2 and all(
            0 < b["guest_ms"] - a["guest_ms"] <= MAX_GAP_MS for a, b in zip(samples, samples[1:])) and all(
            0 <= s["guest_ms"] - s["observed_from_ms"] <= MAX_GAP_MS for s in samples)
        for name, ids in (("power_rises", ("low_up", "mid_up", "high")),
                          ("power_falls", ("high", "mid_down", "low_down"))):
            powers = [results.get(key, {}).get("model_power_median_w") for key in ids]
            valid = all(finite(p) for p in powers)
            checks[name] = valid and all((b - a if name == "power_rises" else a - b) >= 100
                                         for a, b in zip(powers, powers[1:]))
        checks["normal_levels"] = all(0 <= u["requested_level"] <= 20 and not u["defrost"]
                                      for s in samples for u in s["odus"])
        slew = slew_evidence(samples)
        checks["demand_slew_plausible"] = all(r["pairs"] >= 2 and r["plausible"] for r in slew.values())
        tail = self.result.get("post_complete", {})
        last = tail.get("last_valid_ms")
        end_ms = report.get("guest_uptime_ms", samples[-1]["guest_ms"] if samples else 0)
        checks["healthy_until_end"] = (self.result["stage"] == "complete" and last is not None
            and tail.get("invalid_since_ms") is None and 0 <= end_ms - last <= MAX_GAP_MS)
        checks["stable_topology_on_wire"] = True
        for name in ("low_up", "high", "low_down", "restart"):
            result = results.get(name, {})
            owners = result.get("owners", [])
            if len(owners) != (2 if name == "high" else 1):
                checks["stable_topology_on_wire"] = False
                continue
            for row in report["writes"]:
                if row["exception"] or not result["start_ms"] <= row["at_ms"] <= result["end_ms"]:
                    continue
                level, mode = command_value(row, 1999), command_value(row, 3999)
                if ((row["unit"] in owners and (level == 0 or mode == 0))
                        or (row["unit"] not in owners and ((level or 0) > 0 or mode == 2))):
                    checks["stable_topology_on_wire"] = False
        self.result.update(checks=checks, on_intervals=on, off_intervals=off, demand_slew=slew,
                           timing_coverage={"source": "R1999_R3999_commands_and_stable_model_feedback",
                                            "confirmed_start_margin": "not_qualified",
                                            "confirmed_standby_margin": "not_qualified"})
