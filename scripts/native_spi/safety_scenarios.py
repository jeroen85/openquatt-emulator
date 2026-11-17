"""End-to-end inputs, link loss and cooling guards on the production firmware."""
import re
from urllib.parse import urlencode

from .defrost_assist import write_setting
from .dynamics import Dynamics, at_least, entity, near, value
from .lowflow import Lowflow, MIN_OFF_MS, MIN_FLOW_LPH, command_value, event_for
from .thermostat_setpoint import set_input

DEW_CASES = ("rise", "loss")
OFFLINE_UNIT = 2
MIN_RUNTIME_MS = 300000
MAX_HP1_READ_GAP_MS = 20000


def acknowledged(row):
    return not row["dropped"] and not row["exception"]


def odu_recovery_probe(row):
    return row["unit"] == OFFLINE_UNIT and row["fc"] == 3 and row["register"] == 2099 and row["count"] == 1


def cooling_units(sample):
    return [
        index + 1 for index, unit in enumerate(sample["odus"])
        if unit["requested_mode"] == "cooling" and unit["frequency_hz"] > 3
        and at_least(sample["firmware"]["frequency_hz"][index], 3)
    ]


def safe_dew_point(fw):
    return (fw["valid"] is True and near(fw["dew"], 10, 0.5)
            and at_least(fw["supply"], 0) and at_least(fw["floor"], 0) and fw["supply"] >= fw["floor"])


def started(row):
    level = command_value(row, 1999)
    mode = command_value(row, 3999)
    return (level is not None and level > 0) or mode in (1, 2, 4)


def started_between(writes, begin, end):
    return [row for row in writes if begin <= row["at_ms"] < end and started(row)]


def retained_minimum_only(writes, begin, end, running_units):
    return all(row["unit"] in running_units and command_value(row, 1999) == 1
               and command_value(row, 3999) is None for row in started_between(writes, begin, end))


def confirmed_stops(writes, unit, begin, end):
    good = [
        row for row in writes if row["unit"] == unit and begin <= row["at_ms"] <= end
        and acknowledged(row)
    ]
    return all(any(command_value(row, register) == 0 for row in good) for register in (1999, 3999))


class ApiRoomScenario(Dynamics):
    def __init__(self, thermostat, fleet, room_c):
        super().__init__(thermostat, fleet)
        self.room_c = room_c
        self.next_room_refresh_ms = 0

    def refresh_room_temperature(self, get):
        now = self.fleet.now_ms
        if now >= self.next_room_refresh_ms:
            path = entity("number", "api_input_room_temperature") + "/set?" + urlencode({"value": self.room_c})
            if get(path, method="POST") is not None:
                self.next_room_refresh_ms = now + 240000


