"""Readable, inspectable exports of actual local lab evidence."""
from __future__ import annotations
import csv
import io
from pathlib import Path


def exports(report: dict) -> tuple[str, str]:
    state = report.get("state", {})
    lines = ["# OpenQuatt Lab", "", f"Resultaat: **{report['status']}**", "",
             "Echte Q Duo-firmware, lokale ESP32-S3-emulator en vastgepinde synthetische ODU-peers.", ""]
    if report.get("error"):
        lines += ["Fout: " + str(report["error"]), ""]
    lines += ["| Scenario | Resultaat | Duur | Waarneming |", "|---|---|---:|---|"]
    for item in state.get("runs", []):
        detail = str(item.get("detail", "")).replace("|", "/").replace("\n", " ")
        lines.append(f"| {item.get('label', item.get('id'))} | {item['status']} | {item.get('duration_s', 0):.1f} s | {detail} |")
    if report.get("memory"):
        lines += ["", "| Geheugenmeting | Slechtste waarde |", "|---|---:|"]
        for key, value in report["memory"].items():
            lines.append(f"| {key} | {value} |")
    diagnostics = report.get("diagnostics", {}).get("summary")
    if diagnostics:
        lines += ["", f"Taakdiagnostiek: {diagnostics['sample_count']} complete snapshots; "
                  f"{diagnostics['allocation_failures']} allocatiefouten; kleinste taakstack {diagnostics['min_task_stack_hwm_bytes']} bytes.",
                  f"PSRAM-scratch: {diagnostics['scratch_psram_bytes']} bytes; grootste snapshotsduur {diagnostics['max_snapshot_us']} µs (logging apart)."]
    if state.get("load"):
        lines += ["", "Web/SSE-belasting:"]
        for load in state["load"]:
            lines.append(f"- {load['http_requests']} HTTP-verzoeken; {load['sse_delivering_connections']} SSE-verbindingen met complete events; {load['sse_events']} events.")
    lines += ["", "Minimum sinds boot, actuele vrije heap en grootste blok zijn afzonderlijke metingen.",
              "Callbackstack betreft de ESPHome-callbacktaak; aanvullende taakmetingen staan in tasks.csv. Dit is geen fysieke HIL-kwalificatie.", "",
              "Nog fysiek te kwalificeren: elektrische OpenTherm-interface, RS485-elektronica/parity/framing, W5500, fysieke sensoren en gecombineerde HA/MQTT/TLS/OTA-belasting.", ""]
    output = io.StringIO()
    fields = ["phase", "uptime_ms", "wall_seconds", "internal_free", "internal_min_free", "internal_largest_block", "psram_free", "main_stack_hwm_bytes", "heap_integrity"]
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(report.get("samples", []))
    return "\n".join(lines), output.getvalue()


def task_csv(report: dict) -> str:
    output = io.StringIO()
    fields = ["phase", "uptime_ms", "sample_id", "task_id", "priority", "state", "stack_hwm_bytes"]
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    for sample in report.get("diagnostics", {}).get("samples", []):
        for task in sample["tasks"]:
            writer.writerow({**{key: sample[key] for key in ("phase", "uptime_ms", "sample_id")}, **task})
    return output.getvalue()


def write_exports(directory: Path, report: dict) -> None:
    markdown, csv_text = exports(report)
    for name, content in (("summary.md", markdown), ("memory.csv", csv_text), ("tasks.csv", task_csv(report))):
        temporary = directory / (name + ".tmp")
        temporary.write_text(content)
        temporary.replace(directory / name)
