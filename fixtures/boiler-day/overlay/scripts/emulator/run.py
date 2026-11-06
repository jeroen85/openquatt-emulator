#!/usr/bin/env python3
"""Run the real Q Duo firmware in a pinned ESP32-S3 emulator, without hardware."""
from __future__ import annotations

import argparse
import hashlib
import gzip
import io
import json
import math
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "configs/emulator/duo_wifi.yaml"
BUILD = ROOT / ".esphome/build/emulator_q_duo/build"
VERSION = "0.51.0"
ARCHIVES = {
    ("Darwin", "arm64"): ("aarch64-apple-darwin", "ab02524bd286ce84577639731d0e48d623ce9be65e45d9e1246dd220c1476102"),
    ("Linux", "aarch64"): ("aarch64-unknown-linux-gnu", "3fca854df8d1fc2fc73e37f3a4e9db214166d0e07dfdbd4702019fd9dcc3146f"),
    ("Linux", "x86_64"): ("x86_64-unknown-linux-gnu", "2b9e157470edb8b03167996550292c6df8cc6716025ebad8927db950d7931417"),
}
MAX_LOG_BYTES = 16 * 1024 * 1024
FIELDS = ("uptime_ms", "internal_free", "internal_min_free", "internal_largest_block", "psram_free", "main_stack_hwm_bytes")
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
FATAL = re.compile(r"Guru Meditation|CORRUPT HEAP|assert failed:|Stack canary watchpoint|panic'ed|CPU fault|Heap integrity check failed|Task watchdog got triggered|ESP_ERROR_CHECK failed|stack overflow", re.I)


class TestFailure(RuntimeError):
    pass


def install_emulator(destination: Path) -> Path:
    key = (platform.system(), platform.machine())
    if key not in ARCHIVES:
        raise TestFailure(f"No pinned emulator binary for {key}; use Linux/WSL or macOS Apple Silicon")
    target, expected = ARCHIVES[key]
    url = f"https://github.com/espressif/esp-emulator/releases/download/v{VERSION}/esp-emu-{VERSION}-{target}.tar.gz"
    print(f"Downloading pinned esp-emu {VERSION} ({target})", flush=True)
    with urllib.request.urlopen(url, timeout=120) as response:
        data = response.read(100 * 1024 * 1024)
    if hashlib.sha256(data).hexdigest() != expected:
        raise TestFailure("Emulator archive SHA-256 mismatch")
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        members = [m for m in archive.getmembers() if m.isfile() and Path(m.name).name == "esp-emu"]
        if len(members) != 1:
            raise TestFailure("Emulator archive does not contain exactly one executable")
        source = archive.extractfile(members[0])
        assert source is not None
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(".download")
        temporary.write_bytes(source.read())
        temporary.chmod(0o755)
        temporary.replace(destination)
    return destination


