"""Verify native energy history through an hour or day rollover and a hard reset."""
from datetime import datetime, timedelta
from math import ceil
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from .dynamics import Dynamics, entity
from .restart import reset_control

CASES = ("hour", "day")
ZONE = ZoneInfo("Europe/Amsterdam")
DATE = (2026, 1, 15)
ARCHIVE_FLAG_PARTIAL = 1
LIVE_SEQ = 1 << 31
HEATPUMP_HEAT = 3
ELECTRICITY = 0
MAX_ELECTRICAL_W = 12000
MAX_THERMAL_W = 30000
HISTORY_SAMPLE_MS = 30000
BOOT_COUNTER_MARGIN_WH = 3
WRITE_INTERVAL_MS = 60000
DAY_HOLD_MS = 75000
SAVE_HISTORY = entity("button", "Lifetime energiehistorie nu opslaan") + "/press"


def fixture_time(case):
    edge = datetime(*DATE, 12, tzinfo=ZONE) if case == "hour" else datetime(2026, 1, 16, tzinfo=ZONE)
    return edge - timedelta(minutes=8), edge


def date_key(time):
    return time.year * 10000 + time.month * 100 + time.day


def parse_history(body):
    report = {"days": [], "hours": []}
    version = None
    for line in body.decode("utf8").splitlines():
        kind, *fields = line.split("|")
        if kind == "@schema" and len(fields) == 1:
            version = int(fields[0])
        elif kind == "@now" and len(fields) == 1:
            report["epoch_ms"] = int(fields[0])
        elif kind == "@enabled" and len(fields) == 1:
            report["enabled"] = fields[0] == "1"
        elif kind == "@hour" and len(fields) == 11:
            sequence, day, hour, *items = map(int, fields)
            *values, marker = items
            if not 0 <= hour <= 23 or marker not in (0, 1):
                raise ValueError("Ongeldig uurrecord in energiehistorie")
            report["hours"].append({"sequence": sequence, "date": day, "hour": hour,
                                    "wh": values, "recovered": bool(marker)})
        elif kind == "@current" and len(fields) == 8:
            day, *values = map(int, fields)
            report["current"] = {"date": day, "wh": values}
        elif kind.isascii() and kind.isdecimal() and len(fields) == 9:
            day, flags, *values = map(int, fields)
            report["days"].append({"sequence": int(kind), "date": day, "flags": flags, "wh": values})
        elif kind in ("@schema", "@now", "@enabled", "@hour", "@current") or kind.isdecimal():
            raise ValueError("Onvolledig record in energiehistorie")
    if version != 3 or not {"epoch_ms", "enabled"}.issubset(report):
        raise ValueError("Onbekend formaat van energiehistorie")
    return report


def hours_for(snapshot, day, *, stored_only=False):
    # The endpoint exposes both stored revisions and live rows. The latter use
    # the high sequence bit, just as in the web app's merge.
    chosen = {}
    for row in snapshot["hours"]:
        if row["date"] != day or (stored_only and row["sequence"] >= LIVE_SEQ):
            continue
        previous = chosen.get(row["hour"])
        if previous is None or previous["sequence"] < row["sequence"]:
            chosen[row["hour"]] = row
        elif previous["sequence"] == row["sequence"]:
            raise ValueError("Meerdere energierecords met dezelfde dag, hetzelfde uur en dezelfde volgorde")
    return chosen


def has_energy(wh):
    return wh[ELECTRICITY] > 0 and wh[HEATPUMP_HEAT] > 0


def reconciles(wh, hours):
    for index, total in enumerate(wh):
        if total < 0:
            continue
        entries = [row["wh"][index] for row in hours.values()]
        if not any(entry >= 0 for entry in entries) or sum(max(0, value) for value in entries) != total:
            return False
    return bool(hours) and has_energy(wh)


def closed_days(snapshot, day):
    return [row for row in snapshot["days"] if row["date"] == day and not row["flags"] & ARCHIVE_FLAG_PARTIAL]


