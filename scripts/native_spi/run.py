"""Run one unchanged factory image with the native S3 peripheral extension."""
from __future__ import annotations

import argparse
from contextlib import ExitStack, contextmanager
import fcntl
import gzip
import hashlib
import io
import json
import os
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
from .dynamics import Dynamics, wire_evidence
from .factory import factory_info
from .lowflow import Lowflow
from .network_probe import MODES, NetworkProbe
from .qualification import qualify
from .transport import RtuPeer

ROOT = Path(__file__).resolve().parents[2]
LAB = ROOT / ".tmp/native-spi"
PT1000 = "/sensor/" + urllib.parse.quote("Water Supply Temp (PT1000)", safe="")
_cancel_requested = False


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
    expected = {f"n127.0.0.1:{port}" for port in ports.values()}
    deadline = time.monotonic() + 8
    while process.poll() is None and time.monotonic() < deadline:
        status = subprocess.run(
            ["/usr/sbin/lsof", "-nP", "-a", "-p", str(process.pid), "-iTCP", "-sTCP:LISTEN", "-Fn"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=2, check=False,
        )
        if expected.issubset(status.stdout.splitlines()) and process.poll() is None:
            return
        time.sleep(0.05)
    raise RuntimeError("The owned emulator did not open all loopback listeners")


def run(firmware, seconds=90, mode="device", base=19460, name="factory", temperature=30.0, network="wifi", scenario="compatibility"):
    if not 5 <= seconds <= 1800:
        raise ValueError("Duration must be 5..1800 host seconds")
    if not 0 <= temperature <= 100:
        raise ValueError("Temperature must be 0..100 Celsius")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", name):
        raise ValueError("Invalid local run name")
    if network not in MODES or (network != "wifi" and mode != "device"):
        raise ValueError("Ethernet requires the native device mode")
    if scenario not in ("compatibility", "dynamics", "lowflow"):
        raise ValueError("Invalid test scenario")
    if scenario != "compatibility" and (mode != "device" or network == "failover"):
        raise ValueError("Dynamics and lowflow require device mode on a stable Wi-Fi or Ethernet route")
    if not 1024 <= base <= 65533:
        raise ValueError("Port base must be between 1024 and 65533")

    info = factory_info(firmware)
    ports = {"http": base, "uart1": base + 2}
    if network != "wifi":
        ports["ethernet"] = base + 1
    with reserve_ports(ports):
        return run_owned(firmware, info, seconds, mode, ports, name, temperature, network, scenario)


def run_owned(firmware, info, seconds, mode, ports, name, temperature, network, scenario):
    out = LAB / "runs" / f"{name}-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    out.mkdir(parents=True)
    flash = out / "flash.bin"
    shutil.copyfile(firmware, flash)
    if sha(flash) != info["sha256"]:
        raise ValueError("Firmware changed during copy")
    cable = out / "w5500-cable"
    probe = NetworkProbe(network, ports, cable)

    executable, extension, native_build = ensure()
    odu_library = executable.with_name("odu.dylib")
    if mode == "baseline":
        executable = LAB / "runtime/esp-emu.original"
        verify_original(executable)
        extension = None
    report = {
        "schema": 2, "input": info, "mode": mode, "fixture_temperature_c": temperature,
        "duration_s": seconds, "status": "RUNNING", "firmware_elf_used": False,
        "debugger_used": False, "firmware_modified": False, "ports": ports,
        "http": [], "errors": [], "panics": [], "modbus_timing_warnings": [], "setup_complete": False,
        "sources": {p.name: sha(p) for p in Path(__file__).parent.glob("*.py")},
        "native_build": native_build, "extension_sha256": sha(extension) if extension else None,
        "executable_sha256": sha(executable), "odu_library_sha256": sha(odu_library), "network": probe.result,
    }
    command = [
        str(executable), "--chip", "esp32s3", "--firmware", str(flash), "--psram-size", "8M",
        "--wifi-ssid", "OpenQuatt-Native-Probe", "--wifi-password", "native-probe-only",
        "--wifi-fw-ap-password", "openquatt", "--uart1-tcp", f"127.0.0.1:{ports['uart1']}",
        "--net", f"user,restrict=yes,hostfwd=tcp:127.0.0.1:{ports['http']}-:80",
        "--timeout", f"{seconds + 15}s", "--log-color", "never",
    ]
    atomic_json(out / "command.json", command)
    env = dict(os.environ, RUST_LOG="warn")
    for key in ("OQ_NATIVE_GUEST_CLOCK", "OQ_NATIVE_OT_CONTROL", "DYLD_INSERT_LIBRARIES",
                "OQ_NATIVE_SPI_TRACE", "OQ_NATIVE_SPI_MODE", "OQ_W5500_PORT", "OQ_W5500_CABLE"):
        env.pop(key, None)
    trace = out / "mmio.jsonl"
    if extension:
        env.update(DYLD_INSERT_LIBRARIES=str(extension), OQ_NATIVE_SPI_TRACE=str(trace),
                   OQ_NATIVE_SPI_MODE=mode, OQ_NATIVE_RTD_C=str(temperature))
    if network != "wifi":
        env.update(OQ_W5500_PORT=str(ports["ethernet"]), OQ_W5500_CABLE=str(cable))

    clock_path = out / "guest-clock.bin"
    if mode == "device":
        clock_path.write_bytes(bytes(8))
        env["OQ_NATIVE_GUEST_CLOCK"] = str(clock_path)

    def guest_clock():
        return struct.unpack("<Q", clock_path.read_bytes())[0] // 1000

    fleet = model.OduFleet(odu_library)
    peer = RtuPeer(fleet, guest_clock if mode == "device" else None)
    dynamics = None
    if scenario != "compatibility":
        thermostat = out / "thermostat-heat"
        scenario_type = Lowflow if scenario == "lowflow" else Dynamics
        dynamics = scenario_type(thermostat, fleet)
        dynamics.set_heat(False)
        env["OQ_NATIVE_OT_CONTROL"] = str(thermostat)
        report["dynamics"] = dynamics.result
    peer.configure_faults(delay_ms=0)
    process = reader = None
    started = time.monotonic()
    deadline = started + seconds
    stop = threading.Event()
    report_lock = threading.RLock()
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
                        if "Active connection: WiFi (automatic recovery)" in line:
                            report.setdefault("wifi_recovery_event_s", time.monotonic() - started)
                        if "ETH driver install error" in line or "W5500 driver unavailable" in line:
                            report["ethernet_driver_error"] = True
                        if "setup() finished successfully!" in line:
                            report["setup_complete"] = True
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

    def get(path, timeout=2, *, port=None, method="GET", any_status=False):
        remaining = deadline - time.monotonic()
        if remaining <= 0 or _cancel_requested:
            return b"" if any_status else None
        port = probe.port if port is None else port
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}" + path, method=method, data=b"" if method == "POST" else None,
        )
        try:
            opener = status_opener if any_status else openers[port]
            with opener.open(request, timeout=min(timeout, remaining)) as response:
                if any_status:
                    return b""
                raw = response.read(1024 * 1024 + 1)
                if len(raw) > 1024 * 1024:
                    raise ValueError("HTTP wire size bound exceeded")
                data = raw
                if response.headers.get("Content-Encoding") == "gzip" or raw.startswith(b"\x1f\x8b"):
                    with gzip.GzipFile(fileobj=io.BytesIO(raw)) as compressed:
                        data = compressed.read(4 * 1024 * 1024 + 1)
                if len(data) > 4 * 1024 * 1024:
                    raise ValueError("Decoded HTTP size bound exceeded")
                if time.monotonic() >= deadline or _cancel_requested:
                    return None
                report["http"].append({
                    "path": path.split("?")[0], "status": response.status, "wire_bytes": len(raw),
                    "bytes": len(data), "wall_s": round(time.monotonic() - started, 3), "port": port,
                })
                return data
        except urllib.error.HTTPError as exc:
            exc.close()
            return b"" if any_status else None
        except OSError:
            if any_status and (time.monotonic() >= deadline or _cancel_requested):
                return b""
            return None

    verified_asset_ports = set()

    def inspect_web():
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
        sensor = get(PT1000, port=port)
        if sensor:
            value = json.loads(sensor).get("value")
            report["pt1000_value_c"] = value
            report["pt1000_verified"] = isinstance(value, (int, float)) and abs(value - temperature) < 0.15
            (out / "pt1000.json").write_bytes(sensor)
        if port in verified_asset_ports and probe.observe(get, started):
            report["web_checks"] = report.get("web_checks", 0) + 1
            report["web_last_ok_s"] = round(time.monotonic() - started, 3)
            report["web_port"] = port
            if dynamics:
                dynamics.observe(get)

    try:
        process = subprocess.Popen(
            command, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, errors="replace",
        )
        report["pid"] = process.pid
        atomic_json(out / "owner.json", {"pid": process.pid, "owned": True, "ports": ports})
        reader = threading.Thread(target=consume, name="firmware-serial", daemon=True)
        reader.start()
        atomic_json(LAB / "latest-run.json", {"run": str(out), "pid": process.pid})
        print(json.dumps({"run": str(out), "pid": process.pid, "mode": mode}), flush=True)
        wait_for_owned_listeners(process, ports)
        if not _cancel_requested:
            peer.start(ports["uart1"])
        next_http = next_live = 0
        while process.poll() is None and time.monotonic() < deadline and not stop.is_set() and not _cancel_requested:
            now = time.monotonic()
            if now >= next_live:
                publish_live()
                next_live = now + 1
            if report["setup_complete"] and now >= next_http:
                next_http = now + 2
                inspect_web()
            stop.wait(max(0, min(0.2, deadline - time.monotonic())))
    except KeyboardInterrupt:
        report["cancelled"] = True
    except Exception as exc:
        error("run", exc)
    finally:
        # End the workload measurement before teardown.
        workload_ended = time.monotonic()
        report["elapsed_wall_s"] = workload_ended - started
        report["exit_before_cleanup"] = process.poll() if process else None
        report["emulator_stop_requested"] = False
        peer.stop.set()
        try:
            if process and report["exit_before_cleanup"] is None:
                process.terminate()
                report["emulator_stop_requested"] = True
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
        try:
            peer.close()
        except Exception as exc:
            error("UART cleanup", exc)
        peer_stopped = peer.thread is None or not peer.thread.is_alive()
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
            report["flash_file_unchanged"] = sha(flash) == info["sha256"]
        except OSError as exc:
            error("final hash", exc)

        report["model_errors"] = []
        ot_events = []
        try:
            if trace.is_file():
                with trace.open() as capture:
                    for line in capture:
                        event = json.loads(line)
                        if event["kind"] == "model-error":
                            report["model_errors"].append(event)
                        elif event["kind"] == "opentherm":
                            ot_events.append(event)
                        elif event["kind"] == "ethernet":
                            probe.result["wire"] = event
        except (ValueError, OSError, KeyError) as exc:
            error("native trace", exc)

        if dynamics:
            dynamics.result["wire"] = wire_evidence(ot_events)
        if _cancel_requested:
            report["cancelled"] = True
        report["qualification"], report["failure_reasons"] = qualify(report)
        accepted = all(report["qualification"].values())
        report["status"] = "PASS_NATIVE_EXTENSION" if accepted else "NOT_QUALIFIED"
        if report.get("cancelled"):
            report["status"] = "STOPPED"
        atomic_json(out / "report.json", report)
        publish_live()
        print(json.dumps({
            "status": report["status"], "run": str(out), "setup": report["setup_complete"],
            "web": report.get("web_checks", 0), "pt1000": report.get("pt1000_value_c"),
            "requests": report["peer"]["requests"], "responses": report["peer"]["responses"],
            "panics": len(report["panics"]), "errors": report["errors"], "cleanup": report["cleanup"],
        }), flush=True)
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
    parser.add_argument("--mode", choices=("baseline", "trace", "device"), default="device")
    parser.add_argument("--port-base", type=int, default=19460)
    parser.add_argument("--name", default="factory")
    parser.add_argument("--temperature", type=float, default=30.0)
    parser.add_argument("--network", choices=MODES, default="wifi")
    parser.add_argument("--scenario", choices=("compatibility", "dynamics", "lowflow"), default="compatibility")
    args = parser.parse_args()
    default_seconds = {"compatibility": 90, "dynamics": 420, "lowflow": 780}
    seconds = args.seconds if args.seconds is not None else default_seconds[args.scenario]
    raise SystemExit(run(args.firmware, seconds, args.mode, args.port_base, args.name, args.temperature,
                         args.network, args.scenario))