def build(esphome: str, output: Path) -> None:
    required = (ROOT / ".github/requirements-esphome.txt").read_text().strip().split("==")[1]
    version = subprocess.check_output([esphome, "version"], text=True)
    if required not in version:
        raise TestFailure(f"Use ESPHome {required}, found {version.strip()}")
    env = dict(os.environ, ESPHOME_COMPILE_PROCESS_LIMIT="2", CMAKE_BUILD_PARALLEL_LEVEL="2")
    commands = [["npm", "run", "build:web"], [esphome, "compile", str(CONFIG)]]
    with (output / "build.log").open("w") as log:
        for command in commands:
            print(f"Building: {' '.join(command)} (at most 2 compiler jobs)", flush=True)
            result = subprocess.run(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
            if result.returncode:
                raise TestFailure(f"Build failed ({result.returncode}); see {output / 'build.log'}")


def parse_sample(line: str) -> dict | None:
    line = ANSI.sub("", line)
    marker = "OQ_EMU_MEMORY "
    if marker not in line:
        return None
    try:
        sample = json.loads(line.split(marker, 1)[1].strip())
    except (ValueError, TypeError) as exc:
        raise TestFailure(f"Malformed memory sample: {exc}") from exc
    if not isinstance(sample, dict):
        raise TestFailure("Memory sample must be a JSON object")
    if sample.get("schema") != 1:
        raise TestFailure("Unsupported memory probe schema")
    for field in FIELDS:
        if type(sample.get(field)) is not int or sample[field] < 0:
            raise TestFailure(f"Missing or invalid memory field: {field}")
    if sample.get("heap_integrity") is not True:
        raise TestFailure("Firmware heap integrity check failed")
    if sample["internal_largest_block"] > sample["internal_free"]:
        raise TestFailure("Largest internal block exceeds free internal heap")
    if sample["internal_min_free"] > sample["internal_free"]:
        raise TestFailure("Internal minimum exceeds current free heap")
    sample["internal_fragmentation_pct"] = round(
        100 * (1 - sample["internal_largest_block"] / sample["internal_free"]), 2
    ) if sample["internal_free"] else 100.0
    return sample


class Capture:
    """Only the reader thread mutates capture state; snapshots are synchronized."""
    def __init__(self, output: Path):
        self.output = output
        self.lock = threading.Lock()
        self.samples: list[dict] = []
        self.errors: list[str] = []
        self.boots = 0
        self.phase = "boot"
        self.started = time.monotonic()
        self.reader_done = threading.Event()

    def set_phase(self, phase: str) -> None:
        with self.lock:
            self.phase = phase

    def state(self) -> tuple[list[dict], list[str], int]:
        with self.lock:
            return [dict(s) for s in self.samples], list(self.errors), self.boots

    def consume(self, stream) -> None:
        try:
            with (self.output / "serial.log").open("w") as log:
                written = 0
                limited = False
                for line in stream:
                    size = len(line.encode("utf-8", errors="replace"))
                    if written + size <= MAX_LOG_BYTES and not limited:
                        log.write(line)
                        log.flush()
                        written += size
                    elif not limited:
                        limited = True
                        with self.lock:
                            self.errors.append("Serial log exceeded the 16 MiB limit")
                    try:
                        sample = parse_sample(line)
                        with self.lock:
                            if FATAL.search(line):
                                self.errors.append(ANSI.sub("", line).strip())
                            if "OQ_EMU_BOOT schema=1" in line:
                                self.boots += 1
                            if sample:
                                if self.samples and sample["uptime_ms"] <= self.samples[-1]["uptime_ms"]:
                                    self.errors.append("Firmware uptime reset or did not advance")
                                if self.samples and sample["internal_min_free"] > self.samples[-1]["internal_min_free"]:
                                    self.errors.append("Minimum-since-boot watermark increased")
                                sample.update(phase=self.phase, wall_seconds=round(time.monotonic() - self.started, 3))
                                self.samples.append(sample)
                    except TestFailure as exc:
                        with self.lock:
                            self.errors.append(str(exc))
        except Exception as exc:
            with self.lock:
                self.errors.append(f"Serial reader failed: {type(exc).__name__}: {exc}")
        finally:
            self.reader_done.set()


def check_process(process, capture: Capture) -> None:
    samples, errors, boots = capture.state()
    if errors:
        raise TestFailure(errors[0])
    if boots > 1:
        raise TestFailure("Unexpected firmware reboot")
    if capture.reader_done.is_set():
        raise TestFailure("Serial reader stopped before the workload completed")
    if samples and time.monotonic() - capture.started - samples[-1]["wall_seconds"] > 30:
        raise TestFailure("Firmware memory telemetry stalled for more than 30s")
    if process.poll() is not None:
        raise TestFailure(f"Emulator exited early ({process.returncode}); see serial.log")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())


def request(port: int, path: str, body: bytes | None = None) -> tuple[bytes, str]:
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=body,
                                 headers={"Cache-Control": "no-store", "Content-Type": "application/x-www-form-urlencoded"})
    with HTTP.open(req, timeout=3) as response:
        data = response.read(2 * 1024 * 1024 + 1)
        if response.headers.get("Content-Encoding", "").lower() == "gzip":
            with gzip.GzipFile(fileobj=io.BytesIO(data)) as compressed:
                data = compressed.read(2 * 1024 * 1024 + 1)
        if len(data) > 2 * 1024 * 1024:
            raise TestFailure("HTTP response exceeded the 2 MiB probe limit")
        return data, response.headers.get("Content-Type", "")


def firmware_value(port: int) -> str:
    data, _ = request(port, "/text_sensor/ESPHome%20Version")
    value = json.loads(data).get("value")
    if not isinstance(value, str) or not value:
        raise ValueError("Firmware identity missing")
    return value


