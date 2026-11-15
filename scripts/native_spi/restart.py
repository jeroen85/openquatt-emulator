"""Restart and OTA evidence on an owned emulator and its private flash."""
import hashlib
import http.client
import re
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlencode

from .dynamics import Dynamics, entity, near, value
from .factory import MAX_IMAGE, verify_image
from .http_response import bounded_response
from .lowflow import command_value

RESTART_CASES = ("hard", "software", "running", "ota")
SETTING = "Flow Setpoint"
PERSIST_MS = 65000
OTA_CLIENT = (
    "import logging, sys; from pathlib import Path; from esphome.espota2 import run_ota; "
    "logging.basicConfig(level=logging.INFO); "
    "raise SystemExit(run_ota('127.0.0.1', int(sys.argv[1]), None, Path(sys.argv[2]), "
    "allow_plaintext_upload=True)[0])"
)


def ota_info(path, factory):
    app = next((p for p in factory["partitions"] if p["type"] == 0 and p["subtype"] == 0x11), None)
    if not app:
        raise ValueError("Factory-bin heeft geen tweede OTA-partitie")
    with path.open("rb") as source:
        data = source.read(min(MAX_IMAGE, app["size"]) + 1)
    if len(data) > app["size"] or verify_image(data, 0, len(data))["end"] != len(data):
        raise ValueError("Ongeldige ESP32-S3 OTA-image")
    return {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data), "offset": app["offset"]}


def odu_clock_continuous(requested, reboot, last_progress_age_s):
    elapsed = reboot["host_ms"] - requested["host_ms"]
    odu_elapsed = reboot["odu_ms"] - requested["odu_ms"]
    return (elapsed > 0 and odu_elapsed > 0 and abs(elapsed - odu_elapsed) <= 1000
            and 0 <= last_progress_age_s <= 10)


def reset_control(port):
    with socket.create_connection(("127.0.0.1", port), timeout=2) as connection:
        connection.sendall(b"reset\n")
        with connection.makefile("rb") as stream:
            if stream.readline(64).strip() != b"ok":
                raise RuntimeError("Emulator bevestigde de harde reset niet")


