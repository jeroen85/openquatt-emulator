"""Observe defrost boiler assistance with an unmodified Q Duo factory image."""
import json
from urllib.parse import urlencode

from .dynamics import Dynamics, entity, near, value

OPT = "Boiler assist during defrost"
NORMAL = "Boiler assist enabled"
PRESENT = "Auxiliary heat source connected"


def write_setting(get, domain, name, target):
    current = value(get, domain, name)
    if current == target or (domain == "number" and current is not None and near(float(current), target, 0.001)):
        return True
    url = entity(domain, name)
    if domain == "switch":
        url += "/turn_on" if target else "/turn_off"
    else:
        url += "/set?" + urlencode({"option" if domain == "select" else "value": target})
    if current is not None:
        get(url, timeout=2, method="POST")
    return False


ASSIST_CASES = ("r1", "ot", "expire", "curve", "handover")
HANDOVER_VARIANTS = ("demand-drop", "held-load")


class DefrostAssist(Dynamics):
    def __init__(self, thermostat, fleet, case, variant):
        super().__init__(thermostat, fleet)
        self.case = case
        self.variant = variant
        self.result.update(version=1, case=self.case, points={}, fixtures={})
        self.rtd = thermostat.parent / "rtd-control"
        self.boiler_water = thermostat.parent / "boiler-water-control"
        self.settings_pending = [s for s in self.settings if s[1] != "Boiler connection"]
        self.settings_pending += [("select", "Water Supply Source", "Local"),
                                  ("select", "Boiler connection", "OpenTherm" if self.case == "ot" else "R1")]
        self.temp(34)
        self.boiler_water.write_text("42.5\n")
        self.stable_since = None
        self.previous_ms = None
        self.next_refresh_ms = 0
        self.demand = 15000
        self.normal_off_threshold = 10000 if self.variant == "held-load" else 400
        if self.case == "handover":
            self.settings_pending += [
                ("select", "Quatt Hybrid version", "V2"),
                ("select", "Heating Control Mode", "Power House"),
                ("select", "External Heat Demand Source", "API input"),
                ("number", "Rated maximum house power", 15000),
                ("number", "Boiler rated heat power", 15000),
                ("number", "CM3 deficit OFF threshold", self.normal_off_threshold),
            ]
            self.result["variant"] = self.variant
            self.result["contract"] = {"cm3_minimum_s": 300, "cm3_demote_confirmation_s": 120,
                                       "cm3_deficit_off_w": self.normal_off_threshold,
                                       "normal_and_defrost_permission_held": True}
            self.result["handover_samples"] = []
            self.end_after_complete_s = 20
        if self.case == "curve":
            self.settings_pending += [
                ("select", "Quatt Hybrid version", "V2"),
                ("select", "Heating Curve Control Profile", "Comfort"),
                ("select", "Heating Supply Target Source", "API input"),
                ("number", "api_input_heating_supply_target", 45),
                ("select", "Heating Control Mode", "Water Temperature Control (heating curve)"),
            ]

    def temp(self, degrees):
        pending = self.rtd.with_suffix(".next")
        pending.write_text(f"{degrees}\n")
        pending.replace(self.rtd)

    def step(self, stage):
        self.stable_since = self.previous_ms = None
        self.result["stage"] = stage

    def hold(self, condition, ms, duration_ms):
        if not condition or (self.previous_ms is not None and ms - self.previous_ms > 7500):
            self.stable_since = None
        self.previous_ms = ms
        if not condition:
            return False
        if self.stable_since is None:
            self.stable_since = ms
        return ms - self.stable_since >= duration_ms

    def inject(self, enabled, sample, label):
        for unit in range(2):
            self.fleet.set_odu({"index": unit, "defrost": unit == self.result["unit"] - 1 and enabled})
        self.result["fixtures"][label] = sample["guest_ms"]

    def observe(self, get):
        stage = self.result["stage"]
        points = self.result["points"]
        if stage == "complete":
            return
        if stage == "configure":
            if not self.configure(get):
                return
            option = value(get, "switch", OPT)
            if type(option) is not bool:
                return
            self.result["default_opt_out"] = option is False
            if not write_setting(get, "switch", NORMAL, self.case == "handover") or not write_setting(get, "switch", PRESENT, True):
                return
            self.set_heat(True)
            self.step("normal_cm3" if self.case == "handover" else "warm")
            return
        sample = self.sample(get)
        f = sample["firmware"]
        for key, domain, name in (
            ("cm", "text_sensor", "Control Mode"),
            ("source", "text_sensor", "Boiler command source"),
            ("target", "sensor", "Boiler command target temperature"),
            ("active", "binary_sensor", "Boiler active"),
            ("supply", "sensor", "Water Supply Temp (Selected)"),
            ("fallback", "binary_sensor", "Water Supply Temp Fallback Active"),
            ("option", "switch", OPT),
            ("normal", "switch", NORMAL),
        ):
            f[key] = value(get, domain, name)
        sample["sample_end_ms"] = self.fleet.now_ms
        self.result["current"] = sample
        ms = sample["sample_end_ms"]
        cm2_idle = f["cm"] == "CM2" and f["source"] == "None" and f["active"] is False
        defrost = f.get("defrost") is True and f.get("valve") is True
        clear = f.get("defrost") is False and f.get("valve") is False
        flowing = self.has_flow(sample)
        running = [i for i in (0, 1) if self.heating(sample, i)]
        off = f["active"] is False and f["source"] != "Defrost assist"
        assisted = f["cm"] == "CM3" and f["source"] == "Defrost assist" and f["normal"] is False
        if self.case == "handover" and ms >= self.next_refresh_ms:
            url = entity("number", "api_input_external_heat_demand") + "/set?" + urlencode({"value": self.demand})
            if get(url, timeout=2, method="POST") is not None:
                self.next_refresh_ms = ms + 10000
        if self.case == "handover":
            return self.observe_handover(get, stage, sample, points, defrost, clear, flowing, running, cm2_idle)
        if self.case == "r1" and stage not in ("warm", "disabled", "clear"):
            return self.observe_timers(get, stage, sample, points, clear, cm2_idle, assisted)
        if stage == "warm":
            if self.hold(cm2_idle and flowing and bool(running) and near(f["supply"], 34, 0.25)
                         and f["fallback"] is False and (self.case != "ot" or f["otb_link"] is True)
                         and ms > 140000, ms, 8000):
                self.result["unit"] = max(running, key=lambda i: sample["odus"][i]["frequency_hz"]) + 1
                points["reference"] = sample
                if self.case == "r1":
                    self.inject(True, sample, "disabled")
                    self.step("disabled")
                else:
                    self.step("enable")
        elif stage == "disabled":
            if self.hold(defrost and flowing and cm2_idle and f["option"] is False, ms, 15000):
                points["disabled"] = sample
                self.inject(False, sample, "disabled_clear")
                self.step("enable")
        elif stage == "enable":
            if clear and write_setting(get, "switch", OPT, True):
                self.inject(True, sample, "positive")
                self.step("positive")
        elif stage == "positive":
            if defrost and assisted and flowing and near(f["target"], points["reference"]["firmware"]["supply"] + 3, 0.15):
                points["latched"] = sample
                self.temp(28)
                self.step("dip")
        elif stage == "dip":
            if self.case == "ot":
                reason = value(get, "text_sensor", "Boiler block reason")
                f["boiler_block"] = reason
                if self.hold(defrost and assisted and flowing and near(f["supply"], 28, 0.25)
                             and near(f["boiler_c"], 42.5) and f["active"] is False
                             and "too high" in str(reason).lower(), ms, 10000):
                    points["hot_boiler_blocked"] = sample
                    self.result["fixtures"]["ot_cool"] = ms
                    self.boiler_water.write_text("30\n")
                    self.step("ot_cooled")
                return
            if self.hold(defrost and assisted and flowing and near(f["supply"], 28, 0.25) and
                         near(f["target"], points["reference"]["firmware"]["supply"] + 3, 0.15)
                         and f["active"] is True, ms, 8000):
                points["dip"] = sample
                if self.case == "expire":
                    self.step("expire")
                elif self.case == "curve":
                    self.fleet.set_odu({"index": self.result["unit"] - 1, "no_flow": True})
                    self.result["fixtures"]["cut_flow"] = sample["guest_ms"]
                    self.step("curve_lowflow")
                else:
                    self.step("clamp")
        elif stage == "ot_cooled":
            if self.hold(defrost and assisted and flowing and near(f["supply"], 28, 0.25)
                         and near(f["target"], points["reference"]["firmware"]["supply"] + 3, 0.15)
                         and near(f["boiler_c"], 30) and f["active"] is True, ms, 12000):
                points["dip"] = sample
                self.inject(False, sample, "positive_clear")
                self.step("clear")
        elif stage == "curve_lowflow":
            if self.hold(defrost and self.no_flow(sample) and f["active"] is False and
                         f["cm"] == "CM1" and f["lowflow_fault"] is True, ms, 6000):
                points["lowflow_shuts_relay"] = sample
                self.temp(34)
                self.fleet.set_odu({"index": self.result["unit"] - 1, "no_flow": False})
                self.step("curve_flow_restored")
        elif stage == "curve_flow_restored":
            if self.hold(defrost and flowing and f["lowflow_fault"] is False and
                         f["active"] is False and f["source"] != "Defrost assist", ms, 20000):
                points["no_restart_after_flow_loss"] = sample
                self.inject(False, sample, "flow_defrost_clear")
                self.step("clear")
        elif stage == "expire":
            if defrost and cm2_idle:
                points["expired"] = sample
                self.temp(34)
                self.step("expired_hold")
            elif not (defrost and assisted and f["active"] is True):
                self.result["violation"] = "Assistance or ODU report interrupted before expiry."
        elif stage == "expired_hold":
            if self.hold(defrost and cm2_idle and near(f["supply"], 34, 0.25), ms, 20000):
                points["no_restart_after_expiry"] = sample
                self.inject(False, sample, "expired_clear")
                self.step("clear")
        elif stage == "clear":
            self.temp(34)
            if self.hold(clear and cm2_idle and near(f["supply"], 34, 0.25), ms, 10000):
                points["clear"] = sample
                self.step("complete")

    def observe_timers(self, get, stage, sample, p, clear, idle, assisted):
        f, ms = sample["firmware"], sample["sample_end_ms"]
        any_report = f.get("defrost") is True or f.get("valve") is True
        target = p["reference"]["firmware"]["supply"] + 3
        now = self.result.setdefault("observed_ms", {})
        series = self.result.setdefault("timer_samples", [])
        series.append([ms, stage, f.get("defrost"), f.get("valve"), f["cm"], f["source"], f["active"]])

        if stage == "enable":
            if clear and write_setting(get, "switch", OPT, True):
                now["last_clear_before_pulse"] = ms
                self.inject(True, sample, "pulse")
                self.step("pulse_seen")
        elif stage == "pulse_seen":
            if any_report:
                p["pulse_first_report"] = sample
                self.inject(False, sample, "pulse_removed")
                self.step("pulse_clears")
        elif stage == "pulse_clears":
            if clear:
                p.setdefault("pulse_clear", sample)
                if self.hold(idle, ms, 16000):
                    self.inject(True, sample, "positive")
                    self.step("confirmed")
            else:
                self.hold(False, ms, 16000)
        elif stage == "confirmed":
            if any_report:
                p.setdefault("positive_first_report", sample)
            if any_report and assisted and near(f["target"], target, 0.15):
                p["latched"] = sample
                self.step("relay_at_reference")
        elif stage == "relay_at_reference":
            if self.hold(assisted and f["active"] is True and near(f["supply"], target - 3, 0.25), ms, 4000):
                p["relay_stays_on_at_old_reference"] = sample
                self.inject(False, sample, "early_clear")
                self.step("min_run")
        elif stage == "min_run":
            if clear and assisted and f["active"] is True:
                p.setdefault("clear_during_minimum", sample)
                p["last_minimum_hold"] = sample
            if clear and idle:
                p["minimum_released"] = sample
                self.step("early_rearm")
        elif stage == "early_rearm":
            if clear and idle and ms - p["minimum_released"]["sample_end_ms"] >= 90000:
                self.inject(True, sample, "rearm_early")
                self.step("early_rearm_hold")
        elif stage == "early_rearm_hold":
            if self.hold(any_report and idle, ms, 22000):
                p["early_rearm_blocked"] = sample
                self.inject(False, sample, "rearm_early_clear")
                self.step("rearm_wait")
        elif stage == "rearm_wait":
            if clear and idle:
                p.setdefault("rearm_clear", sample)
            if self.hold(clear and idle, ms, 125000):
                p["rearm_completed"] = sample
                self.inject(True, sample, "rearm_positive")
                self.step("rearmed")
        elif stage == "rearmed":
            if any_report and assisted and f["active"] is True and near(f["target"], target, 0.15):
                p["rearmed"] = sample
                self.temp(28)
                self.step("rearmed_dip")
        elif stage == "rearmed_dip":
            if self.hold(any_report and assisted and f["active"] is True and near(f["supply"], 28, 0.25)
                         and near(f["target"], target, 0.15), ms, 5000):
                p["dip"] = sample
                self.step("cap")
        elif stage == "cap":
            if (write_setting(get, "number", "Maximum water temperature", 37)
                and any_report and assisted and f["active"] is True and near(f["target"], 34)):
                p["cap"] = sample
                self.step("lift_cap")
        elif stage == "lift_cap":
            if write_setting(get, "number", "Maximum water temperature", 60) and near(f["target"], target, 0.15):
                self.temp(37)
                self.step("reach_target")
        elif stage == "reach_target":
            if any_report and assisted and near(f["supply"], 37, 0.25) and f["active"] is False:
                p["r1_at_target_off"] = sample
                self.temp(28)
                self.step("supply_loss_prepare")
        elif stage == "supply_loss_prepare":
            if any_report and assisted and near(f["supply"], 28, 0.25) and f["active"] is True:
                p["before_supply_loss"] = sample
                # The next rearmed session may be older than its minimum.
                self.step("supply_loss")
        elif stage == "supply_loss":
            write_setting(get, "select", "Water Supply Source", "HA input")
            if any_report and idle and f["fallback"] is True:
                p["supply_loss"] = sample
                self.temp(34)
                self.step("supply_recovered")
        elif stage == "supply_recovered":
            write_setting(get, "select", "Water Supply Source", "Local")
            if self.hold(any_report and idle and f["fallback"] is False and near(f["supply"], 34, 0.25), ms, 15000):
                p["no_restart_same_report"] = sample
                self.inject(False, sample, "supply_loss_clear")
                self.step("clear")

    def observe_handover(self, get, stage, sample, points, defrost, clear, flowing, running, cm2_idle):
        f = sample["firmware"]
        f["hp_deficit_w"] = value(get, "sensor", "HP deficit (W)")
        f["boiler_power_request_w"] = value(get, "sensor", "Boiler command requested power")
        f["override"] = value(get, "select", "CM Override")
        f["aux_present"] = value(get, "switch", PRESENT)
        ms = sample["sample_end_ms"] = self.fleet.now_ms
        self.result["handover_samples"].append({
            **{key: f.get(key) for key in ("cm", "source", "target", "active", "defrost", "valve", "normal",
                                        "option", "override", "aux_present", "hp_deficit_w", "boiler_power_request_w",
                                        "heat", "supply", "flow_lph", "fallback")},
            "guest_ms": ms, "stage": stage, "demand_w": self.demand,
        })
        normal_cm3 = f["cm"] == "CM3" and f["normal"] is True and f["source"] == "Power House"
        if stage == "normal_cm3":
            enabled = write_setting(get, "switch", OPT, True)
            if f["cm"] == "CM3":
                points.setdefault("normal_cm3_first_seen", sample)
            if self.hold(enabled and normal_cm3 and f["active"] is True and flowing and len(running) == 2
                         and f["fallback"] is False and near(f["supply"], 34, 0.25), ms, 10000):
                points["normal_cm3_duo"] = sample
                self.result["unit"] = max(running, key=lambda i: sample["odus"][i]["frequency_hz"]) + 1
                self.inject(True, sample, "normal_cm3_defrost")
                self.step("normal_during_defrost")
        elif stage == "normal_during_defrost":
            if self.hold(defrost and normal_cm3 and f["active"] is True and flowing and sum(odu["defrost"] for odu in sample["odus"]) == 1,
                         ms, 20000):
                points["normal_during_defrost"] = sample
                if self.variant == "demand-drop":
                    self.demand = 2600
                    self.next_refresh_ms = 0
                self.result["fixtures"]["demand_after_defrost"] = {"guest_ms": ms, "watts": self.demand}
                self.temp(28)
                self.step("handover")
        elif stage == "handover":
            if f["cm"] is not None and f["cm"] != "CM3":
                self.result["violation"] = f"Mode left CM3 before handover: {f['cm']}"
            if normal_cm3:
                points["last_normal"] = sample
            if defrost and f["cm"] == "CM3" and f["source"] == "Defrost assist" and near(f["target"], 37, 0.25):
                points["handover"] = sample
                self.step("handover_active")
        elif stage == "handover_active":
            if self.hold(defrost and f["cm"] == "CM3" and f["source"] == "Defrost assist" and f["active"] is True,
                         ms, 8000):
                points["handover_active"] = sample
                self.inject(False, sample, "handover_clear")
                self.temp(34)
                self.step("handover_clear")
        elif stage == "handover_clear":
            if self.hold(clear and cm2_idle and near(f["supply"], 34, 0.25), ms, 10000):
                points["clear"] = sample
                data = get("/openquatt/decision-log", timeout=3)
                if data:
                    self.result["decision_log"] = json.loads(data).get("events", [])
                self.step("complete")

    def finalize(self, report):
        p = self.result["points"]
        events = [json.loads(line) for line in (self.thermostat.parent / "mmio.jsonl").read_text().splitlines()]
        relay = [e for e in events if e["kind"] == "r1"]
        ot = [e for e in events if e["kind"] == "opentherm" and e["link"] == "otb"]
        checks = {"fresh_flash_opt_out": self.result.get("default_opt_out") is True,
                  "reference_frozen_through_dip": {"reference", "latched", "dip"}.issubset(p),
                  "clear_returns_to_cm2": "clear" in p}
        self.result["relay_edges"] = relay
        if self.case == "handover":
            checks.pop("reference_frozen_through_dip")
            checks["duo_normal_cm3_keeps_ownership"] = {"normal_cm3_duo", "normal_during_defrost"}.issubset(p)
            checks["cm3_handover_and_frozen_reference"] = {"handover", "handover_active"}.issubset(p)
            checks["gpio16_energized_and_released"] = any(e["energized"] for e in relay) and bool(relay) and not relay[-1]["energized"]
            required = {"normal_cm3_first_seen", "normal_during_defrost", "last_normal", "handover", "handover_active"}
            if required.issubset(p):
                stamp = lambda name: p[name]["sample_end_ms"]
                first = stamp("normal_during_defrost")
                last = stamp("handover_active")
                window = [stamp("last_normal"), stamp("handover")]
                samples = [row for row in self.result["handover_samples"] if first <= row["guest_ms"] <= last]
                events = self.result.get("decision_log", [])
                mode_events = [e for e in events if e.get("event_type") == "control_mode_change"]
                self.result["cm_events"] = mode_events
                self.result["handover_source_window_ms"] = window
                self.result["normal_cm3_age_when_handover_first_seen_ms"] = window[1] - stamp("normal_cm3_first_seen")
                checks["permissions_and_auto_held"] = bool(samples) and all(
                    row["normal"] is True and row["option"] is True and row["aux_present"] is True
                    and row["override"] == "Auto" and row["heat"] is True for row in samples)
                checks["cm3_preserved_with_defrost"] = bool(samples) and all(
                    row["cm"] == "CM3" and row["defrost"] is True and row["valve"] is True
                    for row in samples) and bool(mode_events) and not any(
                    first <= e.get("uptime_s", -1) * 1000 <= last for e in mode_events)
                checks["normal_cm3_timers_elapsed"] = window[1] - stamp("normal_cm3_first_seen") >= 417000
                before = [e for e in relay if e["guest_us"] <= first * 1000]
                after = [e for e in relay if e["guest_us"] <= last * 1000]
                checks["gpio16_measured_on_both_sides"] = bool(before and after) and before[-1]["energized"] and after[-1]["energized"]
                gaps = []
                for left, right in zip(relay, relay[1:]):
                    if not left["energized"] and right["energized"]:
                        begin, end = left["guest_us"] / 1000, right["guest_us"] / 1000
                        if begin <= last and end >= first:
                            gaps.append({"begin_guest_ms": begin, "end_guest_ms": end, "duration_ms": end - begin,
                                         "starts_before_last_normal_sample": begin < window[0],
                                         "touches_handover_window": begin <= window[1] and end >= window[0]})
                self.result["r1_off_intervals_during_defrost_and_handover"] = gaps
            else:
                checks["automatic_handover_evidence"] = False
        if self.case in ("r1", "expire", "curve"):
            checks["gpio16_energized_and_released"] = any(e["energized"] for e in relay) and bool(relay) and not relay[-1]["energized"]
        if self.case == "r1":
            fixtures = self.result["fixtures"]
            samples = self.result.get("timer_samples", [])
            stamp = lambda name: p.get(name, {}).get("sample_end_ms", -1)
            pulse_rows = [row for row in samples if row[1] in ("pulse_seen", "pulse_clears")]
            positive_rows = [row for row in samples if row[1] == "confirmed"]
            rearm_rows = [row for row in samples if row[1] in ("early_rearm", "early_rearm_hold", "rearm_wait")]
            minimum_rows = [row for row in samples if row[1] == "min_run" and row[0] < stamp("minimum_released")]
            first_on = next((i for i, row in enumerate(positive_rows) if row[4] == "CM3"), None)
            on_window = None
            confirm_window = None
            minimum_window = None
            persistence = False
            if first_on is not None and first_on > 0 and "positive_first_report" in p:
                on_window = [positive_rows[first_on-1][0], positive_rows[first_on][0]]
                first_report = stamp("positive_first_report")
                # The report was known to be clear immediately before writing the fixture.
                last_clear = next((row[0] for row in reversed(positive_rows[:first_on])
                                   if row[2] is False and row[3] is False),
                                  fixtures["positive"])
                confirm_window = [on_window[0] - first_report, on_window[1] - last_clear]
                persistence = all(row[2] is True or row[3] is True
                                  for row in positive_rows[:first_on+1] if row[0] >= first_report)
                if "last_minimum_hold" in p and "minimum_released" in p:
                    minimum_window = [stamp("last_minimum_hold") - on_window[1],
                                      stamp("minimum_released") - on_window[0]]
            times = {
                "observed_pulse_ms": stamp("pulse_clear") - stamp("pulse_first_report"),
                "first_report_to_cm3_window_ms": confirm_window,
                "cm3_minimum_window_ms": minimum_window,
                "continuous_clear_before_new_report_ms": stamp("rearm_completed") - stamp("rearm_clear"),
            }
            self.result["observed_timer_deltas_ms"] = times
            checks["disabled_during_confirmed_defrost"] = "disabled" in p
            checks["short_report_does_not_start"] = ("pulse_clear" in p and 0 < times["observed_pulse_ms"] <= 14000
                and bool(pulse_rows) and all(row[4] == "CM2" and row[5] == "None" and row[6] is False for row in pulse_rows))
            checks["persistent_report_confirmed"] = ("latched" in p and persistence
                and confirm_window is not None and 8000 <= confirm_window[0] <= 15000
                and confirm_window[1] >= 10000 and confirm_window[1] - confirm_window[0] <= 12000)
            checks["active_for_minimum_after_early_report_clear"] = (
                "clear_during_minimum" in p and minimum_window is not None
                and minimum_window[0] <= 60000 <= minimum_window[1]
                and minimum_window[1] - minimum_window[0] <= 12000
                and all(row[4] == "CM3" and row[5] == "Defrost assist" and row[6] is True
                        for row in minimum_rows if row[0] <= stamp("last_minimum_hold")
                        and row[2] is False and row[3] is False))
            checks["blocks_early_report_and_rearms_on_new_report"] = (
                {"early_rearm_blocked", "rearm_completed", "rearmed"}.issubset(p)
                and times["continuous_clear_before_new_report_ms"] >= 120000
                and all(row[4] == "CM2" and row[5] == "None" and row[6] is False for row in rearm_rows))
            checks["margin_target_band_and_soft_limit"] = {"relay_stays_on_at_old_reference", "cap", "r1_at_target_off"}.issubset(p)
            checks["supply_loss_and_restore_no_repeat"] = {"supply_loss", "no_restart_same_report"}.issubset(p)
        if self.case == "curve":
            checks["lowflow_shuts_r1_and_does_not_repeat"] = {"lowflow_shuts_relay", "no_restart_after_flow_loss"}.issubset(p)
        if self.case == "expire":
            elapsed = ((p["expired"]["sample_end_ms"] - p["latched"]["sample_end_ms"])
                       if "expired" in p and "latched" in p else -1)
            self.result["expiry_observed_delta_ms"] = elapsed
            checks["expires_near_900s_and_does_not_repeat"] = 888000 <= elapsed <= 912000 and "no_restart_after_expiry" in p
        if self.case == "ot":
            tset = []
            ch = []
            for e in ot:
                word = e["word"]
                category, data_id, payload = (word >> 28) & 7, (word >> 16) & 255, word & 65535
                if e["direction"] == "controller-tx" and (category, data_id) == (1, 1):
                    tset.append([e["guest_us"] // 1000, payload / 256])
                if e["direction"] == "controller-tx" and (category, data_id) == (0, 0):
                    ch.append([e["guest_us"] // 1000, bool(payload & 256)])
            self.result["wire_tset_changes"] = [x for i, x in enumerate(tset) if not i or tset[i-1][1] != x[1]]
            self.result["wire_ch_changes"] = [x for i, x in enumerate(ch) if not i or ch[i-1][1] != x[1]]
            start = self.result["fixtures"].get("positive", -1)
            end = self.result["fixtures"].get("positive_clear", -1)
            ref = p.get("reference", {}).get("firmware", {}).get("supply")
            checks["otb_tset_and_ch_on_wire"] = (ref is not None and
                any(start <= ms <= end and near(c, ref + 3, 0.25) for ms, c in tset) and
                any(start <= ms <= end and on for ms, on in ch) and
                bool(ch) and ch[-1][0] > end and ch[-1][1] is False)
            cooled = self.result["fixtures"].get("ot_cool", -1)
            checks["hot_boiler_interlock_no_ch"] = ("hot_boiler_blocked" in p and start <= cooled <= end
                and any(start <= ms <= cooled for ms, _ in ch)
                and not any(start <= ms <= cooled and on for ms, on in ch))
            checks["r1_stays_off_on_opentherm"] = not any(e["energized"] for e in relay)
        self.result["checks"] = checks
