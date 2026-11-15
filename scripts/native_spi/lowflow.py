"""Low-flow interruption during defrost, using the unmodified Q Duo firmware."""

import json
import math

from .dynamics import Dynamics, at_least, near


# Policy from OpenQuatt 31282d13 (oq_substitutions_common.yaml).
MIN_FLOW_LPH = 400
FAULT_MS = 60000
RECOVER_MS = 30000
MIN_OFF_MS = 240000
SUPERVISORY_MS = 5000
HOLD_MS = 65000


def window(before, after):
    return [before["observed_from_ms"], after["guest_ms"]]


def elapsed_window(first, second):
    return [second[0] - first[1], second[1] - first[0]]


def matches_timer(observed, expected_ms):
    return (observed is not None and observed[0] <= expected_ms + SUPERVISORY_MS + 1000
            and observed[1] >= expected_ms and 0 <= observed[1] - observed[0] <= 12000)


def event_for(get, kind, subject, after_ms, *, reason=None):
    response = get("/openquatt/decision-log", timeout=3)
    if not response:
        return None
    events = json.loads(response)["events"]
    return next((event for event in events if event["event_type"] == kind and event["subject"] == subject
                 and (reason is None or event["reason"] == reason) and event["uptime_s"] * 1000 + 999 >= after_ms),
                None)


def command_value(row, register):
    index = register - row["register"]
    return row["values"][index] if 0 <= index < len(row["values"]) else None


