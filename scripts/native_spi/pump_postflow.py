"""Replay a compressor rundown and measure postflow through the first frost phase."""

import json
from urllib.parse import urlencode

from .dynamics import Dynamics, at_least, entity, value
from .lowflow import command_value
from .model import OduFleet
from .power_house import PowerHouse

# Relative timing and a rounded hydraulic fixture. Raw field captures stay outside the repo.
RAMP = ((0, 2, 70), (5000, 2, 68), (15000, 0, 66), (25000, 0, 59),
        (35000, 0, 47), (45000, 0, 39), (55000, 0, 27), (65000, 0, 20), (75000, 0, 0))
FLOW_POINTS = ((492, 558), (545, 486), (616, 404), (710, 220), (800, 0))
OUTSIDE_C = -1.4


def approximate_flow(ipwm):
    if ipwm is None or ipwm >= FLOW_POINTS[-1][0]:
        return 0
    for (low_pwm, low_flow), (high_pwm, high_flow) in zip(FLOW_POINTS, FLOW_POINTS[1:]):
        if ipwm <= low_pwm:
            return low_flow
        if ipwm <= high_pwm:
            return low_flow + (high_flow - low_flow) * (ipwm - low_pwm) / (high_pwm - low_pwm)
    raise ValueError("iPWM valt buiten de postflowfixture")


def no_restart_commands(rows):
    return bool(rows) and all(
        command_value(row, register) in (None, 0) for row in rows for register in (1999, 3999)
    )


class PostflowFleet(OduFleet):
    def __init__(self, library):
        super().__init__(library)
        self.armed = False
        self.demand_withdrawal_ms = None
        self.stop_ms = None
        self.wire = []
        self.set_odu({"index": 0, "profile": "V2 new"})
        self.set_odu({"index": 1, "profile": "V2 old"})
        self.set_environment({"outdoor_c": OUTSIDE_C, "return_c": 30.0, "flow_lph": 1035})

    def write(self, index, address, value):
        return self.record_stop(index, address, [value], super().write(index, address, value))

    def write_many(self, index, address, values):
        return self.record_stop(index, address, values, super().write_many(index, address, values))

    def record_stop(self, index, address, values, success):
        if (success and self.demand_withdrawal_ms is not None and self.now_ms >= self.demand_withdrawal_ms
                and self.stop_ms is None and index == 0):
            if any(address <= register < address + len(values) and values[register-address] == 0
                   for register in (1999, 3999)):
                self.stop_ms = self.now_ms
        return success

    def read(self, index, address):
        if getattr(self, "armed", False) and index == 0:
            if address == 2103 and self.stop_ms is None:
                return RAMP[0][2]
            if self.stop_ms is not None:
                elapsed = self.now_ms - self.stop_ms
                point = next(row for row in reversed(RAMP) if row[0] <= elapsed)
                if address == 2099:
                    return point[1]
                if address == 2103:
                    return point[2]
                if address == 2138:
                    on = super().read(index, 2010) == 4096
                    pwm = super().read(index, 2015)
                    flow = approximate_flow(pwm) if on else 0
                    return round(flow / 0.618)
        return super().read(index, address)

    def observe_request(self, index, address, fc, start, count, dropped):
        super().observe_request(index, address, fc, start, count, dropped)
        if index == 0 and self.stop_ms is not None and fc == 3 and start <= 2103 < start + count and not dropped:
            self.wire.append({"ms": self.now_ms, "mode": self.read(0, 2099), "hz": self.read(0, 2103),
                              "relay": self.read(0, 2010), "ipwm": self.read(0, 2015),
                              "flow": round(self.read(0, 2138) * 0.618, 1)})