def new_day_starts_clean(snapshot, boundary_ms):
    elapsed_ms = snapshot["epoch_ms"] - boundary_ms
    if not 0 <= elapsed_ms <= 35000:
        return False
    # Wide bounds for the two simulated units. Include the uncertainty from
    # the endpoint's whole-second clock and integer Wh rounding.
    maximum_watts = {ELECTRICITY: MAX_ELECTRICAL_W, HEATPUMP_HEAT: MAX_THERMAL_W}
    wh = snapshot["current"]["wh"]
    return all(
        0 <= wh[index] <= ceil(power * (elapsed_ms + 1000) / 3600000) + 1
        for index, power in maximum_watts.items()
    )


def closed_totals_match(before, closed, until_boundary_ms):
    if not 0 < until_boundary_ms <= HISTORY_SAMPLE_MS or not has_energy(before):
        return False
    # A capture can land between the last REST response and the boundary.
    # That capture can include one whole sampling interval, plus rounding.
    elapsed_ms = until_boundary_ms + HISTORY_SAMPLE_MS + 1000
    return all(
        0 <= final - first <= ceil((MAX_ELECTRICAL_W if index < 3 else MAX_THERMAL_W)
                                   * elapsed_ms / 3600000) + 1
        for index, (first, final) in enumerate(zip(before, closed))
        if first >= 0
    )


def monotonic_day_samples(samples):
    if len(samples) < 2:
        return False
    return all(
        0 <= before["wh"][index] <= after["wh"][index]
        for before, after in zip(samples, samples[1:])
        for index in (ELECTRICITY, HEATPUMP_HEAT)
    )


def counters_recovered_once(saved, before, after):
    # A hard reset may discard the tail of ESPHome's deferred preferences
    # writes. Restored totals must retain the archived checkpoint and should
    # stay within the observed energy before reset, with rounding margin.
    return all(
        archived <= restored <= observed + BOOT_COUNTER_MARGIN_WH
        for archived, observed, restored in zip(saved, before, after)
        if archived >= 0 and observed >= 0
    )


