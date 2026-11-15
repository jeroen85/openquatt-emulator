"""Compare qualified fixed-window web memory trials for local PR reporting."""

import argparse
from pathlib import Path
from statistics import median

from .memory import SAMPLE_SLACK_MS, summarize
from .export_memory import compare_exports
from .memory_comparison import compare, read_report, read_reference, require_match, runtime_fingerprint, settings
from .pr_workload import WINDOWS

SCOPE = ("Q Duo via virtuele Wi-Fi, webbelasting binnen 90–210 s sinds boot. "
         "De manifestcheck na 300 s stabiel netwerk valt buiten dit venster.")


def summarize_pr_recording(recording, windows, guest_now_ms):
    # The shared recorder uses inclusive endpoints. A boundary sample in this
    # scenario belongs only to the phase that starts there.
    inclusive = {name: (start, end - 1) for name, (start, end) in windows.items()}
    result = summarize(recording, inclusive, guest_now_ms)
    max_gap = result["interval_ms"] + SAMPLE_SLACK_MS
    for name, phase in result["phases"].items():
        start, end = windows[name]
        phase["window_ms"] = [start, end]
        phase["covered"] = phase["covered"] and end - phase["last_sample_ms"] <= max_gap
    result["complete"] = all(phase["covered"] for phase in result["phases"].values())
    return result


def recovery(memory):
    windows = (WINDOWS["before"], WINDOWS["after"])
    values = {}
    for field in ("free_heap_b", "largest_block_b"):
        groups = [[row[field] for row in memory.get("samples", []) if start <= row["guest_ms"] < end]
                  for start, end in windows]
        if all(groups):
            before, after = (median(group) for group in groups)
            values[field] = {"before_median_b": before, "after_median_b": after, "after_minus_before_b": after - before}
    return values


def assessment(candidate, reference=None, expected_baseline=None, max_drop_bytes=None):
    # An earlier baseline comparison can be retried using the same valid measurements.
    gates = [passed for name, passed in candidate["qualification"].items() if name != "pr_memory_comparison"]
    result = {
        "candidate_firmware_sha256": candidate["input"]["sha256"],
        "candidate_run_id": candidate["pr_memory"]["run_id"],
        "expected_baseline_firmware_sha256": expected_baseline,
        "max_drop_bytes": max_drop_bytes,
        "sample_interval_ms": candidate["memory"].get("interval_ms"),
        "recovery": recovery(candidate["memory"]),
        "stack_margins": "unknown", "allocation_failures": "unknown",
    }
    if reference:
        result.update(reference_report=reference["path"], reference_report_sha256=reference["report_sha256"],
                      reference_firmware_sha256=reference["report"]["input"]["sha256"],
                      reference_run_id=reference["report"].get("pr_memory", {}).get("run_id"))
    if not gates or not all(passed is True for passed in gates):
        return {**result, "status": "uncomparable", "reason": " ".join(candidate["failure_reasons"])}
    if reference is None:
        return {**result, "status": "baseline_ready", "reason": "Gekwalificeerde meting beschikbaar voor een vergelijking."}

    baseline = reference["report"]
    if baseline["input"]["sha256"] != expected_baseline:
        return {**result, "status": "uncomparable", "reason": "De baselinehash wijkt af van de gekozen dev-build."}
    try:
        require_match(settings(baseline), settings(candidate))
        require_match(runtime_fingerprint(baseline), runtime_fingerprint(candidate))
        before, after = baseline["pr_memory"], candidate["pr_memory"]
        if not before.get("run_id") or before["run_id"] == after["run_id"]:
            raise ValueError("Een vergelijking vereist twee verschillende boots.")
        for field in ("plan", "reset_path", "hardware_profile"):
            if before[field] != after[field]:
                raise ValueError(f"De meetopzet verschilt voor {field}.")
        common = set(before["workload"]["confirmed_overlap_targets_ms"]) & set(after["workload"]["confirmed_overlap_targets_ms"])
        if len(common) < len(after["plan"]["get_targets_ms"]) - 2:
            raise ValueError("Te weinig GET-tijdsloten met overlap in beide runs.")
        a, b = (item["workload"]["sse"]["events"] for item in (before, after))
        if abs(a - b) > max(3, 0.10 * max(a, b)):
            raise ValueError(f"Het aantal ontvangen SSE-events verschilt te veel ({a} tegenover {b}).")
        if after["plan"].get("export"):
            result["export_comparison"] = compare_exports(baseline["export_memory"], candidate["export_memory"])
        deltas = compare(reference, candidate, None, True)["phases"]
        if any(data["reference_window_ms"] != list(WINDOWS[name])
               or data["candidate_window_ms"] != list(WINDOWS[name]) for name, data in deltas.items()):
            raise ValueError("Een meetvenster wijkt af van het vastgelegde tijdvenster sinds boot.")
    except (KeyError, TypeError, ValueError) as exc:
        return {**result, "status": "uncomparable", "reason": str(exc)}
    result.update(phases=deltas, reference_recovery=recovery(baseline["memory"]),
                  measurement_profile={**settings(candidate), **runtime_fingerprint(candidate),
                                       **{key: after[key] for key in ("plan", "reset_path", "hardware_profile")}})
    if max_drop_bytes is None:
        return {**result, "status": "measured", "reason": "Verschillen gemeten. Kalibratie over meerdere onafhankelijke boots is nodig voor een grens."}
    exceeded = [f"{phase}/{metric}" for phase, data in deltas.items() for metric, values in data["metrics"].items()
                if values["delta_b"] < -max_drop_bytes]
    if exceeded:
        return {**result, "status": "repeat", "exceeded": exceeded,
                "reason": "Eén meetpaar overschrijdt de grens. Herhaal met twee nieuwe boots."}
    return {**result, "status": "no_signal", "exceeded": [],
            "reason": "De gemeten verschillen blijven binnen de opgegeven grens."}