def wait_ready(process, capture: Capture, port: int, timeout: float) -> str:
    deadline = time.monotonic() + timeout
    provisioned = False
    last_error = "no HTTP response"
    while time.monotonic() < deadline:
        check_process(process, capture)
        try:
            value = firmware_value(port)
            if capture.state()[2] == 1:
                return value
        except (OSError, ValueError, urllib.error.URLError) as exc:
            last_error = str(exc)
        if not provisioned:
            query = urllib.parse.urlencode({"ssid": "OpenQuatt-Emulator", "psk": "emulator-only-pass"})
            try:
                data, _ = request(port, f"/wifisave?{query}")
                if b"Connecting" in data:
                    provisioned = True
                    print("Provisioned synthetic WiFi via the normal captive portal", flush=True)
            except (OSError, urllib.error.URLError):
                pass
        time.sleep(0.5)
    raise TestFailure(f"Firmware web/API did not become ready within {timeout}s: {last_error}")


def verify_bulk(payload: dict, version: str) -> None:
    if not isinstance(payload, dict) or payload.get("missing"):
        raise TestFailure("Bulk entity response contains missing entities")
    entities = payload.get("entities")
    if not isinstance(entities, dict):
        raise TestFailure("Bulk entity response has no entities")
    for name in ("heap", "version"):
        if not isinstance(entities.get(name), dict):
            raise TestFailure(f"Bulk entity response is missing {name}")
    actual_version = entities["version"].get("value", entities["version"].get("state"))
    if actual_version != version:
        raise TestFailure("Bulk firmware version differs from the entity endpoint")
    heap = entities["heap"].get("value", entities["heap"].get("state"))
    if type(heap) not in (int, float) or not math.isfinite(heap) or heap <= 0:
        raise TestFailure("Bulk heap entity has no valid numeric measurement")


def workload(process, capture: Capture, port: int, seconds: float, phase: str) -> dict:
    capture.set_phase(phase)
    print(f"Phase {phase}: {seconds:g}s wall time", flush=True)
    started = time.monotonic()
    count = 0
    while time.monotonic() - started < seconds:
        check_process(process, capture)
        if phase == "web":
            html, _ = request(port, "/")
            (capture.output / "web-root.html").write_bytes(html)
            if b"<esp-app" not in html or b"/0.js" not in html or b"/0.css" not in html:
                raise TestFailure("Web root does not contain the expected app shell")
            javascript, _ = request(port, "/0.js")
            stylesheet, _ = request(port, "/0.css")
            if b"openquatt" not in javascript.lower() or len(stylesheet) < 1000:
                raise TestFailure("Embedded OpenQuatt JS/CSS assets are missing")
            version = firmware_value(port)
            body = urllib.parse.urlencode({"detail": "state", "entities": "heap\tsensor\tHeap Free\nversion\ttext_sensor\tESPHome Version"}).encode()
            data, _ = request(port, "/openquatt/entities", body)
            verify_bulk(json.loads(data), version)
            count += 5
        time.sleep(min(2, max(0, seconds - (time.monotonic() - started))))
    check_process(process, capture)
    return {"wall_seconds": round(time.monotonic() - started, 3), "http_requests": count}


def summarize(samples: list[dict]) -> dict:
    if not samples:
        raise TestFailure("No firmware memory samples captured")
    result = {field: min(s[field] for s in samples) for field in FIELDS if field != "uptime_ms"}
    result["max_internal_fragmentation_pct"] = max(s["internal_fragmentation_pct"] for s in samples)
    result["sample_count"] = len(samples)
    result["last_uptime_ms"] = samples[-1]["uptime_ms"]
    return result


def verify_samples(samples: list[dict], phases: list[str], args) -> dict:
    if len(samples) < 3:
        raise TestFailure(f"Insufficient memory samples: {len(samples)} (need at least 3)")
    for phase in phases:
        if not any(s["phase"] == phase for s in samples):
            raise TestFailure(f"No memory sample during {phase}; increase phase duration")
    summary = summarize(samples)
    for field in ("internal_free", "internal_largest_block", "psram_free", "main_stack_hwm_bytes"):
        if summary[field] <= 0:
            raise TestFailure(f"Invalid zero memory measurement: {field}")
    for field, minimum in (("internal_min_free", args.min_internal_free),
                           ("internal_largest_block", args.min_largest_block),
                           ("main_stack_hwm_bytes", args.min_main_stack)):
        if summary[field] < minimum:
            raise TestFailure(f"{field}={summary[field]} is below required {minimum} bytes")
    return summary


