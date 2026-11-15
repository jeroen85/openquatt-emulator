"""Observe bounded SSE and GET clients on the guest's fixed memory test timeline."""

import http.client
import threading
import time

from .http_response import bounded_response

WINDOWS = {"before": (90000, 120000), "load": (120000, 180000), "after": (180000, 210000)}
READY_BY_MS = WINDOWS["before"][0]
PROBES_STOP_MS = READY_BY_MS - 10000
GET_PERIOD_MS = 4000
MAX_START_LAG_MS = 750
SSE_MAX_EVENT_GAP_MS = 12000
SSE_LIMIT_BYTES = 8 * 1024 * 1024
ASSET_LIMIT_BYTES = 2 * 1024 * 1024


def plan(without_overlap):
    sse_end, get_start = (147000, 150000) if without_overlap else (179000, 124000)
    return {
        "profile": "q-duo-wifi-web-v1",
        "mode": "sequential" if without_overlap else "overlap",
        "windows_ms": {name: list(window) for name, window in WINDOWS.items()},
        "sse_window_ms": [120000, sse_end],
        "get_targets_ms": list(range(get_start, 178000, GET_PERIOD_MS)),
        "max_start_lag_ms": MAX_START_LAG_MS,
        "max_sse_event_gap_ms": SSE_MAX_EVENT_GAP_MS,
        "clock": "guest_systimer_ms",
    }


def summarize_workload(schedule, sse, gets, background, ready_ms):
    targets = schedule["get_targets_ms"]
    successful = [item for item in gets if item["outcome"] == "ok"]
    # The SSE connection is open between its headers and close. Count only GETs
    # between observed SSE events as evidence of overlap with a delivering stream.
    opened, closed = sse.get("headers"), sse.get("closed")
    first, last = sse.get("first_event"), sse.get("last_event")
    socket_overlaps, confirmed = [], []
    for item in successful:
        start, end = item["start"]["host_ms"], item["last_byte"]["host_ms"]
        if opened and closed and start < closed["host_ms"] and opened["host_ms"] < end:
            socket_overlaps.append(item["target_ms"])
        if first and last and first["host_ms"] <= start <= end <= last["host_ms"]:
            confirmed.append(item["target_ms"])

    issues = []
    if ready_ms is None or ready_ms >= READY_BY_MS:
        issues.append("De web-app was niet op tijd klaar voor het rustvenster.")
    if background:
        issues.append("Een gewone webprobe liep door in het meetvenster.")
    if sse.get("outcome") != "ok" or not all((opened, closed, first, last)) or sse["events"] < 2:
        issues.append("De SSE-client leverde geen bruikbare stream.")
    elif (first["guest_ms"] - schedule["sse_window_ms"][0] > SSE_MAX_EVENT_GAP_MS
          or schedule["sse_window_ms"][1] - last["guest_ms"] > SSE_MAX_EVENT_GAP_MS
          or sse["max_event_gap_ms"] > SSE_MAX_EVENT_GAP_MS
          or not opened["host_ms"] <= first["host_ms"] <= last["host_ms"] <= closed["host_ms"]):
        issues.append("De SSE-stream had onvoldoende dekking in het geplande venster.")
    if closed and closed["guest_ms"] >= WINDOWS["after"][0]:
        issues.append("De SSE-client liep door in het herstelvenster.")
    if ([item["target_ms"] for item in successful] != targets
            or any(item["closed"]["guest_ms"] >= WINDOWS["after"][0] for item in successful)):
        issues.append("Niet alle geplande GET-verzoeken zijn op tijd geslaagd.")
    if schedule["mode"] == "sequential":
        issues.append("De controleproef plant SSE en GET na elkaar." if not socket_overlaps
                      else "De controleproef had onverwacht toch overlap.")
    elif len(confirmed) < max(2, len(targets) - 2):
        issues.append("De gemeten SSE- en GET-activiteiten overlapten onvoldoende.")
    return {
        "ready_guest_ms": ready_ms, "sse": sse, "gets": gets, "background": background,
        "socket_overlap_targets_ms": socket_overlaps, "confirmed_overlap_targets_ms": confirmed,
        "valid": not issues, "issues": issues,
    }