def repeat_assessment(first, second):
    identities = ("reference_firmware_sha256", "candidate_firmware_sha256", "max_drop_bytes", "measurement_profile")
    result = {**first, "reference_reports": [
        {"path": pair["reference_report"], "sha256": pair["reference_report_sha256"]}
        for pair in {pair["reference_run_id"]: pair for pair in (first, second)}.values()]}
    result["independent_baselines"] = len(result["reference_reports"])
    if any(pair["status"] == "uncomparable" for pair in (first, second)) or any(
            first[key] != second[key] for key in identities):
        return {**result, "status": "uncomparable", "reason": "De herhaling hoort bij een andere of onvergelijkbare meetopzet."}
    if len({pair[key] for pair in (first, second) for key in ("reference_run_id", "candidate_run_id")}) < 4:
        return {**result, "status": "repeat", "reason": "Een onafhankelijk meetpaar vereist een verse baseline en kandidaat."}
    result["repeat_spread_b"] = {
        f"{phase}/{metric}": sorted((value["delta_b"], second["phases"][phase]["metrics"][metric]["delta_b"]))
        for phase, data in first["phases"].items() for metric, value in data["metrics"].items()
    }
    first_hits, second_hits = set(first.get("exceeded", [])), set(second.get("exceeded", []))
    if first_hits and first_hits == second_hits:
        return {**result, "status": "regression_signal", "reason": "Dezelfde meetwaarden overschrijden de grens in twee onafhankelijke meetparen."}
    if first_hits or second_hits:
        return {**result, "status": "repeat", "reason": "De onafhankelijke meetparen geven verschillende uitslagen."}
    return result