class Lowflow(Dynamics):
    def __init__(self, thermostat, fleet):
        super().__init__(thermostat, fleet)
        self.evidence = self.result["lowflow"] = {
            "policy": {"min_flow_lph": MIN_FLOW_LPH, "fault_ms": FAULT_MS, "recovery_ms": RECOVER_MS,
                       "minimum_off_ms": MIN_OFF_MS, "supervisory_ms": SUPERVISORY_MS, "hold_ms": HOLD_MS},
        }
        self.previous = None

    def cut_flow(self, index):
        with self.fleet.lock:
            self.fleet.set_odu({"index": index, "no_flow": True})
            self.evidence["cut_ms"] = self.fleet.now_ms
        self.previous = self.result["checkpoints"]["defrost"]
        self.result["stage"] = "wait_fault"

    @staticmethod
    def stopped(sample):
        return (
            all(unit["requested_level"] == 0 and unit["requested_mode"] == "standby" and near(unit["frequency_hz"], 0, 0.5)
                for unit in sample["odus"])
            and all(near(freq, 0, 0.5) for freq in sample["firmware"]["frequency_hz"])
        )

    def observe_flow(self, get, stage, sample, index):
        points = self.result["checkpoints"]
        fw = sample["firmware"]
        evidence = self.evidence
        last = self.previous
        self.previous = sample
        flow = fw["flow_lph"]
        if (fw["ott_valid"] is not True or type(fw["heat"]) is not bool or type(fw["lowflow_fault"]) is not bool
                or type(flow) not in (int, float) or not math.isfinite(flow) or flow < 0):
            raise RuntimeError("Ontbrekende flow- of thermostaatfeedback tijdens de lowflowproef")
        if fw["heat"] is not True:
            raise RuntimeError("Warmtevraag viel weg tijdens de lowflowproef")

        healthy = at_least(fw["flow_lph"], MIN_FLOW_LPH)
        if stage == "wait_fault":
            if fw["defrost"] is not True or fw["valve"] is not True:
                raise RuntimeError("Firmware bevestigt defrost niet meer vóór de lowflowmelding")
            if healthy:
                if "flow_loss" in points:
                    raise RuntimeError("Flow werd geldig terwijl de virtuele leiding nog geblokkeerd was")
            elif "flow_loss" not in points:
                points["flow_loss"] = sample
                evidence["low_window_ms"] = window(last, sample)
                evidence["low_window_ms"][0] = max(evidence["cut_ms"], evidence["low_window_ms"][0])
            if "flow_loss" in points and (fw["mode"] or "").startswith("CM1"):
                points.setdefault("flow_interlock", sample)
            if fw["lowflow_fault"] is True and "flow_loss" in points:
                points.setdefault("fault", sample)
                event = event_for(get, "decision_blocked", "SYSTEM", evidence["cut_ms"], reason="flow_too_low")
                if event:
                    evidence["fault_event"] = event
                    fault_ms = event["uptime_s"] * 1000
                    evidence["fault_elapsed_window_ms"] = elapsed_window(evidence["low_window_ms"], [fault_ms, fault_ms + 999])
                    self.result["stage"] = "wait_stop"
            return

        if stage in ("wait_stop", "hold_fault"):
            if fw["lowflow_fault"] is not True or healthy:
                raise RuntimeError("De lowflowfout bleef niet actief bij de geblokkeerde leiding")
            if stage == "wait_stop" and self.no_flow(sample) and self.stopped(sample):
                event = event_for(get, "hp_stop_confirmed", f"HP{index + 1}", evidence["fault_event"]["uptime_s"] * 1000)
                if event:
                    evidence["stop_event"] = event
                    points["stopped"] = sample
                    self.fleet.set_odu({"index": index, "defrost": False})
                    self.result["stage"] = "hold_fault"
            elif stage == "hold_fault":
                if not self.stopped(sample):
                    raise RuntimeError("Een compressor kwam op gang terwijl de lowflowfout actief was")
                if sample["guest_ms"] - points["stopped"]["guest_ms"] >= HOLD_MS:
                    points["fault_held"] = sample
                    with self.fleet.lock:
                        self.fleet.set_odu({"index": index, "no_flow": False})
                        evidence["restore_ms"] = self.fleet.now_ms
                    self.result["stage"] = "wait_clear"
            return

        if stage == "wait_clear":
            if "fault_cleared" in points and fw["lowflow_fault"] is not False:
                raise RuntimeError("De lowflowfout keerde terug tijdens het wachten op herstart")
            if not self.stopped(sample):
                raise RuntimeError("Een compressor kwam op gang voordat flow en defrost waren vrijgegeven")
            if healthy:
                if "flow_restored" not in points:
                    points["flow_restored"] = sample
                    evidence["healthy_window_ms"] = window(last, sample)
                    evidence["healthy_window_ms"][0] = max(evidence["restore_ms"], evidence["healthy_window_ms"][0])
                if fw["lowflow_fault"] is False:
                    if "fault_cleared" not in points:
                        points["fault_cleared"] = sample
                        evidence["clear_window_ms"] = window(last, sample)
                        evidence["recovery_elapsed_window_ms"] = elapsed_window(evidence["healthy_window_ms"], evidence["clear_window_ms"])
                    if fw["defrost"] is False and fw["valve"] is False:
                        self.result["stage"] = "wait_restart"
            elif "flow_restored" in points or fw["lowflow_fault"] is False:
                raise RuntimeError("Flow bleef niet geldig tot aan de vrijgave van de lowflowfout")
        elif stage == "wait_restart":
            if fw["lowflow_fault"] is not False:
                raise RuntimeError("De lowflowfout keerde terug tijdens het wachten op herstart")
            dips = evidence.setdefault("restart_flow_dips", [])
            if not healthy:
                standby = (fw["mode"] or "").split(" ", 1)[0] in ("CM0", "CM1")
                if not (standby and self.stopped(sample) and fw["defrost"] is False and fw["valve"] is False):
                    raise RuntimeError(
                        f"Flow {fw['flow_lph']:g} L/h vóór herstart buiten veilige stilstand "
                        f"(drempel {MIN_FLOW_LPH} L/h, lowflowfout uit)"
                    )
                if not dips or "restored" in dips[-1]:
                    dips.append({"from_ms": last["observed_from_ms"], "first_below_threshold": sample})
                return
            if dips and "restored" not in dips[-1]:
                dips[-1]["restored"] = sample
            if self.has_flow(sample) and self.heating(sample, index):
                event = event_for(get, "hp_start_confirmed", f"HP{index + 1}", evidence["clear_window_ms"][0])
                if event:
                    evidence["restart_event"] = event
                    points["restart"] = sample
                    self.result["stage"] = "complete"