class Restart(Dynamics):
    def __init__(self, thermostat, fleet, case, ports, guest_clock, image, identity, ota_python):
        super().__init__(thermostat, fleet)
        self.case = case
        self.host_epoch = time.monotonic()
        self.ports = ports
        self.clock = guest_clock
        self.out = thermostat.parent
        self.image = None
        self.identity = identity
        self.ota_python = ota_python
        self.ota = None
        self.http_worker = None
        self.http_stop = threading.Event()
        self.http_lock = threading.Lock()
        self.http_requests = []
        self.handoff_stop = threading.Event()
        self.handoff_worker = None
        self.handoff_samples = []
        self.result.update(case=case, stage="configure", ota_image=identity,
                           odu_clock_source="host_monotonic")
        if image:
            self.image = self.out / "update.ota.bin"
            shutil.copyfile(image, self.image)
            if hashlib.sha256(self.image.read_bytes()).hexdigest() != identity["sha256"]:
                raise ValueError("OTA-image veranderde tijdens kopiëren")

    def http_load(self):
        while not self.http_stop.is_set():
            start = time.monotonic()
            started_during_ota = self.ota.poll() is None
            ok = False
            try:
                with bounded_response(self.ports["ethernet"], "/energy/history", start + 2,
                                      self.http_stop.is_set) as response:
                    body = response.read(1024 * 1024 + 1)
                    length = response.getheader("Content-Length")
                    complete = (response.status == 200 and 0 < len(body) <= 1024 * 1024
                                and (length is None or len(body) == int(length)))
                ok = complete
            except (OSError, ValueError, http.client.HTTPException):
                pass
            with self.http_lock:
                self.http_requests.append((start, time.monotonic(), ok, started_during_ota
                                           and self.ota.poll() is None))
            self.http_stop.wait(0.05)

    def sample_running_units(self):
        while not self.handoff_stop.is_set():
            with self.fleet.lock:
                self.handoff_samples.append({
                    "odu_ms": self.fleet.now_ms,
                    "frequency_hz": [unit["frequency_hz"] for unit in self.fleet.snapshot()],
                })
            self.handoff_stop.wait(0.15)

    def trigger(self, get):
        self.requested_at = time.monotonic()
        self.result["requested"] = {"controller_ms": self.clock(), "odu_ms": self.fleet.now_ms,
                                    "host_ms": round((self.requested_at - self.host_epoch) * 1000)}
        self.result["stage"] = "restart_requested"
        if self.case == "hard":
            reset_control(self.ports["control"])
            self.result["command_ack"] = True
        elif self.case == "ota":
            with (self.out / "ota.log").open("wb") as log:
                self.ota = subprocess.Popen(
                    [str(self.ota_python), "-I", "-u", "-c", OTA_CLIENT, str(self.ports["ota"]), str(self.image)],
                    stdout=log, stderr=subprocess.STDOUT,
                )
            self.http_worker = threading.Thread(target=self.http_load, name="restart-http", daemon=True)
            self.http_worker.start()
        else:
            if self.case == "running":
                self.handoff_worker = threading.Thread(target=self.sample_running_units,
                                                       name="restart-odu-samples", daemon=True)
                self.handoff_worker.start()
            self.result["command_ack"] = get("/button/Restart/press", timeout=2, method="POST") is not None

    def observe(self, get):
        stage = self.result["stage"]
        if stage == "complete":
            return
        if self.case == "running" and stage == "configure" and not self.configure(get):
            return
        setting = value(get, "number", SETTING)
        if setting is None:
            return
        if stage == "configure":
            expected = self.result.setdefault("setting", 820 if near(float(setting), 760) else 760)
            if not near(float(setting), expected):
                get(entity("number", SETTING) + "/set?" + urlencode({"value": expected}),
                    timeout=2, method="POST")
                return
            self.result["setting_confirmed_ms"] = self.clock()
            if self.case == "running":
                self.set_heat(True)
            self.result["stage"] = "wait_before_restart"
        elif stage == "wait_before_restart":
            if not near(float(setting), self.result["setting"]):
                raise RuntimeError("Testinstelling veranderde vóór herstart")
            if self.clock() - self.result["setting_confirmed_ms"] < PERSIST_MS:
                return
            if self.case == "running":
                sample = self.sample(get)
                if not self.has_flow(sample) or not all(self.heating(sample, hp) for hp in (0, 1)):
                    return
                self.result["running_before"] = sample
            self.trigger(get)
        elif stage == "restart_requested":
            if self.ota and (code := self.ota.poll()) is not None and code != 0:
                raise RuntimeError(f"Lokale OTA mislukte ({code}). Zie ota.log")
            if 1000 < self.clock() < self.result["requested"]["controller_ms"] - 30000:
                self.recovered_at = time.monotonic()
                self.result["boot_observed"] = {
                    "controller_ms": self.clock(), "odu_ms": self.fleet.now_ms,
                    "setting": float(setting), "host_ms": round((self.recovered_at - self.host_epoch) * 1000),
                }
                self.http_stop.set()
                self.result["stage"] = "verify_boot"
        elif stage == "verify_boot":
            if not near(float(setting), self.result["setting"]):
                raise RuntimeError("Testinstelling ontbreekt na herstart")
            if self.clock() - self.result["boot_observed"]["controller_ms"] < 20000:
                return
            if self.case == "running":
                sample = self.sample(get)
                if not self.has_flow(sample) or not all(self.heating(sample, hp) for hp in (0, 1)):
                    return
                self.result["running_after"] = sample
                self.handoff_stop.set()
            self.result["stage"] = "complete"

    def stop_workers(self):
        self.http_stop.set()
        self.handoff_stop.set()
        if self.ota and self.ota.poll() is None:
            self.ota.terminate()
            try:
                self.ota.wait(3)
            except subprocess.TimeoutExpired:
                self.ota.kill()
                self.ota.wait(3)
        for worker in (self.handoff_worker, self.http_worker):
            if worker:
                worker.join(3)
                if worker.is_alive():
                    raise RuntimeError(f"Herstarttaak stopte niet: {worker.name}")

    def finalize(self, report):
        requested = self.result.get("requested", {})
        boot = self.result.get("boot_observed", {})
        transcript = (self.out / "serial.log").read_text(errors="replace")
        app_offsets = [int(offset, 16) for offset in re.findall(r"Loaded app from partition at offset (0x[0-9a-f]+)", transcript)]
        self.result["boot_partitions"] = app_offsets
        checks = {
            "restart_confirmed": bool(requested and boot and report.get("setup_count") == 2
                                      and self.result["stage"] == "complete"),
            "settings_persisted": near(boot.get("setting"), self.result.get("setting", 0)),
            "web_after_restart": bool(boot and report.get("web_assets_verified") is True
                                      and report.get("pt1000_verified") is True),
            "odu_clock_continuous": bool(requested and boot and odu_clock_continuous(
                requested, boot, report.get("clock_last_progress_age_s", float("inf")))),
        }
        if self.case == "ota":
            ota_written = False
            if self.identity and app_offsets == [report["input"]["application_offset"], self.identity["offset"]]:
                with (self.out / "flash.bin").open("rb") as flash:
                    flash.seek(self.identity["offset"])
                    written = flash.read(self.identity["bytes"])
                ota_written = hashlib.sha256(written).hexdigest() == self.identity["sha256"]
            checks["ota_receipt_and_image"] = self.ota is not None and self.ota.returncode == 0 and ota_written
            if boot:
                with self.http_lock:
                    requests = list(self.http_requests)
                self.result["http_during_update"] = [
                    {"start_s": round(a - self.requested_at, 4), "end_s": round(b - self.requested_at, 4),
                     "ok": ok, "during_ota_session": during}
                    for a, b, ok, during in requests if a < self.recovered_at and b >= self.requested_at
                ]
            checks["http_during_update"] = any(
                row["ok"] and row["during_ota_session"] for row in self.result.get("http_during_update", []))
        else:
            checks["restart_command_ack"] = self.result.get("command_ack") is True
            checks["same_app"] = app_offsets == [report["input"]["application_offset"]] * 2
        if self.case == "running":
            before, after = self.result.get("running_before"), self.result.get("running_after")
            stops = [row for row in report.get("writes", [])
                     if requested.get("odu_ms", float("inf")) <= row["at_ms"] <=
                     (after or {}).get("guest_ms", float("inf"))
                     and (command_value(row, 1999) == 0 or command_value(row, 3999) == 0)]
            self.result["stop_commands_during_handoff"] = stops
            samples = self.handoff_samples
            self.result["odu_handoff_samples"] = samples
            checks["odu_sampled_running_during_restart"] = bool(before and after and len(samples) > 2
                and samples[0]["odu_ms"] - requested["odu_ms"] <= 1000
                and abs(after["guest_ms"] - samples[-1]["odu_ms"]) <= 1000
                and all(b["odu_ms"] - a["odu_ms"] <= 1500 for a, b in zip(samples, samples[1:]))
                and all(all(hz > 3 for hz in row["frequency_hz"]) for row in samples))
            checks["odu_running_handoff"] = bool(before and after and not stops and
                "Controlled restart:" in transcript and "(saved)" in transcript
                and report["peer"].get("write_history_complete") is True)
        self.result["checks"] = checks