class SourceLoss(ApiRoomScenario):
    def __init__(self, thermostat, fleet):
        super().__init__(thermostat, fleet, 19)
        self.result["version"] = 1
        self.settings_pending = [
            row for row in self.settings_pending if row[1] not in ("Room Temperature Source", "Room Setpoint Source")
        ] + [
            ("select", "Room Temperature Source", "API input"),
            ("number", "api_input_room_temperature", 19),
            ("select", "Room Setpoint Source", "API input"),
            ("number", "api_input_room_setpoint", 21),
        ]

    def observe(self, get):
        stage = self.result["stage"]
        points = self.result["checkpoints"]
        if stage == "complete":
            return
        if stage == "configure":
            if self.configure(get):
                # Send an explicit value even when the template switch already displays ON.
                if get("/switch/api_input_heating_enable/turn_on", method="POST") is not None:
                    self.set_heat(True)
                    self.result["stage"] = "heating"
            return
        self.refresh_room_temperature(get)
        sample = self.sample(get)
        fw = sample["firmware"]
        fw.update(
            selected=value(get, "binary_sensor", "Heating Enable (Selected)"),
            valid=value(get, "binary_sensor", "Heating Enable Valid"),
            source=value(get, "text_sensor", "Heating Enable Effective Source"),
        )
        sample["guest_ms"] = now = self.fleet.now_ms
        self.result["current"] = sample
        running = [unit + 1 for unit in range(2) if self.heating(sample, unit)]
        chosen = fw["valid"] is True and fw["selected"] is True
        if stage == "heating":
            if chosen and fw["source"] == "OT thermostat" and fw["ott_valid"] is True and running:
                points["started_on_ott"] = sample
                self.result["running_units"] = running
                with self.fleet.lock:
                    set_input(self.thermostat, "x")
                    self.result["paused_at_ms"] = self.fleet.now_ms
                self.result["stage"] = "wait_unavailable"
        elif stage == "wait_unavailable":
            if fw["ott_valid"] is False and fw["valid"] is False and fw["selected"] is False and fw["source"] == "None":
                points.setdefault("invalid", sample)
                if Lowflow.stopped(sample):
                    points["stopped"] = sample
                    self.result["stage"] = "blocked"
        elif stage == "blocked":
            if not (fw["ott_valid"] is False and fw["valid"] is False and fw["selected"] is False
                    and fw["source"] == "None" and Lowflow.stopped(sample)):
                raise RuntimeError("Verwarming bleef niet geblokkeerd na uitval van de geselecteerde bron")
            if now - points["stopped"]["guest_ms"] >= 25000:
                points["blocked"] = sample
                self.result["stage"] = "switch_api"
        elif stage == "switch_api":
            if write_setting(get, "select", "Heating Enable Source", "API input") and chosen and fw["source"] == "API input":
                if fw["ott_valid"] is not False:
                    raise RuntimeError("OpenTherm werd geldig vóór het beoogde herstel")
                points["api_selected"] = sample
                self.result["stage"] = "restart_api"
        elif stage == "restart_api":
            if chosen and fw["source"] == "API input" and running:
                points["api_restarted"] = sample
                self.set_heat(True)
                self.result["stage"] = "switch_ott"
        elif stage == "switch_ott":
            if fw["ott_valid"] is not True or fw["heat"] is not True:
                return
            if write_setting(get, "select", "Heating Enable Source", "OT thermostat"):
                if chosen and fw["source"] == "OT thermostat" and running:
                    points["restored_ott"] = sample
                    self.result["stage"] = "complete"

    def finalize(self, report):
        points = self.result["checkpoints"]
        lost = self.result.get("paused_at_ms")
        invalid = points.get("invalid")
        running_at_loss = [index + 1 for index in range(2) if invalid and Dynamics.heating(invalid, index)]
        self.result["running_at_confirmed_loss"] = running_at_loss
        active_units = set(self.result.get("running_units", [])) | set(running_at_loss)
        safe = points.get("stopped")
        held = points.get("blocked")
        api_selected = points.get("api_selected")
        writes = report["writes"]
        self.result["checks"] = {
            "ott_loss_revokes_heat": bool(invalid and safe),
            "only_retained_minimum_after_loss": bool(invalid and safe) and retained_minimum_only(
                writes, invalid["guest_ms"], safe["guest_ms"], running_at_loss
            ),
            "stop_on_wire": lost is not None and bool(safe) and all(
                confirmed_stops(writes, unit, lost, safe["guest_ms"]) for unit in active_units
            ),
            "no_enable_while_blocked": bool(safe and held and api_selected) and not started_between(
                writes, safe["guest_ms"], api_selected["guest_ms"]),
            "explicit_api_switch_and_restart": {"api_selected", "api_restarted"}.issubset(points),
            "ott_works_after_return": "restored_ott" in points,
        }


