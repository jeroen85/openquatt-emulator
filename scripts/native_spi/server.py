"""Loopback workbench for selecting and testing an unchanged factory image."""
from __future__ import annotations
import argparse
import collections
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
import uuid
from urllib.parse import unquote
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from .factory import factory_info, MAX_IMAGE
from .run import ROOT, LAB
from .network_probe import MODES


class Conflict(ValueError):
    pass


class Manager:
    def __init__(self):
        self.lock = threading.RLock()
        self.selected = None
        self.process = None
        self.job_id = None
        self.request_ids = {}
        self.run_path = None
        self.started_at = None
        self.logs = collections.deque(maxlen=120)
        self.reader = None
        self.stop_requested = False
        self.duration = 90
        self.temperature = 30.0
        self.network = "wifi"
        self.token = secrets.token_urlsafe(32)
        (LAB / "uploads").mkdir(parents=True, exist_ok=True)

    def active(self):
        return self.process is not None and self.process.poll() is None

    def choose(self, data, name):
        with self.lock:
            if self.active():
                raise Conflict("Stop eerst de actieve proef.")
        temporary = LAB / "uploads" / (".upload-" + uuid.uuid4().hex)
        try:
            temporary.write_bytes(data)
            temporary.chmod(0o600)
            info = factory_info(temporary)
            clean = Path(unquote(name).replace("\\", "/")).name[:140]
            if not clean.lower().endswith(".bin"):
                raise ValueError("Kies een factory-bin met de extensie .bin.")
            target = LAB / "uploads" / (info["sha256"] + ".bin")
            with self.lock:
                if self.active():
                    raise Conflict("Er is inmiddels een proef gestart. Stop die eerst.")
                if target.exists():
                    if hashlib.sha256(target.read_bytes()).hexdigest() != info["sha256"]:
                        raise ValueError("De lokale firmwarekopie doorstaat de hashcontrole niet.")
                else:
                    os.replace(temporary, target)
                    target.chmod(0o444)
                self.selected = {**info, "name": clean, "path": target}
                self.process = self.job_id = self.run_path = self.started_at = self.reader = None
                self.stop_requested = False
                self.logs.clear()
                return self.public_input()
        finally:
            temporary.unlink(missing_ok=True)

    def public_input(self):
        return {k: v for k, v in self.selected.items() if k != "path"} if self.selected else None

    def start(self, request_id, duration, temperature, network="wifi"):
        if not isinstance(request_id, str) or not re.fullmatch(r"[a-f0-9-]{36}", request_id):
            raise ValueError("Ongeldige startaanvraag.")
        if type(duration) is not int or duration not in (90, 180, 300, 900):
            raise ValueError("Kies een geldige testduur.")
        if isinstance(temperature, bool) or not isinstance(temperature, (int, float)) or not 0 <= temperature <= 100:
            raise ValueError("Kies een temperatuur tussen 0 en 100 °C.")
        if network not in MODES:
            raise ValueError("Kies een geldige netwerkproef.")
        with self.lock:
            if request_id in self.request_ids:
                return {"job_id": self.request_ids[request_id], "replayed": True}
            if self.active():
                raise Conflict("Er draait al een proef.")
            if not self.selected:
                raise ValueError("Kies eerst een factory-bin.")
            if hashlib.sha256(self.selected["path"].read_bytes()).hexdigest() != self.selected["sha256"]:
                raise ValueError("De firmwarekopie is gewijzigd. Kies het bestand opnieuw.")
            job_id = uuid.uuid4().hex
            command = [sys.executable, "-m", "scripts.native_spi.run", "--firmware", str(self.selected["path"]),
                       "--seconds", str(duration), "--temperature", str(temperature), "--mode", "device",
                       "--name", "ui-" + job_id[:8], "--network", network]
            process = subprocess.Popen(command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       stdin=subprocess.DEVNULL, text=True, errors="replace", start_new_session=True,
                                       env=dict(os.environ, PYTHONUNBUFFERED="1"))
            self.process = process
            self.job_id = job_id
            self.request_ids[request_id] = job_id
            self.run_path = None
            self.started_at = time.monotonic()
            self.logs.clear()
            self.stop_requested = False
            self.duration, self.temperature = duration, temperature
            self.network = network
            self.reader = threading.Thread(target=self.consume, args=(process, job_id), daemon=True)
            self.reader.start()
            return {"job_id": job_id, "replayed": False}

    def consume(self, process, job_id):
        for line in process.stdout:
            clean = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", line).strip()
            with self.lock:
                if self.job_id != job_id:
                    return
                self.logs.append(clean[-1000:])
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if "run" in event:
                    path = Path(event["run"]).resolve()
                    if path.parent == (LAB / "runs").resolve():
                        self.run_path = path

    def stop(self, job_id=None):
        with self.lock:
            if job_id is not None and job_id != self.job_id:
                raise Conflict("Deze proefsessie is inmiddels vervangen.")
            process, current_id = self.process, self.job_id
            if not process or process.poll() is not None:
                return
            first_stop = not self.stop_requested
            self.stop_requested = True
        if first_stop:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=5)
        else:
            process.wait(timeout=21)
        with self.lock:
            reader = self.reader if self.job_id == current_id else None
        if reader:
            reader.join(timeout=2)

    def state(self):
        with self.lock:
            active = self.active()
            details = None
            if self.run_path:
                for name in ("report.json", "live.json"):
                    path = self.run_path / name
                    if path.exists():
                        try:
                            details = json.loads(path.read_text())
                            break
                        except (OSError, ValueError):
                            pass
            phase = "preparing" if active else "stopped" if self.stop_requested else "idle"
            if details:
                if active:
                    phase = details.get("phase", "testing")
                else:
                    phase = "passed" if details.get("status") == "PASS_NATIVE_EXTENSION" else "stopped" if self.stop_requested or details.get("status") == "STOPPED" else "failed"
            elif self.process is not None and not active and not self.stop_requested:
                phase = "failed"
            web_port = details.get("web_port") if active and details else None
            phase_port = 19461 if details and details.get("network", {}).get("phase") == "ethernet" else 19460
            web_url = f"http://127.0.0.1:{web_port}/" if web_port == phase_port else None
            return {"selected": self.public_input(), "job_id": self.job_id, "active": active, "phase": phase,
                    "network": self.network, "example_available": (LAB / "assets/q-duo.factory.bin").is_file(),
                    "elapsed_s": round(time.monotonic() - self.started_at, 1) if self.started_at and active else details.get("elapsed_wall_s", 0) if details else 0,
                    "duration_s": self.duration, "temperature_c": self.temperature, "details": details,
                    "logs": list(self.logs)[-8:], "web_url": web_url}


