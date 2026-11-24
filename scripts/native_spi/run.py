"""Run one unchanged Q Duo factory image on an ESP32-S3 emulator."""
from __future__ import annotations

import argparse
from contextlib import ExitStack, contextmanager
import fcntl
import gzip
import hashlib
import http.client
import io
import json
import math
import os
import platform
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from . import model
from .atomic import atomic_json
from .build import ensure, verify_original
from .cosim import CosimPeer
from .cosim_opentherm import OpenTherm
from .scenario_load import ScenarioLoad, regulation_windows
from .cosim_build import ensure_cosim
from .dynamics import Dynamics, wire_evidence
from .factory import factory_info
from .http_response import bounded_response
from .lowflow import Lowflow
from .memory import RECORDING_PATH, SAMPLE_SLACK_MS, lowflow_windows, normal_windows, summarize
from .memory_comparison import compare, read_reference, require_match, runtime_fingerprint, settings
from .network_probe import MODES, NetworkProbe
from .pr_memory import assessment as pr_assessment, markdown as pr_markdown
from .pr_memory import summarize_pr_recording
from .pr_workload import PROBES_STOP_MS, WINDOWS as PR_WINDOWS, PrWebLoad, summarize_workload
from .pt1000 import dynamic_qualification, matches_fixture
from .qualification import FUNCTIONAL_COMPLETION, qualify
from .transport import RtuPeer
from .heating_curve import HeatingCurve
from .odu_scenarios import ResponseFleet, OduResponse, OduTransients
from .https_fixture import TlsFixture
from .https_scenario import HttpsScenario
from .power_house import PowerHouse, CLIMATES
from .defrost_assist import DefrostAssist, ASSIST_CASES, HANDOVER_VARIANTS
from .thermostat_setpoint import ThermostatSetpoint
from .restart import Restart, RESTART_CASES, ota_info
from .export_memory import ExportMeasurement, EXPORT_PATHS
from .safety_scenarios import DEW_CASES, DewGuard, OduLinkLoss, SourceLoss
from .energy_boundary import CASES as ENERGY_CASES, EnergyBoundary, parse_history
from .mqtt_reconnect import LocalBroker, MqttReconnect

SCENARIOS = {"compatibility": 90, "dynamics": 420, "lowflow": 780, "pr-memory": 240,
             "heating-curve": 2400, "odu-response": 2100, "odu-transients": 2100,
             "https": 420, "https-reject": 240, "power-house": 2700,
             "defrost-assist": 1800, "ott-setpoint": 210, "restart": 340,
             "source-loss": 950, "odu-link-loss": 820, "dew-guard": 1100,
             "mqtt-reconnect": 420, "energy-boundary": 900}
DYNAMIC_SCENARIOS = {"dynamics": Dynamics, "lowflow": Lowflow, "heating-curve": HeatingCurve,
                     "odu-response": OduResponse, "odu-transients": OduTransients, "power-house": PowerHouse,
                     "defrost-assist": DefrostAssist, "ott-setpoint": ThermostatSetpoint, "restart": Restart,
                     "source-loss": SourceLoss, "odu-link-loss": OduLinkLoss, "dew-guard": DewGuard,
                     "mqtt-reconnect": MqttReconnect, "energy-boundary": EnergyBoundary}

ROOT = Path(__file__).resolve().parents[2]
LAB = ROOT / ".tmp/native-spi"
PT1000 = "/sensor/" + urllib.parse.quote("Water Supply Temp (PT1000)", safe="")
RECORDER_TIMEOUT_S = 8
RECORDER_FINISH_MARGIN_S = 1
_cancel_requested = False


class ScenarioDeadline(TimeoutError):
    pass


def observe_with_deadline(observe, get, deadline, cancelled):
    def budgeted_get(*args, **kwargs):
        if cancelled() or time.monotonic() >= deadline:
            raise ScenarioDeadline()
        result = get(*args, **kwargs)
        if cancelled() or time.monotonic() >= deadline:
            raise ScenarioDeadline()
        return result
    try:
        observe(budgeted_get)
    except ScenarioDeadline:
        return False
    return True


def sha(path):
    with Path(path).open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


class LocalRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, port):
        super().__init__()
        self.port = port

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urllib.parse.urlsplit(newurl)
        if target.scheme != "http" or target.netloc != f"127.0.0.1:{self.port}":
            raise ValueError("Redirect outside owned emulator origin")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def recorder_response(port, deadline, cancelled):
    return bounded_response(port, RECORDING_PATH, deadline, cancelled)


@contextmanager
def reserve_ports(ports):
    lock_dir = LAB / "port-locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    with ExitStack() as stack:
        for port in sorted(ports.values()):
            lock = stack.enter_context((lock_dir / f"{port}.lock").open("a"))
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ValueError(f"Emulator port {port} is reserved by another run") from error
            with socket.socket() as check:
                check.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                check.bind(("127.0.0.1", port))
        yield


