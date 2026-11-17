"""Web/SSE load and fixed recorder windows during the regulation scenarios."""
import bisect
import gzip
import hashlib
import io
import http.client
import threading
import time
import zlib

from .http_response import bounded_response



def read_web_asset(response):
    """Validate the wire body and bounded gzip content of the embedded bundle."""
    body = response.read(2 * 1024 * 1024 + 1)
    length = response.getheader("Content-Length")
    encoding = response.getheader("Content-Encoding", "").lower()
    if (response.status != 200 or not 1000 < len(body) <= 2 * 1024 * 1024
            or length is not None and int(length) != len(body)
            or encoding not in ("", "identity", "gzip")):
        raise ValueError("Incomplete or invalid web bundle response")
    content = body
    if encoding == "gzip" or body.startswith(b"\x1f\x8b"):
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(body)) as compressed:
                content = compressed.read(4 * 1024 * 1024 + 1)
        except zlib.error as error:
            raise ValueError("Corrupt gzip web bundle") from error
    if len(content) > 4 * 1024 * 1024 or b"OpenQuatt" not in content:
        raise ValueError("Invalid decoded web bundle")
    return {"ok": True, "bytes": len(body), "decoded_bytes": len(content),
            "sha256": hashlib.sha256(body).hexdigest(), "content_encoding": encoding}


def regulation_windows(scenario, result):
    if result["stage"] != "complete":
        return None
    if scenario == "power-house":
        phases = result["phase_results"]
        endpoints = {name: phases[phase]["end_ms"] for name, phase in (
            ("single", "low_up"), ("duo", "high"), ("stop", "stop"), ("recovered", "restart"))}
    else:
        points = result["checkpoints"]
        endpoints = {name: points[point]["guest_ms"] for name, point in (
            ("defrost", "defrost_held"), ("duo", "maintain_near_held"), ("recovered", "single_stable"))}
    return {"startup": (0, 30000), **{name: (end - 30000, end) for name, end in endpoints.items()}}


class ScenarioLoad:
    def __init__(self, port, clock, deadline):
        self.port, self.clock, self.deadline = port, clock, deadline
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.threads = []
        self.result = {"sse_events": 0, "sse_times_ms": [], "gets": [], "scope": "SSE and web assets during regulation"}

    def start(self):
        if self.threads:
            return
        for target in (self.sse, self.assets):
            worker = threading.Thread(target=target, daemon=True)
            self.threads.append(worker)
            worker.start()

    def sse(self):
        try:
            with bounded_response(self.port, "/events", self.deadline, self.stop.is_set) as response:
                if response.status != 200:
                    raise ValueError("SSE response status")
                for line in iter(lambda: response.readline(65537), b""):
                    if len(line) > 65536:
                        raise ValueError("SSE line bound exceeded")
                    if line.startswith(b"data:"):
                        with self.lock:
                            now = self.clock()
                            times = self.result["sse_times_ms"]
                            if len(times) >= 100000:
                                raise ValueError("SSE event history bound exceeded")
                            times.append(now)
                            self.result.setdefault("sse_first_ms", now)
                            self.result["sse_last_ms"] = now
                            self.result["sse_events"] += 1
                if not self.stop.is_set():
                    raise EOFError("SSE stream ended before the test completed")
        except (OSError, EOFError, ValueError, http.client.HTTPException) as error:
            if not self.stop.is_set() and time.monotonic() < self.deadline:
                with self.lock:
                    self.result["sse_error"] = str(error)

    def assets(self):
        next_ms = 0
        while not self.stop.wait(.1) and time.monotonic() < self.deadline:
            if self.clock() < next_ms:
                continue
            begin = self.clock()
            next_ms = begin + 10000
            row = {"begin_ms": begin, "ok": False}
            try:
                with bounded_response(self.port, "/0.js", min(self.deadline, time.monotonic() + 4),
                                      self.stop.is_set) as response:
                    observation = read_web_asset(response)
                row.update(observation)
            except (OSError, EOFError, ValueError, http.client.HTTPException) as error:
                row["error"] = str(error)
            row["end_ms"] = self.clock()
            with self.lock:
                self.result["gets"].append(row)

    def close(self):
        self.stop.set()
        for thread in self.threads:
            thread.join(3)
        self.result["clients_stopped"] = all(not thread.is_alive() for thread in self.threads)
        return self.result["clients_stopped"]

    def finalize(self, windows):
        with self.lock:
            times = self.result["sse_times_ms"]
            for row in self.result["gets"]:
                before = bisect.bisect_right(times, row["begin_ms"]) - 1
                after = bisect.bisect_left(times, row["end_ms"])
                row["sse_overlap"] = (0 <= before < len(times) and after < len(times)
                    and all(0 <= b - a <= 12000 for a, b in zip(
                        times[before:after + 1], times[before + 1:after + 1])))
            rows = [row for row in self.result["gets"] if row["ok"] and row["sse_overlap"]]
            checks = {"clients_stopped": self.result.get("clients_stopped") is True,
                      "sse": self.result["sse_events"] >= 2 and not self.result.get("sse_error"),
                      "sse_continuous": len(times) >= 2 and all(0 <= b - a <= 12000
                          for a, b in zip(times, times[1:])),
                      "overlapping_downloads": len(rows) >= 2}
            if windows:
                for phase, (begin, end) in windows.items():
                    if phase != "startup":
                        window_times = times[bisect.bisect_left(times, begin):bisect.bisect_right(times, end)]
                        covered = (bool(window_times) and window_times[0] - begin <= 12000
                                   and end - window_times[-1] <= 12000)
                        checks[phase] = covered and any(
                            begin <= row["begin_ms"] <= row["end_ms"] <= end for row in rows)
            else:
                checks["memory_windows"] = False
            self.result["checks"] = checks
        return self.result
