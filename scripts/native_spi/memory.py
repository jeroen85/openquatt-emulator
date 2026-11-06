"""Read the stock firmware's rolling internal heap measurements."""


RECORDING_PATH = "/openquatt/debug-recording/download"
FIELDS = {
    "uptimeMs": ("guest_ms", "ms"),
    "freeHeap": ("free_heap_b", "B"),
    "minFreeHeap": ("heapmin_b", "B"),
    "largestFreeHeapBlock": ("largest_block_b", "B"),
}
RESTART_WINDOW_MS = 30000
SAMPLE_SLACK_MS = 2500
SAMPLE_INTERVAL_S = 10


def read_samples(recording):
    if recording["format"] != "openquatt-debug-device-v1" or recording["encoding"] != "device-psram-delta-json-v1":
        raise ValueError("Onbekend formaat van de systeemrecorder")
    columns = recording["columns"]
    units = dict(recording["units"])
    indices = {}
    for field, (name, unit) in FIELDS.items():
        if columns.count(field) != 1 or units.get(columns.index(field)) != unit:
            raise ValueError(f"Ontbrekend of ongeldig recorderkanaal {field}")
        indices[columns.index(field)] = name

    header = recording["recording"]
    interval_s = header["interval_s"]
    if type(interval_s) is not int or interval_s != SAMPLE_INTERVAL_S:
        raise ValueError("De systeemrecorder moet elke 10 seconden meten")
    rows = recording["samples"]
    if (header["active"] is not True or len(rows) != header["sample_count"]
            or not rows or rows[0] != [0, []]):
        raise ValueError("De systeemrecorder bevat geen volledige actieve opname")
    samples = []
    state = {indices[index]: value for index, value in recording["initial"] if index in indices}
    for _, changes in rows:
        state.update((indices[index], value) for index, value in changes if index in indices)
        if len(state) != len(FIELDS) or any(type(value) is not int or value < 0 for value in state.values()):
            raise ValueError("Een heapmeting is onvolledig of ongeldig")
        if max(state["heapmin_b"], state["largest_block_b"]) > state["free_heap_b"]:
            raise ValueError("De heapwaarden van de systeemrecorder zijn inconsistent")
        if samples and (state["guest_ms"] <= samples[-1]["guest_ms"] or state["heapmin_b"] > samples[-1]["heapmin_b"]):
            raise ValueError("De heapmeting springt terug naar een andere firmwareboot")
        samples.append(dict(state))
    return interval_s * 1000, samples


def summarize(recording, dynamics, guest_now_ms):
    interval_ms, samples = read_samples(recording)
    max_gap = interval_ms + SAMPLE_SLACK_MS
    if not 0 <= guest_now_ms - samples[-1]["guest_ms"] <= max_gap:
        raise ValueError("De laatste recorderwaarde loopt niet mee met de firmwareklok")
    points = dynamics["checkpoints"]
    lowflow = dynamics["lowflow"]
    restart = points["restart"]["guest_ms"]
    windows = {
        "startup": (0, points["standby"]["observed_from_ms"]),
        "defrost": (points["defrost"]["guest_ms"], lowflow["fault_event"]["uptime_s"] * 1000),
        "lowflow_recovery": (lowflow["restore_ms"], lowflow["clear_window_ms"][0]),
        "restart": (restart, restart + RESTART_WINDOW_MS),
    }
    phases = {}
    for name, (start, end) in windows.items():
        selected = [row for row in samples if start <= row["guest_ms"] <= end]
        times = [start, *(row["guest_ms"] for row in selected), end]
        phases[name] = phase = {
            "window_ms": [start, end], "sample_count": len(selected),
            "covered": len(selected) >= 2 and all(0 <= b - a <= max_gap for a, b in zip(times, times[1:])),
        }
        if selected:
            phase.update(
                first_sample_ms=selected[0]["guest_ms"],
                last_sample_ms=selected[-1]["guest_ms"],
                lowest_free_heap_b=min(row["free_heap_b"] for row in selected),
                heapmin_at_last_sample_b=selected[-1]["heapmin_b"],
                lowest_largest_block_b=min(row["largest_block_b"] for row in selected),
            )
    return {
        "source": "openquatt_debug_recorder", "capabilities": "MALLOC_CAP_INTERNAL",
        "interval_ms": interval_ms, "samples": samples, "phases": phases,
        "complete": all(phase["covered"] for phase in phases.values()),
    }
