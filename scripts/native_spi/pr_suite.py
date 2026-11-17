"""Run the Q Duo PR tests and write a single summary with the original reports."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import os
import shutil
import signal
import subprocess
import sys
import uuid

from .build import LAB
from .memory_comparison import METRICS

stop_requested = False
BASE = "geheugen"
CASES = {
    BASE: ("Webbelasting en geheugen", "wifi", "pr-memory", "--seconds", "285"),
    "herstart": ("Instellingen behouden na softwareherstart", "ethernet", "restart", "--restart-case", "software"),
    "modbus": ("Modbus-uitval en herstel van HP2", "wifi", "odu-link-loss"),
    "energie-uur": ("Energiehistorie bij uurwisseling en herstart", "ethernet", "energy-boundary", "--energy-case", "hour"),
    "energie-dag": ("Energiehistorie bij dagwisseling", "ethernet", "energy-boundary", "--energy-case", "day"),
    "power-house": ("Power House-vraag en Single/Duo", "wifi", "power-house", "--power-house-climate", "mild"),
    "lowflow": ("Flowverlies tijdens defrost en herstel", "wifi", "lowflow"),
    "ethernet-ntp": ("W5500-Ethernet en lokale tijdsynchronisatie", "ethernet", "compatibility",
                     "--ntp-epoch", "1768474320", "--seconds", "90"),
    "ethernet-failover": ("Automatische terugval van Ethernet naar Wi-Fi", "failover", "compatibility",
                          "--seconds", "180"),
}
NETWORK_CASES = ("ethernet-ntp", "ethernet-failover")
PROFILES = {
    "basis": (BASE,),
    "herstart": (BASE, "herstart"),
    "modbus": (BASE, "modbus"),
    "energie": (BASE, "energie-uur", "energie-dag"),
    "regeling": (BASE, "power-house", "lowflow"),
    "alles": tuple(case for case in CASES if case not in NETWORK_CASES),
    "netwerk": (BASE, *NETWORK_CASES),
    "uitgebreid": (BASE, "herstart", *NETWORK_CASES),
}
COSIM_PROFILES = ("basis", "herstart", "netwerk", "uitgebreid")


def select_cases(profile, backend):
    cases = PROFILES[profile]
    if backend == "cosim" and profile not in COSIM_PROFILES:
        raise ValueError("Dit profiel vereist lokaal de macOS-backend.")
    if backend == "native" and profile in ("netwerk", "uitgebreid"):
        raise ValueError("Kies --backend cosim voor dit profiel.")
    return cases


def request_stop(_signum, _frame):
    global stop_requested
    stop_requested = True


def wait_for_case(process):
    while not stop_requested:
        try:
            return process.wait(timeout=0.25)
        except subprocess.TimeoutExpired:
            continue
    if process.poll() is None:
        process.terminate()
    try:
        return process.wait(timeout=25)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        return process.wait(timeout=5)


def run_case(key, firmware, backend, port_base, folder, extra=()):
    title, network, scenario, *options = CASES[BASE if key == "geheugen-baseline" else key]
    if key == "geheugen-baseline":
        title = "Referentiemeting voor webgeheugen"
    if backend == "cosim" and key == "herstart":
        network = "wifi"

    name = f"github-q-duo-{key}-{uuid.uuid4().hex[:10]}"
    folder.mkdir(parents=True)
    log = folder / "runner.log"
    print(f"{title} wordt uitgevoerd", flush=True)
    with log.open("w") as stream:
        with subprocess.Popen([
            sys.executable, "-B", "-m", "scripts.native_spi.run", "--firmware", str(firmware),
            "--backend", backend, "--network", network, "--scenario", scenario, "--port-base", str(port_base),
            "--name", name, *options, *extra,
        ], stdout=stream, stderr=subprocess.STDOUT, start_new_session=True) as process:
            exit_code = wait_for_case(process)
    matches = list((LAB / "runs").glob(f"{name}-*"))
    if len(matches) == 1:
        for artifact in ("report.json", "live.json", "serial.log", "pr-memory.md"):
            source = matches[0] / artifact
            if source.is_file():
                shutil.copyfile(source, folder / artifact)
    report_path = folder / "report.json"
    report = json.loads(report_path.read_text()) if report_path.is_file() else {}
    passed = exit_code == 0 and report.get("status") in ("PASS_COSIM", "PASS_NATIVE_EXTENSION")
    print(f"{title} {'geslaagd' if passed else 'afgekeurd'}", flush=True)
    fallback = f"Geen geslaagd eindrapport. Zie runner.log (exitcode {exit_code})."
    return {"title": title, "passed": passed, "reasons": report.get("failure_reasons") or ([] if passed else [fallback]),
            "report": report}


def run_suite(profile, firmware, backend, port_base, output, baseline=None):
    cases = select_cases(profile, backend)
    output.mkdir(parents=True, exist_ok=True)
    results = {}
    comparison = ()
    if baseline is not None and not stop_requested:
        key = "geheugen-baseline"
        results[key] = run_case(key, baseline, backend, port_base, output / key)
        if results[key]["passed"]:
            with baseline.open("rb") as source:
                digest = hashlib.file_digest(source, "sha256").hexdigest()
            comparison = ("--memory-baseline", str(output / key / "report.json"),
                          "--memory-baseline-firmware-sha256", digest)
    for key in cases:
        if stop_requested:
            break
        extra = comparison if key == BASE else ()
        results[key] = run_case(key, firmware, backend, port_base, output / key, extra)

    lines = ["## Q Duo · virtuele HIL", ""]
    base_result = results.get(BASE, {})
    if stop_requested:
        lines += ["**Proef gestopt**", ""]
    if base_result.get("passed"):
        lines += ["**Q Duo-rooktest geslaagd**", ""]
    lines += ["| Proef | Uitslag |", "| --- | --- |"]
    for result in results.values():
        lines.append(f"| {result['title']} | {'Geslaagd' if result['passed'] else 'Afgekeurd'} |")
    for result in results.values():
        if result["reasons"]:
            lines += ["", f"### {result['title']}"] + [f"- {reason}" for reason in result["reasons"]]

    if base_result.get("passed"):
        memory = base_result["report"]["memory"]
        lines += ["", "### Intern geheugen in bytes", "",
                  "| Fase | Vrije heap, laagste | Heapmin sinds boot | Grootste vrije blok, laagste |",
                  "| --- | ---: | ---: | ---: |"]
        for phase, label in (("before", "Rust"), ("load", "Webbelasting"), ("after", "Herstel")):
            values = " | ".join(str(memory["phases"][phase][metric]) for metric in METRICS)
            lines.append(f"| {label} | {values} |")
    if baseline is not None and not comparison:
        lines += ["", "Voor een geheugenvergelijking is een geslaagde referentiemeting nodig."]
    memory_report = output / BASE / "pr-memory.md"
    if memory_report.is_file():
        lines += ["", memory_report.read_text().rstrip()]
    (output / "summary.md").write_text("\n".join(lines) + "\n")
    (output / "pr-result.json").write_text(json.dumps({
        "profile": profile, "cases": {key: result["passed"] for key, result in results.items()},
    }))
    if stop_requested:
        return 130
    return 0 if all(result["passed"] for result in results.values()) else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--firmware", type=Path, required=True)
    parser.add_argument("--baseline-firmware", type=Path)
    parser.add_argument("--profile", choices=PROFILES, default="basis")
    parser.add_argument("--backend", choices=("cosim", "native"), required=True)
    parser.add_argument("--port-base", type=int, default=19460)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, request_stop)
    try:
        return run_suite(args.profile, args.firmware.resolve(), args.backend, args.port_base, args.output.resolve(),
                         args.baseline_firmware.resolve() if args.baseline_firmware else None)
    except ValueError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