def finalize_measurements(report: dict, capture: Capture, args, reader_alive: bool = False) -> None:
    # Decide on the fully drained stream, including a final buffered low sample.
    samples, errors, boots = capture.state()
    report.update(samples=samples, serial_errors=errors, boot_count=boots,
                  wall_seconds=round(time.monotonic() - capture.started, 3))
    if samples:
        report["memory"] = summarize(samples)
        report["memory_by_phase"] = {phase: summarize([s for s in samples if s["phase"] == phase])
                                     for phase in dict.fromkeys(s["phase"] for s in samples)}
        elapsed = samples[-1]["uptime_ms"] - samples[0]["uptime_ms"]
        wall = samples[-1]["wall_seconds"] - samples[0]["wall_seconds"]
        report["emulated_seconds_per_wall_second"] = round(elapsed / 1000 / wall, 3) if wall > 0 else None
    if report["status"] != "PASS":
        return
    try:
        if reader_alive:
            raise TestFailure("Serial reader did not finish draining")
        if errors:
            raise TestFailure(errors[0])
        if boots != 1:
            raise TestFailure(f"Expected exactly one boot, observed {boots}")
        report["memory"] = verify_samples(samples, ["idle", "web"], args)
    except TestFailure as exc:
        report.update(status="FAIL", error=str(exc))


def free_port(port: int) -> None:
    with socket.socket() as sock:
        # A drained previous session may still leave TIME_WAIT tuples.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError as exc:
            raise TestFailure(f"Local port {port} is occupied; choose another local port") from exc


def write_report(output: Path, report: dict) -> None:
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    lines = ["# OpenQuatt ESP32-S3 emulation", "", f"Result: **{report['status']}**", "",
             "Experimental firmware measurements; not physical HIL qualification.", ""]
    if report.get("error"):
        lines.extend([f"Failure: {report['error']}", ""])
    if report.get("memory"):
        lines.extend(["| Firmware measurement | Minimum / worst observed |", "|---|---:|"])
        for key, value in report["memory"].items():
            lines.append(f"| {key} | {value} |")
    lines.extend(["", "Scope: cold boot, idle and serial HTTP workload. No ODU/OpenTherm models, HA, MQTT or OTA load.",
                  "Stack measurement covers only the task running the ESPHome interval callback.", ""])
    markdown = "\n".join(lines)
    (output / "summary.md").write_text(markdown)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a") as target:
            target.write(markdown)


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install", action="store_true", help="Download the SHA-256 pinned emulator if absent")
    parser.add_argument("--build", action="store_true", help="Build web assets and Q Duo firmware sequentially, maximum 2 jobs")
    parser.add_argument("--esphome", default=str(ROOT / ".venv/bin/esphome") if (ROOT / ".venv/bin/esphome").exists() else "esphome")
    parser.add_argument("--emulator", type=Path, default=ROOT / ".tmp/emulator/runtime/esp-emu")
    parser.add_argument("--firmware", type=Path, default=BUILD / "firmware.factory.bin")
    parser.add_argument("--elf", type=Path, default=BUILD / "firmware.elf")
    parser.add_argument("--output", type=Path, default=ROOT / ".tmp/emulator" / time.strftime("run-%Y%m%d-%H%M%S"))
    parser.add_argument("--http-port", type=int, default=18080)
    parser.add_argument("--boot-timeout", type=float, default=180)
    parser.add_argument("--idle-seconds", type=float, default=20)
    parser.add_argument("--web-seconds", type=float, default=40)
    parser.add_argument("--psram-size", choices=["0", "4M", "8M", "16M"], default="8M", help="0 exercises the missing-PSRAM failure path")
    parser.add_argument("--min-internal-free", type=int, default=0)
    parser.add_argument("--min-largest-block", type=int, default=0)
    parser.add_argument("--min-main-stack", type=int, default=0)
    args = parser.parse_args(argv)
    if not 1024 <= args.http_port <= 65535:
        parser.error("--http-port must be between 1024 and 65535")
    for name in ("boot_timeout", "idle_seconds", "web_seconds"):
        if not 0 < getattr(args, name) <= 3600:
            parser.error(f"--{name.replace('_', '-')} must be > 0 and <= 3600")
    for name in ("min_internal_free", "min_largest_block", "min_main_stack"):
        if getattr(args, name) < 0:
            parser.error("Memory thresholds must be non-negative")
    return args