class Handler(BaseHTTPRequestHandler):
    server_version = "OpenQuattLocalSimulator"

    def log_message(self, *args):
        pass

    def allowed(self, mutate=False):
        origin = f"http://127.0.0.1:{self.server.server_port}"
        if self.headers.get("Host") != f"127.0.0.1:{self.server.server_port}":
            return False
        if self.headers.get("Origin") not in (None, origin):
            return False
        return not mutate or hmac.compare_digest(self.headers.get("X-Simulator-Token", ""), self.server.manager.token)

    def send(self, status, data, content_type="application/json; charset=utf-8"):
        if not isinstance(data, bytes):
            data = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        if not self.allowed():
            self.send(403, {"error": "Alleen deze lokale werkbank is toegestaan."})
            return
        if self.path == "/":
            html = Path(__file__).with_name("ui.html").read_text().replace("__TOKEN__", self.server.manager.token)
            self.send(200, html.encode(), "text/html; charset=utf-8")
        elif self.path in ("/logo.svg", "/favicon.svg"):
            self.send(200, (ROOT / "docs/assets/brand/openquatt-logo-horizontal-light.svg").read_bytes(), "image/svg+xml")
        elif self.path == "/api/state":
            self.send(200, self.server.manager.state())
        elif self.path == "/api/report":
            with self.server.manager.lock:
                path = self.server.manager.run_path
            report = path / "report.json" if path else None
            self.send(200, report.read_bytes()) if report and report.exists() else self.send(404, {"error": "Er is nog geen afgerond rapport."})
        else:
            self.send(404, {"error": "Niet gevonden."})

    def do_POST(self):
        if not self.allowed(mutate=True):
            self.send(403, {"error": "Ongeldige lokale aanvraag. Herlaad de werkbank."})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 <= length <= MAX_IMAGE:
                self.close_connection = True
                self.send(413, {"error": "Het bestand is te groot."})
                return
            self.connection.settimeout(10)
            body = self.rfile.read(length)
            if len(body) != length:
                raise ValueError("De aanvraag is onvolledig ontvangen.")
            if self.path == "/api/upload":
                name = self.headers.get("X-Filename", "firmware.factory.bin")
                result = self.server.manager.choose(body, name)
            elif self.path == "/api/example":
                path = LAB / "assets/q-duo.factory.bin"
                result = self.server.manager.choose(path.read_bytes(), "Q Duo · lokale proefbuild.factory.bin")
            elif self.path == "/api/start":
                request = json.loads(body)
                if not isinstance(request, dict):
                    raise ValueError("De aanvraag moet een JSON-object zijn.")
                result = self.server.manager.start(request.get("request_id"), request.get("duration_s", 90), request.get("temperature_c", 30), request.get("network", "wifi"))
            elif self.path == "/api/stop":
                request = json.loads(body)
                if not isinstance(request, dict):
                    raise ValueError("De aanvraag moet een JSON-object zijn.")
                if not request.get("job_id"):
                    raise ValueError("De te stoppen proefsessie ontbreekt.")
                self.server.manager.stop(request["job_id"])
                result = {"stopped": True}
            else:
                self.send(404, {"error": "Niet gevonden."})
                return
            self.send(200, result)
        except Conflict as error:
            self.send(409, {"error": str(error)})
        except (ValueError, OSError) as error:
            self.send(400, {"error": str(error)})


def main(port=19450):
    manager = Manager()
    with ThreadingHTTPServer(("127.0.0.1", port), Handler) as server:
        server.manager = manager
        def finish(signum, frame):
            threading.Thread(target=server.shutdown, daemon=True).start()
        signal.signal(signal.SIGTERM, finish)
        signal.signal(signal.SIGINT, finish)
        print(f"OpenQuatt simulator: http://127.0.0.1:{port}/", flush=True)
        try:
            server.serve_forever()
        finally:
            manager.stop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=19450)
    main(parser.parse_args().port)