class PumpPostflow(Dynamics):
    settings = tuple(r for r in PowerHouse.settings if r[1] not in ("api_input_external_heat_demand", "api_input_outside_temperature")) + (
        ("number", "api_input_outside_temperature", OUTSIDE_C),
        ("number", "api_input_external_heat_demand", 2600),
    )

    def __init__(self, thermostat, fleet):
        super().__init__(thermostat, fleet)
        self.result.update(version=1, samples=[], checks={}, frequency_replay=RAMP,
                           flow_fixture=FLOW_POINTS, outside_c=OUTSIDE_C)
        self.last_refresh = -40000
        self.last_sample = -5000
        self.stop_sent = False

    @staticmethod
    def write_number(get, name, target):
        path = entity("number", name) + "/set?" + urlencode({"value": target})
        if get(path, method="POST", timeout=2) is None:
            raise RuntimeError(f"Cannot set fixture input {name}")

    def observe(self, get):
        stage = self.result["stage"]
        if stage == "complete":
            return
        if stage == "configure":
            if self.configure(get):
                self.set_heat(True)
                self.result["stage"] = "heating"
            return
        if self.fleet.now_ms - self.last_refresh >= 30000:
            for name, setting in (("api_input_external_heat_demand", 0 if self.stop_sent else 2600),
                                  ("api_input_room_temperature", 21), ("api_input_room_setpoint", 21),
                                  ("api_input_outside_temperature", OUTSIDE_C)):
                self.write_number(get, name, setting)
            self.last_refresh = self.fleet.now_ms
        if self.fleet.now_ms - self.last_sample < 3500:
            return
        self.last_sample = self.fleet.now_ms
        with self.fleet.lock:
            observed = self.fleet.now_ms
            wire_hz = self.fleet.read(0, 2103)
            pwm = self.fleet.read(0, 2015)
            level = self.fleet.read(0, 1999)
            stop_ms = self.fleet.stop_ms
        sample = {"ms": observed, "wire_hz": wire_hz, "ipwm": pwm, "level": level,
                  "cm": value(get, "text_sensor", "Control Mode (Label)"),
                  "flow": value(get, "sensor", "Flow average (Selected)"),
                  "hz": value(get, "sensor", "HP1 - Compressor frequency"),
                  "hp2_hz": value(get, "sensor", "HP2 - Compressor frequency"),
                  "flow_mode": value(get, "text_sensor", "Flow Control Mode Status")}
        self.result["samples"].append(sample)
        self.result["current"] = sample
        if stage == "heating":
            if (sample["cm"] or "").startswith("CM2") and level > 0 and sample["hz"] and sample["hp2_hz"] == 0:
                with self.fleet.lock:
                    self.fleet.armed = True
                self.result["stage"] = "armed"
            return
        if stage == "armed":
            if sample["hz"] == RAMP[0][2] and level > 0 and (sample["cm"] or "").startswith("CM2"):
                with self.fleet.lock:
                    if not at_least(self.fleet.read(0, 1999), 1):
                        return
                    self.set_heat(False)
                    withdrawn = self.fleet.demand_withdrawal_ms = self.fleet.now_ms
                self.result["demand_withdrawal_ms"] = withdrawn
                self.stop_sent = True
                self.result["stage"] = "rundown"
                self.write_number(get, "api_input_external_heat_demand", 0)
            return
        if stop_ms is None:
            return
        self.result["stop_ms"] = stop_ms
        if observed - stop_ms < 130000:
            return
        raw = get("/openquatt/decision-log", timeout=3)
        if not raw:
            return
        self.result["events"] = [event for event in json.loads(raw)["events"]
                                 if event["uptime_s"] * 1000 + 999 >= stop_ms]
        self.result["stage"] = "complete"

    def finalize(self, report):
        self.result["wire_feedback"] = self.fleet.wire.copy()
        stop_ms = self.result.get("stop_ms")
        if stop_ms is None:
            self.result["checks"] = {"stop_command_seen": False}
            return
        ramp_wire = [x for x in self.fleet.wire if x["hz"] > 0]
        zero_wire = [x for x in self.fleet.wire if x["hz"] == 0]
        events = self.result.get("events", [])
        sample_rows = self.result["samples"]
        def event(kind, subject, reason=None):
            return next((x for x in events if x["event_type"] == kind and x["subject"] == subject
                         and (reason is None or x["reason"] == reason)), None)
        timeout = event("incident_start", "HP1", "hp_stop_unconfirmed")
        confirmed = event("hp_stop_confirmed", "HP1")
        frost = event("frost_protection_start", "SYSTEM")
        after = [x for x in sample_rows if x["ms"] >= stop_ms + 105000]
        waiting = [x for x in sample_rows if stop_ms + 31000 <= x["ms"] < stop_ms + 75000]
        commands = [x for x in report["writes"] if x["unit"] == 1 and not x["exception"]
                    and not x["dropped"] and x["at_ms"] >= stop_ms]
        boost = zero_flow = None
        if frost:
            frost_ms = frost["uptime_s"] * 1000
            zero_flow = next((row for row in zero_wire if row["ms"] >= frost_ms and row["flow"] == 0), None)
        if zero_flow:
            boost = next((row["at_ms"] for row in commands
                          if row["at_ms"] >= zero_flow["ms"]
                          and (pwm := command_value(row, 2015)) is not None and pwm <= 440), None)
        boosted = [row for row in zero_wire if boost is not None and row["ms"] >= boost]
        selected = [row for row in sample_rows if boosted and row["ms"] >= boosted[0]["ms"]]
        restored = next((row for row in selected if at_least(row["flow"], 400)), None)
        held = [row for row in selected if restored and row["ms"] >= restored["ms"]]
        self.result["recovery"] = {
            "zero_modbus_flow_ms": zero_flow["ms"] if zero_flow else None,
            "pump_boost_ms": boost,
            "restored_modbus_flow_ms": boosted[0]["ms"] if boosted else None,
            "restored_selected_flow_ms": restored["ms"] if restored else None,
        }
        self.result["checks"] = {
            "demand_withdrawn_before_stop": 0 <= self.result.get("demand_withdrawal_ms", -1) <= stop_ms,
            "rundown_replayed": {47, 39, 27, 20}.issubset(row["hz"] for row in ramp_wire)
            and len(zero_wire) >= 2,
            "postflow_while_turning": bool(waiting) and all((x["cm"] or "").startswith("CM1") for x in waiting)
            and bool(ramp_wire) and all(x["relay"] == 4096 and x["flow"] >= 400 for x in ramp_wire),
            "watchdog_and_recovery": timeout is not None and confirmed is not None
            and event("incident_clear", "HP1", "hp_stop_unconfirmed") is not None
            and 57000 <= timeout["uptime_s"] * 1000 - stop_ms <= 64000
            and confirmed["uptime_s"] > timeout["uptime_s"] and len(zero_wire) >= 2
            and confirmed["uptime_s"] * 1000 + 999 >= zero_wire[1]["ms"],
            "frost_after_confirmed_stop": bool(after) and all((x["cm"] or "").startswith("CM98") for x in after)
            and confirmed is not None and frost is not None and events.index(frost) > events.index(confirmed)
            and len(zero_wire) >= 2 and frost["uptime_s"] * 1000 + 999 >= zero_wire[1]["ms"],
            "frost_flow_recovered": boost is not None and boost - zero_flow["ms"] <= 16000
            and len(boosted) >= 2 and boosted[0]["ms"] - boost <= 11000
            and all(row["flow"] >= 400 for row in boosted)
            and len(held) >= 2 and all(at_least(row["flow"], 400) for row in held)
            and restored["ms"] - boosted[0]["ms"] <= 10000,
            "no_restart_during_stop": no_restart_commands(commands),
        }
