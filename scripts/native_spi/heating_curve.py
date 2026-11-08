"""Heating-curve staging and defrost regression through stock HTTP and Modbus."""
import json
from urllib.parse import urlencode

from .dynamics import Dynamics, entity, near, value
from .lowflow import command_value
from .odu_scenarios import command_transitions


def minimum_pair_hold(writes, begin, end):
    levels, modes, since, interval = [0, 0], [0, 0], None, None
    for row in writes:
        if row["exception"]:
            continue
        hp = row["unit"] - 1
        level, mode = command_value(row, 1999), command_value(row, 3999)
        if level is not None:
            levels[hp] = level
        if mode is not None:
            modes[hp] = mode
        active = [l > 0 and m in (1, 2, 4) for l, m in zip(levels, modes)]
        minimum = levels == [1, 1] and all(active)
        now = row["at_ms"]
        if minimum and since is None:
            since = now
        elif not minimum:
            if since is not None and sum(active) == 1 and begin <= now <= end:
                interval = {"start_ms": since, "end_ms": now, "ms": now - since}
            since = None
    return interval


def rundown_hold(writes, begin, end):
    levels, modes, baseline, since = [0, 0], [0, 0], None, None
    for row in writes:
        if row["exception"]:
            continue
        now, hp = row["at_ms"], row["unit"] - 1
        if now >= begin and baseline is None:
            baseline = sum(levels) if all(levels) and all(m in (1, 2, 4) for m in modes) else 0
        level, mode = command_value(row, 1999), command_value(row, 3999)
        if level is not None:
            levels[hp] = level
        if mode is not None:
            modes[hp] = mode
        if not begin <= now <= end:
            continue
        active = [l > 0 and m in (1, 2, 4) for l, m in zip(levels, modes)]
        if sum(active) == 1:
            return {"start_ms": since, "end_ms": now, "ms": now - since} if since is not None else None
        reduced_pair = all(active) and 0 < sum(levels) < baseline
        since = (since if since is not None else now) if reduced_pair else None
    return None