class DewGuard(ApiRoomScenario):
    def __init__(self, thermostat, fleet, case):
        super().__init__(thermostat, fleet, 25)
        self.case = case
        self.result.update(version=1, case=case)
        self.settings_pending += [
            ("select", "Heating Enable Source", "Disabled"),
            ("select", "Cooling Enable Source", "API input"),
            ("select", "Room Temperature Source", "API input"),
            ("number", "api_input_room_temperature", 25),
            ("select", "Room Setpoint Source", "API input"),
            ("number", "api_input_room_setpoint", 21),
            ("select", "Water Supply Source", "Local"),
            ("number", "api_input_outside_temperature", 25),
            ("select", "Cooling Dew Point Source", "API input"),
            ("select", "Cooling Without Dew Point", "Dew point required"),
            ("number", "api_input_cooling_dew_point", 10),
            ("select", "Cooling Restart Mode", "Minimum off time"),
            ("number", "Cooling Minimum Off Time", MIN_OFF_MS // 1000),
        ]
        # Avoid sending intermediate contradictory settings on a cold boot.
        wanted = {}
        for domain, name, target in self.settings_pending:
            wanted[domain, name] = target
        self.settings_pending = [(domain, name, target) for (domain, name), target in wanted.items()]
        self.fleet.set_environment({"outdoor_c": 25, "return_c": 18})

    def inject(self, get):
        if self.case == "loss":
            path = entity("select", "Cooling Dew Point Source") + "/set?" + urlencode({"option": "Home Assistant"})
        else:
            path = entity("number", "api_input_cooling_dew_point") + "/set?" + urlencode({"value": 23})
        return get(path, timeout=2, method="POST") is not None

    def restore(self, get):
        if self.case == "loss":
            return write_setting(get, "select", "Cooling Dew Point Source", "API input")
        return write_setting(get, "number", "api_input_cooling_dew_point", 10)

    def observe(self, get):
        stage = self.result["stage"]
        points = self.result["checkpoints"]
        if stage == "complete":
            return
        if stage == "configure":
            if self.configure(get) and get("/switch/api_input_cooling_enable/turn_on", method="POST") is not None:
                self.result["stage"] = "cooling"
            return
        self.refresh_room_temperature(get)
        sample = self.sample(get)
        fw = sample["firmware"]
        fw.update(
            valid=value(get, "binary_sensor", "Cooling Dew Point Available"),
            dew=value(get, "sensor", "Cooling Dew Point (Selected)"),
            floor=value(get, "sensor", "Cooling Minimum Safe Supply Temp"),
            supply=value(get, "sensor", "Water Supply Temp (Selected)"),
            permitted=value(get, "binary_sensor", "Cooling Permitted"),
            cooling_request=value(get, "binary_sensor", "Cooling Request Active"),
        )
        sample["guest_ms"] = now = self.fleet.now_ms
        self.result["current"] = sample
        running = cooling_units(sample)
        safe_dew = safe_dew_point(fw)
        unsafe = (fw["valid"] is False and fw["permitted"] is False) if self.case == "loss" else (
            fw["valid"] is True and at_least(fw["dew"], 18)
            and at_least(fw["floor"], 18.5) and near(fw["supply"], 18, 0.3)
        )
        if stage == "cooling":
            if len(running) == 1 and safe_dew and fw["permitted"] is True:
                issued_ms = self.fleet.now_ms
                if self.inject(get):
                    points["cooling"] = sample
                    self.result.update(unit=running[0], started_injection_ms=issued_ms, stage="wait_stop")
        elif stage == "wait_stop":
            if unsafe:
                points.setdefault("unsafe", sample)
                if Lowflow.stopped(sample):
                    unit = self.result["unit"]
                    unsafe_ms = points["unsafe"]["observed_from_ms"]
                    event = event_for(get, "hp_stop_confirmed", f"HP{unit}", unsafe_ms)
                    if event:
                        self.result["stop_event"] = event
                        points["stopped"] = sample
                        self.result["stage"] = "blocked"
        elif stage == "blocked":
            if not unsafe or not Lowflow.stopped(sample):
                raise RuntimeError("Koeling bleef niet gestopt tijdens de onveilige dauwpuntconditie")
            if now - points["stopped"]["guest_ms"] >= 25000:
                points["blocked"] = sample
                self.result["stage"] = "restore"
        elif stage == "restore":
            if self.restore(get) and safe_dew and Lowflow.stopped(sample):
                points["safe"] = sample
                self.result["stage"] = "wait_restart"
        elif stage == "wait_restart":
            if not safe_dew:
                raise RuntimeError("Dauwpunt werd opnieuw onveilig voordat de koeling hervatte")
            if running and fw["permitted"] is True:
                points["restarted"] = sample
                self.result["stage"] = "complete"

    def finalize(self, report):
        points = self.result["checkpoints"]
        injection = self.result.get("started_injection_ms")
        safe = points.get("stopped")
        held = points.get("blocked")
        unsafe = points.get("unsafe")
        start = points.get("cooling")
        if unsafe and safe:
            self.result["unsafe_to_stop_ms"] = safe["guest_ms"] - unsafe["guest_ms"]
        stop_event = self.result.get("stop_event")
        writes = report["writes"]
        unit = self.result["unit"]
        restored_safe = points.get("safe")
        restarted = points.get("restarted")
        # Decision-log timestamps have 1 s resolution. Use the earliest possible
        # stop for the minimum-off bound, and the latest for the runtime bound.
        earliest_stop = stop_event["uptime_s"] * 1000 if stop_event else None
        restart_writes = (
            started_between(writes, earliest_stop, restarted["guest_ms"] + 1)
            if earliest_stop is not None and restarted else []
        )
        if self.case == "loss" and safe and earliest_stop is not None:
            # A retained minimum before confirmed standby can overlap the last
            # second of the decision-log interval. A positive mode write is always checked.
            proven_stop_ms = min(earliest_stop + 1000, safe["guest_ms"])
            restart_writes = [
                row for row in restart_writes
                if not (row["at_ms"] < proven_stop_ms and row["unit"] == unit
                        and command_value(row, 1999) == 1 and command_value(row, 3999) is None)
            ]
        self.result["first_restart_command"] = restart_writes[0] if restart_writes else None
        self.result["checks"] = {
            "unsafe_point_observed": injection is not None and bool(unsafe)
            and injection <= unsafe["observed_from_ms"] and unit in cooling_units(unsafe),
            "stop_on_wire": bool(unsafe and safe) and earliest_stop is not None
            and earliest_stop + 999 >= unsafe["observed_from_ms"]
            and confirmed_stops(writes, unit, unsafe["observed_from_ms"], safe["guest_ms"]),
            "no_enable_during_guard": bool(safe and held and restored_safe and restarted)
            and not any(row["at_ms"] < restored_safe["guest_ms"] for row in restart_writes),
            "minimum_off_before_any_cooling_start": bool(restart_writes)
            and restart_writes[0]["at_ms"] >= earliest_stop + MIN_OFF_MS,
            "safe_cooling_restored": bool(restored_safe and restarted)
            and safe_dew_point(restored_safe["firmware"]) and safe_dew_point(restarted["firmware"]) and any(
                row["at_ms"] >= restored_safe["guest_ms"] and row["unit"] in cooling_units(restarted)
                and command_value(row, 3999) == 1 and acknowledged(row) for row in restart_writes
            ),
        }
        if self.case == "rise":
            first_cooling_command_ms = next((
                row["at_ms"] for row in writes if row["unit"] == unit
                and command_value(row, 3999) == 1 and acknowledged(row)
            ), None)
            self.result["checks"]["confirmed_high_dew_overrides_minimum_runtime"] = (
                first_cooling_command_ms is not None and earliest_stop is not None and bool(start)
                and first_cooling_command_ms <= start["guest_ms"]
                and earliest_stop + 999 - first_cooling_command_ms < MIN_RUNTIME_MS
            )
        else:
            self.result["checks"]["missing_dew_revokes_cooling_permission"] = bool(
                unsafe and unsafe["firmware"]["permitted"] is False
            )
            self.result["checks"]["only_retained_minimum_after_loss"] = bool(
                unsafe and safe and retained_minimum_only(
                    writes, unsafe["guest_ms"], safe["guest_ms"], [self.result["unit"]]
                )
            )


class OduLinkLoss(Dynamics):
    def __init__(self, thermostat, fleet, peer):
        super().__init__(thermostat, fleet)
        self.result.update(version=1, disconnected_unit=OFFLINE_UNIT)
        self.peer = peer

    def observe_hp1_reads(self, reads, now):
        last = self.result["hp1_latest_read_ms"]
        largest = self.result.get("hp1_max_read_gap_ms", 0)
        for row in reads:
            when = row["at_ms"]
            if row["unit"] == 1 and last < when <= now:
                largest = max(largest, when - last)
                last = when
        self.result.update(hp1_latest_read_ms=last, hp1_max_read_gap_ms=max(largest, now - last))
        if self.result["hp1_max_read_gap_ms"] > MAX_HP1_READ_GAP_MS:
            raise RuntimeError("Tijdens de HP2-uitval kwamen minstens twee HP1-pollvensters zonder antwoord voorbij")

    def observe(self, get):
        stage = self.result["stage"]
        points = self.result["checkpoints"]
        if stage == "complete":
            return
        if stage == "configure":
            if self.configure(get):
                self.set_heat(True)
                self.result["stage"] = "duo"
            return
        sample = self.sample(get)
        snapshot = self.fleet.snapshot()
        sample["odu_link"] = [
            {"reads": model["protocol"]["reads"], "watchdog_counter": model["watchdog_counter"],
             "fault_words": model["fault_words"], "frequency_hz": model["frequency_hz"]}
            for model in snapshot
        ]
        self.result["current"] = sample
        first, second = sample["odu_link"]
        firmware = sample["firmware"]
        responses = [row for row in self.peer.snapshot()["recent"] if acknowledged(row)]
        reads = [row for row in responses if row["fc"] == 3]
        hp1_safe = near(first["frequency_hz"], 0, 0.5) or at_least(firmware["flow_lph"], MIN_FLOW_LPH)
        if stage == "duo":
            if self.has_flow(sample) and not (second["fault_words"][2] & 1) and all(self.heating(sample, i) for i in range(2)):
                points["duo"] = sample
                with self.fleet.lock:
                    self.fleet.set_odu({"index": OFFLINE_UNIT - 1, "online": False})
                    self.result["cut_ms"] = self.fleet.now_ms
                    self.result["reads_at_cut"] = self.fleet.snapshot()[1]["protocol"]["reads"]
                    self.result["hp1_latest_read_ms"] = self.result["cut_ms"]
                self.result["stage"] = "outage"
        elif stage == "outage":
            if not hp1_safe:
                raise RuntimeError("HP1 draait tijdens de verbindingstest zonder bevestigde flow")
            self.observe_hp1_reads(reads, sample["guest_ms"])
            stop_watchdog = second["fault_words"][2] & 1 and near(second["frequency_hz"], 0, 0.5)
            if (sample["guest_ms"] - self.result["cut_ms"] >= 135000 and stop_watchdog
                    and second["reads"] == self.result["reads_at_cut"]):
                points["watchdog_stop"] = sample
                with self.fleet.lock:
                    self.fleet.set_odu({"index": OFFLINE_UNIT - 1, "online": True})
                    self.result["restore_ms"] = self.fleet.now_ms
                self.result["stage"] = "restore"
        elif stage == "restore":
            offline = points["watchdog_stop"]["odu_link"]
            reply = self.result.get("recovery_reply")
            if reply is None:
                # A queued write can bring the controller online before the next sparse read.
                reply = next((row for row in responses if row["unit"] == OFFLINE_UNIT
                              and row["at_ms"] >= self.result["restore_ms"]
                              and (odu_recovery_probe(row) or row["fc"] in (6, 16))), None)
                if reply:
                    self.result["recovery_reply"] = reply
            if reply and "resumed_poll" not in self.result:
                poll = next((row for row in reads if row["unit"] == OFFLINE_UNIT
                             and row["register"] == 2099 and row["count"] > 1
                             and row["at_ms"] > reply["at_ms"]), None)
                if poll:
                    self.result["resumed_poll"] = poll
            resumed = self.result.get("resumed_poll")
            if (resumed and sample["observed_from_ms"] >= resumed["at_ms"]
                    and hp1_safe and first["reads"] > offline[0]["reads"]
                    and second["reads"] >= offline[1]["reads"] + 3 and second["watchdog_counter"] < 1500
                    and second["fault_words"][2] & 1 == 0 and near(firmware["frequency_hz"][1], second["frequency_hz"], 0.5)):
                points["recovered"] = sample
                self.result["stage"] = "complete"

    def finalize(self, report):
        points = self.result["checkpoints"]
        peer = report["peer"]
        offline = peer["offline_frames"]
        cut = self.result.get("cut_ms")
        restore = self.result.get("restore_ms")
        planned = bool(offline) and cut is not None and restore is not None and all(
            row["unit"] == OFFLINE_UNIT and cut <= row["at_ms"] <= restore for row in offline
        )
        warnings = report.get("modbus_timing_events", [])
        expected_warnings = planned and len(warnings) == len(report["modbus_timing_warnings"]) and all(
            cut <= row["guest_ms"] <= restore + 2000
            and re.search(r"Stop waiting for response from 2 [0-9]+us after last send", row["line"])
            for row in warnings
        )
        link_events = report["modbus_link_events"]
        offline_seen = planned and any(
            cut <= row["guest_ms"] < restore and "HP2 - Modbus OFFLINE" in row["line"] for row in link_events
        )
        offline_probes = [row for row in offline if odu_recovery_probe(row)]
        reply = self.result.get("recovery_reply")
        online_seen = reply is not None and any(
            row["guest_ms"] >= reply["at_ms"] and "HP2 - Modbus ONLINE" in row["line"] for row in link_events
        )
        self.result["checks"] = {
            "planned_drops_only": planned and peer["dropped"] == len(offline),
            "planned_modbus_timeouts_only": bool(expected_warnings),
            "second_unit_watchdog_stopped": "watchdog_stop" in points,
            "firmware_detected_outage": bool(offline_seen),
            "first_unit_kept_responding": "watchdog_stop" in points
            and self.result["hp1_max_read_gap_ms"] <= MAX_HP1_READ_GAP_MS,
            "offline_probe_attempted": planned and bool(offline_probes),
            "second_unit_recovers_and_resumes_polling": bool(online_seen) and "recovered" in points,
        }
