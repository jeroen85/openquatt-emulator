"""Exercise CM98 at nominal, low and zero ODU flow on the factory binary."""
from .dynamics import Dynamics, near, value
from .lowflow import command_value

PUMP_START_LIMIT_MS = 20_000
PHASES = (
    ("nominal", 800, 20_000),
    ("low", 230, 40_000),
    ("zero", 0, 40_000),
    ("recovered", 800, 20_000),
)


class FixedFrostPump(Dynamics):
    settings = tuple(
        (domain, name, -2 if name == "api_input_outside_temperature" else expected)
        for domain, name, expected in Dynamics.settings
    )

    def __init__(self, thermostat, fleet):
        super().__init__(thermostat, fleet)
        self.index = 0
        self.cm98_entered_ms = None
        self.samples = {name: [] for name, _, _ in PHASES}
        self.result.update(
            version=1, samples=self.samples, phases={}, checks={},
        )

    def observe(self, get):
        if self.result["stage"] == "complete":
            return
        if self.result["stage"] == "configure":
            if self.configure(get):
                self.fleet.set_environment({"outdoor_c": -2, "flow_lph": 800})
                self.result["stage"] = "nominal"
            return
        phase = PHASES[self.index]
        name, target, duration_ms = phase
        outside = value(get, "sensor", "Outside Temperature (Selected)")
        mode = value(get, "text_sensor", "Control Mode (Label)")
        flow = value(get, "sensor", "Flow average (Selected)")
        with self.fleet.lock:
            _, pwm, pump_running = self.fleet._pump_commands()
            guest_ms = self.fleet.now_ms
            hz = [unit["frequency_hz"] for unit in self.fleet.snapshot()]
        sample = {"guest_ms": guest_ms, "mode": mode, "outside_c": outside, "flow_lph": flow,
                  "r2015": pwm, "pumps_running": pump_running, "compressor_hz": hz}
        self.result["current"] = sample
        in_cm98 = str(mode).split(" ", 1)[0] == "CM98"
        ready = in_cm98 and near(outside, -2) and near(flow, target, 14)
        samples = self.samples[name]
        if name == "nominal" and not samples:
            if in_cm98 and self.cm98_entered_ms is None:
                self.cm98_entered_ms = guest_ms
                self.result["cm98_entered_ms"] = guest_ms
            if self.cm98_entered_ms is None:
                return
            if guest_ms - self.cm98_entered_ms > PUMP_START_LIMIT_MS:
                raise RuntimeError("CM98 stuurde niet binnen 20 gastseconden iPWM 800 naar beide buitenunits")
            if not (ready and pwm == [800, 800] and pump_running == [True, True] and all(near(f, 0) for f in hz)):
                return
            self.result["nominal_established_ms"] = guest_ms
        if ready or samples:
            samples.append(sample)
        else:
            return
        if guest_ms - samples[0]["guest_ms"] < duration_ms:
            return
        self.result["phases"][name] = {"target_lph": target, "first_ms": samples[0]["guest_ms"],
                                      "last_ms": guest_ms, "count": len(samples)}
        if self.index + 1 == len(PHASES):
            self.result["stage"] = "complete"
            return
        self.index += 1
        next_phase, next_target, _ = PHASES[self.index]
        self.fleet.set_environment({"flow_lph": next_target})
        self.result["stage"] = next_phase

    def finalize(self, report):
        checks = self.result["checks"]
        for name, target, duration_ms in PHASES:
            samples = self.samples[name]
            checks[f"{name}_qualified"] = (
                len(samples) >= 5 and samples[-1]["guest_ms"] - samples[0]["guest_ms"] >= duration_ms
                and all(near(sample["outside_c"], -2) and str(sample["mode"]).split(" ", 1)[0] == "CM98"
                        and near(sample["flow_lph"], target, 14) and sample["r2015"] == [800, 800]
                        and sample["pumps_running"] == [True, True]
                        and all(near(hz, 0) for hz in sample["compressor_hz"]) for sample in samples)
            )
        first_ms = self.samples["nominal"][0]["guest_ms"] if self.samples["nominal"] else float("inf")
        writes = [{"unit": row["unit"], "guest_ms": row["at_ms"], "r2015": command_value(row, 2015),
                   "exception": row["exception"], "dropped": row["dropped"]}
                  for row in report["writes"] if command_value(row, 2015) is not None]
        self.result["r2015_writes"] = writes
        checks["both_units_received_800"] = {
            row["unit"] for row in writes if row["r2015"] == 800 and not row["exception"] and not row["dropped"]
        } == {1, 2}
        checks["no_other_r2015_after_nominal_established"] = bool(self.samples["nominal"]) and all(
            row["r2015"] == 800 and not row["exception"] and not row["dropped"]
            for row in writes if row["guest_ms"] >= first_ms
        )
