"""Short local virtual HIL for the daily accumulator across CM3 and CM4."""
import argparse
import hashlib
import json
import math
import socket
import time
from pathlib import Path

from scripts.emulator.run import request
from scripts.simulator.runtime import Session

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--output", required=True, type=Path)
parser.add_argument("--http-port", type=int, default=19630)
parser.add_argument("--uart-port", type=int, default=19632)
args = parser.parse_args()
if not __debug__:
    parser.error("Gebruik Python zonder optimalisatie om de proefcontroles uit te voeren")
HTTP, UART = args.http_port, args.uart_port
if HTTP == UART or not all(1024 <= port <= 65535 for port in (HTTP, UART)):
    parser.error("Kies twee verschillende loopbackpoorten tussen 1024 en 65535")
OUTPUT = args.output.resolve()
OUTPUT.mkdir(parents=True, exist_ok=False)
for port in (HTTP, UART):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", port))

session = Session(http_port=HTTP, uart_port=UART, output=OUTPUT / "emulator", lab_profile="ot-boiler")
report = {
    "product_commit": "b4a986654902806eee914f28e1c8749d6ef62c54",
    "fixture": "virtual RMT boiler (initial 32/28 C), Modbus ODUs, GPIO flow pulses, HA supply publication and seeded UTC",
    "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    "extra_fixture_sources": {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in
                              [Path("configs/emulator/cv_virtual_clock.h"),
                               Path("openquatt/experimental/virtual-memory-feature.yaml"),
                               *sorted(Path("scripts/simulator/io/virtual_flow").glob("*.*"))]},
    "verdict": "INCOMPLETE", "phases": {},
}

def save():
    (OUTPUT / "report.json").write_text(json.dumps(report, indent=2) + "\n")

def sample():
    export, _ = request(HTTP, "/openquatt/learning/export")
    data = json.loads(export)
    collection = data["collection_state"]
    control = dict(session.telemetry)
    controller = dict(session.controller_values)
    peer = dict(session.ot_telemetry.get("OQ_OT_PEER", {}))
    extra = session.controller.values({
        "ch": ("binary_sensor", "OTB - Central Heating Active"),
        "boiler_w": ("sensor", "Boiler Heat Power"),
        "deficit_w": ("sensor", "HP deficit (W)"),
        "command_w": ("sensor", "Boiler command requested power"),
        "system_w": ("sensor", "System Heat Power"),
        "block_reason": ("text_sensor", "Boiler block reason"),
        "warm_start": ("text_sensor", "Boiler warm-start guard"),
    }, optional=True)
    return {"control": control, "controller": controller, "peer": peer, "extra": extra,
            "collection": collection, "daily": data["daily_collection"], "records": len(data["records"]),
            "synthetic": data.get("synthetic"), "auto_apply_allowed": data["auto_apply_allowed"]}

def poll(phase, predicate, seconds):
    deadline = time.monotonic() + seconds
    last = {}
    with (OUTPUT / (phase + ".jsonl")).open("x") as evidence:
        while time.monotonic() < deadline:
            session._check()
            last = sample()
            evidence.write(json.dumps(last) + "\n")
            evidence.flush()
            if predicate(last):
                return last
            time.sleep(1.8)
    raise TimeoutError(f"{phase} never satisfied, last evidence in {phase}.jsonl")

def actual_phase(row, mode):
    ctrl, odus, peer, extra = row["control"], row["controller"], row["peer"], row["extra"]
    frequencies = (odus.get("frequency1"), odus.get("frequency2"))
    if any(type(f) not in (int, float) or not math.isfinite(f) for f in frequencies):
        return False
    stopped = (max(frequencies) < 0.5 and ctrl.get("applied_level1") == 0 and ctrl.get("applied_level2") == 0)
    running = max(frequencies) > 3.0 and max(ctrl.get("applied_level1", 0), ctrl.get("applied_level2", 0)) > 0
    return (
        ctrl.get("control_mode") == mode and (running if mode == 3 else stopped)
        and peer.get("ch_enabled") is True and (peer.get("target_c") or 0) > 0
        and extra.get("ch") is True and (extra.get("boiler_w") or 0) > 1000
        and odus.get("supply_source") == "HA input" and (ctrl.get("flow_lph") or 0) > 500
    )

def collecting(row):
    state = row["collection"]
    return (state["enabled"] and state["batch_active"] and not state["batch_gap_pending"]
            and state["batch_source_status"] == "ok" and state["batch_target_s"] == 86400
            and row["auto_apply_allowed"] is False and state["tick_epoch"] >= 1793966400)

def stable_day(row, anchor):
    state, initial = row["collection"], anchor["collection"]
    assert state["context_revision"] == initial["context_revision"]
    assert abs((state["tick_epoch"] - state["batch_elapsed_s"])
               - (initial["tick_epoch"] - initial["batch_elapsed_s"])) <= 2
    assert row["daily"]["interrupted_periods"] == anchor["daily"]["interrupted_periods"]
    assert row["records"] == anchor["records"]

