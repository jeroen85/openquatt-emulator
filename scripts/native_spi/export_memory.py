"""Bound a real export and attribute memory samples to its download window."""
import hashlib
import json
import re

from .memory import RECORDING_PATH, read_samples

EXPORT_PATHS = {"recorder": RECORDING_PATH, "forecast": "/openquatt/weather/recording"}
EXPORT_START_MS = 162500
EXPORT_END_MS = 177000
MAX_EXPORT_BYTES = 4 * 1024 * 1024
FIELDS = ("guest_ms", "free_heap_b", "largest_block_b", "heapmin_b",
          "httpd_stack_b", "free_psram_b", "largest_psram_block_b")
COLOR = re.compile(r"\x1b\[[0-9;]*m")
PROBE_LINE = re.compile(r"hil\.exp[^\n]*?\bmem ((?:\d+ ){5,6}\d+)[ \t]*$")


def payload_evidence(kind, body, minimum_rows):
    if not 0 < len(body) <= MAX_EXPORT_BYTES:
        raise ValueError("Export is leeg of overschrijdt de downloadlimiet")
    try:
        document = json.loads(body)
        if kind == "recorder":
            count = len(read_samples(document)[1])
        elif (document["kind"] == "openquatt-forecast-measurements"
              and document["schema"] == 2 and isinstance(document["observations"], list)
              and isinstance(document["issues"], list)):
            columns = document["observation_columns"]
            if not isinstance(columns, list) or not columns or not all(
                    isinstance(row, list) and len(row) == len(columns) for row in document["observations"]):
                raise ValueError("Onvolledige Vooruitblik-meetrij")
            count = len(document["observations"])
        else:
            raise ValueError("Onbekend exportformaat")
    except (AttributeError, KeyError, TypeError) as error:
        raise ValueError("Onvolledig exportbestand") from error
    if count < minimum_rows:
        raise ValueError(f"Export bevat {count} rijen. Ten minste {minimum_rows} verwacht.")
    return {"rows": count, "bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()}


def serial_probe_samples(lines):
    samples = []
    for line in lines:
        if not (match := PROBE_LINE.search(COLOR.sub("", line))):
            continue
        sample = dict(zip(FIELDS, map(int, match[1].split())))
        if (sample["largest_block_b"] > sample["free_heap_b"] or sample["heapmin_b"] > sample["free_heap_b"]
                or sample.get("largest_psram_block_b", 0) > sample["free_psram_b"]
                or (samples and (sample["guest_ms"] <= samples[-1]["guest_ms"]
                                 or sample["heapmin_b"] > samples[-1]["heapmin_b"]))):
            raise ValueError("Geheugenprobe bevat inconsistente waarden of meerdere boots")
        samples.append(sample)
    return samples


def measure_window(samples, start_ms, end_ms, flank_ms):
    ranges = {
        "before": (start_ms - flank_ms, start_ms),
        "during": (start_ms, end_ms + 1),
        "after": (end_ms + 1, end_ms + flank_ms + 1),
    }
    phases = {}
    for name, (low, high) in ranges.items():
        selected = [sample for sample in samples if low <= sample["guest_ms"] < high]
        fields = set.intersection(*(set(sample) for sample in selected)) if selected else set()
        phases[name] = {
            "sample_count": len(selected),
            "min_b": {field: min(sample[field] for sample in selected)
                      for field in FIELDS[1:] if field in fields},
            "samples": selected,
        }
    return phases


class ExportMeasurement:
    def __init__(self, kind, minimum_rows, probe, seed=False):
        self.kind = kind
        self.minimum_rows = minimum_rows
        self.probe = probe
        self.seed = seed
        self.seed_requested = False
        self.seed_confirmed = False
        self.row = {"path": EXPORT_PATHS[kind], "target_ms": EXPORT_START_MS, "outcome": "not_run"}
        self.plan = {"kind": kind, "minimum_rows": minimum_rows, "probe": probe,
                     "target_ms": EXPORT_START_MS, "deadline_ms": EXPORT_END_MS, "seed_forecast": seed}

    def prepare(self, get):
        if not self.seed or self.seed_confirmed:
            return True
        if not self.seed_requested:
            self.seed_requested = get("/button/HIL%20Seed%20Forecast%20Recording/press",
                                      timeout=2, method="POST") is not None
        body = get("/openquatt/weather/recording/status", timeout=2)
        if body:
            count = json.loads(body).get("count")
            self.seed_confirmed = type(count) is int and count >= self.minimum_rows
        return self.seed_confirmed

    def run(self, load):
        def consume(row, response):
            body = response.read(MAX_EXPORT_BYTES + 1)
            row["last_byte"] = load.stamp()
            row["payload"] = payload_evidence(self.kind, body, self.minimum_rows)

        load.request(self.row, EXPORT_START_MS, EXPORT_END_MS, consume, timeout=12)

    def finalize(self, load, recorder, serial):
        row = self.row
        first = load.sse.get("first_event", {}).get("host_ms")
        last = load.sse.get("last_event", {}).get("host_ms")
        overlap = bool(row.get("last_byte") and first is not None and last is not None and
                       first <= row["start"]["host_ms"] <= row["last_byte"]["host_ms"] <= last)
        result = {"plan": self.plan, "request": row, "sse_overlap": overlap, "phases": {}}
        if row.get("last_byte") and row["outcome"] == "ok":
            start = row["start"]["guest_ms"]
            end = row["last_byte"]["guest_ms"]
            if self.probe:
                with serial.open() as transcript:
                    samples = serial_probe_samples(transcript)
                flank = 2000
            else:
                samples = recorder.get("samples", [])
                flank = 12500
            result["phases"] = measure_window(samples, start, end, flank)
        phases = result["phases"]
        result["checks"] = {
            "real_export_completed": row["outcome"] == "ok" and "payload" in row,
            "export_during_sse": overlap,
        }
        if self.seed:
            result["checks"]["fixture_seed_confirmed"] = self.seed_requested and self.seed_confirmed
        if self.probe:
            result["checks"]["serial_probe_covers_transfer"] = bool(
                phases and all(phase["sample_count"] for phase in phases.values()))
        result["largest_psram_block_available"] = bool(phases and all(
            "largest_psram_block_b" in phase["min_b"] for phase in phases.values()))
        result["httpd_stack_available"] = bool(phases and all(
            "httpd_stack_b" in phase["min_b"] for phase in phases.values()))
        return result


def compare_exports(before, after):
    """Compare observed values while keeping per-transfer coverage explicit."""
    phases = {}
    for key, baseline in before["phases"].items():
        candidate = after["phases"][key]
        phases[key] = {
            "reference_samples": baseline["sample_count"],
            "candidate_samples": candidate["sample_count"],
            "delta_b": {field: candidate["min_b"][field] - reference_value
                        for field, reference_value in baseline["min_b"].items()
                        if field in candidate["min_b"]},
        }
    return {
        "reference_rows": before["request"]["payload"]["rows"],
        "candidate_rows": after["request"]["payload"]["rows"],
        "phases": phases,
    }