class EnergyBoundary(Dynamics):
    def __init__(self, thermostat, fleet, case, control_port, clock):
        super().__init__(thermostat, fleet)
        self.case, self.port, self.clock = case, control_port, clock
        self.start_time, self.boundary = fixture_time(case)
        self.before_date = date_key(self.boundary - timedelta(seconds=1))
        self.before_hour = (self.boundary - timedelta(seconds=1)).hour
        self.after_date, self.after_hour = date_key(self.boundary), self.boundary.hour
        query = urlencode({"from": self.before_date, "to": self.after_date, "hours": 1})
        self.url = "/energy/history?" + query
        self.result.update(version=1, case=case, epoch=int(self.start_time.timestamp()),
                           boundary_epoch_ms=int(self.boundary.timestamp()) * 1000,
                           timezone=ZONE.key)

    def read_history(self, get):
        body = get(self.url, timeout=3)
        if body:
            sample = parse_history(body)
            sample["guest_ms"] = self.clock()
            return sample
        return None

    def request_reset(self, snapshot):
        self.set_heat(False)
        self.result["checkpoints"]["reset_requested"] = snapshot
        reset_control(self.port)
        self.result["stage"] = "verify_reboot"

    def observe(self, get):
        stage = self.result["stage"]
        points = self.result["checkpoints"]
        if stage == "complete":
            return
        if stage == "configure":
            if self.configure(get):
                self.set_heat(True)
                self.result["stage"] = "boundary"
            return
        sample = self.read_history(get)
        if not sample or not sample["enabled"] or not sample["epoch_ms"] or "current" not in sample:
            return
        self.result["current"] = sample
        now = datetime.fromtimestamp(sample["epoch_ms"] / 1000, ZONE)
        if now < self.start_time or now >= self.boundary + timedelta(minutes=30):
            raise RuntimeError("De firmwareklok valt buiten de ingestelde energiehistorieproef")
        current = sample["current"]
        old_hours = hours_for(sample, self.before_date)
        old_hour = old_hours.get(self.before_hour)
        new_hours = hours_for(sample, self.after_date)
        new_hour = new_hours.get(self.after_hour)
        passed_edge = now >= self.boundary
        if stage == "boundary":
            if not passed_edge:
                if (current["date"] == self.before_date and old_hour and not old_hour["recovered"]
                        and reconciles(current["wh"], old_hours)):
                    points["before"] = sample
                return
            if "before" not in points or not new_hour or current["date"] != self.after_date:
                return
            if self.case == "day":
                points["boundary_crossed"] = sample
                self.result["day_samples"] = [{"guest_ms": sample["guest_ms"], "wh": current["wh"]}]
                self.result["stage"] = "observe_new_day"
            elif has_energy(new_hour["wh"]) and reconciles(current["wh"], new_hours):
                points["boundary_crossed"] = sample
                if get(SAVE_HISTORY, timeout=3, method="POST") is not None:
                    self.result["stage"] = "check_saved_hours"
        elif stage == "observe_new_day":
            if current["date"] != self.after_date:
                raise RuntimeError("De actieve dag sprong terug na middernacht")
            self.result["day_samples"].append({"guest_ms": sample["guest_ms"], "wh": current["wh"]})
            if sample["guest_ms"] - points["boundary_crossed"]["guest_ms"] >= DAY_HOLD_MS:
                points["new_day_steady"] = sample
                self.request_reset(sample)
        elif stage == "check_saved_hours":
            stored = hours_for(sample, self.after_date, stored_only=True)
            if self.before_hour in stored and self.after_hour in stored and not stored[self.after_hour]["recovered"]:
                points["checkpoint_written"] = sample
                self.result["stage"] = "accumulate_unflushed"
        elif stage == "accumulate_unflushed":
            checkpoint = points["checkpoint_written"]
            saved = hours_for(checkpoint, self.after_date, stored_only=True)[self.after_hour]["wh"]
            if (new_hour and new_hour["wh"][HEATPUMP_HEAT] > saved[HEATPUMP_HEAT] + BOOT_COUNTER_MARGIN_WH
                    and sample["guest_ms"] - checkpoint["guest_ms"] >= WRITE_INTERVAL_MS
                    and reconciles(current["wh"], new_hours)):
                self.request_reset(sample)
        elif stage == "verify_reboot":
            requested = points["reset_requested"]
            if (sample["guest_ms"] >= requested["guest_ms"] // 2 or sample["guest_ms"] < 30000
                    or sample["epoch_ms"] <= requested["epoch_ms"] or current["date"] != self.after_date):
                return
            if self.case == "day":
                if len(closed_days(sample, self.before_date)) == 1 and old_hour:
                    points["after_reboot"] = sample
                    self.result["stage"] = "complete"
            elif new_hour and new_hour["recovered"] and reconciles(current["wh"], new_hours):
                points["after_reboot"] = sample
                if get(SAVE_HISTORY, timeout=3, method="POST") is not None:
                    self.result["stage"] = "verify_recovery_on_flash"
        elif stage == "verify_recovery_on_flash":
            stored = hours_for(sample, self.after_date, stored_only=True).get(self.after_hour)
            if stored and stored["recovered"]:
                points["recovery_saved"] = sample
                self.result["stage"] = "complete"

    def finalize(self, report):
        points = self.result["checkpoints"]
        before, crossed, reboot = map(points.get, ("before", "boundary_crossed", "after_reboot"))
        reset = points.get("reset_requested")
        self.result["checks"] = checks = {
            "local_ntp_sync": report.get("local_services", {}).get("ntp", 0) >= 1,
            "clock_crossed_boundary": bool(before and crossed)
            and before["epoch_ms"] < self.result["boundary_epoch_ms"] <= crossed["epoch_ms"],
            "new_boot_with_forward_wall_clock": bool(reset and reboot) and report.get("setup_count") == 2
            and reboot["guest_ms"] < reset["guest_ms"] // 2 and reboot["epoch_ms"] > reset["epoch_ms"],
        }
        key = self.before_date
        previous = self.before_hour
        pre_hour = hours_for(before, key).get(previous) if before else None
        closed_hour = hours_for(crossed, key).get(previous) if crossed else None
        until_boundary_ms = self.result["boundary_epoch_ms"] - before["epoch_ms"] if before else 0
        checks["nonzero_hour_kept_at_boundary"] = bool(pre_hour and closed_hour) and closed_totals_match(
            pre_hour["wh"], closed_hour["wh"], until_boundary_ms
        )
        if self.case == "day":
            archived = closed_days(crossed, key) if crossed else []
            after = closed_days(reboot, key) if reboot else []
            steady = points.get("new_day_steady")
            stored_hours = hours_for(crossed, key, stored_only=True) if crossed else {}
            checks.update(
                single_closed_day_matches_last_total=bool(before and closed_hour) and len(archived) == 1
                and bool(stored_hours.get(previous))
                and stored_hours[previous]["wh"] == closed_hour["wh"]
                and reconciles(archived[0]["wh"], stored_hours)
                and closed_totals_match(before["current"]["wh"], archived[0]["wh"], until_boundary_ms),
                day_and_hours_survive_reset=bool(archived and after) and archived == after
                and previous in stored_hours and stored_hours == hours_for(reboot, key, stored_only=True),
                new_day_starts_without_carryover=bool(crossed) and new_day_starts_clean(
                    crossed, self.result["boundary_epoch_ms"]),
                new_day_counters_never_roll_back=monotonic_day_samples(self.result.get("day_samples", [])),
                new_day_has_own_energy=bool(crossed and steady) and steady["current"]["date"] == self.after_date
                and all(steady["current"]["wh"][index] > crossed["current"]["wh"][index]
                        for index in (ELECTRICITY, HEATPUMP_HEAT))
                and reconciles(steady["current"]["wh"], hours_for(steady, self.after_date)),
            )
            return
        reopened = hours_for(reboot, key) if reboot else {}
        saved = points.get("recovery_saved")
        persisted = hours_for(saved, key, stored_only=True) if saved else {}
        checkpoint = points.get("checkpoint_written")
        stored_partial = max(
            (row for row in checkpoint["days"] if row["date"] == key and row["flags"] & ARCHIVE_FLAG_PARTIAL),
            key=lambda row: row["sequence"], default=None,
        ) if checkpoint else None
        if stored_partial and reset and reboot:
            self.result["recovery_counters_wh"] = {
                "saved": stored_partial["wh"],
                "before_reset": reset["current"]["wh"],
                "after_reset": reboot["current"]["wh"],
            }
        stored_hours = hours_for(checkpoint, key, stored_only=True) if checkpoint else {}
        stored_active_hour = stored_hours.get(self.after_hour)
        active_hour = reopened.get(self.after_hour)
        old_hour = reopened.get(previous)
        checks.update(
            closed_hour_unchanged_after_reset=bool(closed_hour and old_hour) and not old_hour["recovered"]
            and old_hour["wh"] == closed_hour["wh"],
            recovery_marker_only_in_reboot_hour=bool(active_hour and stored_active_hour) and active_hour["recovered"]
            and active_hour["wh"][HEATPUMP_HEAT] > stored_active_hour["wh"][HEATPUMP_HEAT]
            and [hour for hour, row in reopened.items() if row["recovered"]] == [self.after_hour],
            recovered_energy_is_not_double_counted=bool(reboot and reset and stored_partial)
            and reconciles(stored_partial["wh"], stored_hours)
            and reconciles(reboot["current"]["wh"], reopened)
            and counters_recovered_once(stored_partial["wh"], reset["current"]["wh"], reboot["current"]["wh"]),
            recovery_marker_written_to_flash=bool(persisted.get(self.after_hour))
            and persisted[self.after_hour]["recovered"] and bool(old_hour)
            and previous in persisted and persisted[previous]["wh"] == old_hour["wh"]
            and not persisted[previous]["recovered"],
        )