def main(argv=None) -> int:
    args = arguments(argv)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    report = {"schema": 1, "status": "FAIL", "emulator_version": VERSION,
              "profile": "q-duo-emulator-v1", "host": {"system": platform.system(), "machine": platform.machine()},
              "coverage": {"odu": False, "opentherm": False, "physical_hil": False},
              "settings": {"psram_size": args.psram_size, "idle_seconds": args.idle_seconds, "web_seconds": args.web_seconds},
              "phases": {}, "samples": []}
    process = None
    reader = None
    capture = Capture(output)
    try:
        if args.build:
            build_started = time.monotonic()
            build(args.esphome, output)
            report["build_wall_seconds"] = round(time.monotonic() - build_started, 3)
        binary = args.emulator.resolve()
        if not binary.is_file():
            if not args.install:
                raise TestFailure("Emulator missing; rerun with --install")
            binary = install_emulator(binary)
        version = subprocess.run([str(binary), "--version"], text=True, capture_output=True, check=True).stdout.strip()
        if not re.search(rf"\bv?{re.escape(VERSION)}\b", version):
            raise TestFailure(f"Expected esp-emu v{VERSION}, got {version}")
        if not args.firmware.is_file() or not args.elf.is_file():
            raise TestFailure("Factory firmware or ELF missing; rerun with --build")
        report["firmware_sha256"] = hashlib.sha256(args.firmware.read_bytes()).hexdigest()
        report["config_sha256"] = hashlib.sha256(CONFIG.read_bytes()).hexdigest()
        report["emulator_sha256"] = hashlib.sha256(binary.read_bytes()).hexdigest()
        report["git_commit"] = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        report["git_dirty"] = bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True).strip())
        report["runner_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        # Each run starts from its own factory flash. The emulator may mutate it.
        flash = output / "flash.bin"
        shutil.copyfile(args.firmware, flash)
        free_port(args.http_port)
        command = [str(binary), "--chip", "esp32s3", "--firmware", str(flash), "--elf", str(args.elf.resolve()),
                   "--psram-size", args.psram_size, "--wifi-ssid", "OpenQuatt-Emulator",
                   "--wifi-password", "emulator-only-pass", "--wifi-fw-ap-password", "openquatt",
                   "--net", f"user,restrict=yes,hostfwd=tcp:127.0.0.1:{args.http_port}-:80", "--log-color", "never"]
        (output / "command.json").write_text(json.dumps(command, indent=2) + "\n")
        capture.started = time.monotonic()
        process = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True, errors="replace", env=dict(os.environ, RUST_LOG="warn"))
        assert process.stdout is not None
        reader = threading.Thread(target=capture.consume, args=(process.stdout,), daemon=True)
        reader.start()
        report["firmware_identity"] = wait_ready(process, capture, args.http_port, args.boot_timeout)
        print(f"Firmware API ready at http://127.0.0.1:{args.http_port}", flush=True)
        for phase, duration in (("idle", args.idle_seconds), ("web", args.web_seconds)):
            report["phases"][phase] = workload(process, capture, args.http_port, duration, phase)
        check_process(process, capture)
        report["status"] = "PASS"  # Provisional until the serial reader has drained.
    except (TestFailure, OSError, ValueError, subprocess.SubprocessError, urllib.error.URLError) as exc:
        report["error"] = str(exc)
    except KeyboardInterrupt:
        report["error"] = "Interrupted"
    finally:
        if process is not None:
            if process.poll() is not None and report["status"] == "PASS":
                report.update(status="FAIL", error=f"Emulator exited unexpectedly ({process.returncode})")
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if reader is not None:
            reader.join(timeout=5)
        finalize_measurements(report, capture, args, reader_alive=reader.is_alive() if reader else False)
        write_report(output, report)
    print(f"{report['status']}: {report.get('error', 'cold boot, web/API and memory probe completed')}")
    print(f"Report: {output / 'report.json'}")
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