def wait_for_owned_listeners(process, ports):
    lsof = shutil.which("lsof")
    if lsof is None:
        raise ValueError("lsof is nodig om de eigen emulatorpoorten te controleren")
    expected = {f"n127.0.0.1:{port}" for port in ports.values()}
    deadline = time.monotonic() + 8
    while process.poll() is None and time.monotonic() < deadline:
        status = subprocess.run(
            [lsof, "-nP", "-a", "-p", str(process.pid), "-iTCP", "-sTCP:LISTEN", "-Fn"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=2, check=False,
        )
        if expected.issubset(status.stdout.splitlines()) and process.poll() is None:
            return
        time.sleep(0.05)
    raise RuntimeError("The owned emulator did not open all loopback listeners")


def run(firmware, seconds=90, mode="device", base=19460, name="factory", temperature=30.0,
        network="wifi", scenario="compatibility", measure_memory=False, *,
        memory_reference=None, memory_max_drop_bytes=None,
        baseline_firmware_sha256=None, without_overlap=False, https_fixture=None, power_house_climate=None,
        assist_case=None, handover_variant=None, restart_case=None, ota_image=None, ota_python=None,
        memory_export=None, export_min_rows=None, export_probe=False, export_seed=False, dew_case=None,
        energy_case=None, backend="native", ntp_epoch=None):
    if ntp_epoch is not None and (backend != "cosim" or scenario != "compatibility" or network != "ethernet"
                                  or type(ntp_epoch) is not int or not 1700000000 <= ntp_epoch <= 2085976695
                                  or measure_memory or memory_reference is not None):
        raise ValueError("De lokale NTP-klok vereist een Q Duo-rooktest met co-simulatie op Ethernet en een geldige UTC-epoch")
    if backend not in ("native", "cosim"):
        raise ValueError("Ongeldige emulatorbackend")
    if backend == "cosim" and (mode != "device" or (network != "wifi" and scenario not in ("compatibility", "https", "https-reject", "power-house", "heating-curve", "lowflow", "dynamics"))
                               or scenario not in ("compatibility", "pr-memory", "restart", "https", "https-reject", "power-house", "heating-curve", "lowflow", "dynamics")
                               or memory_export is not None
                               or (scenario == "restart" and restart_case != "software")):
        raise ValueError("Co-simulatie ondersteunt de Q Duo-rooktest, pr-memory, softwareherstart, HTTPS en de regelproeven met OpenTherm")
    if scenario in ("mqtt-reconnect", "energy-boundary") and (network != "ethernet" or mode != "device"):
        raise ValueError("Deze proef vereist de lokale Ethernet-fixture in device-modus")
    if scenario == "mqtt-reconnect":
        if memory_reference is not None:
            raise ValueError("mqtt-reconnect rapporteert geheugen rond herstel zonder gespiegelde referentierun")
        measure_memory = True
    if scenario == "energy-boundary":
        energy_case = energy_case or "hour"
        if energy_case not in ENERGY_CASES:
            raise ValueError("energy-boundary vereist een geldige --energy-case")
    elif energy_case is not None:
        raise ValueError("--energy-case hoort bij energy-boundary")
    if scenario == "dew-guard":
        if (dew_case is not None and dew_case not in DEW_CASES) or temperature != 18:
            raise ValueError("dew-guard gebruikt 18 °C PT1000 en een geldige dauwpuntvariant")
    elif dew_case is not None:
        raise ValueError("--dew-case hoort bij dew-guard")
    if memory_export is not None:
        if (scenario != "pr-memory" or memory_export not in EXPORT_PATHS or without_overlap
                or (export_seed and memory_export != "forecast")
                or (export_min_rows is not None and (type(export_min_rows) is not int or export_min_rows < 1))):
            raise ValueError("Een exportmeting vereist pr-memory met overlap en een geldige exportkeuze")
    elif export_min_rows is not None or export_probe or export_seed:
        raise ValueError("Exportopties vereisen --memory-export")
    if scenario == "restart":
        restart_case = restart_case or "hard"
        if restart_case not in RESTART_CASES or (backend == "native" and network != "ethernet"):
            raise ValueError("restart vereist een bekende herstartvariant en Ethernet")
        if restart_case == "ota":
            if ota_image is None or not ota_image.is_file() or ota_python is None or not ota_python.is_file():
                raise ValueError("OTA vereist --ota-image en --ota-python met ESPHome")
        elif ota_image is not None or ota_python is not None:
            raise ValueError("OTA-opties horen bij --restart-case ota")
    elif any(v is not None for v in (restart_case, ota_image, ota_python)):
        raise ValueError("Herstartopties horen bij restart")
    if scenario == "defrost-assist":
        if (assist_case not in ASSIST_CASES or network != "ethernet" or temperature != 34
                or (handover_variant is not None and (assist_case != "handover"
                    or handover_variant not in HANDOVER_VARIANTS))):
            raise ValueError("defrost-assist vereist Ethernet, 34 °C en een passend --assist-case")
    elif assist_case is not None or handover_variant is not None:
        raise ValueError("Ketelproefopties horen bij defrost-assist")
    if power_house_climate is not None and (scenario != "power-house" or power_house_climate not in CLIMATES):
        raise ValueError("--power-house-climate requires power-house and a known climate profile")
    if scenario.startswith("https"):
        if mode != "device" or network != "ethernet" or https_fixture is None or memory_reference is not None:
            raise ValueError("HTTPS requires device mode, Ethernet, --https-fixture and no memory baseline")
        if not (https_fixture / "fixture.json").is_file():
            raise ValueError("HTTPS fixture manifest missing")
        measure_memory = True
    elif https_fixture is not None:
        raise ValueError("--https-fixture belongs to HTTPS scenarios")
    if memory_max_drop_bytes is not None and (
            memory_reference is None or type(memory_max_drop_bytes) is not int or memory_max_drop_bytes < 0):
        raise ValueError("--memory-max-drop-bytes vereist een referentie en een geheel getal van nul of hoger")
    pr_memory = scenario == "pr-memory"
    if pr_memory:
        if mode != "device" or network != "wifi" or seconds < 230:
            raise ValueError("pr-memory vereist device, Wi-Fi en minstens 230 hostseconden")
        if (memory_reference is None) != (baseline_firmware_sha256 is None):
            raise ValueError("pr-memory vereist bij een referentie ook --memory-baseline-firmware-sha256")
        if baseline_firmware_sha256 is not None and not re.fullmatch(r"[0-9a-f]{64}", baseline_firmware_sha256):
            raise ValueError("Ongeldige SHA-256 voor de gekozen baselinefirmware")
        measure_memory = True
    elif baseline_firmware_sha256 is not None or without_overlap:
        raise ValueError("De baselinehash en --without-overlap horen bij pr-memory")
    if memory_reference is not None and not pr_memory:
        measure_memory = True
        require_match(settings(memory_reference["report"]), {
            "scenario": scenario, "mode": mode, "network": network,
            "duration_s": seconds, "fixture_temperature_c": temperature,
        })
    if not 5 <= seconds <= (3600 if scenario in SCENARIOS and SCENARIOS[scenario] > 1800 else 1800):
        raise ValueError("Duration outside the scenario's host time limit")
    if not 0 <= temperature <= 100:
        raise ValueError("Temperature must be 0..100 Celsius")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", name):
        raise ValueError("Invalid local run name")
    if network not in MODES or (network != "wifi" and mode != "device"):
        raise ValueError("Ethernet requires the native device mode")
    if scenario not in SCENARIOS:
        raise ValueError("Invalid test scenario")
    if scenario in DYNAMIC_SCENARIOS and (mode != "device" or network == "failover"):
        raise ValueError("Dynamic scenarios require device mode on a stable Wi-Fi or Ethernet route")
    if measure_memory and (mode != "device" or network == "failover" or scenario not in ("compatibility", "lowflow", "pr-memory", "https", "https-reject", "mqtt-reconnect", "power-house", "heating-curve")):
        raise ValueError("Memory recording is unsupported for this scenario or network route")
    if not 1024 <= base <= 65533:
        raise ValueError("Port base must be between 1024 and 65533")

    if backend == "cosim" and scenario in ("power-house", "heating-curve", "lowflow", "https"):
        measure_memory = True
    info = factory_info(firmware)
    ports = {"http": base}
    if not uses_cosim_modbus(backend, scenario):
        ports["uart1"] = base + 2
    if backend == "cosim":
        ports["cosim"] = base + 3
    if network != "wifi":
        ports["ethernet"] = base + 1
    ota_identity = ota_info(ota_image, info) if ota_image is not None else None
    if restart_case or energy_case:
        ports["control"] = base + (4 if backend == "cosim" else 3)
    if scenario == "mqtt-reconnect":
        ports["mqtt"] = base + 3
    if ota_identity:
        ports["ota"] = base + 4
    if max(ports.values()) > 65535:
        raise ValueError("Poortreeks overschrijdt 65535")
    with reserve_ports(ports):
        return run_owned(firmware, info, seconds, mode, ports, name, temperature, network, scenario, measure_memory,
                         memory_reference, memory_max_drop_bytes, baseline_firmware_sha256, without_overlap,
                         https_fixture=https_fixture, power_house_climate=power_house_climate,
                         assist_case=assist_case, handover_variant=handover_variant, restart_case=restart_case,
                         ota_image=ota_image, ota_identity=ota_identity, ota_python=ota_python,
                         memory_export=memory_export, export_min_rows=export_min_rows, export_probe=export_probe,
                         export_seed=export_seed, dew_case=dew_case, energy_case=energy_case, backend=backend, ntp_epoch=ntp_epoch)


def uses_cosim_modbus(backend, scenario):
    return backend == "cosim" and scenario in ("power-house", "heating-curve")


def run_owned(firmware, info, seconds, mode, ports, name, temperature, network, scenario, measure_memory,
              memory_reference, memory_max_drop_bytes, baseline_firmware_sha256, without_overlap, https_fixture=None, power_house_climate=None,
              assist_case=None, handover_variant=None, restart_case=None, ota_image=None,
              ota_identity=None, ota_python=None, memory_export=None, export_min_rows=None,
              export_probe=False, export_seed=False, dew_case=None, energy_case=None, backend="native", ntp_epoch=None):
    out = LAB / "runs" / f"{name}-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    out.mkdir(parents=True)
    flash = out / "flash.bin"
    shutil.copyfile(firmware, flash)
    if sha(flash) != info["sha256"]:
        raise ValueError("Firmware changed during copy")
    cable = out / "w5500-cable"
    probe = NetworkProbe(network, ports, cable)

    use_cosim = backend == "cosim"
    cosim_modbus = uses_cosim_modbus(backend, scenario)
    if use_cosim:
        executable, odu_library, native_build = ensure_cosim()
        extension = None
    else:
        executable, extension, native_build = ensure()
        odu_library = executable.with_name("odu.dylib")
    if mode == "baseline":
        executable = LAB / "runtime/esp-emu.original"
        verify_original(executable)
        extension = None
    python_sources = {p.name: p.read_bytes() for p in Path(__file__).parent.glob("*.py")}
    report = {
        "schema": 2, "input": info, "mode": mode, "scenario": scenario, "fixture_temperature_c": temperature,
        "duration_s": seconds, "status": "RUNNING", "firmware_elf_used": False,
        "debugger_used": False, "firmware_modified": False, "ports": ports,
        "http": [], "errors": [], "panics": [], "modbus_timing_warnings": [], "setup_complete": False,
        "sources": {name: hashlib.sha256(data).hexdigest() for name,data in python_sources.items()},
        "native_build": native_build, "extension_sha256": sha(extension) if extension else None,
        "executable_sha256": sha(executable), "odu_library_sha256": sha(odu_library), "network": probe.result,
    }
    if use_cosim:
        report["backend"] = backend
    if ntp_epoch is not None:
        report["ntp_fixture"] = {"epoch": ntp_epoch}
    if cosim_modbus or scenario == "odu-link-loss":
        report["modbus_timing_events"] = []
    if scenario == "odu-link-loss":
        report["modbus_link_events"] = []
    if memory_export or scenario not in ("compatibility", "dynamics", "pr-memory"):
        report["source_snapshot"] = "scenario-source"
        source_root = out / report["source_snapshot"]
        source_root.mkdir()
        for name, data in python_sources.items():
            (source_root / name).write_bytes(data)
        # ensure() compiled these captured native bytes into this exact cache.
        for name, expected in native_build["source_sha256"].items():
            source = (odu_library.parent if use_cosim else executable.parent) / name
            if sha(source) != expected:
                raise ValueError("Compiled native source snapshot changed")
            target = source_root / "native" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
    if memory_reference is not None and scenario != "pr-memory":
        require_match(runtime_fingerprint(memory_reference["report"]), runtime_fingerprint(report))
    if measure_memory:
        report["memory"] = {"complete": False, "error": "Geen heapopname ontvangen."}
        if scenario == "compatibility":
            report["memory"]["error"] = "Geen opname met minstens 30 seconden normaal bedrijf na opstarten."
    command = [
        str(executable), "--chip", "esp32s3", "--firmware", str(flash), "--psram-size", "8M",
        "--wifi-ssid", "OpenQuatt-Native-Probe", "--wifi-password", "native-probe-only",
        "--wifi-fw-ap-password", "openquatt",
        "--net", f"user,restrict=yes,hostfwd=tcp:127.0.0.1:{ports['http']}-:80",
        "--timeout", f"{seconds + 15}s", "--log-color", "never",
    ]
    if not cosim_modbus:
        command.extend(("--uart1-tcp", f"127.0.0.1:{ports['uart1']}"))
    if use_cosim:
        command.extend(("--cosim-tcp", f"127.0.0.1:{ports['cosim']}"))
    if restart_case or energy_case:
        command.extend(("--control-tcp", f"127.0.0.1:{ports['control']}", "--save-state"))
    atomic_json(out / "command.json", command)
    env = dict(os.environ, RUST_LOG="warn", ESP_EMU_DISABLE_AUTOUPDATES="1")
    for key in ("OQ_NATIVE_GUEST_CLOCK", "OQ_NATIVE_OT_CONTROL", "DYLD_INSERT_LIBRARIES", "LD_PRELOAD",
                "OQ_NATIVE_SPI_TRACE", "OQ_NATIVE_SPI_MODE", "OQ_W5500_PORT", "OQ_W5500_CABLE",
                "OQ_LOCAL_TLS_PORT", "OQ_LOCAL_EPOCH", "OQ_LOCAL_MQTT_PORT", "OQ_NATIVE_RTD_CONTROL",
                "OQ_NATIVE_OTB_DISCONNECTED", "OQ_NATIVE_OTB_WATER_CONTROL",
                "OQ_NATIVE_OTT_ROOM", "OQ_NATIVE_OTT_SETPOINT", "OQ_NATIVE_OTA_PORT"):
        env.pop(key, None)
    trace = out / "mmio.jsonl"
    if extension:
        env.update(DYLD_INSERT_LIBRARIES=str(extension), OQ_NATIVE_SPI_TRACE=str(trace),
                   OQ_NATIVE_SPI_MODE=mode, OQ_NATIVE_RTD_C=str(temperature))
    if network != "wifi":
        env.update(OQ_W5500_PORT=str(ports["ethernet"]), OQ_W5500_CABLE=str(cable))

    if ota_identity:
        env["OQ_NATIVE_OTA_PORT"] = str(ports["ota"])

    cosim = None
    clock_path = out / "guest-clock.bin"
    if mode == "device" and not use_cosim:
        clock_path.write_bytes(bytes(8))
        env["OQ_NATIVE_GUEST_CLOCK"] = str(clock_path)

    def guest_clock():
        if cosim:
            return cosim.clock_ms
        return struct.unpack("<Q", clock_path.read_bytes())[0] // 1000

    tls = https = None
    if https_fixture:
        identity = json.loads((https_fixture / "fixture.json").read_text())
        if scenario == "https" and identity.get("factory_sha256") != info["sha256"]:
            raise ValueError("HTTPS success test requires the factory bin bound to this test CA fixture")

    fleet = (ResponseFleet if scenario.startswith("odu-") else model.OduFleet)(odu_library)
    model_epoch = time.monotonic()

    def odu_clock():
        return int((time.monotonic() - model_epoch) * 1000)

    rebooting = bool(restart_case or energy_case)
    peer = RtuPeer(fleet, odu_clock if rebooting else guest_clock if mode == "device" else None)
    dynamics = mqtt = None
    if scenario in DYNAMIC_SCENARIOS:
        thermostat = out / "thermostat-heat"
        scenario_type = DYNAMIC_SCENARIOS[scenario]
        if scenario == "power-house":
            dynamics = PowerHouse(thermostat, fleet, power_house_climate or "mild")
        elif scenario == "defrost-assist":
            dynamics = DefrostAssist(thermostat, fleet, assist_case, handover_variant or "demand-drop")
            env.update(OQ_NATIVE_RTD_CONTROL=str(dynamics.rtd),
                       OQ_NATIVE_OTB_WATER_CONTROL=str(dynamics.boiler_water))
            if assist_case != "ot":
                env["OQ_NATIVE_OTB_DISCONNECTED"] = "1"
        elif scenario == "dew-guard":
            dynamics = DewGuard(thermostat, fleet, dew_case or "rise")
        elif scenario == "odu-link-loss":
            dynamics = OduLinkLoss(thermostat, fleet, peer)
        elif scenario == "mqtt-reconnect":
            broker = LocalBroker(ports["mqtt"], guest_clock)
            dynamics = mqtt = MqttReconnect(thermostat, fleet, broker, ports["ethernet"], guest_clock)
            env["OQ_LOCAL_MQTT_PORT"] = str(ports["mqtt"])
        elif energy_case:
            dynamics = EnergyBoundary(thermostat, fleet, energy_case, ports["control"], guest_clock)
            env["OQ_LOCAL_EPOCH"] = str(dynamics.result["epoch"])
        elif restart_case:
            dynamics = Restart(thermostat, fleet, restart_case, ports, guest_clock,
                               ota_image, ota_identity, ota_python)
        else:
            dynamics = scenario_type(thermostat, fleet)
        if scenario == "ott-setpoint":
            env.update(OQ_NATIVE_OTT_SETPOINT=str(dynamics.setpoint), OQ_NATIVE_OTT_ROOM=str(dynamics.room))
        dynamics.set_heat(False)
        env["OQ_NATIVE_OT_CONTROL"] = str(thermostat)
        report["dynamics"] = dynamics.result
    peer.configure_faults(delay_ms=0)
    process = reader = None
    started = time.monotonic()
    deadline = started + seconds
    stop = threading.Event()
    report_lock = threading.RLock()
    export = ExportMeasurement(memory_export, export_min_rows or 1, export_probe, export_seed) if memory_export else None
    pr_load = (PrWebLoad(ports["http"], guest_clock, started, deadline,
                         lambda: _cancel_requested or stop.is_set(), without_overlap, export)
               if scenario == "pr-memory" else None)
    scenario_load = (ScenarioLoad(probe.port, guest_clock, deadline)
                     if use_cosim and measure_memory and scenario in ("power-house", "heating-curve", "lowflow") else None)
    background = []
    pr_end_ms = PR_WINDOWS["after"][1] + SAMPLE_SLACK_MS
    openers = {
        port: urllib.request.build_opener(urllib.request.ProxyHandler({}), LocalRedirect(port))
        for port in {ports["http"], ports.get("ethernet", ports["http"])}
    }

    status_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def error(where, exc):
        with report_lock:
            report["errors"].append(f"{where}: {exc!r}")

    def consume():
        try:
            with (out / "serial.log").open("w") as capture:
                total = 0
                for line in process.stdout:
                    total += len(line)
                    if total > 16 * 1024 * 1024:
                        raise ValueError("Serial capture bound exceeded")
                    capture.write(line)
                    capture.flush()
                    with report_lock:
                        if "Stop waiting for response" in line or "Received unexpected frame" in line:
                            report["modbus_timing_warnings"].append(line.strip())
                            if "modbus_timing_events" in report:
                                report["modbus_timing_events"].append({"line": line.strip(), "guest_ms": guest_clock()})
                        if scenario == "odu-link-loss" and ("HP2 - Modbus OFFLINE" in line or "HP2 - Modbus ONLINE" in line):
                            report["modbus_link_events"].append({"line": line.strip(), "guest_ms": guest_clock()})
                        if "Active connection: WiFi (automatic recovery)" in line:
                            report.setdefault("wifi_recovery_event_s", time.monotonic() - started)
                        if "ETH driver install error" in line or "W5500 driver unavailable" in line:
                            report["ethernet_driver_error"] = True
                        if "setup() finished successfully!" in line:
                            report["setup_complete"] = True
                            report["setup_count"] = report.get("setup_count", 0) + 1
                        if any(marker in line for marker in ("Guru Meditation", "watchdog got triggered", "abort()", "Backtrace:")):
                            report["panics"].append(line.strip())
        except Exception as exc:
            error("serial reader", exc)
            stop.set()

    def publish_live():
        with report_lock:
            snapshot = {key: value for key, value in report.items() if key not in ("http", "sources", "native_build")}
            snapshot = json.loads(json.dumps(snapshot))
        snapshot["http_successes"] = len(report["http"])
        snapshot["elapsed_wall_s"] = round(time.monotonic() - started, 3)
        snapshot["peer"] = report["peer"] if "cleanup" in report else peer.snapshot()
        snapshot["read_units"] = sorted({
            row["unit"] for row in snapshot["peer"]["recent"]
            if row["fc"] == 3 and not row["exception"] and not row["dropped"]
        })
        healthy = not (
            any(snapshot.get(key) for key in ("errors", "panics", "modbus_timing_warnings", "model_errors"))
            or snapshot["peer"]["error"] or snapshot["peer"]["crc_errors"]
        )
        ready = (
            report.get("web_checks", 0) >= 2 and report.get("pt1000_verified")
            and snapshot["read_units"] == [1, 2]
            and (network != "failover" or probe.result["wifi_restored_s"] is not None)
        )
        snapshot["phase"] = "booting"
        if not healthy:
            snapshot["phase"] = "attention"
        elif ready:
            snapshot["phase"] = "ready"
        elif report["setup_complete"]:
            snapshot["phase"] = "testing"
        atomic_json(out / "live.json", snapshot)

    def read_http_body(response, path, port, request_deadline):
        raw = response.read(1024 * 1024 + 1)
        if time.monotonic() >= request_deadline or _cancel_requested:
            return None
        if len(raw) > 1024 * 1024:
            raise ValueError("HTTP wire size bound exceeded")
        data = raw
        if response.headers.get("Content-Encoding") == "gzip" or raw.startswith(b"\x1f\x8b"):
            with gzip.GzipFile(fileobj=io.BytesIO(raw)) as compressed:
                data = compressed.read(4 * 1024 * 1024 + 1)
        if len(data) > 4 * 1024 * 1024:
            raise ValueError("Decoded HTTP size bound exceeded")
        if time.monotonic() >= request_deadline or _cancel_requested:
            return None
        report["http"].append({
            "path": path.split("?")[0], "status": response.status, "wire_bytes": len(raw),
            "bytes": len(data), "wall_s": round(time.monotonic() - started, 3), "port": port,
        })
        return data

    def get(path, timeout=2, *, port=None, method="GET", any_status=False, data=None):
        remaining = deadline - time.monotonic()
        if remaining <= 0 or _cancel_requested:
            return b"" if any_status else None
        port = probe.port if port is None else port
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}" + path, method=method, data=(b"" if data is None else data) if method == "POST" else None,
        )
        try:
            opener = status_opener if any_status else openers[port]
            with opener.open(request, timeout=min(timeout, remaining)) as response:
                if any_status:
                    return b""
                return read_http_body(response, path, port, deadline)
        except urllib.error.HTTPError as exc:
            exc.close()
            return b"" if any_status else None
        except OSError:
            if any_status and (time.monotonic() >= deadline or _cancel_requested):
                return b""
            return None

    verified_asset_ports = set()
    memory_requested = False
    memory_web_ready_ms = None

    def inspect_memory(port):
        nonlocal memory_requested, memory_web_ready_ms
        if memory_requested:
            return
        guest_ms = guest_clock()
        if pr_load:
            if guest_ms < pr_end_ms:
                return
            windows = PR_WINDOWS
        elif https or mqtt:
            windows = (https or mqtt).windows()
            if windows is None or (mqtt and guest_ms < windows["reconnected"][1] + SAMPLE_SLACK_MS):
                return
        elif scenario in ("power-house", "heating-curve"):
            windows = regulation_windows(scenario, dynamics.result)
            if windows is None:
                return
        elif scenario == "lowflow":
            if dynamics.result["stage"] != "complete":
                return
            windows = lowflow_windows(dynamics.result)
            if guest_ms < windows["restart"][1] + SAMPLE_SLACK_MS:
                return
        else:
            if memory_web_ready_ms is None:
                memory_web_ready_ms = guest_ms
            # Leave time for the download before the existing run deadline.
            if deadline - time.monotonic() > 12:
                return
            windows = normal_windows(memory_web_ready_ms, guest_ms)
            if windows is None:
                return
        memory_requested = True
        download_deadline = min(time.monotonic() + RECORDER_TIMEOUT_S, deadline - RECORDER_FINISH_MARGIN_S)
        try:
            with recorder_response(port, download_deadline, lambda: _cancel_requested or stop.is_set()) as response:
                data = read_http_body(response, RECORDING_PATH, port, download_deadline) if response.status == 200 else None
            if data:
                summarize_recording = summarize_pr_recording if pr_load else summarize
                report["memory"] = summarize_recording(json.loads(data), windows, guest_clock())
            else:
                report["memory"]["error"] = "De ingebouwde systeemrecorder gaf geen opname terug."
        except TimeoutError:
            report["memory"]["error"] = "De download van de systeemrecorder is afgebroken na de tijdslimiet."
        except (OSError, EOFError, http.client.HTTPException, KeyError, TypeError, ValueError) as exc:
            report["memory"]["error"] = str(exc)
        finally:
            if mqtt:
                mqtt.result["stage"] = "complete"

    web_setup_count = 0
    pt1000_reads = None

    def inspect_web():
        nonlocal web_setup_count
        if rebooting and report.get("setup_count", 0) != web_setup_count:
            web_setup_count = report["setup_count"]
            verified_asset_ports.clear()
            report["web_assets_verified"] = False
            report["pt1000_verified"] = False
        probe.verify_cut(get)
        port = probe.port
        version = get("/text_sensor/ESPHome%20Version", port=port)
        if not version:
            return
        report["firmware_identity"] = json.loads(version).get("value")
        (out / "version.json").write_bytes(version)
        root = get("/", port=port)
        if not root:
            return
        (out / "web-root.html").write_bytes(root)
        if b"WiFi Networks" in root and not report.get("provisioned"):
            query = urllib.parse.urlencode({"ssid": "OpenQuatt-Native-Probe", "psk": "native-probe-only"})
            receipt = get("/wifisave?" + query, timeout=5, port=port)
            report["provisioned"] = receipt is not None and b"Connecting" in receipt
        if b"<esp-app>" not in root:
            return
        if port not in verified_asset_ports:
            js = get("/0.js", timeout=8, port=port)
            css = get("/0.css", timeout=5, port=port)
            if js and css and len(js) > 1000 and len(css) > 100 and b"OpenQuatt" in js:
                verified_asset_ports.add(port)
                report["web_assets_verified"] = True
                (out / "web-app.js").write_bytes(js)
                (out / "web-app.css").write_bytes(css)
        before_ms = guest_clock() if pt1000_reads is not None else None
        sensor = get(PT1000, port=port)
        if pt1000_reads is not None:
            after_ms = guest_clock()
        value = json.loads(sensor).get("value") if sensor else None
        if pt1000_reads is not None:
            if type(value) is float and not math.isfinite(value):
                value = None
            pt1000_reads.append((before_ms, after_ms, value))
        if sensor:
            report["pt1000_value_c"] = value
            if pt1000_reads is None:
                report["pt1000_verified"] = matches_fixture(value, temperature)
            (out / "pt1000.json").write_bytes(sensor)
        if port in verified_asset_ports and probe.observe(get, started):
            report["web_checks"] = report.get("web_checks", 0) + 1
            report["web_last_ok_s"] = round(time.monotonic() - started, 3)
            report["web_port"] = port
            if ntp_epoch is not None and "guest_epoch_ms" not in report["ntp_fixture"]:
                history = get("/energy/history?hours=0", timeout=2, port=port)
                if history:
                    epoch_ms = parse_history(history)["epoch_ms"]
                    if ntp_epoch * 1000 <= epoch_ms <= (ntp_epoch + time.monotonic() - started + 5) * 1000:
                        report["ntp_fixture"]["guest_epoch_ms"] = epoch_ms
            if dynamics:
                observe_with_deadline(dynamics.observe, get, deadline, lambda: _cancel_requested or stop.is_set())
            if https:
                observe_with_deadline(https.observe, get, deadline, lambda: _cancel_requested or stop.is_set())
            if pr_load and report["web_checks"] >= 2 and pr_load.ready_ms is None:
                if not export or export.prepare(get):
                    pr_load.ready(guest_clock())
            if scenario_load:
                scenario_load.start()
            if measure_memory:
                inspect_memory(port)

    try:
        if mqtt:
            mqtt.start(started, deadline, lambda: _cancel_requested or stop.is_set())
        if https_fixture:
            tls = TlsFixture(https_fixture, guest_clock)
            env.update(OQ_LOCAL_TLS_PORT=str(tls.port), OQ_LOCAL_EPOCH=str(tls.epoch))
            https = HttpsScenario(tls, guest_clock, ports["ethernet"], deadline, scenario == "https-reject")
            report["https"] = https.result
        if use_cosim:
            ethernet = (ports["ethernet"], cable, trace, tls.epoch if tls else ntp_epoch or 0) if network != "wifi" else None
            ot = OpenTherm(thermostat) if dynamics and not restart_case else None
            cosim = CosimPeer(odu_library, temperature, ethernet=ethernet, opentherm=ot,
                              tls_port=tls.port if tls else 0, modbus=peer if cosim_modbus else None)
        process = subprocess.Popen(
            command, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, errors="replace",
        )
        if extension and mode == "device" and env.get("OQ_NATIVE_RTD_CONTROL"):
            pt1000_reads = []
            report["pt1000_verified"] = False
        report["pid"] = process.pid
        atomic_json(out / "owner.json", {"pid": process.pid, "runner_pid": os.getpid(), "owned": True, "ports": ports})
        reader = threading.Thread(target=consume, name="firmware-serial", daemon=True)
        reader.start()
        atomic_json(LAB / "latest-run.json", {"run": str(out), "pid": process.pid})
        print(json.dumps({"run": str(out), "pid": process.pid, "mode": mode}), flush=True)
        other_listeners = {"mqtt", "ethernet"} if use_cosim else {"mqtt"}
        wait_for_owned_listeners(process, {kind: port for kind, port in ports.items() if kind not in other_listeners})
        if not _cancel_requested:
            if cosim:
                cosim.start(ports["cosim"])
            if not cosim_modbus:
                peer.start(ports["uart1"])
        next_http = next_live = 0
        functional_complete_since = None
        while process.poll() is None and time.monotonic() < deadline and not stop.is_set() and not _cancel_requested:
            if cosim and cosim.error:
                raise RuntimeError(cosim.error)
            now = time.monotonic()
            if now >= next_live:
                publish_live()
                next_live = now + 1
            if report["setup_complete"] and now >= next_http:
                next_http = now + 2
                if not pr_load:
                    inspect_web()
                else:
                    probe_start = guest_clock()
                    if probe_start < PROBES_STOP_MS or probe_start >= pr_end_ms:
                        inspect_web()
                        probe_end = guest_clock()
                        if probe_start < PR_WINDOWS["after"][1] and probe_end >= PR_WINDOWS["before"][0]:
                            background.append([probe_start, probe_end])
            if scenario in FUNCTIONAL_COMPLETION:
                if dynamics.result["stage"] == "complete":
                    if functional_complete_since is None:
                        functional_complete_since = guest_clock()
                    elif (guest_clock() - functional_complete_since >= 15000
                          and time.monotonic() < deadline and not stop.is_set() and not _cancel_requested):
                        report["functional_completion"] = True
                        break
            stop.wait(max(0, min(0.2, deadline - time.monotonic())))
    except KeyboardInterrupt:
        report["cancelled"] = True
    except Exception as exc:
        error("run", exc)
    finally:
        # End the workload measurement before teardown.
        workload_ended = time.monotonic()
        if restart_case or mqtt:
            try:
                dynamics.stop_workers()
            except Exception as exc:
                error("scenario workers cleanup", exc)
        report["elapsed_wall_s"] = workload_ended - started
        report["exit_before_cleanup"] = process.poll() if process else None
        report["emulator_stop_requested"] = False
        if https:
            https.stop.set()
        if tls:
            try:
                fixture_stopped = tls.close()
            except Exception as exc:
                fixture_stopped = False
                error("TLS fixture cleanup", exc)
        if https:
            try:
                clients_stopped = https.close()
                https.finalize()
                https.result["checks"]["clients_stopped"] = clients_stopped and fixture_stopped
            except Exception as exc:
                error("HTTPS cleanup", exc)
                https.result["checks"] = {"clients_stopped": False}
        if pr_load:
            try:
                if not pr_load.close():
                    raise RuntimeError("PR-clients did not stop")
                workload = summarize_workload(pr_load.schedule, pr_load.sse, pr_load.gets, background, pr_load.ready_ms)
            except Exception as exc:
                error("web workload cleanup", exc)
                workload = {"valid": False, "issues": ["De afhandeling van de PR-clients is mislukt."],
                            "confirmed_overlap_targets_ms": []}
            report["pr_memory"] = {
                "run_id": out.name, "plan": pr_load.schedule, "workload": workload,
                "reset_path": "new_emulator_process_from_factory_copy",
                "hardware_profile": f"esp32s3-8M-psram-q-duo-{backend}",
            }
        if scenario_load:
            workload_end_ms = guest_clock()
            scenario_load.close()
            if scenario == "lowflow":
                windows = (lowflow_windows(dynamics.result)
                           if dynamics.result["stage"] == "complete" else None)
            else:
                windows = regulation_windows(scenario, dynamics.result)
            report["scenario_load"] = scenario_load.finalize(windows, workload_end_ms)
        peer.stop.set()
        if cosim:
            cosim.stopping.set()
        try:
            if process and report["exit_before_cleanup"] is None:
                process.terminate()
                report["emulator_stop_requested"] = True
                # A pending SPI transaction can block esp-emu's signal handler
                # from reaching shutdown while the co-simulation socket is open.
                if cosim:
                    cosim.disconnect()
                report["cleanup_exit_code"] = process.wait(timeout=5)
        except subprocess.TimeoutExpired as exc:
            error("emulator stop", exc)
            try:
                process.kill()
                process.wait(timeout=5)
            except Exception as kill_error:
                error("emulator kill", kill_error)
        except Exception as exc:
            error("emulator stop", exc)

        if reader:
            reader.join(timeout=5)
        if cosim:
            try:
                if not cosim.close():
                    raise RuntimeError("De SPI-peer stopte niet")
            except Exception as exc:
                error("Co-simulatie", exc)
            report["cosim"] = cosim.evidence()
        try:
            peer.close()
        except Exception as exc:
            error("UART cleanup", exc)
        peer_thread = cosim.thread if cosim_modbus and cosim else peer.thread
        peer_stopped = peer_thread is None or not peer_thread.is_alive()
        if peer_stopped:
            try:
                report["model"] = fleet.snapshot()
                report["odu_uptime_ms"] = fleet.now_ms
                if mode == "device":
                    report["guest_uptime_ms"] = guest_clock()
                    last_progress = peer.clock_last_progress_at
                    if last_progress is not None:
                        report["clock_last_progress_age_s"] = max(0.0, workload_ended - last_progress)
            except Exception as exc:
                error("outside-unit snapshot", exc)
            try:
                fleet.close()
            except Exception as exc:
                error("outside-unit cleanup", exc)
            report["peer"] = peer.snapshot()
            if cosim_modbus:
                report["peer"]["transport"] = "cosim-uart"
            report["writes"] = peer.write_history()
        else:
            report["peer"] = {
                "requests": 0, "responses": 0, "recent": [], "crc_errors": 0,
                "error": "UART worker did not stop",
            }
            report["writes"] = []
        report["cleanup"] = {
            "process_stopped": process is None or process.poll() is not None,
            "reader_stopped": reader is None or not reader.is_alive(),
            "peer_stopped": peer_stopped,
        }
        report["input_hash_unchanged"] = report["flash_file_unchanged"] = False
        try:
            report["input_hash_unchanged"] = sha(firmware) == info["sha256"]
            final_flash = sha(flash)
            report["flash_file_unchanged"] = final_flash == info["sha256"]
            if rebooting:
                report["flash_final_sha256"] = final_flash
                report["flash_reset_path"] = "private_writable_copy"
        except OSError as exc:
            error("final hash", exc)

        report["model_errors"] = []
        ot_events = []
        pt1000_samples = []
        try:
            if trace.is_file():
                with trace.open() as capture:
                    for line in capture:
                        event = json.loads(line)
                        if event["kind"] == "model-error":
                            report["model_errors"].append(event)
                        elif event["kind"] == "opentherm":
                            ot_events.append(event)
                        elif event["kind"] == "pt1000-sample" and pt1000_reads is not None:
                            pt1000_samples.append(event)
                        elif event["kind"] == "ethernet":
                            probe.result["wire"] = event
                        elif event["kind"] == "local-services":
                            report["local_services"] = event
        except (ValueError, OSError, KeyError) as exc:
            error("native trace", exc)

        if pt1000_reads is not None:
            report["pt1000_dynamic"] = dynamic_qualification(
                temperature, pt1000_reads, pt1000_samples, report.get("guest_uptime_ms", 0))
            report["pt1000_verified"] = report["pt1000_dynamic"]["verified"]

        if cosim and cosim.opentherm is not None:
            ot_events.extend(cosim.opentherm.delivered(report.get("guest_uptime_ms", 0)))
            (out / "opentherm.json").write_text(json.dumps(cosim.opentherm.events) + "\n")
        if dynamics:
            dynamics.result["wire"] = wire_evidence(ot_events)
            if hasattr(dynamics, "finalize"):
                try:
                    dynamics.finalize(report)
                except Exception as exc:
                    error("scenario evidence", exc)
                    dynamics.result["checks"] = {"evidence_complete": False}
        if export:
            try:
                report["export_memory"] = export.finalize(pr_load, report["memory"], out / "serial.log")
                report["pr_memory"]["export"] = report["export_memory"]
            except Exception as exc:
                error("export memory evidence", exc)
                report["export_memory"] = {"checks": {"evidence_complete": False}}
        if _cancel_requested:
            report["cancelled"] = True
        report["qualification"], report["failure_reasons"] = qualify(report)
        if pr_load:
            report["pr_memory"]["assessment"] = pr_assessment(
                report, memory_reference, baseline_firmware_sha256, memory_max_drop_bytes,
            )
            report["qualification"], report["failure_reasons"] = qualify(report)
        elif memory_reference is not None:
            report["memory_comparison"] = compare(
                memory_reference, report, memory_max_drop_bytes, all(report["qualification"].values()),
            )
            report["qualification"], report["failure_reasons"] = qualify(report)
        accepted = all(report["qualification"].values())
        pass_status = "PASS_COSIM" if use_cosim else "PASS_NATIVE_EXTENSION"
        report["status"] = pass_status if accepted else "NOT_QUALIFIED"
        if report.get("cancelled"):
            report["status"] = "STOPPED"
        if pr_load:
            (out / "pr-memory.md").write_text(pr_markdown(report["pr_memory"]["assessment"], report["pr_memory"]), encoding="utf-8")
        atomic_json(out / "report.json", report)
        publish_live()
        result = {
            "status": report["status"], "run": str(out), "setup": report["setup_complete"],
            "web": report.get("web_checks", 0), "pt1000": report.get("pt1000_value_c"),
            "requests": report["peer"]["requests"], "responses": report["peer"]["responses"],
            "panics": len(report["panics"]), "errors": report["errors"], "cleanup": report["cleanup"],
        }
        if pr_load:
            result["pr_memory"] = report["pr_memory"]["assessment"]["status"]
        elif memory_reference is not None:
            result["memory_comparison"] = report["memory_comparison"]["status"]
        print(json.dumps(result), flush=True)
    return 0 if accepted else 1