def markdown(result, trial):
    labels = {
        "baseline_ready": "Baseline beschikbaar", "measured": "Gemeten zonder gekalibreerde grens",
        "uncomparable": "Onvergelijkbaar", "repeat": "Herhalen",
        "regression_signal": "Regressiesignaal", "no_signal": "Geen signaal binnen de gekozen grens",
    }
    workload = trial["workload"]
    lines = ["## Virtuele HIL · webgeheugen", "", f"**{labels[result['status']]}**. {result['reason']}", "",
             SCOPE, "", f"- Kandidaat SHA-256 `{result['candidate_firmware_sha256']}`",
             f"- Aangetoonde overlap bij {len(workload['confirmed_overlap_targets_ms'])} GET-verzoeken",
             "- Stackmarges en allocatiefouten onbekend voor de factory-bin."]
    if trial.get("export"):
        export = trial["export"]
        request = export["request"]
        count = request.get("payload", {}).get("rows")
        during = export["phases"].get("during", {}).get("sample_count", 0)
        lines.append(f"- Export `{request['path']}`, {count if count is not None else 'onbekend'} rijen, "
                     f"{during} meetpunten tijdens de overdracht.")
        if export["httpd_stack_available"]:
            stack = export["phases"]["during"]["min_b"]["httpd_stack_b"]
            lines.append(f"- Laagste gemeten virtuele HTTPD-stackreserve tijdens de export {stack} B.")
        if export["largest_psram_block_available"]:
            block = export["phases"]["during"]["min_b"]["largest_psram_block_b"]
            lines.append(f"- Kleinste gemeten grootste vrije PSRAM-blok tijdens de export {block} B.")
    if result["sample_interval_ms"]:
        lines.append(f"- De recorder meet elke {result['sample_interval_ms'] // 1000} s. Korte dips kunnen tussen meetpunten vallen.")
    if result.get("reference_report_sha256"):
        lines.append(f"- Baseline SHA-256 `{result['reference_firmware_sha256']}`")
        for reference in result.get("reference_reports", [{"sha256": result["reference_report_sha256"]}]):
            lines.append(f"- Origineel baselinerapport SHA-256 `{reference['sha256']}`")
    if result.get("phases"):
        lines += ["", "| Fase | Vrije heap min Δ B | Heapmin sinds boot Δ B | Grootste blok min Δ B |",
                  "| --- | ---: | ---: | ---: |"]
        for phase, data in result["phases"].items():
            values = [f"{metric['delta_b']:+d}" for metric in data["metrics"].values()]
            lines.append(f"| {phase} | {' | '.join(values)} |")
    if result.get("repeat_spread_b"):
        lines += ["", "| Fase / meetwaarde | Laagste Δ B | Hoogste Δ B |", "| --- | ---: | ---: |"]
        for metric, (low, high) in result["repeat_spread_b"].items():
            lines.append(f"| {metric} | {low:+d} | {high:+d} |")
    for name, rec in (("Kandidaat", result["recovery"]), ("Baseline", result.get("reference_recovery", {}))):
        if rec:
            free, block = (rec[key]["after_minus_before_b"] for key in ("free_heap_b", "largest_block_b"))
            lines.append(f"\n{name} na herstel tegenover rust. Vrije heap {free:+g} B, grootste blok {block:+g} B (medianen).")
    lines.append("\nHet oordeel geldt voor deze virtuele belasting. Fysieke geheugenmarges en fysieke power-on vragen hardware-HIL.")
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--baseline-firmware-sha256", required=True)
    parser.add_argument("--max-drop-bytes", type=int)
    parser.add_argument("--repeat", nargs=2, type=Path, metavar=("BASELINE", "CANDIDATE"))
    args = parser.parse_args()
    if args.max_drop_bytes is not None and args.max_drop_bytes < 0:
        parser.error("--max-drop-bytes moet nul of hoger zijn")
    try:
        baseline, candidate = read_reference(args.baseline), read_report(args.candidate)
        report = candidate["report"]
        if report["scenario"] != "pr-memory":
            raise ValueError("Kies een rapport van het scenario pr-memory.")
        result = assessment(report, baseline, args.baseline_firmware_sha256, args.max_drop_bytes)
        if args.repeat:
            base2, candidate2 = read_reference(args.repeat[0]), read_report(args.repeat[1])
            next_report = candidate2["report"]
            if next_report["scenario"] != "pr-memory":
                raise ValueError("Kies een herhaling van het scenario pr-memory.")
            result = repeat_assessment(result, assessment(next_report, base2, args.baseline_firmware_sha256,
                                                           args.max_drop_bytes))
        print(markdown(result, report["pr_memory"]), end="")
        return int(result["status"] == "uncomparable")
    except (OSError, AttributeError, KeyError, TypeError, ValueError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
