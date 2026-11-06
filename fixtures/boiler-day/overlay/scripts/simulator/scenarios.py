"""System scenarios against real firmware observations and commands on the virtual bus."""
from __future__ import annotations

import math
import re
from pathlib import Path
import threading
import time

from scripts.emulator import run as emulator
from scripts.simulator.model import PROFILES
from scripts.simulator.runtime import utc_now

CATALOGUE = [
    {"id": "smoke", "label": "Opstarten en buitenunits herkennen", "description": "Controleert de firmwarestart, beide buitenunits, generatieherkenning en geheugenmetingen."},
    {"id": "inputs", "label": "Thermostaat en buitentemperatuur", "description": "Stelt controllerinvoer via de API in en controleert de geselecteerde waarden."},
    {"id": "heating", "label": "Verwarmen en normaal stoppen", "description": "Controleert compressorcommando’s en de teruggelezen frequentie. Normaal stoppen wacht op de minimumlooptijd."},
    {"id": "communications", "label": "Verbindingsverlies en herstel", "description": "Maakt HP1 tijdelijk onbereikbaar. HP2 blijft bereikbaar; HP1 moet via verse Modbus-antwoorden herstellen."},
    {"id": "no-flow", "label": "Flowuitval en herstel", "description": "Simuleert flowuitval tijdens verwarmen en controleert fouttimer, stilstand, herstel en herstart."},
    {"id": "no-flow-defrost", "label": "Flowuitval tijdens ontdooien", "description": "Controleert stop en herstel bij flowuitval terwijl de buitenunit ontdooien meldt."},
    {"id": "stale", "label": "Verouderde controllerinvoer", "description": "Stopt de periodieke invoerupdates. De controller moet de verouderde bron afwijzen en normaal afbouwen."},
    {"id": "profiles", "label": "Buitenunitgeneraties herkennen", "description": "Controleert V1, V1.5, V2 old en V2 new via Modbus en de bijbehorende registertabellen."},
    {"id": "network", "label": "OpenQuatt web-app belasten", "description": "Controleert gelijktijdige HTTP-verzoeken en live-updates (SSE), ook na opnieuw verbinden."},
    {"id": "memory", "label": "Geheugen onder belasting", "description": "Herhaalt verzoeken aan de OpenQuatt web-app en API en meet interne heap, PSRAM en stackreserves."},
]


PROFILE_CASES = {
    "ot-thermostat": {"id": "ot-wire", "label": "OpenTherm-thermostaat", "description": "Test digitale OpenTherm-frames via RMT, bronselectie, pariteitsfouten en herstel."},
    "ot-boiler": {"id": "ot-boiler", "label": "OpenTherm-ketel", "description": "Test digitale ketelantwoorden via RMT, temperatuurterugmelding, verbindingsverlies en herstel."},
    "local-flow": {"id": "local-flow-wire", "label": "Lokale flowsensor (GPIO)", "description": "Controleert GPIO15-pulsen via de productie-pulse_meter, Huba-conversie en de geselecteerde flow."},
}


def catalogue_for(profile):
    result = [dict(item) for item in CATALOGUE]
    if profile in PROFILE_CASES:
        if profile == "ot-boiler":
            # The incident test deliberately leaves real HP recovery timers active.
            result.append(dict(PROFILE_CASES[profile]))
        else:
            result.insert(2, dict(PROFILE_CASES[profile]))
    if profile == "ot-thermostat":
        next(item for item in result if item["id"] == "inputs")["description"] = "Thermostaatwaarden via OpenTherm; buitentemperatuur via de API. Controleert de geselecteerde controllerwaarden."
    if profile == "local-flow":
        result.insert(2, {"id": "hydraulics", "label": "Circulatie en nulflow", "description": "CM0 met pompen uit geeft nulflow. CM1 laat beide buitenunits in serie doorstromen zonder compressorstart."})
        next(item for item in result if item["id"] == "no-flow")["description"] = "Onderbreekt alleen GPIO15-pulsen. Controleert de fouttimer, 65 s stopbewaking en herstart na flowherstel."
    return result


def compressor_enable_writes(writes, since_ms, until_ms=None, command_registers=(1999,)):
    """Successful bus writes count even when their acknowledgement was lost."""
    result = []
    for row in writes:
        if row.get("unit") not in (1, 2) or row.get("fc") not in (6, 16) or row.get("exception") != 0 or row.get("at_ms", -1) <= since_ms:
            continue
        if until_ms is not None and row["at_ms"] > until_ms:
            continue
        for register in command_registers:
            offset = register - row["register"]
            if 0 <= offset < len(row["values"]) and row["values"][offset] > 0:
                result.append({"unit": row["unit"], "at_ms": row["at_ms"], "register": register,
                               "level": row["values"][offset], "ack_dropped": row["dropped"]})
    return result


def flow_policy():
    # Read the production policy rather than silently shortening fault/recovery
    # times in the test. Profile overlays must not override these substitutions.
    source = (Path(__file__).resolve().parents[2] / "openquatt/oq_substitutions_common.yaml").read_text()
    values = {}
    for name in ("oq_cm_min_flow_lph", "oq_cm_flow_fault_s", "oq_cm_flow_recover_s", "oq_supervisory_loop_s"):
        match = re.search(r'^' + name + r': "([0-9.]+)"', source, re.MULTILINE)
        if not match:
            raise ValueError("Ontbrekende productie-flowpolicy: " + name)
        values[name] = float(match.group(1))
    return (values["oq_cm_min_flow_lph"], values["oq_cm_flow_fault_s"] * 1000,
            values["oq_cm_flow_recover_s"] * 1000, values["oq_supervisory_loop_s"] * 1000)


