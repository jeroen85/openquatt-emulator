"""Owned local emulator lifecycle and live virtual HCQ lab state."""
from __future__ import annotations

import collections
import hashlib
import json
import math
import os
import queue
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from scripts.emulator import run as emulator
from scripts.simulator.controller import Controller
from scripts.simulator.diagnostics import Diagnostics, PROFILE as DIAGNOSTICS_PROFILE
from scripts.simulator.model import OduFleet, PROFILES, ROOT, checked_environment, checked_odu
from scripts.simulator.transport import RtuPeer
from scripts.simulator import provenance
from scripts.simulator.profiles import get_profile
from scripts.simulator.opentherm import Evidence as OtEvidence, MARKERS as OT_MARKERS, source_fingerprint

BUILD = ROOT / ".esphome/build/simulator_q_duo/build"
CONFIG = ROOT / "configs/emulator/lab_duo.yaml"
BINARY = ROOT / ".tmp/emulator/runtime/esp-emu"
CONTRACT = "openquatt-local-lab-v1"
# Capture once while modules are being loaded, never relabel a live server after edits.
PROCESS_SOURCES = provenance.process_sources()


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def validate_control_state(value, previous=None):
    """One-second production-control observations; no HTTP polling timestamp."""
    if not isinstance(value, dict) or type(value.get("state_schema")) is not int or value["state_schema"] != 2:
        raise ValueError("Unsupported lab control-state schema")
    for key in ("uptime_ms", "control_mode", "hp1_generation", "hp2_generation",
                "requested_level1", "requested_level2", "applied_level1", "applied_level2"):
        if type(value.get(key)) is not int or not 0 <= value[key] <= 0xFFFFFFFF:
            raise ValueError("Invalid control-state integer: " + key)
    for key in ("hp1_online", "hp2_online", "flow_valid", "lowflow_fault"):
        if type(value.get(key)) is not bool:
            raise ValueError("Invalid control-state boolean: " + key)
    if type(value.get("flow_lph")) not in (int, float) or not math.isfinite(value["flow_lph"]) or value["flow_lph"] < 0:
        raise ValueError("Invalid control-state flow")
    # Power House dispatch can request and apply levels through 20.
    if any(value[key] > 20 for key in ("requested_level1", "requested_level2", "applied_level1", "applied_level2")):
        raise ValueError("Invalid control-state level")
    if previous is not None and not 0 < ((value["uptime_ms"] - previous["uptime_ms"]) & 0xFFFFFFFF) < 0x80000000:
        raise ValueError("Control-state clock did not advance")
    return dict(value)