class PrWebLoad:
    def __init__(self, port, guest_clock, started, deadline, cancelled, without_overlap, export=None):
        self.schedule = plan(without_overlap)
        self.export = export
        if export:
            self.schedule = {**self.schedule, "export": export.plan}
        self.port, self.guest_clock, self.started, self.deadline = port, guest_clock, started, deadline
        self.cancelled = cancelled
        self.stop = threading.Event()
        self.threads = []
        self.ready_ms = None
        self.sse = {"path": "/events", "events": 0, "wire_bytes": 0, "max_event_gap_ms": 0}
        self.gets = []

    def stamp(self):
        return {"host_ms": round((time.monotonic() - self.started) * 1000, 3),
                "guest_ms": self.guest_clock()}

    def stopped(self):
        return self.stop.is_set() or self.cancelled() or time.monotonic() >= self.deadline

    def wait_until(self, guest_ms):
        while not self.stopped():
            if self.guest_clock() >= guest_ms:
                return True
            self.stop.wait(0.05)
        return False

    def ready(self, guest_ms):
        if self.ready_ms is not None or guest_ms >= READY_BY_MS:
            return
        self.ready_ms = guest_ms
        targets = (self.run_sse, self.run_gets, self.run_export) if self.export else (self.run_sse, self.run_gets)
        for target in targets:
            worker = threading.Thread(target=target, daemon=True, name=target.__name__)
            worker.start()
            self.threads.append(worker)

    def close(self):
        self.stop.set()
        for thread in self.threads:
            thread.join(timeout=3)
        return not any(thread.is_alive() for thread in self.threads)

    def run_export(self):
        self.export.run(self)

    def request(self, row, target, end_ms, consume, *, sse=False, timeout=3):
        if not self.wait_until(target):
            return
        row["start"] = self.stamp()
        if row["start"]["guest_ms"] > target + MAX_START_LAG_MS:
            row["outcome"] = "missed_start"
            return
        at_end = lambda: self.guest_clock() >= end_ms
        deadline = min(self.deadline, time.monotonic() + (75 if sse else timeout))
        row["outcome"] = "error"
        try:
            with bounded_response(self.port, row["path"], deadline, lambda: self.stopped() or at_end(),
                                  headers={"Accept": "text/event-stream"} if sse else None,
                                  connect_timeout=2) as response:
                row["headers"], row["status"] = self.stamp(), response.status
                if response.status != 200:
                    raise ValueError(f"HTTP {response.status}")
                consume(row, response)
            if not sse:
                row["outcome"] = "ok"
            else:
                row["error"] = "SSE sloot vóór het eind van het venster."
        except ConnectionAbortedError as exc:
            if sse and at_end() and not self.stopped() and row.get("events"):
                row["outcome"] = "ok"
            else:
                row["error"] = str(exc)
        except (OSError, http.client.HTTPException, ValueError) as exc:
            row["error"] = str(exc)
        finally:
            row["closed"] = self.stamp()

    def run_sse(self):
        start, end = self.schedule["sse_window_ms"]
        self.request(self.sse, start, end, self.read_sse, sse=True)

    def read_sse(self, row, response):
        if response.getheader("Content-Type", "").split(";")[0] != "text/event-stream":
            raise ValueError("Ongeldig SSE-contenttype")
        pending_data = False
        while line := response.readline(65537):
            row["wire_bytes"] += len(line)
            if len(line) > 65536 or row["wire_bytes"] > SSE_LIMIT_BYTES:
                raise ValueError("SSE-limiet overschreden")
            if line.startswith(b"data:"):
                pending_data = True
            elif line in (b"\n", b"\r\n"):
                if pending_data:
                    stamp = self.stamp()
                    if last := row.get("last_event"):
                        row["max_event_gap_ms"] = max(row["max_event_gap_ms"], stamp["guest_ms"] - last["guest_ms"])
                    row.setdefault("first_event", stamp)
                    row["last_event"] = stamp
                    row["events"] += 1
                pending_data = False

    def run_gets(self):
        for target in self.schedule["get_targets_ms"]:
            row = {"path": "/0.js", "target_ms": target, "outcome": "not_run"}
            self.gets.append(row)
            self.request(row, target, min(target + GET_PERIOD_MS, WINDOWS["load"][1]), self.read_asset)

    def read_asset(self, row, response):
        body = response.read(ASSET_LIMIT_BYTES + 1)
        if not 1000 < len(body) <= ASSET_LIMIT_BYTES:
            raise ValueError("Ongeldige grootte van webbundel")
        row["last_byte"], row["wire_bytes"] = self.stamp(), len(body)
