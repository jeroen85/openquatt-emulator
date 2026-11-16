"""Compare qualified Q Duo heap runs with the same emulator settings."""

import hashlib
import json
import re

from .memory import SAMPLE_INTERVAL_S

PHASES = {
    "compatibility": ("startup", "normal"),
    "pr-memory": ("before", "load", "after"),
    "lowflow": ("startup", "defrost", "lowflow_recovery", "restart"),
}
METRICS = ("lowest_free_heap_b", "heapmin_at_last_sample_b", "lowest_largest_block_b")
MAX_REPORT_BYTES = 16 * 1024 * 1024


def settings(report):
    # Older reports recorded the scenario through the presence of the lowflow trace.
    scenario = report.get("scenario", "lowflow" if "dynamics" in report else "compatibility")
    return {
        "scenario": scenario, "mode": report["mode"], "network": report["network"]["mode"],
        "duration_s": report["duration_s"], "fixture_temperature_c": report["fixture_temperature_c"],
    }


def runtime_fingerprint(report):
    sources = ["memory.py", "model.py", "network_probe.py", "transport.py"]
    if settings(report)["scenario"] == "lowflow":
        sources.extend(("dynamics.py", "lowflow.py"))
    if settings(report)["scenario"] == "pr-memory":
        sources.extend(("run.py", "pr_workload.py", "pr_memory.py", "http_response.py"))
    if report.get("pr_memory", {}).get("plan", {}).get("export"):
        sources.append("export_memory.py")
    cosim = report.get("backend") == "cosim"
    binaries = ["executable_sha256", "odu_library_sha256"]
    if cosim:
        sources.append("cosim.py")
    else:
        binaries.append("extension_sha256")
    fingerprint = {key: report[key] for key in binaries}
    fingerprint.update({name: report["sources"][name] for name in sources})
    if any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value) for value in fingerprint.values()):
        raise ValueError("Ongeldige emulatorvingerafdruk in het geheugenrapport")
    if cosim:
        return {**fingerprint, "backend": "cosim", "chip": report["input"]["chip"]}
    return {**fingerprint, "slirp": report["native_build"]["slirp"], "chip": report["input"]["chip"]}


def require_match(reference, candidate):
    different = sorted(key for key in reference.keys() | candidate.keys() if reference.get(key) != candidate.get(key))
    if different:
        raise ValueError(f"De kandidaat wijkt af van de referentie voor {', '.join(different)}")


def measured_phases(report):
    memory = report["memory"]
    phases = memory["phases"]
    expected = PHASES[settings(report)["scenario"]]
    if (memory.get("complete") is not True or memory.get("interval_ms") != SAMPLE_INTERVAL_S * 1000
            or memory.get("source") != "openquatt_debug_recorder" or memory.get("capabilities") != "MALLOC_CAP_INTERNAL"
            or set(phases) != set(expected)):
        raise ValueError("Het geheugenrapport mist een volledige opname voor dit scenario")
    for name in expected:
        phase = phases[name]
        metrics = [phase[key] for key in METRICS]
        start, end = phase["window_ms"]
        count = phase["sample_count"]
        if (phase.get("covered") is not True or type(count) is not int or count < 2
                or any(type(value) is not int or value < 0 for value in (*metrics, start, end))
                or start >= end or max(metrics[1:]) > metrics[0]):
            raise ValueError(f"Ongeldige heapmetingen voor fase {name}")
    return phases


def read_report(path):
    with path.open("rb") as source:
        raw = source.read(MAX_REPORT_BYTES + 1)
    if len(raw) > MAX_REPORT_BYTES:
        raise ValueError("Het geheugenrapport is te groot")
    return {"report": json.loads(raw), "report_sha256": hashlib.sha256(raw).hexdigest(), "path": str(path.resolve())}


def read_reference(path):
    try:
        evidence = read_report(path)
        report = evidence["report"]
        gates = report["qualification"]
        firmware_sha256 = report["input"]["sha256"]
        expected_status = "PASS_COSIM" if report.get("backend") == "cosim" else "PASS_NATIVE_EXTENSION"
        if (report.get("schema") != 2 or report.get("status") != expected_status
                or gates.get("memory") is not True or not all(value is True for value in gates.values())
                or report.get("input_hash_unchanged") is not True or report.get("flash_file_unchanged") is not True
                or not re.fullmatch(r"[0-9a-f]{64}", firmware_sha256)):
            raise ValueError("Kies een geslaagd geheugenrapport met een geverifieerde firmwarehash")
        measured_phases(report)
        runtime_fingerprint(report)
    except (OSError, AttributeError, KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"Kan geheugenreferentie {path} niet gebruiken: {exc}") from exc
    return evidence


def compare(reference, candidate, max_drop_bytes, qualified):
    baseline = reference["report"]
    result = {
        "reference_report_sha256": reference["report_sha256"],
        "reference_firmware_sha256": baseline["input"]["sha256"],
        "candidate_firmware_sha256": candidate["input"]["sha256"],
        "max_drop_bytes": max_drop_bytes, "phases": {},
    }
    if not qualified:
        return {**result, "status": "unavailable", "reason": "De kandidaatmeting is niet volledig gekwalificeerd."}

    reference_phases = measured_phases(baseline)
    candidate_phases = measured_phases(candidate)
    exceeded = []
    for name in PHASES[settings(candidate)["scenario"]]:
        before, after = reference_phases[name], candidate_phases[name]
        metrics = {}
        for metric in METRICS:
            delta = after[metric] - before[metric]
            metrics[metric] = {"reference_b": before[metric], "candidate_b": after[metric], "delta_b": delta}
            if max_drop_bytes is not None and delta < -max_drop_bytes:
                exceeded.append(f"{name}/{metric} ({delta} B)")
        result["phases"][name] = {
            "reference_window_ms": before["window_ms"], "candidate_window_ms": after["window_ms"],
            "reference_samples": before["sample_count"], "candidate_samples": after["sample_count"],
            "metrics": metrics,
        }
    if exceeded:
        return {**result, "status": "regression",
                "reason": f"De toegestane daling van {max_drop_bytes} B is overschreden bij {', '.join(exceeded)}."}
    return {**result, "status": "compared"}