class LabCapture(emulator.Capture):
    def __init__(self, output, session):
        super().__init__(output)
        self.session = session
        self.epoch = session.epoch
        self.fleet = session.fleet
        self.diagnostics = Diagnostics()
        self.first_tick_ms = None
        self.finish_at_commit = threading.Event()
        self.ended_at_commit = False
        self.ot_evidence = OtEvidence(session.profile.peer, session.profile.loopbacks, source_fingerprint(ROOT)) if session.profile.peer else None
        from scripts.simulator.flow import FlowEvidence
        self.flow_evidence = FlowEvidence() if session.profile.name in ("local-flow", "ot-boiler") else None

    def consume(self, stream):
        def lines():
            for line in stream:
                clean = emulator.ANSI.sub("", line)
                marker = "OQ_LAB_STATE "
                diagnostic_marker = next((tag for tag in ("OQ_LAB_TASK", "OQ_LAB_DIAGNOSTICS") if tag + " " in clean), None)
                ot_marker = next((tag for tag in OT_MARKERS if tag + " " in clean), None)
                if "OQ_OT_PEER_ERROR" in clean:
                    yield line
                    raise ValueError("OpenTherm peer driver error: " + clean.strip())
                if ot_marker:
                    try:
                        if not self.ot_evidence:
                            raise ValueError("Unexpected OpenTherm telemetry for this profile")
                        value = self.ot_evidence.accept(ot_marker, json.loads(clean.split(ot_marker + " ", 1)[1]))
                        with self.session.lock:
                            if self.epoch != self.session.epoch:
                                raise RuntimeError("OpenTherm telemetry from an obsolete session")
                            self.session.ot_telemetry[ot_marker] = value
                            if len(self.session.ot_history) >= self.session.ot_history.maxlen:
                                raise ValueError("OpenTherm evidence history limit exceeded")
                            self.session.ot_history.append({"marker": ot_marker, "phase": self.phase, **value})
                    except (ValueError, TypeError):
                        yield line
                        raise
                elif "OQ_LAB_FLOW " in clean:
                    try:
                        if self.flow_evidence is None:
                            raise ValueError("Unexpected local flow fixture")
                        marker = json.loads(clean.split("OQ_LAB_FLOW ", 1)[1])
                        with self.session.lock:
                            if self.epoch != self.session.epoch:
                                raise RuntimeError("Flow telemetry from an obsolete session")
                            if len(self.session.flow_history) >= self.session.flow_history.maxlen:
                                raise ValueError("Flow evidence history limit exceeded")
                            value = self.flow_evidence.accept(marker, self.phase)
                            self.session.flow_telemetry = value
                            self.session.flow_history.append(dict(value))
                    except (ValueError, TypeError, RuntimeError):
                        yield line
                        raise
                elif diagnostic_marker:
                    try:
                        value = json.loads(clean.split(diagnostic_marker + " ", 1)[1])
                        with self.lock:
                            phase = self.phase
                        self.diagnostics.accept(diagnostic_marker, value, phase, time.monotonic() - self.started)
                    except (ValueError, TypeError):
                        yield line  # Preserve the rejected record in the serial evidence first.
                        raise
                elif marker in clean:
                    value = json.loads(clean.split(marker, 1)[1])
                    if not isinstance(value, dict) or type(value.get("uptime_ms")) is not int:
                        raise ValueError("Invalid lab firmware telemetry")
                    with self.session.lock:
                        if self.epoch != self.session.epoch:
                            raise RuntimeError("Telemetry from an obsolete lab session")
                        if len(self.session.control_history) >= self.session.control_history.maxlen:
                            raise ValueError("Control evidence history limit exceeded")
                        previous = self.session.control_history[-1] if self.session.control_history else None
                        value = validate_control_state(value, previous)
                    self.fleet.advance(value["uptime_ms"])
                    with self.session.lock:
                        self.session.control_history.append({"phase": self.phase, **value})
                        if self.first_tick_ms is None:
                            self.first_tick_ms = value["uptime_ms"]
                        self.session.telemetry = value
                        self.session.last_tick = time.monotonic()
                elif (handshake := re.search(r"OpenTherm off handshake (acknowledged in|timed out after) (\d+) ms", clean)):
                    with self.session.lock:
                        if self.epoch != self.session.epoch:
                            raise RuntimeError("OpenTherm event from an obsolete session")
                        if len(self.session.ot_events) >= self.session.ot_events.maxlen:
                            raise ValueError("OpenTherm event history limit exceeded")
                        self.session.ot_events.append({"kind": "off-ack" if handshake[1] == "acknowledged in" else "off-timeout",
                            "duration_ms": int(handshake[2]), "phase": self.phase,
                            "observed_uptime_ms": self.session.telemetry.get("uptime_ms", 0)})
                    self.session.log(clean.strip()[:800], "info" if handshake[1] == "acknowledged in" else "warning")
                elif "OQ_EMU_MEMORY " not in clean and any(tag in clean for tag in ("[E]", "[W]", "OQ_EMU_BOOT")):
                    self.session.log(clean.strip()[:800], "warning" if "[W]" in clean else "error" if "[E]" in clean else "info")
                yield line
                if self.finish_at_commit.is_set() and diagnostic_marker == "OQ_LAB_DIAGNOSTICS":
                    self.ended_at_commit = True
                    return
        super().consume(lines())