def timer_window(duration_ms, loop_ms):
    # Serial sampling is 1s. The timer starts at the next supervisory tick;
    # an expiry just short of the deadline may require one more complete tick.
    # Keep an asymmetric bound: cadence may delay release, never advance it.
    sampling_ms = 1500
    return duration_ms - sampling_ms, duration_ms + 2 * loop_ms + sampling_ms


class ScenarioRunner:
    def __init__(self, session):
        self.session = session
        self.thread = None
        self.cancel_event = threading.Event()
        self.items = catalogue_for(session.profile.name)
        self.statuses = {item["id"]: "idle" for item in self.items}
        self.lock = threading.Lock()
        self.active = None
        self.last_refresh = 0.0
        self.weather_only = False
        self.case_evidence = {}
        self.no_flow_attempt = 0

    def is_running(self):
        return bool(self.thread and self.thread.is_alive())

    def catalogue(self):
        with self.lock:
            return [{**item, "status": self.statuses[item["id"]]} for item in self.items]

    def cancel(self):
        self.cancel_event.set()

    def join(self):
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=10)
            if self.thread.is_alive():
                raise RuntimeError("Scenario worker kon niet stoppen")

    def start(self, scenario_id):
        if scenario_id not in self.statuses and scenario_id != "all":
            raise ValueError("Onbekend testscenario")
        with self.lock:
            if self.is_running():
                raise RuntimeError("Er loopt al een test")
            if self.session.state != "running":
                raise RuntimeError("Start eerst de simulator")
            self.cancel_event.clear()
            self.thread = threading.Thread(target=self._run, args=(scenario_id,), name="lab-scenarios", daemon=True)
            self.thread.start()

    def check(self):
        self.session._check()
        if self.cancel_event.is_set():
            raise InterruptedError("Test afgebroken")

    def refresh(self):
        self.check()
        if self.weather_only and time.monotonic() - self.last_refresh > 15:
            self.session.controller.number("api_input_outside_temperature", self.session.environment["outdoor_c"])
            self.last_refresh = time.monotonic()
        elif not self.session.heartbeat_paused and time.monotonic() - self.last_refresh > 15:
            self.session.controller.apply_environment(self.session.environment, auto_mode=False)
            self.last_refresh = time.monotonic()

    def wait(self, predicate, label, timeout=75):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.check()
            if predicate():
                return
            self.refresh()
            self.cancel_event.wait(0.3)
        raise AssertionError("Niet waargenomen: " + label)

    def wait_stable(self, predicate, label, timeout, stable_ms=10000):
        since = None
        def stable():
            nonlocal since
            now = self.session.telemetry.get("uptime_ms", 0)
            if not predicate():
                since = None
                return False
            if since is None:
                since = now
            return now - since >= stable_ms
        self.wait(stable, label, timeout)

    def pause(self, seconds):
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            self.check()
            self.refresh()
            self.cancel_event.wait(min(0.4, max(0, until - time.monotonic())))

    def _run(self, scenario_id):
        selected = self.items if scenario_id == "all" else [item for item in self.items if item["id"] == scenario_id]
        with self.session.command_lock:
            self.last_refresh = time.monotonic()
            for item in selected:
                self.active = item["id"]
                started = time.monotonic()
                result = {"id": item["id"], "label": item["label"], "status": "RUNNING", "started_at": utc_now()}
                with self.lock:
                    self.statuses[item["id"]] = "running"
                self.session.log("Test gestart: " + item["label"])
                self.case_evidence = {}
                phase_start = len(self.session.capture.state()[0])
                diagnostic_start = len(self.session.capture.diagnostics.state()[0])
                self.session.capture.set_phase(item["id"])
                try:
                    self.check()
                    getattr(self, "test_" + item["id"].replace("-", "_"))()
                    self.wait(lambda: len([sample for sample in self.session.capture.state()[0][phase_start:]
                                            if sample.get("phase") == item["id"]]) >= 3 and
                                      len([sample for sample in self.session.capture.diagnostics.state()[0][diagnostic_start:]
                                           if sample.get("phase") == item["id"]]) >= 3,
                              "minimaal drie geheugen- en taakmetingen in deze scenariofase", 20)
                    self.check()
                    result.update(status="PASS", detail="Alle controles geslaagd.")
                except InterruptedError as exc:
                    result.update(status="CANCELLED", detail=str(exc))
                except Exception as exc:
                    result.update(status="FAIL", detail=str(exc), failure_state={
                        "telemetry": dict(self.session.telemetry),
                        "controller_values": dict(self.session.controller_values),
                        "odus": self.session.fleet.snapshot(),
                        "transport": self.session.peer.snapshot(),
                    })
                finally:
                    self.weather_only = False
                    self.session.heartbeat_paused = False
                    self.session.peer.configure_faults(drop_every=0, corrupt_every=0, delay_ms=2)
                    for index in (0, 1):
                        self.session.fleet.set_odu({"index": index, "online": True, "no_flow": False, "defrost": False, "fault": 0})
                    if not self.session.stop_event.is_set():
                        try:
                            self.session.controller.select("CM Override", "Force CM0")
                            if self.session.controller.flow_peer:
                                self.session.controller.flow_peer.set_enabled(True)
                        except Exception as exc:
                            result.update(status="FAIL", detail="Veilige scenario-afsluiting mislukt: " + str(exc))
                    if self.case_evidence:
                        result["evidence"] = dict(self.case_evidence)
                    result["duration_s"] = round(time.monotonic() - started, 2)
                    with self.session.lock:
                        self.session.runs.append(dict(result))
                    with self.lock:
                        self.statuses[item["id"]] = result["status"].lower()
                    self.session.log(item["label"] + ": " + result["status"] + " · " + result["detail"],
                                     "error" if result["status"] == "FAIL" else "info")
                    try:
                        self.session.save_report()
                    except (OSError, ValueError) as exc:
                        result.update(status="FAIL", detail="Rapport niet opgeslagen: " + str(exc))
                        with self.session.lock:
                            self.session.runs[-1] = dict(result)
                        with self.lock:
                            self.statuses[item["id"]] = "fail"
                if result["status"] != "PASS":
                    break
            self.active = None
            self.session.capture.set_phase("idle")

    def test_smoke(self):
        self.wait(lambda: self.session.telemetry.get("hp1_online") and self.session.telemetry.get("hp2_online"), "beide ODU's online")
        self.wait(lambda: self.session.telemetry.get("hp1_generation", 0) > 0 and self.session.telemetry.get("hp2_generation", 0) > 0,
                  "beide generaties vastgesteld")
        samples, errors, boots = self.session.capture.state()
        assert not errors and boots == 1, "Firmware crashte, herstartte of meldde ongeldige telemetry"
        assert samples and samples[-1]["internal_largest_block"] > 0 and samples[-1]["psram_free"] > 0
        assert self.session.peer.snapshot()["responses"] >= 2, "Geen bewezen ODU-antwoorden op de echte UART"
        assert self.session.controller.value("text_sensor", "Virtual Lab Profile").startswith("q-duo-local-lab-v1")

    def test_inputs(self):
        original = dict(self.session.environment)
        try:
            self.session.fleet.set_environment({"room_c": 18.5, "setpoint_c": 20.5, "outdoor_c": 6.0, "heating_enable": False})
            self.session.environment = dict(self.session.fleet.environment)
            self.session.controller.apply_environment(self.session.environment)
            self.last_refresh = time.monotonic()
            expected = {"room": 18.5, "setpoint": 20.5, "outside": 6.0}
            fields = {"room": ("sensor", "Room Temperature (Selected)"), "setpoint": ("sensor", "Room Setpoint (Selected)"),
                      "outside": ("sensor", "Outside Temperature (Selected)")}
            def matches():
                actual = self.session.controller.values(fields)
                return all(type(actual[k]) in (int, float) and abs(actual[k] - v) < 0.11 for k, v in expected.items())
            self.wait(matches, "geselecteerde API-invoer komt overeen met de ingestelde omgeving", 30)
        finally:
            self.session.fleet.set_environment(original)
            self.session.environment = original

    def _begin_heating(self):
        if self.session.controller.flow_peer:
            self.session.controller.flow_peer.set_enabled(True)
        self.session.fleet.set_environment({"heating_enable": True, "flow_lph": 800.0, "room_c": 19.0, "setpoint_c": 21.0})
        self.session.environment = dict(self.session.fleet.environment)
        self.session.controller.apply_environment(self.session.environment)
        self.last_refresh = time.monotonic()
        def confirmed():
            units = self.session.fleet.snapshot()
            with self.session.lock:
                observed = dict(self.session.controller_values)
            return any(unit["frequency_hz"] > 3 and unit["level"] > 0 and
                       type(observed.get(f"frequency{index + 1}")) in (int, float) and
                       observed[f"frequency{index + 1}"] > 3 for index, unit in enumerate(units))
        self.wait(confirmed, "compressor draait en de echte controller heeft die feedback ontvangen", 180 if self.session.profile.peer == "boiler" else 120)

    def _stopped(self):
        units = self.session.fleet.snapshot()
        with self.session.lock:
            observed = dict(self.session.controller_values)
        return all(s.get("requested_level", s["level"]) == 0 and s["frequency_hz"] < 0.5 and
                   type(observed.get(f"frequency{index + 1}")) in (int, float) and
                   observed[f"frequency{index + 1}"] < 0.5 for index, s in enumerate(units))

    def test_heating(self):
        self._begin_heating()
        self.session.controller.select("CM Override", "Force CM0")
        minimum_runtime = float(self.session.controller.value("number", "Minimum runtime"))
        self.session.log(f"Normale stop wacht op de firmware-minimumlooptijd ({minimum_runtime:g}s)")
        self.wait_stable(self._stopped, "normale stop is door model én controller bevestigd en blijft 10s stabiel", minimum_runtime + 80)
        self.session.environment["heating_enable"] = False

    def test_communications(self):
        self.session.controller.select("CM Override", "Force CM0")
        self.wait(lambda: self.session.telemetry.get("hp1_online") and self.session.telemetry.get("hp2_online"), "online beginstaat")
        try:
            self.session.fleet.set_odu({"index": 0, "online": False})
            self.wait(lambda: self.session.telemetry.get("hp1_online") is False, "HP1 offline in de controller", 90)
            assert self.session.telemetry.get("hp2_online") is True, "HP2 viel onbedoeld mee uit"
        finally:
            self.session.fleet.set_odu({"index": 0, "online": True})
        self.wait(lambda: self.session.telemetry.get("hp1_online") is True, "HP1 hersteld via verse Modbus-antwoorden", 90)

    def test_no_flow_defrost(self):
        self.test_no_flow(defrost=True)

    def test_no_flow(self, defrost=False):
        self._begin_heating()
        if defrost:
            unit = max(range(2), key=lambda i: self.session.fleet.snapshot()[i]["frequency_hz"])
            self.session.fleet.set_odu({"index": unit, "defrost": True})
            self.wait(lambda: self.session.controller.value("binary_sensor", f"HP{unit + 1} - Defrost") is True and
                      self.session.controller.value("binary_sensor", f"HP{unit + 1} - 4-Way valve") is True,
                      "productiecontroller ontvangt defrost en vierwegklep vóór flowuitval", 35)
            self.case_evidence["defrost_unit"] = unit + 1
        minimum_flow, fault_ms, recover_ms, loop_ms = flow_policy()
        self.no_flow_attempt += 1
        # Repeat runs exercise a different phase relative to 1/5/30/60s loops.
        offset_ms = 2000 if self.no_flow_attempt % 2 else 7000
        phase_at = self.session.telemetry["uptime_ms"]
        self.wait(lambda: self.session.telemetry["uptime_ms"] - phase_at >= offset_ms, "fase-offset vóór sensoruitval", 15)
        off_ms = self.session.telemetry["uptime_ms"]
        if self.session.profile.name == "local-flow":
            self._stop_local_flow()
            assert any(unit["flow_lph"] > 100 for unit in self.session.fleet.snapshot()), "Geen onafhankelijke ODU-flow tijdens lokale pulsuitval"
        else:
            for index in (0, 1):
                self.session.fleet.set_odu({"index": index, "no_flow": True})
        fault_seen_ms = None
        def safe_no_flow_stop():
            nonlocal fault_seen_ms
            fault_active = self.session.telemetry.get("lowflow_fault") is True
            if fault_active:
                if fault_seen_ms is None:
                    fault_seen_ms = next(row["uptime_ms"] for row in self.session.control_history
                                     if row["uptime_ms"] >= off_ms and row["lowflow_fault"] is True)
                self._assert_no_enable(fault_seen_ms + 2000)
            return fault_active and self._stopped() and self._requests_zero()
        self.wait_stable(safe_no_flow_stop, "lowflowfout actief: ingetrokken opdracht én bevestigde stilstand blijven 10s stabiel", fault_ms / 1000 + 80)
        low = next(row for row in self.session.control_history if row["uptime_ms"] >= off_ms and
                   (not row["flow_valid"] or row["flow_lph"] < minimum_flow))
        latch_delay = fault_seen_ms - low["uptime_ms"]
        fault_window = timer_window(fault_ms, loop_ms)
        assert fault_window[0] <= latch_delay <= fault_window[1], f"Lowflowtimer {latch_delay}ms buiten policyvenster {fault_window}ms (loop={loop_ms}ms)"
        # Keep the fault active beyond the longest strategy cadence: a short
        # final zero snapshot must never hide later stale-request reassertions.
        # Exercise the two final write_lambda gates independently of the
        # request calculator. These internal select calls must emit no enable.
        for hp in (1, 2):
            for register, label, safe, positive in ((3999, "Set Working Mode", "Standby", "Heating"),
                                                   (1999, "Set Compressor Level", "0", "5")):
                name = f"HP{hp} - {label}"
                def zero_count():
                    return sum(row["unit"] == hp and row["register"] == register and row["values"] == [0] and
                               row["exception"] == 0 for row in self.session.peer.write_history())
                before = zero_count()
                self.session.controller.select(name, safe)
                self.wait(lambda: zero_count() > before, "expliciete nul is eerst op de bus afgerond", 20)
                fence = zero_count()
                self.session.controller.select(name, positive)
                self.wait(lambda: zero_count() > fence, "positieve poging levert een NIEUWE fysieke nulwrite", 20)
                self._assert_no_enable(fault_seen_ms + 2000)
        self.case_evidence["direct_positive_select_attempts"] = 4
        self.wait_stable(safe_no_flow_stop, "directe positieve selectpogingen blijven veilig geblokkeerd", 25, stable_ms=3000)
        held_at = self.session.telemetry["uptime_ms"]
        def held_safely():
            assert self.session.telemetry.get("lowflow_fault") is True, "Lowflowfout verdween zonder flowherstel"
            self._assert_no_enable(fault_seen_ms + 2000)
            assert self._stopped() and self._requests_zero(), "Compressor of cached request hervatte tijdens lowflowfout"
            return self.session.telemetry["uptime_ms"] - held_at >= 65000
        self.wait(held_safely, "65 firmwareseconden fout actief zonder heraangeboden compressor-enable", 85)
        if defrost:
            for index in (0, 1):
                self.session.fleet.set_odu({"index": index, "defrost": False})
        restored_ms = self.session.telemetry["uptime_ms"]
        if self.session.controller.flow_peer:
            self.session.controller.flow_peer.set_enabled(True)
        else:
            for index in (0, 1):
                self.session.fleet.set_odu({"index": index, "no_flow": False})
        clear_ms = None
        def recovered():
            nonlocal clear_ms
            fault = self.session.telemetry.get("lowflow_fault")
            assert type(fault) is bool, "Lowflowherstel heeft geen geldige firmwarewaarneming"
            if fault is True:
                self._assert_no_enable(fault_seen_ms + 2000)
                assert self._stopped() and self._requests_zero(), "Request of compressor hervatte vóór vrijgave lowflowfout"
                return False
            clear_ms = next(row["uptime_ms"] for row in self.session.control_history
                            if row["uptime_ms"] >= restored_ms and row["lowflow_fault"] is False)
            return True
        self.wait(recovered, "lowflowfout verdwijnt pas na aaneengesloten geldig flowherstel", recover_ms / 1000 + 80)
        recovery_rows = [row for row in self.session.control_history if restored_ms <= row["uptime_ms"] <= clear_ms]
        healthy = next(row for row in recovery_rows if row["flow_valid"] and row["flow_lph"] >= minimum_flow)
        assert all(row["flow_valid"] and row["flow_lph"] >= minimum_flow for row in recovery_rows
                   if row["uptime_ms"] >= healthy["uptime_ms"]), "Flowherstel was niet aaneengesloten geldig"
        recovery_delay = clear_ms - healthy["uptime_ms"]
        recovery_window = timer_window(recover_ms, loop_ms)
        assert recovery_window[0] <= recovery_delay <= recovery_window[1], f"Hersteltimer {recovery_delay}ms buiten policyvenster {recovery_window}ms (loop={loop_ms}ms)"
        # Exclude only the observation uncertainty at the clearing edge; all
        # earlier commands remain part of the invariant, including lost ACKs.
        self._assert_no_enable(fault_seen_ms + 2000, clear_ms - 1500)
        latched = [row for row in self.session.control_history if fault_seen_ms + 5000 <= row["uptime_ms"] and row["lowflow_fault"] is True]
        assert latched and all(row[key] == 0 for row in latched for key in
                               ("requested_level1", "requested_level2", "applied_level1", "applied_level2")), "Cached/applied request werd opnieuw positief binnen de gelatchte fout"
        self._begin_heating()
        restart_ms = self.session.telemetry["uptime_ms"]
        writes = compressor_enable_writes(self.session.peer.write_history(), clear_ms - 3000)
        assert writes, "Herstel gaf geen nieuwe compressoropdracht op de echte Modbusbus"
        self.case_evidence.update(policy=dict(minimum_flow_lph=minimum_flow, fault_ms=fault_ms, recovery_ms=recover_ms, supervisory_loop_ms=loop_ms),
            phase_offset_ms=offset_ms, source_off_ms=off_ms, first_low_ms=low["uptime_ms"], fault_seen_ms=fault_seen_ms,
            fault_delay_ms=latch_delay, source_restored_ms=restored_ms, first_healthy_ms=healthy["uptime_ms"],
            fault_cleared_ms=clear_ms, recovery_delay_ms=recovery_delay, confirmed_restart_ms=restart_ms,
            first_restart_write=writes[0], forbidden_enable_writes=0, stable_fault_hold_ms=65000,
            observation_tolerance_ms=1500, fault_window_ms=fault_window, recovery_window_ms=recovery_window)
        self.session.log(f"Lowflow: fout na {latch_delay}ms, 65s stabiele stop, herstel na {recovery_delay}ms en echte compressorherstart bevestigd")
        self.session.environment["heating_enable"] = False

    def _requests_zero(self):
        values = self.session.controller_values
        # ModbusSelect also publishes ODU register readback. During forced
        # defrost that register may read 1 in Standby with requested_level=0 and
        # frequency=0. Cached requests/applied levels are distinct from readback;
        # actual outgoing positive writes remain forbidden by the bus invariant.
        return all(type(values.get(f"{kind}_level{hp}")) in (int, float) and values[f"{kind}_level{hp}"] == 0
                   for hp in (1, 2) for kind in ("requested", "applied"))

    def _assert_no_enable(self, since_ms, until_ms=None):
        violations = compressor_enable_writes(self.session.peer.write_history(), since_ms, until_ms, (1999, 3999))
        assert not violations, "Niet-nul compressorcommando na bevestigde lowflowfout: " + str(violations[0] if violations else "")

    def test_stale(self):
        self._begin_heating()
        self.session.controller.apply_environment(self.session.environment)
        self.session.heartbeat_paused = True
        thermostat = self.session.profile.peer == "thermostat"
        if thermostat:
            self.session.controller.ot_peer.offline()
            self.weather_only = True
        start = self.session.telemetry.get("uptime_ms", 0)
        if thermostat:
            self.wait(lambda: self.session.ot_telemetry["OQ_OT_CONTROLLER"]["thermostat_status_valid"] is False,
                      "OT STATUS vervalt na de normale 30s terwijl API-weer vers blijft", 45)
            assert self.session.ot_telemetry["OQ_OT_CONTROLLER"]["room_fresh"] is True, "OT-room verliep vóór de afzonderlijke linktimeout"
        expiry = 132000 if thermostat else 57000
        self.wait(lambda: self.session.telemetry.get("uptime_ms", 0) - start >= expiry,
                  "productie-OT-linktimeout120s plus bronhold" if thermostat else "45s stale plus 10s bronhold verstreken", 170 if thermostat else 80)
        if thermostat:
            assert self.session.ot_telemetry["OQ_OT_CONTROLLER"]["thermostat_status_valid"] is False
        raw = self.session.controller.value("sensor", "Room Temperature (Selected)")
        assert raw is None or str(raw).lower() in ("nan", "unknown", "unavailable"), f"Room-invoer bleef geldig: {raw}"
        minimum_runtime = float(self.session.controller.value("number", "Minimum runtime"))
        self.wait_stable(self._stopped, "verlopen enable/demand: normale afbouw en bevestigde stilstand blijven 10s stabiel", minimum_runtime + 80)
        self.session.environment["heating_enable"] = False

    def test_ot_wire(self):
        controller = self.session.controller
        peer = controller.ot_peer
        original = dict(self.session.environment)
        controller.select("CM Override", "Force CM0")
        self.session.heartbeat_paused = True
        try:
            self.session.environment.update(room_c=18.75, setpoint_c=22.25, heating_enable=False)
            controller.apply_environment(self.session.environment, auto_mode=False)
            controller.number("Lab OT Peer Control Setpoint", 43.5)
            for name in ("Room Temperature Source", "Room Setpoint Source", "Heating Enable Source"):
                assert controller.value("select", name) == "OT thermostat"
            received = self.session.ot_telemetry["OQ_OT_CONTROLLER"]["received"]
            def accepted(room):
                raw = self.session.ot_telemetry.get("OQ_OT_CONTROLLER", {})
                selected = controller.values({"room": ("sensor", "Room Temperature (Selected)"),
                                              "setpoint": ("sensor", "Room Setpoint (Selected)")})
                return (raw.get("received", 0) > received and raw.get("thermostat_status_valid") is True and
                        all(raw.get(key) is True for key in ("room_fresh", "room_setpoint_fresh", "control_setpoint_fresh")) and
                        abs((raw.get("room_c") or -100) - room) < .05 and
                        abs((raw.get("room_setpoint_c") or -100) - 22.25) < .05 and
                        abs((raw.get("control_setpoint_c") or -100) - 43.5) < .05 and
                        type(selected["room"]) in (int, float) and abs(selected["room"] - room) < .11 and
                        type(selected["setpoint"]) in (int, float) and abs(selected["setpoint"] - 22.25) < .11)
            self.wait(lambda: accepted(18.75), "OT-driver en geselecteerde bronnen ontvangen unieke Manchesterwaarden", 35)
            peer.offline()
            controller.switch("Lab OT Peer Bad Parity", True)
            controller.number("Lab OT Peer Room Temperature", 27.25)
            self.pause(2)
            baseline = dict(self.session.ot_telemetry["OQ_OT_CONTROLLER"])
            controller.switch("Lab OT Peer Online", True)
            self.wait(lambda: self.session.ot_telemetry["OQ_OT_CONTROLLER"]["invalid"] > baseline["invalid"], "echte OT-driver verwerpt pariteitsfout", 20)
            self.pause(5)
            raw = self.session.ot_telemetry["OQ_OT_CONTROLLER"]
            assert raw["received"] == baseline["received"], "Corrupte frames werden als geldig ontvangen"
            assert abs(raw["room_c"] - 18.75) < .05, "Corrupte frame wijzigde OT-roomwaarde"
            selected = controller.value("sensor", "Room Temperature (Selected)")
            assert type(selected) in (int, float) and abs(selected - 18.75) < .11, "Corrupte frame wijzigde geselecteerde waarde"
            peer.offline()
            controller.switch("Lab OT Peer Bad Parity", False)
            controller.number("Lab OT Peer Room Temperature", 20.5)
            controller.switch("Lab OT Peer Online", True)
            self.wait(lambda: accepted(20.5), "verse geldige OT-frames herstellen ontvangst en bronselectie", 35)
            self.session.log(f"OT-wire: peer TX={self.session.ot_telemetry['OQ_OT_PEER']['tx']}, productie RX={raw['received']}, corrupt verworpen={raw['invalid'] - baseline['invalid']}")
        finally:
            peer.offline()
            controller.switch("Lab OT Peer Bad Parity", False)
            self.session.environment = original
            controller.apply_environment(original, auto_mode=False)
            self.session.heartbeat_paused = False

    def test_ot_boiler(self):
        controller = self.session.controller
        controller.select("CM Override", "Force CM0")
        def healthy():
            values = self.session.controller_values
            peer = self.session.ot_telemetry.get("OQ_OT_PEER", {})
            return (values.get("boiler_link") is True and peer.get("rx", 0) > 2 and peer.get("tx", 0) > 2 and
                    type(values.get("boiler_water")) in (int, float) and abs(values["boiler_water"] - 42.5) < .1 and
                    type(values.get("boiler_return")) in (int, float) and abs(values["boiler_return"] - 35) < .1)
        self.wait(healthy, "echte ketelhub ontvangt en correleert RMT-antwoorden en temperaturen", 100)
        before = self.session.ot_telemetry["OQ_OT_PEER"]["rx"]
        controller.switch("Lab OT Peer Online", False)
        self.wait(lambda: self.session.controller_values.get("boiler_link") is False, "ketellink wordt onbeschikbaar zonder antwoorden", 100)
        assert self.session.ot_telemetry["OQ_OT_PEER"]["rx"] > before, "Geen echte requests tijdens antwoordverlies"
        controller.switch("Lab OT Peer Online", True)
        self.wait(healthy, "ketellink en temperatuurfeedback herstellen via verse RMT-antwoorden", 100)
        peer = self.session.ot_telemetry["OQ_OT_PEER"]
        assert peer["ch_enabled"] is False and peer["target_c"] == 0, "CM0 stuurde onterecht een ketelwarmtevraag"
        self.session.log(f"OT-boiler: echte requests={peer['rx']}, antwoorden={peer['tx']}, linkverlies en herstel bevestigd")
        original = dict(self.session.environment)
        faults = [unit["fault"] for unit in self.session.fleet.snapshot()]
        try:
            controller.switch("Auxiliary heat source connected", True)
            controller.switch("Boiler fallback on heat-pump fault", True)
            self.session.fleet.set_environment({"heating_enable": True, "flow_lph": 800, "room_c": 19, "setpoint_c": 21})
            self.session.environment = dict(self.session.fleet.environment)
            for index in (0, 1):
                # R2119.b0 main_line_current is a catalogued hard incident:
                # firmware must receive fresh fault and stop confirmations.
                self.session.fleet.set_odu({"index": index, "fault": 1})
            controller.apply_environment(self.session.environment)
            self.last_refresh = time.monotonic()
            def fallback_active():
                peer = self.session.ot_telemetry.get("OQ_OT_PEER", {})
                return (self.session.telemetry.get("control_mode") == 4 and peer.get("ch_enabled") is True and
                        type(peer.get("target_c")) in (int, float) and math.isfinite(peer["target_c"]) and peer["target_c"] > 0 and self._stopped())
            self.wait(fallback_active, "echte incidentbevestiging en CM4 sturen positieve ketelvraag met bevestigde HP-stilstand", 180)
            event_start = len(self.session.ot_events)
            controller.switch("Auxiliary heat source connected", False)
            def confirmed_off():
                peer = self.session.ot_telemetry.get("OQ_OT_PEER", {})
                events = list(self.session.ot_events)[event_start:]
                return (any(event["kind"] == "off-ack" and 0 <= event["duration_ms"] <= 5000 for event in events) and
                        peer.get("ch_enabled") is False and peer.get("target_c") == 0)
            self.wait(confirmed_off, "presence-gate trekt vraag in en echte driver bevestigt beide OFF-antwoorden", 10)
            assert not any(event["kind"] == "off-timeout" for event in list(self.session.ot_events)[event_start:]), "OFF-handshake liep vast"
            self.session.log("OT-boiler: CM4-warmtevraag via echte incidentroute en geacknowledgeerde OFF-handshake na presence-gate bewezen")
        finally:
            if not self.session.stop_event.is_set():
                # Keep the physical-absence gate closed while restoring inputs.
                controller.switch("Auxiliary heat source connected", False)
                controller.select("CM Override", "Force CM0")
                self.session.fleet.set_environment(original)
                self.session.environment = original
                for index, fault in enumerate(faults):
                    self.session.fleet.set_odu({"index": index, "fault": fault})
                controller.apply_environment(original, auto_mode=False)
                controller.switch("Boiler fallback on heat-pump fault", False)
                controller.switch("Auxiliary heat source connected", True)


    def _stop_local_flow(self):
        self.session.controller.flow_peer.set_enabled(False)
        def committed():
            value = self.session.flow_telemetry
            return (value.get("enabled") is False and value.get("applied_enabled") is False and
                    value.get("active") is False and value.get("generation") == value.get("requested_generation"))
        self.wait(committed, "OFF-intent is door de onafhankelijke bron gecommitteerd", 10)
        value = self.session.flow_telemetry
        edges = (value["generated_rising_edges"], value["generated_falling_edges"])
        self.wait_stable(lambda: committed() and (self.session.flow_telemetry["generated_rising_edges"],
                         self.session.flow_telemetry["generated_falling_edges"]) == edges,
                         "gecommitteerde OFF-bron heeft drie firmwareseconden geen flanken", 12, stable_ms=3000)

    def test_hydraulics(self):
        controller = self.session.controller
        self.session.fleet.set_environment({"heating_enable": False, "flow_lph": 800})
        self.session.environment = dict(self.session.fleet.environment)
        controller.select("CM Override", "Force CM0")
        controller.apply_environment(self.session.environment, auto_mode=False)
        minimum_runtime = float(controller.value("number", "Minimum runtime"))
        self.wait(self._stopped, "compressoren staan werkelijk stil vóór hydrauliekproef", minimum_runtime + 80)
        def idle():
            hydraulic = self.session.fleet.hydraulics()
            values = self.session.controller_values
            return (self.session.telemetry.get("control_mode") == 0 and not any(hydraulic["pump_requested"]) and
                    hydraulic["flow_lph"] < 1 and all(type(values.get(key)) in (int, float) and
                    0 <= values[key] < 1 for key in ("flow", "controller_flow")))
        self.wait(idle, "CM0: ontvangen pompcommando's OFF, nativeflow en gemeten GPIO-flow nul", 110)
        initial = self.session.telemetry["uptime_ms"]
        nominal = self.session.fleet.environment["flow_lph"]
        assert nominal > 0
        self._assert_no_enable(initial)
        controller.select("CM Override", "Force CM1")
        def circulating():
            self._assert_no_enable(initial)
            hydraulic = self.session.fleet.hydraulics()
            return (any(hydraulic["pump_running"]) and all(abs(unit["flow_lph"] - nominal) < max(1, nominal * .01)
                    for unit in self.session.fleet.snapshot()) and all(type(self.session.controller_values.get(key)) in (int, float) and
                    abs(self.session.controller_values[key] - nominal) < max(30, nominal * .05) for key in ("flow", "controller_flow")))
        self.wait(circulating, "CM1: echte pompcommando's geven dezelfde serieflow in beide ODU's en GPIO-meting", 90)
        assert self._stopped(), "Circulatieproef startte onbedoeld de compressor"
        running = self.session.telemetry["uptime_ms"]
        controller.select("CM Override", "Force CM0")
        self.wait(idle, "CM0 na circulatie: pompcommando's OFF en de volledige meetketen weer nul", 110)
        self._assert_no_enable(initial)
        self.case_evidence.update(nominal_flow_lph=nominal, initial_idle_ms=initial, circulation_seen_ms=running,
                                 returned_idle_ms=self.session.telemetry["uptime_ms"], idle_flow_lph=self.session.controller_values["flow"],
                                 series_flow_shared=True, no_compressor_started=True)
        self.session.log(f"Hydrauliek: CM0 nulflow, CM1 seriedebiet {nominal:g} L/h zonder compressor, daarna CM0 weer nul")

    def test_local_flow_wire(self):
        controller = self.session.controller
        controller.select("CM Override", "Force CM1")
        assert controller.value("select", "Q Flow Source") == "Local"
        assert controller.value("select", "Controller Flow Meter") == "Huba Control"
        original = dict(self.session.environment)
        try:
            for flow in (600, 1200):
                self.session.fleet.set_environment({"flow_lph": flow})
                self.session.environment = dict(self.session.fleet.environment)
                controller.flow_peer.apply(self.session.environment)
                baseline = self.session.flow_telemetry.get("generated_rising_edges", 0)
                def observed():
                    values = controller.values({"raw": ("sensor", "Controller Flow"), "selected": ("sensor", "Flow average (Selected)")})
                    fixture = self.session.flow_telemetry
                    return (fixture.get("enabled") is True and fixture.get("applied_enabled") is True and
                            fixture.get("active") is True and fixture.get("applied_flow_lph") == flow and
                            fixture.get("generation") == fixture.get("requested_generation") and
                            fixture.get("generated_rising_edges", 0) > baseline + 50 and
                            all(type(value) in (int, float) and abs(value - flow) < max(30, flow * .05) for value in values.values()))
                self.wait(observed, f"GPIO15 -> pulse_meter -> geselecteerde Huba-flow {flow} L/h", 60)
            self._stop_local_flow()
            self.wait(lambda: all(type(value) in (int, float) and math.isfinite(value) and 0 <= value < 1 for value in controller.values(
                {"raw": ("sensor", "Controller Flow"), "selected": ("sensor", "Flow average (Selected)")}).values()), "gestopte pulsen geven echt nuldebiet", 65)
            self.session.log("Lokale flow: 600/1200 L/h en pulse-stop via productiecounter, filtering en bronselectie bewezen")
        finally:
            self.session.fleet.set_environment(original)
            self.session.environment = original
            self.session.heartbeat_paused = False
            controller.flow_peer.apply(original)
            controller.flow_peer.set_enabled(True)

    def test_profiles(self):
        self.session.controller.select("CM Override", "Force CM0")
        original = list(self.session.fleet.profiles)
        try:
            for profile, code in (("V1", 1), ("V1.5", 2), ("V2 old", 3), ("V2 new", 4)):
                self.check()
                self.session.fleet.set_odu({"index": 0, "profile": profile})
                self.session.controller.action("button", "HP1 - Detect ODU generation", "press")
                self.wait(lambda: self.session.telemetry.get("hp1_generation") == code, "firmware herkent " + profile, 60)
                unit = self.session.fleet.snapshot()[0]
                assert unit["protocol"]["legacy_extension_reads"] == 0, "Firmware las een V2-extensie bij een legacyprofiel"
        finally:
            for index, profile in enumerate(original):
                self.session.fleet.set_odu({"index": index, "profile": profile})
                self.session.controller.action("button", f"HP{index + 1} - Detect ODU generation", "press")

    def test_network(self):
        from scripts.simulator.load import WebLoad
        self.session.controller.select("CM Override", "Force CM0")
        load = WebLoad(self.session.http_port)
        try:
            load.start()
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                self.check()
                self.refresh()
                state = load.snapshot()
                assert not state["errors"], "Web/SSE-belasting: " + "; ".join(state["errors"])
                assert state["workers_alive"] == 3, "Een web/SSE-worker stopte onverwacht"
                self.cancel_event.wait(.4)
        finally:
            load.close()
            with self.session.lock:
                self.session.load_results.append({"scenario": "network", **load.snapshot()})
        state = load.snapshot()
        assert not state["errors"], "Web/SSE-belasting meldde een fout"
        assert state["http_requests"] >= 20, "Te weinig echte HTTP-verzoeken"
        assert state["sse_delivering_connections"] >= 2 and state["sse_events"] >= 2, "SSE-streams/reconnects niet bewezen"
        self.session.log(f"Web/SSE: {state['http_requests']} HTTP-verzoeken, {state['sse_connections']} streams, {state['sse_events']} events")

    def test_memory(self):
        self.session.controller.select("CM Override", "Force CM0")
        start_samples = len(self.session.capture.state()[0])
        deadline = time.monotonic() + 90
        requests = 0
        while time.monotonic() < deadline:
            self.check()
            self.refresh()
            html, _ = emulator.request(self.session.http_port, "/")
            assert b"<esp-app" in html, "Webshell ontbreekt"
            javascript, _ = emulator.request(self.session.http_port, "/0.js")
            assert b"openquatt" in javascript.lower(), "Ingebedde webapp ontbreekt"
            self.session._poll_controller()
            requests += 3
            self.cancel_event.wait(0.5)
        samples = self.session.capture.state()[0][start_samples:]
        assert len(samples) >= 10, "Te weinig geheugensamples onder belasting"
        summary = emulator.summarize(samples)
        assert summary["internal_min_free"] > 0 and summary["internal_largest_block"] > 0
        assert summary["psram_free"] > 0 and summary["main_stack_hwm_bytes"] > 0
        self.session.log(f"Webbelasting: {requests} HTTP-verzoeken; interne minimumheap {summary['internal_min_free']} bytes")