class HeatingCurve(Dynamics):
    target_name = "api_input_heating_supply_target"
    settings = Dynamics.settings + (
        ("select", "Quatt Hybrid version", "V2"),
        ("select", "Heating Curve Control Profile", "Comfort"),
        ("select", "Heating Supply Target Source", "API input"),
        ("number", target_name, 45),
        ("number", "Dual HP Disable Hold", 1),
        ("number", "Minimum runtime", 300),
        ("select", "Heating Control Mode", "Water Temperature Control (heating curve)"),
    )

    def __init__(self, thermostat, fleet):
        super().__init__(thermostat, fleet)
        self.target, self.last_refresh = 45, -5000
        self.holds = {}
        self.result["contract"] = {
            "profile": "Comfort", "startup_grace_s": 300, "recovery_emergency_hold_s": 240,
            "duo_off_hold_s": 60, "minimum_runtime_s": 300,
            "target_high_c": 45, "target_near_c": 30, "target_low_c": 28.5,
            "defrost_after_lead_s": 360, "defrost_observation_s": 195, "near_observation_s": 120,
        }
        self.result.update(version=1, samples=[], checks={})

    def sample(self, get):
        sample = super().sample(get)
        firmware = sample["firmware"]
        for key, domain, name in (
            ("regime", "text_sensor", "Strategy phase"),
            ("capacity", "text_sensor", "Curve capacity mode"),
            ("demand", "sensor", "Curve demand (effective)"),
            ("supply_c", "sensor", "Water Supply Temp (Selected)"),
            ("target_c", "sensor", "Heating Supply Target (Effective)"),
        ):
            firmware[key] = value(get, domain, name)
        raw = get("/openquatt/service/status", timeout=1)
        status = json.loads(raw).get("entities", {}) if raw else {}
        firmware["start_guard"] = status.get("duoStartGuardStatus", {}).get("value")
        return sample

    def set_target(self, get, target):
        path = entity("number", self.target_name) + "/set?" + urlencode({"value": target})
        ok = get(path, timeout=2, method="POST") is not None
        if ok:
            self.target = target
        return ok

    def held(self, name, sample, valid, duration):
        now = sample["guest_ms"]
        prior = self.holds.get(name)
        if not valid or now - sample["observed_from_ms"] > 12000:
            self.holds.pop(name, None)
            return False
        if prior is None or sample["observed_from_ms"] - prior[1] > 12000:
            self.holds[name] = [now, now]
            self.result["checkpoints"][name] = sample
        else:
            self.holds[name][1] = now
        return now - self.holds[name][0] >= duration

    def observe(self, get):
        stage = self.result["stage"]
        if stage in ("complete", "failed"):
            return
        if stage == "configure":
            if self.configure(get):
                self.result["stage"] = "standby"
            return
        if self.fleet.now_ms - self.last_refresh >= 5000:
            self.last_refresh = self.fleet.now_ms
            if (not self.set_target(get, self.target) or get(entity("number", "api_input_outside_temperature") +
                    "/set?value=8", method="POST") is None):
                raise RuntimeError("Heating-curve fixture input refresh failed")
        sample = self.sample(get)
        self.result["samples"].append(sample)
        self.result["current"] = sample
        fw, units, now = sample["firmware"], sample["odus"], sample["guest_ms"]
        running = [i for i in (0, 1) if self.heating(sample, i) and units[i]["requested_mode"] == "heating"]
        positive = [i for i in (0, 1) if units[i]["requested_level"] > 0]
        points = self.result["checkpoints"]
        lead = (self.result["unit"] or 1) - 1
        peer = 1 - lead

        def fail(reason):
            self.result.update(stage="failed", violation=reason)

        if stage == "standby":
            if (fw["ott_valid"] is True and fw["heat"] is False and near(fw["target_c"], 45)
                    and near(fw["supply_c"], 30) and self.no_flow(sample) and not positive):
                points["standby"] = sample
                self.set_heat(True)
                self.result["stage"] = "single_recovery"
        elif stage == "single_recovery":
            if len(positive) == 2:
                fail("Duo staged before the defrost fixture.")
                return
            valid = len(running) == 1 and fw["regime"] == "recovery" and self.has_flow(sample)
            if valid and "single_recovery" not in self.holds:
                self.result["unit"] = running[0] + 1
            if self.held("single_recovery", sample, valid, 360000):
                points["defrost_injected"] = sample
                self.fleet.set_odu({"index": self.result["unit"] - 1, "defrost": True})
                self.result["stage"] = "defrost"
        elif stage == "defrost":
            if peer in positive:
                fail("Peer received compressor demand during defrost.")
            elif self.held("defrost_confirmed", sample, fw["defrost"] is True and fw["valve"] is True
                           and units[lead]["defrost"] and self.has_flow(sample), 195000):
                points["defrost_held"] = sample
                self.result["defrost_release_ms"] = now
                self.fleet.set_odu({"index": lead, "defrost": False})
                self.result["stage"] = "restage"
        elif stage == "restage":
            if fw["defrost"] is False and fw["valve"] is False:
                points.setdefault("defrost_clear", sample)
            clear = points.get("defrost_clear")
            if peer in positive and not clear:
                fail("Peer staged before defrost clearance was observed.")
            elif self.held("duo_recovery", sample, len(running) == 2 and fw["regime"] == "recovery"
                           and self.has_flow(sample), 45000) and self.set_target(get, 30):
                points["duo_high"] = sample
                self.result["stage"] = "near_target"
        elif stage == "near_target":
            if len(positive) != 2:
                fail("Duo shed a compressor when the target met the supply.")
            elif self.held("maintain_near", sample, len(running) == 2 and self.has_flow(sample)
                           and fw["regime"] == "maintain" and near(fw["target_c"], 30)
                           and near(fw["supply_c"], 30), 120000) and self.set_target(get, 28.5):
                points["maintain_near_held"] = sample
                self.result["stage"] = "low_load"
        elif stage == "low_load":
            if fw["regime"] == "maintain" and near(fw["target_c"], 28.5):
                points.setdefault("low_load", sample)
                if len(positive) == 2 and sum(u["requested_level"] for u in units) < sum(
                        u["requested_level"] for u in points["maintain_near_held"]["odus"]):
                    points.setdefault("duo_ramp_down", sample)
                if len(positive) == 1:
                    points.setdefault("single_command", sample)
                    if running == positive and near(fw["frequency_hz"][1-running[0]], 0, .5) and self.has_flow(sample):
                        points["single_low_load"] = sample
                        self.result["stage"] = "single_stable"
        elif stage == "single_stable":
            expected = [i for i,u in enumerate(points["single_low_load"]["odus"]) if u["requested_level"] > 0]
            valid = positive == running == expected and self.has_flow(sample) and near(fw["frequency_hz"][1-running[0]], 0, .5)
            if not valid:
                fail("Single lost compressor activity, mode, flow or owner during recovery.")
            elif self.held("stable_hold", sample, True, 60000):
                points["single_stable"] = sample
                self.result["stage"] = "complete"

    def finalize(self, report):
        points = self.result["checkpoints"]
        checks = {name: name in points for name in (
            "standby", "single_recovery", "defrost_confirmed", "defrost_held", "defrost_clear",
            "duo_recovery", "maintain_near_held", "duo_ramp_down", "single_command", "single_stable",
        )}
        blocked = []
        if "defrost_injected" in points and "defrost_clear" in points:
            start = points["defrost_injected"]["observed_from_ms"]
            end = points["defrost_clear"]["guest_ms"]
            peer = 3 - self.result["unit"]
            blocked = [row for row in report["writes"] if row["unit"] == peer and start <= row["at_ms"] <= end
                       and not row["exception"] and ((command_value(row, 1999) or 0) > 0
                                                    or command_value(row, 3999) in (1, 2, 4))]
        checks["peer_blocked_on_wire"] = checks["defrost_clear"] and not blocked
        release = self.result.get("defrost_release_ms", points.get("defrost_held", {}).get("guest_ms"))
        clear = points.get("defrost_clear", {}).get("guest_ms")
        peer_index = 2-self.result["unit"] if self.result.get("unit") in (1,2) else None
        starts = [ms for hp,ms,active in command_transitions(report["writes"])
                  if release is not None and active and hp == peer_index and ms >= release]
        if release is not None and clear is not None and starts and 0 <= release <= clear:
            interval = [starts[0]-clear, starts[0]-release]
            self.result["restage_hold_window_ms"] = interval
            checks["restage_hold_proven"] = interval[0] >= 240000
            if interval[0] < 240000 <= interval[1]:
                self.result["measurement_issue"] = "De 240s-hold ligt binnen het meetinterval; de exacte vrijgavetijd ontbreekt."
        else:
            checks["restage_hold_proven"] = False
        if "single_low_load" in points and "single_stable" in points:
            begin = points["single_low_load"]["guest_ms"]
            end = points["single_stable"]["guest_ms"]
            idle = next(i for i, u in enumerate(points["single_low_load"]["odus"]) if u["requested_level"] == 0) + 1
            checks["single_stable_on_wire"] = end - begin >= 60000 and not any(
                row["unit"] == idle and begin <= row["at_ms"] <= end and not row["exception"]
                and (command_value(row, 1999) or 0) > 0 for row in report["writes"])
            active = 3 - idle
            checks["single_stable_on_wire"] &= not any(row["unit"] == active and begin <= row["at_ms"] <= end
                and not row["exception"] and (command_value(row, 1999) == 0 or command_value(row, 3999) == 0)
                for row in report["writes"])
        else:
            checks["single_stable_on_wire"] = False
        if "duo_ramp_down" in points and "single_command" in points:
            elapsed = points["single_command"]["guest_ms"] - points["duo_ramp_down"]["guest_ms"]
            self.result["ramp_to_single_ms"] = elapsed
            checks["ramp_before_shedding"] = elapsed >= 60000
        else:
            checks["ramp_before_shedding"] = False
        interval = minimum_pair_hold(report["writes"], points.get("low_load", {}).get("observed_from_ms", 0),
                                     points.get("single_command", {}).get("guest_ms", -1))
        self.result["minimum_pair_wire_hold"] = interval
        # This sample precedes the target POST; a later confirmation poll may
        # already miss the first legitimate reduction.
        before_target = points.get("maintain_near_held", points.get("low_load", {}))
        rundown = rundown_hold(report["writes"], before_target.get("observed_from_ms", 0),
                               points.get("single_command", {}).get("guest_ms", -1))
        checks["rundown_before_shedding_on_wire"] = rundown is not None and rundown["ms"] >= 60000
        self.result["rundown_wire_hold"] = rundown
        self.result["disable_hold_coverage"] = "Rundown observed; exact internal power-threshold timer not qualified."
        self.result.update(checks=checks, blocked_peer_writes=blocked)