class Session:
    def __init__(self, *, http_port=18080, uart_port=18082, output=None, lab_profile="duo"):
        if http_port == uart_port:
            raise ValueError("Firmware HTTP and UART ports must differ")
        self.profile = get_profile(lab_profile)
        if self.profile.backend != "esp-emu":
            raise ValueError("Physical profiles cannot be started in the emulator")
        self.build = BUILD if lab_profile == "duo" else ROOT / self.profile.build
        self.config = CONFIG if lab_profile == "duo" else ROOT / self.profile.config
        self.http_port = http_port
        self.uart_port = uart_port
        self.output_base = Path(output) if output else ROOT / ".tmp/simulator/runs"
        self.lock = threading.RLock()
        self.report_lock = threading.RLock()
        self.command_lock = threading.RLock()
        self.commands = queue.Queue(maxsize=8)
        self.stop_event = threading.Event()
        self.ready = threading.Event()
        self.epoch = 0
        self.user_stop = False
        self.budgets = {"min_internal_free": 32768, "min_largest_block": 32768, "min_main_stack": 2048, "min_task_stack": 256}
        self.worker = None
        self.process = None
        self.reader = None
        self.capture = None
        self.fleet = None
        self.peer = None
        self.controller = None
        self.output = None
        self.state = "stopped"
        self.stage = "Klaar om te starten"
        self.error = None
        self.operation_error = None
        self.firmware = None
        self.started_at = None
        self.telemetry = {}
        self.controller_values = {}
        self.last_tick = 0.0
        self.observations = collections.deque(maxlen=720)
        self.control_history = collections.deque(maxlen=20000)
        self.logs = collections.deque(maxlen=180)
        self.runs = collections.deque(maxlen=100)
        self.environment = {"outdoor_c": 8.0, "return_c": 28.0, "room_c": 19.0,
                            "setpoint_c": 21.0, "flow_lph": 800.0, "heating_enable": False}
        self.profiles = ["V2 old", "V2 new"]
        self.pending_odu = [{"index": 0}, {"index": 1}]
        self.heartbeat_paused = False
        self.scenarios = None
        self.report = None
        self.source_manifest = {}
        self.frozen_firmware_sha256 = None
        self.frozen_elf_sha256 = None
        self.emulator_sha256 = None
        self.run_configuration = {}
        self.build_manifest = None
        self.load_results = []
        self.flow_telemetry = {}
        self.flow_history = collections.deque(maxlen=20000)
        self.ot_telemetry = {}
        self.ot_history = collections.deque(maxlen=20000)
        self.ot_events = collections.deque(maxlen=2000)
        self.log("OpenQuatt Simulator gereed. De fysieke installatie is niet verbonden.")
        if self.profile.name == "local-flow":
            self.log("Flowsensortest beschikbaar: pulsuitval, stopbewaking en herstel via OpenQuatt-firmware.")

    def log(self, message, level="info"):
        with self.lock:
            self.logs.append({"time": utc_now(), "level": level, "message": str(message)})

    def _stage(self, value):
        with self.lock:
            self.stage = value
        self.log(value)

    def start(self):
        with self.lock:
            if self.worker and self.worker.is_alive():
                if self.state in ("starting", "running"):
                    return
                raise RuntimeError("De vorige sessie sluit nog af")
            if self.scenarios and self.scenarios.is_running():
                raise RuntimeError("Een scenario sluit nog af")
            if (self.reader and self.reader.is_alive()) or (self.peer and self.peer.thread and self.peer.thread.is_alive()):
                raise RuntimeError("Verbindingen van de vorige sessie sluiten nog af")
            self.epoch += 1
            self.user_stop = False
            self.runs.clear()
            self.observations.clear()
            self.control_history.clear()
            self.load_results = []
            self.flow_telemetry = {}
            self.flow_history.clear()
            self.ot_telemetry = {}
            self.ot_history.clear()
            self.ot_events.clear()
            self.report = None
            self.source_manifest = {}
            self.frozen_firmware_sha256 = None
            self.frozen_elf_sha256 = None
            self.emulator_sha256 = None
            self.run_configuration = {}
            self.build_manifest = None
            self.scenarios = None
            self.capture = None
            self.peer = None
            self.process = None
            self.reader = None
            self.controller = None
            self.state = "starting"
            self.stage = "ESP32-emulator voorbereiden"
            self.error = None
            self.operation_error = None
            self.ready.clear()
            while True:
                try:
                    self.commands.get_nowait()
                    self.commands.task_done()
                except queue.Empty:
                    break
            self.stop_event.clear()
            self.worker = threading.Thread(target=self._run, name="virtual-hcq", daemon=True)
            self.worker.start()

    def stop(self, *, wait=True):
        with self.lock:
            if self.state not in ("stopped", "failed"):
                self.state = "stopping"
                self.stage = "Simulatiesessie en verbindingen afsluiten"
            self.user_stop = True
            self.stop_event.set()
            if self.scenarios:
                self.scenarios.cancel()
            worker = self.worker
        if wait and worker and worker is not threading.current_thread():
            worker.join(timeout=20)
            if worker.is_alive():
                raise RuntimeError("De emulatorsessie kon niet binnen 20s worden gestopt")

    def _check(self):
        if self.stop_event.is_set():
            raise InterruptedError("Sessie gestopt")
        if not self.process or not self.capture:
            raise RuntimeError("Emulator ontbreekt")
        emulator.check_process(self.process, self.capture)
        if self.peer and self.peer.error:
            raise RuntimeError("ODU-transport: " + self.peer.error)
        if self.last_tick and time.monotonic() - self.last_tick > 10:
            raise RuntimeError("Firmwareklok staat meer dan 10s stil")
        if isinstance(self.capture, LabCapture):
            with self.lock:
                now = self.telemetry.get("uptime_ms", 0)
                first = self.capture.first_tick_ms
                flow = self.flow_telemetry
            samples = self.capture.state()[0]
            diagnostics = self.capture.diagnostics.latest()
            if samples and now - samples[-1]["uptime_ms"] > 5000:
                raise RuntimeError("Geheugenmeting is meer dan 5s firmwaretijd verouderd")
            if diagnostics and now - diagnostics["uptime_ms"] > 5000:
                raise RuntimeError("Taak-/allocatiemeting is meer dan 5s firmwaretijd verouderd")
            if self.profile.peer and first is not None and now - first > 10000:
                for marker in ("OQ_OT_PEER", "OQ_OT_CONTROLLER"):
                    value = self.ot_telemetry.get(marker)
                    if not value or now - value["uptime_ms"] > 5000:
                        raise RuntimeError("OpenTherm telemetry ontbreekt of is verouderd: " + marker)
                if "OQ_OT_PEER_BUILD" not in self.ot_telemetry:
                    raise RuntimeError("OpenTherm build marker ontbreekt")
            # The boiler profile can also run with a GPIO-flow fixture in its firmware.
            if (self.profile.name == "local-flow" or flow) and first is not None and now - first > 10000:
                if not flow or now - flow["uptime_ms"] > 5000:
                    raise RuntimeError("Local flow fixture telemetry ontbreekt of is verouderd")
            if first is not None and now - first > 10000 and (not samples or not diagnostics):
                raise RuntimeError("Geheugen- of taakdiagnostiek ontbreekt na de opstartgrace")

    def _wait(self, predicate, label, timeout=60):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._check()
            result = predicate()
            if result:
                return result
            self.stop_event.wait(0.2)
        raise RuntimeError("Timeout: " + label)

    def _run(self):
        self.started_at = utc_now()
        self.telemetry = {}
        self.last_tick = 0
        self.controller_values = {}
        self.firmware = None
        self.heartbeat_paused = False
        self.output = self.output_base / (time.strftime("run-%Y%m%d-%H%M%S") + f"-{time.time_ns() % 1000000:06d}")
        failure = None
        try:
            self.output.mkdir(parents=True, exist_ok=False)
            provenance.assert_unchanged(PROCESS_SOURCES, provenance.process_sources(),
                "Simulatorcode gewijzigd sinds het starten van de server; herstart de lokale simulatorserver")
            build_manifest = provenance.verified_build(self.build, profile=self.profile.name)
            self.source_manifest = dict(PROCESS_SOURCES)
            for name, fingerprint in build_manifest["sources"].items():
                if name.startswith("configs/emulator/") or name.startswith("scripts/simulator/firmware/") or name.startswith("scripts/simulator/io/virtual_"):
                    self.source_manifest[name] = fingerprint
            self.build_manifest = build_manifest
            self.run_configuration = {"profiles": list(self.profiles), "environment": dict(self.environment),
                "pending_odu": [dict(item) for item in self.pending_odu], "memory_sample_interval_ms": 1000,
                "diagnostics_interval_ms": 1000, "scenario_min_samples": 3, "clock": "guest", "backend": "esp-emu",
                "lab_profile": self.profile.name, "rmt_loopbacks": self.profile.loopbacks, "ot_peer": self.profile.peer,
                "ot_peer_source_sha256": source_fingerprint(ROOT) if self.profile.peer else None}
            self.emulator_sha256 = hashlib.sha256(BINARY.read_bytes()).hexdigest() if BINARY.is_file() else None
            for port in (self.http_port, self.uart_port):
                emulator.free_port(port)
            if not BINARY.is_file() or not (self.build / "firmware.factory.bin").is_file():
                raise RuntimeError("Simulatorfirmware of emulator ontbreekt. Voer lokaal uit: python3 scripts/simulator/lab.py build --install")
            version = subprocess.run([str(BINARY), "--version"], capture_output=True, text=True, check=True).stdout.strip()
            if not re.search(rf"\bv?{re.escape(emulator.VERSION)}\b", version):
                raise RuntimeError("Emulatorversie komt niet overeen met de vastgepinde versie")
            if self.fleet:
                self.fleet.close()
            self.fleet = OduFleet()
            for index, profile in enumerate(self.profiles):
                self.fleet.set_odu({**self.pending_odu[index], "index": index, "profile": profile})
            self.fleet.set_environment(self.environment)
            self.peer = RtuPeer(self.fleet)
            self.capture = LabCapture(self.output, self)
            flash = self.output / "flash.bin"
            shutil.copyfile(self.build / "firmware.factory.bin", flash)
            self.frozen_firmware_sha256 = hashlib.sha256(flash.read_bytes()).hexdigest()
            if self.frozen_firmware_sha256 != build_manifest["firmware_sha256"]:
                raise RuntimeError("Firmware veranderde tijdens kopiëren; bouw opnieuw")
            owned_elf = self.output / "firmware.elf"
            shutil.copyfile(self.build / "firmware.elf", owned_elf)
            self.frozen_elf_sha256 = hashlib.sha256(owned_elf.read_bytes()).hexdigest()
            if self.frozen_elf_sha256 != build_manifest["elf_sha256"]:
                raise RuntimeError("ELF veranderde tijdens kopiëren; bouw opnieuw")
            command = [str(BINARY), "--chip", "esp32s3", "--firmware", str(flash),
                       "--elf", str(owned_elf), "--psram-size", "8M",
                       "--wifi-ssid", "OpenQuatt-Emulator", "--wifi-password", "emulator-only-pass",
                       "--wifi-fw-ap-password", "openquatt", "--uart1-tcp", f"127.0.0.1:{self.uart_port}",
                       "--net", f"user,restrict=yes,hostfwd=tcp:127.0.0.1:{self.http_port}-:80", "--log-color", "never"]
            if self.profile.loopbacks:
                command += ["--rmt-loopback", self.profile.loopbacks]
            (self.output / "command.json").write_text(json.dumps(command, indent=2) + "\n")
            if self.stop_event.is_set():
                raise InterruptedError("Sessie gestopt tijdens voorbereiden")
            self.process = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, errors="replace", env=dict(os.environ, RUST_LOG="warn"))
            self.reader = threading.Thread(target=self.capture.consume, args=(self.process.stdout,), name="firmware-log", daemon=True)
            self.reader.start()
            self.peer.start(self.uart_port)
            self._stage("OpenQuatt-firmware starten en virtuele wifi instellen")
            self.firmware = self._wait_ready()
            self.controller = Controller(self.http_port, self.stop_event, ot_role=self.profile.peer, local_flow=self.profile.name == "local-flow")
            self._stage("Buitenunits herkennen via Modbus")
            self._wait(lambda: all(not self.fleet.online[i] or self.profiles[i] == "Disabled" or self.telemetry.get(f"hp{i + 1}_online") for i in (0, 1)),
                       "beide virtuele ODU's online", 90)
            self._stage("Controllerinvoer instellen en controleren")
            self.controller.setup_inputs()
            self.controller.apply_environment(self.environment)
            if self.profile.name == "local-flow":
                def local_flow_ready():
                    self.controller.sync_hydraulics(self.fleet.hydraulics())
                    actual = self.controller.flow_peer.flow_lph
                    return (self.flow_telemetry.get("enabled") is True and
                            self.flow_telemetry.get("applied_enabled") is True and
                            self.flow_telemetry.get("active") == (actual > 0) and
                            self.flow_telemetry.get("generation") == self.flow_telemetry.get("requested_generation") and
                            abs(self.flow_telemetry.get("applied_flow_lph", -100) - actual) < .11)
                self._wait(local_flow_ready, "gecommitteerde lokale GPIO-flowbron", 10)
            if self.profile.peer == "thermostat":
                self._wait(lambda: self.ot_telemetry.get("OQ_OT_CONTROLLER", {}).get("received", 0) > 0 and
                           self.ot_telemetry["OQ_OT_CONTROLLER"].get("thermostat_status_valid") is True and
                           self.ot_telemetry["OQ_OT_CONTROLLER"].get("room_fresh") is True and
                           self.ot_telemetry["OQ_OT_CONTROLLER"].get("room_setpoint_fresh") is True,
                           "verse thermostaatframes door de productie-RMT-driver", 40)
            if self.profile.peer == "boiler":
                self._wait(lambda: self.ot_telemetry.get("OQ_OT_CONTROLLER", {}).get("boiler_link") is True and
                           self.ot_telemetry.get("OQ_OT_PEER", {}).get("rx", 0) > 0,
                           "echte duplex-ketellink via RMT", 90)
            self._poll_controller()
            self.capture.set_phase("idle")
            self._stage("Geheugen- en taakmetingen verzamelen")
            self._wait(lambda: len([s for s in self.capture.state()[0] if s["phase"] == "idle"]) >= 3 and
                       len([s for s in self.capture.diagnostics.state()[0] if s["phase"] == "idle"]) >= 3,
                       "complete nulmeting", 15)
            with self.lock:
                self.state = "running"
                self.stage = "OpenQuatt-firmware en buitenunits actief"
            self.capture.set_phase("idle")
            self.log("Simulator actief. De virtuele buitenunits communiceren via de UART1- en Modbus-stack van OpenQuatt.")
            from scripts.simulator.scenarios import ScenarioRunner
            self.scenarios = ScenarioRunner(self)
            self.ready.set()
            last_heartbeat = time.monotonic()
            last_poll = 0.0
            while not self.stop_event.wait(0.2):
                self._check()
                if time.monotonic() - last_poll > 2:
                    self._poll_controller()
                    last_poll = time.monotonic()
                if not self.command_lock.acquire(blocking=False):
                    continue
                try:
                    try:
                        operation, values = self.commands.get_nowait()
                    except queue.Empty:
                        operation = None
                    if operation:
                        self._apply_command(operation, values)
                        self.commands.task_done()
                        last_heartbeat = time.monotonic()
                    elif not self.heartbeat_paused and time.monotonic() - last_heartbeat > 18:
                        self.controller.apply_environment(self.environment, auto_mode=False)
                        last_heartbeat = time.monotonic()
                finally:
                    self.command_lock.release()
        except InterruptedError:
            pass
        except Exception as exc:
            failure = str(exc)
            self.log(failure, "error")
        finally:
            self.stop_event.set()
            if self.scenarios:
                self.scenarios.cancel()
                try:
                    self.scenarios.join()
                except RuntimeError as exc:
                    failure = failure or str(exc)
            if self.peer:
                self.peer.stop.set()
            if self.process:
                if isinstance(self.capture, LabCapture) and self.process.poll() is None and not self.capture.reader_done.is_set():
                    self.capture.finish_at_commit.set()
                    if not self.capture.reader_done.wait(timeout=5):
                        failure = failure or "Geen complete diagnostiekcommit binnen 5s bij afsluiten"
                if self.process.poll() is not None and failure is None:
                    failure = f"Emulator stopte onverwacht ({self.process.returncode})"
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=5)
            if self.peer:
                try:
                    self.peer.close()
                except RuntimeError as exc:
                    failure = failure or str(exc)
            if self.reader:
                self.reader.join(timeout=5)
                if self.reader.is_alive():
                    failure = failure or "Firmwarelog kon niet worden uitgelezen"
            failure = failure or self.error
            with self.lock:
                self.error = failure
                self.state = "failed" if failure else "stopped"
                self.stage = "Simulatiesessie mislukt" if failure else "Gestopt"
            self.ready.set()
            try:
                self.save_report()
            except (OSError, ValueError, RuntimeError) as exc:
                with self.lock:
                    self.error = failure or ("Rapport opslaan mislukt: " + str(exc))
                    self.state = "failed"
                self.log(self.error, "error")

    def _wait_ready(self):
        deadline = time.monotonic() + 120
        provisioned = False
        last_error = "Nog geen HTTP-antwoord"
        while time.monotonic() < deadline:
            self._check()
            try:
                value = emulator.firmware_value(self.http_port)
                if self.capture.state()[2] == 1:
                    return value
            except (OSError, ValueError, emulator.urllib.error.URLError) as exc:
                last_error = str(exc)
            if not provisioned:
                try:
                    data, _ = emulator.request(self.http_port, "/wifisave?ssid=OpenQuatt-Emulator&psk=emulator-only-pass")
                    provisioned = b"Connecting" in data
                except (OSError, emulator.urllib.error.URLError):
                    pass
            self.stop_event.wait(0.2)
        raise RuntimeError("Firmware-API werd niet gereed: " + last_error)

    def _poll_controller(self):
        if self.profile.name == "local-flow":
            self.controller.sync_hydraulics(self.fleet.hydraulics())
        fields = {
            "mode": ("text_sensor", "Control Mode"),
            "room": ("sensor", "Room Temperature (Selected)"),
            "setpoint": ("sensor", "Room Setpoint (Selected)"),
            "outside": ("sensor", "Outside Temperature (Selected)"),
            "flow": ("sensor", "Flow average (Selected)"),
            "lowflow_fault": ("binary_sensor", "Lowflow fault active"),
            "supply": ("sensor", "Water Supply Temp (Selected)"),
            "supply_source": ("text_sensor", "Water Supply Temp Effective Source"),
            "room_source": ("text_sensor", "Room Temperature Effective Source"),
            "enable_source": ("text_sensor", "Heating Enable Effective Source"),
            "request_reason": ("text_sensor", "Request Reason"),
            "requested_level1": ("sensor", "HP1 requested control level"),
            "requested_level2": ("sensor", "HP2 requested control level"),
            "applied_level1": ("sensor", "HP1 applied control level"),
            "applied_level2": ("sensor", "HP2 applied control level"),
            "command_level1": ("select", "HP1 - Set Compressor Level"),
            "command_level2": ("select", "HP2 - Set Compressor Level"),
            "working_mode1": ("sensor", "HP1 - Working Mode"),
            "working_mode2": ("sensor", "HP2 - Working Mode"),
            "frequency1": ("sensor", "HP1 - Compressor frequency"),
            "frequency2": ("sensor", "HP2 - Compressor frequency"),
            "generation1": ("text_sensor", "HP1 - ODU generation variant"),
            "generation2": ("text_sensor", "HP2 - ODU generation variant"),
        }
        if self.profile.peer == "boiler":
            fields.update(boiler_link=("binary_sensor", "OTB - Boiler Link Available"),
                          boiler_water=("sensor", "OTB - Boiler Water Temperature"),
                          boiler_return=("sensor", "OTB - Return Water Temperature"),
                          boiler_transport_errors=("sensor", "OTB - Transport Error Count"),
                          boiler_timeouts=("sensor", "OTB - Response Timeout Count"))
        if self.profile.name == "local-flow":
            fields["controller_flow"] = ("sensor", "Controller Flow")
        values = self.controller.values(fields, optional=True)
        with self.lock:
            self.controller_values = values
            self.observations.append({"uptime_ms": self.telemetry.get("uptime_ms"),
                "values": dict(values), "odus": [{key: unit.get(key) for key in
                    ("address", "requested_mode", "requested_level", "frequency_hz", "flow_lph", "outlet_c")}
                    for unit in self.fleet.snapshot()]})

    def _apply_command(self, operation, values):
        with self.lock:
            self.operation_error = None
        self._stage("Instellingen toepassen en controleren")
        try:
            if operation == "environment":
                self.fleet.set_environment(values)
                self.environment = dict(self.fleet.environment)
                self.controller.apply_environment(self.environment)
            elif operation == "odu":
                index = values["index"]
                changed = "profile" in values and values["profile"] != self.profiles[index]
                if changed:
                    self.controller.select("CM Override", "Force CM0")
                self.fleet.set_odu(values)
                self.profiles = list(self.fleet.profiles)
                self.pending_odu[index].update(values)
                if changed:
                    self.controller.action("button", f"HP{index + 1} - Detect ODU generation", "press")
                    self.log("Buitenunitprofiel gewijzigd; de controller herkent de generatie opnieuw.")
            elif operation == "transport":
                self.peer.configure_faults(**values)
            self._poll_controller()
        except Exception as exc:
            with self.lock:
                self.operation_error = "Instelling niet toegepast: " + str(exc)
            self.log(self.operation_error, "error")
        finally:
            with self.lock:
                self.stage = "OpenQuatt-firmware en buitenunits actief"

    def update_environment(self, values):
        self._accept_operation("environment", values)

    def update_odu(self, values):
        self._accept_operation("odu", values)

    def _accept_operation(self, operation, values):
        if not isinstance(values, dict):
            raise ValueError("Verwacht een JSON-object")
        if operation == "environment":
            environment = checked_environment(values, self.environment)
            if self.profile.peer == "thermostat" and not all(5 <= environment[key] <= 35 for key in ("room_c", "setpoint_c")):
                raise ValueError("OT thermostat room/setpoint must be between 5 and 35 C")
        elif operation == "odu":
            checked_odu(values)
        with self.lock:
            if self.scenarios and self.scenarios.is_running():
                raise RuntimeError("Een test bestuurt de simulator. Breek de test eerst af.")
            if self.state == "running":
                try:
                    self.commands.put_nowait((operation, dict(values)))
                except queue.Full as exc:
                    raise RuntimeError("Er staan al te veel instellingen klaar") from exc
            elif self.state in ("stopped", "failed"):
                # Keep pre-start settings as desired inputs; validate using the same model.
                if self.fleet is None:
                    self.fleet = OduFleet()
                if operation == "environment":
                    self.fleet.set_environment(values)
                    self.environment = dict(self.fleet.environment)
                elif operation == "odu":
                    self.fleet.set_odu(values)
                    self.profiles = list(self.fleet.profiles)
                    self.pending_odu[values["index"]].update(values)
            else:
                raise RuntimeError("Wacht tot de sessie is gestart of gestopt")

    def snapshot(self):
        with self.lock:
            session = {"state": self.state, "backend": "esp-emu", "profile": self.profile.name, "stage": self.stage,
                       "started_at": self.started_at, "error": self.error, "operation_error": self.operation_error}
            controller = {"url": f"http://127.0.0.1:{self.http_port}", "firmware": self.firmware, "opentherm": dict(self.ot_telemetry), "opentherm_events": list(self.ot_events), "local_flow": dict(self.flow_telemetry),
                          **self.telemetry, "values": dict(self.controller_values)}
            logs = list(self.logs)
            runs = list(self.runs)
            environment = dict(self.environment)
            fleet, peer, capture, scenarios = self.fleet, self.peer, self.capture, self.scenarios
        if capture:
            samples, errors, boots = capture.state()
            controller.update(memory=samples[-1] if samples else None, samples=samples[-180:], boot_count=boots)
            if isinstance(capture, LabCapture):
                diagnostic_samples, _ = capture.diagnostics.state()
                controller["diagnostics"] = diagnostic_samples[-1] if diagnostic_samples else None
                controller["diagnostics_summary"] = capture.diagnostics.summary()
        else:
            controller.update(memory=None, samples=[], boot_count=0)
        if fleet:
            with fleet.lock:
                odus = fleet.snapshot()
                hydraulics = fleet.hydraulics()
        else:
            odus = [{"address": index + 1, "profile": p, "online": True} for index, p in enumerate(self.profiles)]
            hydraulics = None
        return {"schema": 1, "contract": CONTRACT, "session": session, "controller": controller,
                "odus": odus, "hydraulics": hydraulics, "environment": environment, "transport": peer.snapshot() if peer else {},
                "load": list(self.load_results),
                "scenarios": scenarios.catalogue() if scenarios else self.scenario_catalogue(),
                "runs": runs, "logs": logs,
                "capabilities": [
                    {"id": "firmware", "label": "OpenQuatt-firmware (Q Duo)", "status": "available", "detail": "De OpenQuatt-regeling draait in een lokale ESP32-S3-emulator."},
                    {"id": "odu", "label": "Virtuele buitenunits", "status": "available", "detail": "Twee buitenunitmodellen communiceren via de UART1- en Modbus-stack van de controller."},
                    {"id": "memory", "label": "Geheugenmetingen", "status": "available", "detail": "Meet vrije interne heap, grootste vrije blok, PSRAM en stackreserves in de emulator."},
                    *([{"id": "local_flow", "label": "Lokale flowsensor", "status": "available", "detail": "GPIO15-pulsen volgen het modeldebiet. Tests controleren de productie-pulse_meter, stopcommando’s en herstel; elektrische validatie blijft HIL-werk."}] if self.profile.name == "local-flow" else []),
                    {"id": "inputs", "label": "Thermostaat en buitentemperatuur", "status": "available", "detail": "Controllerinvoer via de API of een digitale OpenTherm-thermostaat, afhankelijk van het simulatieprofiel."},
                    {"id": "opentherm", "label": "OpenTherm-communicatie", "status": "available" if self.profile.peer else "limited", "detail": "De profielen ot-thermostat en ot-boiler testen digitale communicatie via de OpenQuatt-drivers. Elektrische busvalidatie vereist HIL."},
                    {"id": "wire", "label": "Elektrische I/O en PT1000", "status": "unavailable", "detail": "Elektrische RS485-signalen en lijnsturing zijn niet gevalideerd. De PT1000-ingang is niet gemodelleerd."},
                    {"id": "timers", "label": "Productietimers", "status": "limited", "detail": "Voor tests: 10 s minimale uit-tijd, 10 s vasthoudtijd bij bronverlies en 45 s geldigheid voor API-invoer. De productie-instellingen blijven ongewijzigd."},
                ]}

    def scenario_catalogue(self):
        from scripts.simulator.scenarios import catalogue_for
        return [{**item, "status": "idle"} for item in catalogue_for(self.profile.name)]

    def save_report(self):
        with self.report_lock:
            try:
                return self._save_report()
            except (OSError, ValueError) as exc:
                with self.lock:
                    self.error = "Rapport opslaan mislukt: " + str(exc)
                    self.state = "failed"
                    self.stop_event.set()
                raise

    def _save_report(self):
        if self.output is None:
            return
        if not self.output.is_dir():
            raise OSError("Rundirectory ontbreekt: " + str(self.output))
        if self.source_manifest:
            try:
                provenance.assert_unchanged(PROCESS_SOURCES, provenance.process_sources(),
                    "Simulatorcode gewijzigd tijdens de sessie; herstart de lokale simulatorserver")
            except RuntimeError as exc:
                with self.lock:
                    self.error = str(exc)
                    self.state = "failed"
                    self.stop_event.set()
        state = self.snapshot()
        with self.lock:
            observations = list(self.observations)
        samples, capture_errors, boots = self.capture.state() if self.capture else ([], [], 0)
        failure = self.error or (capture_errors[0] if capture_errors else None)
        failed_case = next((row for row in self.runs if row["status"] == "FAIL"), None)
        if not failure and failed_case:
            failure = "Scenario " + failed_case["id"] + ": " + failed_case.get("detail", "mislukt")
        if self.state in ("stopped", "failed") and not (self.user_stop and not self.runs) and (boots != 1 or len(samples) < 3):
            failure = failure or "Onvoldoende geldige boot-/geheugenwaarnemingen"
        status = "FAIL" if failure or any(r["status"] == "FAIL" for r in self.runs) else "RUNNING" if self.state in ("running", "starting", "stopping") else "PASS" if self.runs and all(r["status"] == "PASS" for r in self.runs) else "CANCELLED" if self.user_stop or any(r["status"] == "CANCELLED" for r in self.runs) else "NOT_RUN"
        report = {"schema": 1, "contract": CONTRACT, "state": state,
                  "samples": samples, "observations": observations, "control_history": list(self.control_history),
                  "ot_history": list(self.ot_history), "ot_events": list(self.ot_events), "flow_history": list(self.flow_history), "modbus_writes": self.peer.write_history() if self.peer else [], "status": status, "error": failure,
                  "provenance": {"emulator_version": emulator.VERSION,
                      "firmware_sha256": self.frozen_firmware_sha256,
                      "elf_sha256": self.frozen_elf_sha256,
                      "emulator_sha256": self.emulator_sha256,
                      "instrumentation_schema": DIAGNOSTICS_PROFILE,
                      "run_configuration": self.run_configuration,
                      "build_manifest": self.build_manifest,
                      "model_source": "OpenQuatt/OpenQuatt-Simulator@e61e8abceaf24cd50f4bed97e80b46d1eb8ba9db",
                      "source_sha256": self.source_manifest,
                      "profile": self.profile.identity, "clock": "firmware telemetry, 1s ticks; model substeps <=100ms"},
                  "coverage": {"odu_modbus_bytes": True, "api_inputs": True, "physical_wire": False,
                      "opentherm": any(r["id"] in ("ot-wire", "ot-boiler") and r["status"] == "PASS" for r in self.runs),
                      "local_flow_gpio": any(r["id"] == "local-flow-wire" and r["status"] == "PASS" for r in self.runs)}}
        if isinstance(self.capture, LabCapture):
            report["capture_window"] = {"stop_requested": self.capture.finish_at_commit.is_set(),
                                        "ended_at_diagnostics_commit": self.capture.ended_at_commit}
            if report["status"] == "PASS" and not self.capture.ended_at_commit:
                report.update(status="FAIL", error="Opname eindigde zonder geselecteerde diagnostiekcommit")
            diagnostic_samples, partial = self.capture.diagnostics.state()
            report["diagnostics"] = {"schema": 1, "profile": DIAGNOSTICS_PROFILE,
                "summary": self.capture.diagnostics.summary(), "samples": diagnostic_samples}
            if report["status"] == "PASS" and (len(diagnostic_samples) < 3 or partial):
                report.update(status="FAIL", error="Onvoldoende complete taak-/allocatiediagnostiek")
            if report["status"] == "PASS" and samples and diagnostic_samples and samples[-1]["uptime_ms"] - diagnostic_samples[-1]["uptime_ms"] > 5000:
                report.update(status="FAIL", error="Taak-/allocatiediagnostiek is verouderd")
            summary = report["diagnostics"]["summary"]
            if summary and summary["min_task_stack_hwm_bytes"] < self.budgets["min_task_stack"]:
                report.update(status="FAIL", error=f"Taakstack {summary['min_task_stack_hwm_bytes']} onder budget {self.budgets['min_task_stack']} bytes")
                with self.lock:
                    self.error = report["error"]
                    self.state = "failed"
                    self.stop_event.set()
        if samples:
            report["memory"] = emulator.summarize(samples)
        report["budgets"] = dict(self.budgets)
        if len(samples) >= 3 or (samples and report["status"] in ("PASS", "FAIL")):
            from types import SimpleNamespace
            try:
                emulator.verify_samples(samples, [], SimpleNamespace(**self.budgets))
            except emulator.TestFailure as exc:
                report["memory_error"] = str(exc)
                report.update(status="FAIL", error=report["error"] or str(exc))
                with self.lock:
                    self.error = report["error"]
                    self.state = "failed"
                    self.stop_event.set()
        if report["status"] == "FAIL" and self.state in ("stopped", "failed"):
            with self.lock:
                self.error = report["error"] or "Een test is mislukt"
                self.state = "failed"
                self.stage = "Test mislukt"
            report["state"]["session"].update(state=self.state, stage=self.stage, error=self.error)
        from scripts.simulator.reports import write_exports
        write_exports(self.output, report)
        temporary = self.output / "report.json.tmp"
        temporary.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
        temporary.replace(self.output / "report.json")
        self.report = report
        return report