def prove_mode(mode, baseline):
    ticks = {}
    def observe(row):
        if not actual_phase(row, mode):
            ticks.clear()
            return False
        assert collecting(row), f"CM{mode} is active but day paused: {row['collection']}"
        stable_day(row, baseline)
        state = row["collection"]
        ticks[state["tick_epoch"]] = row
        span = max(ticks) - min(ticks)
        if span < 20 or len(ticks) < 3:
            return False
        first, last = ticks[min(ticks)], ticks[max(ticks)]
        delta = last["collection"]["batch_elapsed_s"] - first["collection"]["batch_elapsed_s"]
        assert delta >= 20 and abs(delta - span) <= 2
        report["phases"][f"CM{mode}"] = {"first": first, "last": last, "distinct_ticks": len(ticks),
                                          "elapsed_advance_s": delta}
        save()
        print(f"CM{mode} PASS: day progress +{delta}s, {len(ticks)} distinct learner ticks", flush=True)
        return True
    return poll(f"cm{mode}", observe, 215)

save()
try:
    session.start()
    assert session.ready.wait(155), session.stage
    assert session.state == "running", session.error or session.stage
    report["firmware_sha256"] = session.frozen_firmware_sha256
    report["elf_sha256"] = session.frozen_elf_sha256
    report["emulator_sha256"] = session.emulator_sha256
    report["emulator_output"] = str(session.output)
    save()
    ctrl = session.controller
    print("Virtual RMT and Modbus online, applying inputs", flush=True)
    with session.command_lock:
        ctrl.select("CM Override", "Force CM0")
        ctrl.number("Minimum runtime", 30)
        ctrl.select("Water Supply Source", "HA input")
        ctrl.select("Q Flow Source", "Local")
        ctrl.select("External Heat Demand Source", "Disabled")
        ctrl.number("Rated maximum house power", 15000)
        ctrl.number("HP1 - Excluded frequency minimum", 61)
        ctrl.number("HP1 - Excluded frequency maximum", 120)
        ctrl.number("HP2 - Excluded frequency minimum", 20)
        ctrl.number("HP2 - Excluded frequency maximum", 120)
        ctrl.number("CM3 deficit ON threshold", 100)
        ctrl.number("CM3 deficit OFF threshold", 0)
        ctrl.switch("Auxiliary heat source connected", True)
        ctrl.switch("Boiler assist enabled", True)
        ctrl.switch("Boiler fallback on heat-pump fault", True)
        ctrl.switch("Power House Passive Learning", True)
    baseline = poll("baseline", lambda row: collecting(row) and row["collection"]["batch_elapsed_s"] >= 10
                    and row["controller"].get("supply_source") == "HA input"
                    and (row["control"].get("flow_lph") or 0) > 500, 65)
    report["baseline"] = baseline
    save()
    print("Baseline day collecting. Requesting enough heat for CM3.", flush=True)
    with session.command_lock:
        session.fleet.set_environment({"heating_enable": True, "outdoor_c": -10.0, "room_c": 19.0,
                                       "setpoint_c": 21.0, "flow_lph": 800.0, "return_c": 28.0})
        session.environment = dict(session.fleet.environment)
        ctrl.apply_environment(session.environment)
    cm3 = prove_mode(3, baseline)
    print("Injecting the documented hard incident in both virtual ODUs for CM4.", flush=True)
    with session.command_lock:
        for i in (0, 1):
            session.fleet.set_odu({"index": i, "fault": 1})
    cm4 = prove_mode(4, baseline)
    assert cm4["collection"]["batch_elapsed_s"] > cm3["collection"]["batch_elapsed_s"]
    with session.lock:
        controls = list(session.control_history)
        flows = list(session.flow_history)
    report["harness_evidence"] = {
        "runtime_sha256": hashlib.sha256(Path("scripts/simulator/runtime.py").read_bytes()).hexdigest(),
        "max_requested_level": max(max(row["requested_level1"], row["requested_level2"]) for row in controls),
        "max_applied_level": max(max(row["applied_level1"], row["applied_level2"]) for row in controls),
        "control_samples": len(controls), "gpio_flow_samples": len(flows), "gpio_by_phase": {},
    }
    save()
    for name, phase in report["phases"].items():
        start = phase["first"]["control"]["uptime_ms"]
        end = phase["last"]["control"]["uptime_ms"]
        active = [row for row in flows if start <= row["uptime_ms"] <= end
                  and row["applied_enabled"] and row["active"] and row["applied_flow_lph"] > 500]
        assert len(active) >= 3, f"{name} is missing concurrent GPIO flow evidence"
        edges = active[-1]["generated_rising_edges"] - active[0]["generated_rising_edges"]
        assert edges > 0, f"{name} GPIO pulse source did not advance"
        report["harness_evidence"]["gpio_by_phase"][name] = {"samples": len(active), "rising_edges": edges}
    report["verdict"] = "PASS"
except BaseException as error:
    report["verdict"] = "FAIL"
    report["error"] = str(error)
    report["session_state"] = session.state
    report["session_error"] = session.error
    report["emulator_output"] = str(session.output)
    raise
finally:
    try:
        session.stop()
    except BaseException as error:
        report["verdict"] = "FAIL"
        report["cleanup_error"] = str(error)
        raise
    finally:
        cleanup = report["cleanup"] = {
            "state": session.state, "error": session.error,
            "emulator_exit": session.process.poll() if session.process else None,
            "worker_joined": not session.worker or not session.worker.is_alive(),
        }
        if cleanup["state"] != "stopped" or cleanup["emulator_exit"] != 0 or not cleanup["worker_joined"]:
            report["verdict"] = "FAIL"
        save()
if report["verdict"] != "PASS":
    raise SystemExit(1)
