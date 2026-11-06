#!/usr/bin/env python3
"""OpenQuatt Simulator: lokale testomgeving voor OpenQuatt-firmware."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.emulator import run as emulator
from scripts.simulator.runtime import BINARY, BUILD, CONFIG, Session
from scripts.simulator.scenarios import CATALOGUE, catalogue_for
from scripts.simulator import provenance
from scripts.simulator.profiles import LAB_PROFILES, get_profile

WEB = Path(__file__).parent / "web"


def build_lab(esphome: str, install=False, lab_profile="duo"):
    spec = get_profile(lab_profile)
    config, build = ROOT / spec.config, ROOT / spec.build
    if install and not BINARY.is_file():
        emulator.install_emulator(BINARY)
    required = (ROOT / ".github/requirements-esphome.txt").read_text().strip().split("==")[1]
    actual = subprocess.check_output([esphome, "version"], text=True)
    if required not in actual:
        raise RuntimeError(f"Gebruik ESPHome {required}, gevonden: {actual.strip()}")
    output = ROOT / (".tmp/simulator/build.log" if lab_profile == "duo" else f".tmp/simulator/build-{lab_profile}.log")
    output.parent.mkdir(parents=True, exist_ok=True)
    environment = dict(os.environ, ESPHOME_COMPILE_PROCESS_LIMIT="2", CMAKE_BUILD_PARALLEL_LEVEL="2")
    before = None
    with output.open("w") as log:
        for command in (["npm", "run", "build:web"], [esphome, "compile", str(config)]):
            if command[0] == esphome:
                before = provenance.firmware_sources(profile=lab_profile)
            print("Bouwen: " + " ".join(command) + " (maximaal 2 compilerjobs)", flush=True)
            result = subprocess.run(command, cwd=ROOT, env=environment, stdout=log, stderr=subprocess.STDOUT)
            if result.returncode:
                raise RuntimeError("Build mislukt; zie " + str(output))
    provenance.write_build_manifest(build, before, esphome_version=required, profile=lab_profile)
    print("Labfirmware gebouwd en bronmanifest geverifieerd", flush=True)


class LabServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, port, session):
        super().__init__(("127.0.0.1", port), Handler)
        self.session = session
        port = self.server_address[1]
        self.origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
        self.hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}


class Handler(BaseHTTPRequestHandler):
    server_version = "OpenQuattLab/1"

    def log_message(self, format, *args):
        pass

    def _send(self, status, data, kind="application/json; charset=utf-8"):
        if not isinstance(data, bytes):
            data = json.dumps(data, ensure_ascii=False, allow_nan=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers()
        self.wfile.write(data)

    def _local(self):
        if self.headers.get("Host") not in self.server.hosts:
            self._send(403, {"error": "Deze werkbank is uitsluitend lokaal bereikbaar"})
            return False
        origin = self.headers.get("Origin")
        if origin and origin not in self.server.origins:
            self._send(403, {"error": "Cross-origin toegang geweigerd"})
            return False
        return True

    def do_GET(self):
        if not self._local():
            return
        path = urlsplit(self.path).path
        if path == "/api/state":
            self._send(200, self.server.session.snapshot())
        elif path == "/api/report":
            try:
                report = self.server.session.save_report()
                self._send(200, report or {"status": "NOT_RUN", "detail": "Start eerst de simulator"})
            except (OSError, ValueError) as exc:
                self._send(500, {"status": "FAIL", "error": "Rapport opslaan mislukt: " + str(exc)})
        elif path in ("/api/memory.csv", "/api/tasks.csv"):
            try:
                report = self.server.session.save_report()
                if report is None:
                    self._send(404, {"error": "Start eerst de simulator"})
                    return
                from scripts.simulator.reports import exports, task_csv
                data = exports(report)[1] if path == "/api/memory.csv" else task_csv(report)
                self._send(200, data.encode(), "text/csv; charset=utf-8")
            except (OSError, ValueError) as exc:
                self._send(500, {"status": "FAIL", "error": "Rapport opslaan mislukt: " + str(exc)})
        elif path == "/openquatt-logo.svg":
            self._send(200, (ROOT / "docs/assets/brand/openquatt-logo-horizontal-dark.svg").read_bytes(), "image/svg+xml")
        elif path in ("/", "/app.js", "/style.css"):
            name = "index.html" if path == "/" else path[1:]
            kind = {"index.html": "text/html; charset=utf-8", "app.js": "text/javascript; charset=utf-8", "style.css": "text/css; charset=utf-8"}[name]
            self._send(200, (WEB / name).read_bytes(), kind)
        else:
            self._send(404, {"error": "Niet gevonden"})

    def do_POST(self):
        if not self._local():
            return
        if self.headers.get("X-OpenQuatt-Lab") != "1" or self.headers.get_content_type() != "application/json":
            self._send(403, {"error": "Lokale JSON-bediening vereist"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 16384 or self.headers.get("Transfer-Encoding"):
                raise ValueError("Ongeldige of te grote JSON-invoer")
            self.connection.settimeout(5)
            values = json.loads(self.rfile.read(length))
            if not isinstance(values, dict):
                raise ValueError("Verwacht een JSON-object")
            session = self.server.session
            path = urlsplit(self.path).path
            if path == "/api/session/start":
                if values:
                    raise ValueError("Start accepteert geen procesargumenten")
                session.start()
            elif path == "/api/session/stop":
                session.stop(wait=False)
            elif path == "/api/environment":
                session.update_environment(values)
            elif path == "/api/odu":
                session.update_odu(values)
            elif path == "/api/scenarios/run":
                with session.lock:
                    if session.commands.unfinished_tasks:
                        raise RuntimeError("Wacht tot de instellingen zijn toegepast")
                    if not session.scenarios:
                        raise RuntimeError("Start eerst de simulator")
                    session.scenarios.start(values.get("id"))
            elif path == "/api/scenarios/stop":
                if session.scenarios:
                    session.scenarios.cancel()
            else:
                self._send(404, {"error": "Onbekende actie"})
                return
            self._send(202, {"accepted": True})
        except (ValueError, TypeError, KeyError) as exc:
            self._send(400, {"error": str(exc)})
        except RuntimeError as exc:
            self._send(409, {"error": str(exc)})


def acquire_lock():
    path = ROOT / ".tmp/simulator/lab.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise RuntimeError("Er is al een lokale OpenQuatt-simulatiesessie actief") from exc
    return handle


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command")
    for command in ("build", "serve", "test", "doctor"):
        sub = commands.add_parser(command)
        sub.add_argument("--esphome", default=str(ROOT / ".venv/bin/esphome") if (ROOT / ".venv/bin/esphome").exists() else "esphome")
        sub.add_argument("--lab-profile", choices=sorted(LAB_PROFILES) if command in ("build", "doctor") else [key for key,value in LAB_PROFILES.items() if value.backend == "esp-emu"], default="duo")
        sub.add_argument("--install", action="store_true", help="Download de vastgepinde lokale emulator indien nodig")
        if command in ("serve", "test"):
            sub.add_argument("--build", action="store_true")
            sub.add_argument("--http-port", type=int, default=18080)
            sub.add_argument("--uart-port", type=int, default=18082)
        if command == "serve":
            sub.add_argument("--port", type=int, default=18600)
        if command == "test":
            sub.add_argument("--inject-allocation-failure", action="store_true", help="Negatieve test: één te grote interne allocatie; verwacht FAIL/exit1")
            sub.add_argument("--scenario", default="all", help="Scenario-id of komma-gescheiden lijst")
            sub.add_argument("--profiles", default="V2 old,V2 new")
            sub.add_argument("--max-seconds", type=float, default=1800)
            sub.add_argument("--min-internal-free", type=int, default=32768)
            sub.add_argument("--min-largest-block", type=int, default=32768)
            sub.add_argument("--min-main-stack", type=int, default=2048)
            sub.add_argument("--min-task-stack", type=int, default=256)
            sub.add_argument("--output", type=Path, default=ROOT / ".tmp/simulator/suites" / time.strftime("suite-%Y%m%d-%H%M%S"))
    for forwarded in ("compare", "io-probe", "io-peripherals", "hardware-memory"):
        sub = commands.add_parser(forwarded, add_help=False)
        sub.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.command is None:
        return parser.parse_args(["serve"])
    for name in ("http_port", "uart_port", "port"):
        if hasattr(args, name) and not 1024 <= getattr(args, name) <= 65535:
            parser.error("Poorten moeten tussen 1024 en 65535 liggen")
    return args


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "compare":
        from scripts.simulator.compare import main as compare_main
        return compare_main(argv[1:])
    if argv and argv[0] == "hardware-memory":
        from scripts.simulator.hardware import main as hardware_main
        return hardware_main(argv[1:])
    if argv and argv[0] in ("io-probe", "io-peripherals"):
        if argv[0] == "io-probe":
            from scripts.simulator.io.run_rmt_probe import main as probe_main
        else:
            from scripts.simulator.io.run_io_probe import main as probe_main
        if any(flag in argv[1:] for flag in ("-h", "--help")):
            return probe_main(argv[1:])
        try:
            handle = acquire_lock()
        except (OSError, RuntimeError) as exc:
            print("FAIL: " + str(exc), file=sys.stderr)
            return 1
        try:
            return probe_main(argv[1:])
        finally:
            handle.close()
    args = arguments(argv)
    lock = None
    session = None
    server = None
    exit_code = 0
    try:
        lock = acquire_lock()
        if args.command == "build" or getattr(args, "build", False):
            build_lab(args.esphome, args.install, args.lab_profile)
        elif args.install and not BINARY.is_file():
            emulator.install_emulator(BINARY)
        if args.command == "build":
            return 0
        if args.command == "doctor":
            selected_profile = get_profile(args.lab_profile)
            selected_build = ROOT / selected_profile.build
            checks = {"firmware": (selected_build / "firmware.factory.bin").is_file()}
            if selected_profile.backend == "esp-emu":
                checks.update(emulator=BINARY.is_file(), native_model=(ROOT / "scripts/simulator/native/odu_bridge.cpp").is_file(),
                              web_ui=(WEB / "index.html").is_file())
            try:
                provenance.verified_build(selected_build, profile=args.lab_profile)
                checks["firmware_current"] = True
            except (OSError, ValueError, RuntimeError):
                checks["firmware_current"] = False
            print(json.dumps(checks, indent=2))
            return 0 if all(checks.values()) else 1
        session = Session(http_port=args.http_port, uart_port=args.uart_port, lab_profile=args.lab_profile,
                          output=args.output / "sessions" if args.command == "test" else None)
        if args.command == "serve":
            if args.port in (args.http_port, args.uart_port):
                raise ValueError("Werkbank, firmware en UART moeten verschillende poorten gebruiken")
            server = LabServer(args.port, session)
            print(f"OpenQuatt Simulator: http://127.0.0.1:{args.port}", flush=True)
            print("Volledig lokaal. Klik Start om de echte firmware te draaien. Ctrl-C sluit alles af.", flush=True)
            server.serve_forever(poll_interval=0.3)
        else:
            from scripts.simulator.model import PROFILES
            profiles = args.profiles.split(",")
            if len(profiles) != 2 or any(profile not in PROFILES or profile == "Disabled" for profile in profiles):
                raise ValueError("Kies twee ingeschakelde ODU-profielen")
            session.profiles = profiles
            for name in ("min_internal_free", "min_largest_block", "min_main_stack", "min_task_stack"):
                if getattr(args, name) < 0:
                    raise ValueError("Geheugengrenzen moeten niet-negatief zijn")
                session.budgets[name] = getattr(args, name)
            catalogue = catalogue_for(args.lab_profile)
            chosen = [item["id"] for item in catalogue] if args.scenario == "all" else args.scenario.split(",")
            if not chosen or any(item not in {s["id"] for s in catalogue} for item in chosen):
                raise ValueError("Onbekend testscenario")
            if not 60 <= args.max_seconds <= 86400:
                raise ValueError("max-seconds moet tussen 60 en 86400 liggen")
            deadline = time.monotonic() + args.max_seconds
            session.start()
            previous = None
            while not session.ready.wait(0.5):
                stage = session.snapshot()["session"]["stage"]
                if stage != previous:
                    print(stage, flush=True)
                    previous = stage
                if time.monotonic() > deadline:
                    raise RuntimeError("Suite-timeout tijdens opstarten")
            if session.state != "running":
                raise RuntimeError(session.error or "Lab kon niet starten")
            print("Lab actief; firmware + virtuele ODU's gekoppeld", flush=True)
            # ready and scenario runner are published together by the startup worker.
            while session.scenarios is None:
                if time.monotonic() > deadline:
                    raise RuntimeError("Scenario runner werd niet gereed")
                time.sleep(0.05)
            if args.inject_allocation_failure:
                print("NEGATIEVE TEST: echte interne allocatiefout injecteren; verwacht FAIL", flush=True)
                session.controller.action("button", "Lab Inject Allocation Failure", "press")
            for scenario in chosen:
                print("RUN " + scenario, flush=True)
                session.scenarios.start(scenario)
                while session.scenarios.is_running():
                    if time.monotonic() > deadline:
                        session.scenarios.cancel()
                        raise RuntimeError("Suite-timeout")
                    time.sleep(0.3)
                result = list(session.runs)[-1]
                print(result["status"] + " " + scenario + ": " + result["detail"], flush=True)
                if result["status"] != "PASS":
                    raise RuntimeError("Scenario faalde: " + scenario)
    except KeyboardInterrupt:
        exit_code = 130
    except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as exc:
        print("FAIL: " + str(exc), file=sys.stderr, flush=True)
        exit_code = 1
    finally:
        if server:
            server.server_close()
        if session:
            session.stop()
            if session.error or (session.report and session.report["status"] == "FAIL"):
                exit_code = 1
            if args.command == "test":
                final_status = session.report["status"] if session.report else "FAIL" if session.error else "CANCELLED"
                detail = session.error or (session.report or {}).get("error")
                print("Eindresultaat: " + final_status + (": " + detail if detail else ""), flush=True)
            if session.output:
                print("Rapport: " + str(session.output / "report.json"), flush=True)
        if lock:
            lock.close()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