if __name__ == "__main__":
    def terminate_owned(signum, frame):
        global _cancel_requested
        _cancel_requested = True

    signal.signal(signal.SIGTERM, terminate_owned)
    signal.signal(signal.SIGINT, terminate_owned)
    parser = argparse.ArgumentParser()
    parser.add_argument("--firmware", type=Path, required=True)
    parser.add_argument("--seconds", type=int)
    parser.add_argument("--mode", choices=("baseline", "trace", "device"))
    parser.add_argument("--backend", choices=("native", "cosim"),
                        default="cosim" if platform.system() == "Linux" else "native",
                        help="Linux gebruikt standaard cosim, macOS de native uitbreiding")
    parser.add_argument("--port-base", type=int, default=19460)
    parser.add_argument("--name", default="factory")
    parser.add_argument("--temperature", type=float)
    parser.add_argument("--network", choices=MODES)
    parser.add_argument("--scenario", choices=SCENARIOS)
    parser.add_argument("--memory", action="store_true", help="Meet interne heap via de systeemrecorder in ondersteunde scenario's")
    parser.add_argument("--memory-baseline", type=Path, help="Compare the heap run with a qualified report.json")
    parser.add_argument("--memory-max-drop-bytes", type=int, help="Maximum heap drop in bytes. A PR-memory excess requests a repeat run.")
    parser.add_argument("--memory-baseline-firmware-sha256", help="Expected dev factory-bin SHA-256 for pr-memory")
    parser.add_argument("--without-overlap", action="store_true", help="Run the pr-memory negative control")
    parser.add_argument("--https-fixture", type=Path, help="Private local TLS fixture directory")
    parser.add_argument("--power-house-climate", choices=CLIMATES, help="Power House climate profile (default: mild)")
    parser.add_argument("--assist-case", choices=ASSIST_CASES)
    parser.add_argument("--handover-variant", choices=HANDOVER_VARIANTS)
    parser.add_argument("--restart-case", choices=RESTART_CASES)
    parser.add_argument("--ota-image", type=Path)
    parser.add_argument("--ota-python", type=Path, help="Python uit de ESPHome-buildomgeving")
    parser.add_argument("--memory-export", choices=EXPORT_PATHS)
    parser.add_argument("--export-min-rows", type=int)
    parser.add_argument("--export-probe", action="store_true", help="Vereis seriële meetpunten rond en tijdens de download")
    parser.add_argument("--seed-forecast-export", action="store_true",
                        help="Vul de Vooruitblik-export via de aparte HIL-meetbuild")
    parser.add_argument("--dew-case", choices=DEW_CASES)
    parser.add_argument("--energy-case", choices=ENERGY_CASES)
    parser.add_argument("--ntp-epoch", type=int, help="Vaste starttijd voor de lokale NTP-fixture op co-simulatie-Ethernet")
    args = parser.parse_args()
    try:
        reference = read_reference(args.memory_baseline) if args.memory_baseline is not None else None
        defaults = settings(reference["report"]) if reference else {}
        scenario = args.scenario or defaults.get("scenario", "compatibility")
        default_seconds = SCENARIOS
        restart_seconds = {"hard": 340, "software": 340, "running": 1100, "ota": 440}
        default_duration = (1400 if scenario == "dew-guard" and args.dew_case == "loss"
                            else restart_seconds.get(args.restart_case, default_seconds[scenario]))
        seconds = args.seconds if args.seconds is not None else defaults.get("duration_s", default_duration)
        scenario_temperature = {"defrost-assist": 34.0, "dew-guard": 18.0}.get(scenario, 30.0)
        raise SystemExit(run(
            args.firmware, seconds, args.mode or defaults.get("mode", "device"), args.port_base, args.name,
            args.temperature if args.temperature is not None else defaults.get("fixture_temperature_c", scenario_temperature),
            args.network or defaults.get("network", "wifi"), scenario, args.memory,
            memory_reference=reference, memory_max_drop_bytes=args.memory_max_drop_bytes,
            baseline_firmware_sha256=args.memory_baseline_firmware_sha256, without_overlap=args.without_overlap,
            https_fixture=args.https_fixture,
            power_house_climate=args.power_house_climate,
            assist_case=args.assist_case, handover_variant=args.handover_variant,
            restart_case=args.restart_case, ota_image=args.ota_image, ota_python=args.ota_python,
            memory_export=args.memory_export, export_min_rows=args.export_min_rows, export_probe=args.export_probe,
            export_seed=args.seed_forecast_export, dew_case=args.dew_case, energy_case=args.energy_case, backend=args.backend,
            ntp_epoch=args.ntp_epoch,
        ))
    except ValueError as exc:
        parser.error(str(exc))
