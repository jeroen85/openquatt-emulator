"""Offline TLS success, serialization, interruption and recovery evidence."""
from .scenario_load import read_web_asset

import http.client
import json
import threading
import time
import secrets
from urllib.parse import urlencode

from .dynamics import entity, value
from .http_response import bounded_response

MANIFEST_RESPONSE_MS = 60000
AUTOMATIC_BOOT_CHECK_MS = 300000


class HttpsScenario:
    def __init__(self, fixture, clock, port, deadline, reject=False):
        self.fixture, self.clock, self.port, self.deadline = fixture, clock, port, deadline
        self.reject, self.phase_start = reject, 0
        self.stop = threading.Event()
        self.location_token, self.location_sent = secrets.token_hex(16), False
        self.sse_lock, self.sse = threading.RLock(), {"sse_events": 0}
        self.thread = None
        self.asset_thread = None
        self.assets = []
        self.result = {"version": 1, "stage": "configure", "checks": {}, "checkpoints": {}, "samples": [],
                       "profile": "production_trust_rejection" if reject else "local_https_test_ca",
                       "fixture": fixture.identity, "sse_events": 0,
                       "scope": "Local weather/manifest TLS and web. No OTA installation or physical memory qualification."}

    def transition(self, stage, requested_ms=None):
        self.result["stage"] = stage
        self.phase_start = self.clock() if requested_ms is None else requested_ms
        self.result["checkpoints"][stage] = self.phase_start

    def start_sse(self):
        def work():
            try:
                with bounded_response(self.port, "/events", self.deadline, self.stop.is_set) as response:
                    if response.status != 200:
                        raise ValueError("SSE status differs from 200")
                    line = bytearray()
                    while not self.stop.is_set():
                        byte = response.read(1)
                        if not byte:
                            break
                        line.extend(byte)
                        if len(line) > 65536:
                            raise ValueError("SSE line bound exceeded")
                        if byte == b"\n":
                            if line.startswith(b"data:"):
                                now = self.clock()
                                with self.sse_lock:
                                    self.sse.setdefault("sse_first_ms", now)
                                    self.sse["sse_last_ms"] = now
                                    self.sse["sse_events"] += 1
                                    self.sse.setdefault("sse_times_ms", []).append(now)
                                    if len(self.sse["sse_times_ms"]) > 20000:
                                        raise ValueError("SSE event bound exceeded")
                            line.clear()
            except Exception as exc:
                if not self.stop.is_set() and time.monotonic() < self.deadline:
                    with self.sse_lock:
                        self.sse["sse_error"] = str(exc)
        self.thread = threading.Thread(target=work, name="https-web-sse", daemon=True)
        self.thread.start()
        self.asset_thread = threading.Thread(target=self.download_assets, name="https-web-assets", daemon=True)
        self.asset_thread.start()

    def download_assets(self):
        next_ms = 0
        while not self.stop.wait(.1) and time.monotonic() < self.deadline:
            now = self.clock()
            if now < next_ms:
                continue
            next_ms = now + 5000
            row = {"begin_ms": now, "ok": False}
            try:
                with bounded_response(self.port, "/0.js", min(self.deadline, time.monotonic() + 4),
                                      self.stop.is_set) as response:
                    observation = read_web_asset(response)
                row.update(observation)
            except (OSError, EOFError, ValueError, http.client.HTTPException) as error:
                row["error"] = str(error)
            row["end_ms"] = self.clock()
            with self.sse_lock:
                self.assets.append(row)

    def check_manifest(self, get):
        requested_ms = self.clock()
        if get(entity("button", "Check Firmware Updates") + "/press", method="POST") is None:
            raise RuntimeError("Manifest check request failed")
        return requested_ms

    def manifest_attempts(self, events, phase, outcome):
        begin = self.result["checkpoints"].get(phase)
        if begin is None:
            return []
        return [e for e in events if e["role"] == "manifest" and e.get("outcome") == outcome
                and begin <= e.get("connected", {}).get("guest_ms", -1)
                <= e.get("closed", {}).get("guest_ms", -1) <= begin + MANIFEST_RESPONSE_MS
                and e["closed"]["guest_ms"] < AUTOMATIC_BOOT_CHECK_MS]

    def observe(self, get):
        with self.sse_lock:
            self.result.update({k: v.copy() if isinstance(v, list) else v for k, v in self.sse.items()})
        stage, now = self.result["stage"], self.clock()
        if stage in ("complete", "failed"):
            return
        if (not self.reject and stage in ("serialize", "failure", "retry")
                and (now > self.phase_start + MANIFEST_RESPONSE_MS or now >= AUTOMATIC_BOOT_CHECK_MS)):
            self.result.update(stage="failed", violation="Manifestbewijs ontbreekt binnen 60 s na de expliciete aanvraag en vóór de automatische bootcheck.")
            return
        if stage == "configure":
            raw = get(entity("text", "Weather Location"))
            location = json.loads(raw) if raw else {}
            if (location.get("value") != "52.10000,5.10000" or location.get("save_request") != self.location_token
                    or location.get("save_status") != "stored"):
                if not self.location_sent:
                    # One write, then correlate its durable receipt. A missing
                    # HTTP acknowledgement never causes a repeated mutation.
                    self.location_sent = True
                    get(entity("text", "Weather Location") + "/set?" + urlencode({
                        "value": "52.10000,5.10000", "save_request": self.location_token}), method="POST")
                return
            self.result["location_stored"] = True
            if value(get, "switch", "Weather Passive Experiment") is not False:
                get(entity("switch", "Weather Passive Experiment") + "/turn_off", method="POST")
                return
            self.start_sse()
            raw = get(entity("update", "Firmware Update"))
            self.result["initial_manifest_revision"] = json.loads(raw).get("manifest_revision") if raw else None
            self.transition("before")
            return
        events = self.fixture.snapshot()
        self.result["tls_events"] = events
        weather = [e for e in events if e["role"] == "weather"]
        manifest = [e for e in events if e["role"] == "manifest"]
        self.result["samples"].append({"guest_ms": now, "stage": stage,
                                       "weather_failures": value(get, "sensor", "Weather Failure Count"),
                                       "weather_requests": value(get, "sensor", "Weather Request Count")})
        if stage == "before" and now - self.phase_start >= 30000:
            if get(entity("switch", "Weather Passive Experiment") + "/turn_on", method="POST") is not None:
                self.transition("weather")
        elif stage == "weather":
            if self.reject:
                if weather and any(e.get("outcome") == "tls_or_io_error" and "closed" in e for e in weather):
                    requested_ms = self.check_manifest(get)
                    self.transition("reject_manifest", requested_ms)
            elif any("request" in e and "closed" not in e for e in weather):
                requested_ms = self.check_manifest(get)
                self.transition("serialize", requested_ms)
        elif stage == "serialize":
            if now - self.phase_start >= 3000:
                self.fixture.release_weather.set()
            if self.manifest_attempts(events, "serialize", "ok"):
                # Firmware publication must contain the fixture's version.
                raw = get(entity("update", "Firmware Update"))
                revision = json.loads(raw).get("manifest_revision") if raw else None
                age = value(get, "sensor", "Weather Forecast Age")
                if (raw and b"v0.58.1-hil-local" in raw and type(revision) is int
                        and type(self.result["initial_manifest_revision"]) is int
                        and revision > self.result["initial_manifest_revision"]
                        and type(age) in (int, float) and age >= 0):
                    self.result["manifest_published"] = True
                    self.result["first_manifest_revision"] = revision
                    self.transition("failure_wait")
        elif stage == "failure_wait" and now - self.phase_start >= 30000:
            with self.fixture.lock:
                self.fixture.fail_manifest = True
            requested_ms = self.check_manifest(get)
            self.transition("failure", requested_ms)
        elif stage == "failure":
            if self.manifest_attempts(events, "failure", "injected_disconnect"):
                self.transition("retry_wait")
        elif stage == "retry_wait" and now - self.phase_start >= 35000:
            requested_ms = self.check_manifest(get)
            self.transition("retry", requested_ms)
        elif stage == "retry":
            successes = self.manifest_attempts(events, "retry", "ok")
            raw = get(entity("update", "Firmware Update"))
            revision = json.loads(raw).get("manifest_revision") if raw else None
            if (successes and raw and b"v0.58.1-hil-local" in raw and type(revision) is int
                    and revision > self.result.get("first_manifest_revision", revision)):
                self.result["recovered_manifest_revision"] = revision
                self.transition("after")
        elif stage == "reject_manifest":
            if (now - self.phase_start >= 30000
                    and any(e.get("outcome") == "tls_or_io_error" and "closed" in e for e in manifest)):
                self.transition("after")
        elif stage == "after" and now - self.phase_start >= 30000:
            self.transition("complete")

    def windows(self):
        p = self.result["checkpoints"]
        if self.result["stage"] != "complete":
            return None
        return {"before": (p["before"], p["weather"]), "load": (p["weather"], p["after"]),
                "after": (p["after"], p["complete"])}

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(2)
        if self.asset_thread:
            self.asset_thread.join(2)
        return ((self.thread is None or not self.thread.is_alive())
                and (self.asset_thread is None or not self.asset_thread.is_alive()))

    def finalize(self):
        with self.sse_lock:
            self.result.update({k: v.copy() if isinstance(v, list) else v for k, v in self.sse.items()})
        events = self.fixture.snapshot()
        self.result["tls_events"] = events
        points = self.result["checkpoints"]
        checks = {"completed": self.result["stage"] == "complete", "web_stream": self.result["sse_events"] >= 5,
                  "sse_healthy": not self.result.get("sse_error"), "location_stored": self.result.get("location_stored") is True}
        times = self.result.get("sse_times_ms", [])
        load_start, load_end = points.get("weather"), points.get("after")
        with self.sse_lock:
            self.result["web_downloads"] = [dict(row) for row in self.assets]
        checks["web_downloads_during_tls"] = (load_start is not None and load_end is not None
            and sum(row["ok"] and load_start <= row["begin_ms"] <= row["end_ms"] <= load_end
                    for row in self.result["web_downloads"]) >= 2)
        checks["stream_covers_tls"] = (load_start is not None and load_end is not None and bool(times)
                                      and times[0] <= load_start and times[-1] >= load_end
                                      and all(0 <= b-a <= 12000 for a,b in zip(times,times[1:])))
        if self.reject:
            for role in ("weather", "manifest"):
                checks[role + "_rejected"] = any(e["role"] == role and e.get("outcome") == "tls_or_io_error"
                    and "handshake" not in e and e.get("tls_alert") in (
                        "TLSV1_ALERT_UNKNOWN_CA", "SSLV3_ALERT_CERTIFICATE_UNKNOWN", "SSLV3_ALERT_BAD_CERTIFICATE",
                        "TLSV1_ALERT_ACCESS_DENIED") for e in events)
            checks["no_trust_bypass"] = not any("request" in e or "handshake" in e for e in events)
            checks["weather_failure_reported"] = any(type(s.get("weather_failures")) in (int, float)
                and s["weather_failures"] >= 1 for s in self.result["samples"])
        else:
            for role in ("weather", "manifest"):
                checks[role + "_success"] = any(e["role"] == role and e.get("outcome") == "ok" for e in events)
            # A failed parallel handshake still consumed a TLS connection.
            # Include every attempt, not just successful HTTPS requests.
            attempts_complete = bool(events) and all(e.get("role") in ("weather", "manifest")
                and "connected" in e and "closed" in e and "host_s" in e["connected"] and "host_s" in e["closed"]
                and e["closed"]["host_s"] >= e["connected"]["host_s"] for e in events)
            pairs = [(w, m) for w in events for m in events if w["role"] == "weather" and m["role"] == "manifest"]
            checks["serialization_observed"] = attempts_complete and bool(pairs) and all(
                m["connected"]["host_s"] >= w["closed"]["host_s"]
                or m["closed"]["host_s"] <= w["connected"]["host_s"] for w, m in pairs)
            checks["check_requested_during_weather"] = "serialize" in points and any(
                e["role"] == "weather" and "request" in e and "closed" in e
                and e["request"]["guest_ms"] <= points["serialize"] <= e["closed"]["guest_ms"] for e in events)
            checks["explicit_manifest_checks"] = all(self.manifest_attempts(events, phase, outcome)
                for phase, outcome in (("serialize", "ok"), ("failure", "injected_disconnect"), ("retry", "ok")))
            checks["manifest_published"] = (self.result.get("manifest_published") is True
                and "failure_wait" in points and "serialize" in points
                and 0 <= points["failure_wait"] - points["serialize"] <= MANIFEST_RESPONSE_MS
                and points["failure_wait"] < AUTOMATIC_BOOT_CHECK_MS)
            checks["disconnect_exercised"] = bool(self.manifest_attempts(events, "failure", "injected_disconnect"))
            checks["retry_recovered"] = ("after" in points and "retry" in points
                and 0 <= points["after"] - points["retry"] <= MANIFEST_RESPONSE_MS
                and points["after"] < AUTOMATIC_BOOT_CHECK_MS
                and bool(self.manifest_attempts(events, "retry", "ok")))
        self.result["checks"] = checks