def lowflow_gates(dynamics, writes):
    evidence = dynamics["lowflow"]
    points = dynamics["checkpoints"]
    fault_event = evidence.get("fault_event")
    stop_event = evidence.get("stop_event")
    clear = evidence.get("clear_window_ms")
    unit = dynamics["unit"]
    stop_commands = {}
    prohibited = []
    early_peer_restarts = []
    first_restart_commands = {}
    restart_command = None
    commands_during_flow_dips = []

    if fault_event and stop_event and clear:
        fault_ms = fault_event["uptime_s"] * 1000
        stop_ms = stop_event["uptime_s"] * 1000
        stopped_ms = points["stopped"]["guest_ms"]
        running_units = {
            number
            for stage in ("heating", "defrost", "flow_loss", "fault")
            for number, freq in enumerate(points.get(stage, {}).get("firmware", {}).get("frequency_hz", ()), 1)
            if at_least(freq, 0.5)
        }
        for row in writes:
            when = row["at_ms"]
            level = command_value(row, 1999)
            mode = command_value(row, 3999)
            if row["unit"] == unit and fault_ms <= when <= stopped_ms and not row["exception"] and not row["dropped"]:
                for register, command in ((1999, level), (3999, mode)):
                    if command == 0:
                        stop_commands.setdefault(register, row)
            # Include retries and writes inside the uncertainty of either fault edge.
            enables = (level is not None and level > 0) or mode in (1, 2, 4)
            if enables and any(
                    dip["from_ms"] <= when <= dip.get("restored", {}).get("guest_ms", float("inf"))
                    for dip in evidence.get("restart_flow_dips", [])):
                commands_during_flow_dips.append(row)
            if enables and fault_ms <= when < clear[1]:
                prohibited.append(row)
            if enables and when >= stop_ms:
                first_restart_commands.setdefault(row["unit"], row)
                if row["unit"] == unit and restart_command is None:
                    restart_command = row
                # The joint stop sample is the latest possible stop of the other unit.
                if row["unit"] != unit and row["unit"] in running_units and when < stopped_ms + MIN_OFF_MS:
                    early_peer_restarts.append(row)
        evidence["stop_commands"] = stop_commands
        evidence["positive_writes_while_latched"] = prohibited
        evidence["early_peer_restart_writes"] = early_peer_restarts
        evidence["first_restart_command"] = restart_command
        evidence["first_restart_commands"] = first_restart_commands
        evidence["commands_during_restart_flow_dips"] = commands_during_flow_dips
        if restart_command:
            command_ms = restart_command["at_ms"]
            evidence["off_time_at_restart_command_window_ms"] = elapsed_window(
                [stop_ms, stop_ms + 999], [command_ms, command_ms],
            )

    return {
        "lowflow_fault_timer": (
            matches_timer(evidence.get("fault_elapsed_window_ms"), FAULT_MS),
            "De lowflowfout viel buiten het verwachte tijdvenster.",
        ),
        "lowflow_defrost_stop": (
            set(stop_commands) == {1999, 3999} and points.get("fault", {}).get("firmware", {}).get("defrost") is True
            and points.get("stopped", {}).get("firmware", {}).get("defrost") is True,
            "De defroststop is onvoldoende aangetoond op de Modbus-bus en in de firmware.",
        ),
        "lowflow_held": (
            "fault_held" in points and not prohibited,
            "De lowflowfout hield de compressoren niet aantoonbaar gestopt.",
        ),
        "lowflow_recovery_timer": (
            matches_timer(evidence.get("recovery_elapsed_window_ms"), RECOVER_MS),
            "De vrijgave na flowherstel viel buiten het verwachte tijdvenster.",
        ),
        "lowflow_restart": (
            restart_command is not None and restart_command["at_ms"] >= clear[1]
            and evidence["off_time_at_restart_command_window_ms"][0] >= MIN_OFF_MS
            and not early_peer_restarts and not commands_during_flow_dips
            and all("restored" in dip for dip in evidence.get("restart_flow_dips", []))
            and "restart" in points,
            "De compressorherstart na vrijgave en minimum-uit-tijd is onvoldoende aangetoond.",
        ),
    }
