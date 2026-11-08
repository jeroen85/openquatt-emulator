"""Scenario failure boundaries and real local TLS lifecycle checks."""
import json
from pathlib import Path
import socket
import ssl
import tempfile
import time
import unittest
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock, patch

from .heating_curve import HeatingCurve, minimum_pair_hold, rundown_hold
from .https_fixture import TlsFixture, prepare
from .https_scenario import HttpsScenario
from .odu_scenarios import command_intervals, OduResponse
from .qualification import qualify
from .run import run, observe_with_deadline


def write(ms, value, unit=1, exception=False):
    return {"at_ms": ms, "register": 1999, "values": [value], "unit": unit, "exception": exception}


class Evidence(unittest.TestCase):
    def test_cooling_flip_between_web_polls_fails_every_heating_scenario(self):
        rows = [{**write(0, 2), "register":3999},write(1000,2)]
        for name in ("heating-curve", "odu-response", "odu-transients"):
            report={"scenario":name,"duration_s":90,"dynamics":{"version":1,"stage":"complete"},"writes":rows.copy()}
            self.assertTrue(qualify(report)[0]["heating_modes"])
            report["writes"] += [{**write(30000,1),"register":3999},{**write(31000,2),"register":3999}]
            self.assertFalse(qualify(report)[0]["heating_modes"])

    def test_old_manifest_success_cannot_replace_explicit_check(self):
        event={"role":"manifest","outcome":"ok","connected":{"guest_ms":40000},"closed":{"guest_ms":41000}}
        s=HttpsScenario(Mock(identity={},snapshot=lambda:[event]),lambda:75000,0,time.monotonic()+1)
        s.result["checkpoints"]={"serialize":73000}
        self.assertEqual(s.manifest_attempts([event],"serialize","ok"),[])
        s.result.update(stage="serialize",initial_manifest_revision=0)
        s.phase_start=73000
        s.observe(lambda *a,**k:b"{}")
        self.assertEqual(s.result["stage"],"serialize")

    def test_lost_explicit_check_cannot_wait_for_automatic_boot_check(self):
        event={"role":"manifest","outcome":"ok","connected":{"guest_ms":315000},"closed":{"guest_ms":316000}}
        s=HttpsScenario(Mock(identity={},snapshot=lambda:[event]),lambda:316000,0,time.monotonic()+1)
        s.result.update(stage="serialize",checkpoints={"serialize":75000})
        s.phase_start=75000
        get=Mock()
        s.observe(get)
        get.assert_not_called()
        self.assertEqual(s.result["stage"],"failed")
        self.assertEqual(s.manifest_attempts([event],"serialize","ok"),[])
        s.result["checkpoints"]["serialize"]=280000
        self.assertEqual(s.manifest_attempts([event],"serialize","ok"),[])

    def test_manifest_trigger_precedes_ack_and_keeps_fast_connection(self):
        clock=[1000]
        s=HttpsScenario(Mock(identity={}),lambda:clock[0],0,time.monotonic()+1)
        def get(*a,**k):clock[0]=2000;return b"{}"
        requested=s.check_manifest(get)
        s.transition("retry",requested)
        event={"role":"manifest","outcome":"ok","connected":{"guest_ms":1100},"closed":{"guest_ms":1500}}
        self.assertEqual(s.manifest_attempts([event],"retry","ok"),[event])

    def test_manifest_evidence_requires_fresh_bounded_first_failure_and_retry(self):
        def event(begin,end,outcome):
            return {"role":"manifest","outcome":outcome,"connected":{"guest_ms":begin},"closed":{"guest_ms":end}}
        events=[event(78000,79000,"ok"),event(109000,110000,"injected_disconnect"),event(147000,149000,"ok")]
        s=HttpsScenario(Mock(identity={},snapshot=lambda:events),lambda:0,0,time.monotonic()+1)
        s.result.update(stage="complete",manifest_published=True,checkpoints={"serialize":73000,
            "failure_wait":80000,"failure":108000,"retry":146000,"after":150000})
        s.finalize()
        self.assertTrue(s.result["checks"]["explicit_manifest_checks"])
        self.assertTrue(s.result["checks"]["manifest_published"])
        self.assertTrue(s.result["checks"]["retry_recovered"])
        events[2]=event(207000,208000,"ok")
        s.finalize()
        self.assertFalse(s.result["checks"]["explicit_manifest_checks"])
        self.assertFalse(s.result["checks"]["retry_recovered"])

    def test_deadline_mid_read_does_not_become_a_missing_entity_error(self):
        now = [0]
        def get(*a, **k): now[0] = 2; return None
        def observe(get):
            if get("/") is None: raise RuntimeError("Entity missing")
        with patch("scripts.native_spi.run.time.monotonic", lambda: now[0]):
            self.assertFalse(observe_with_deadline(observe, get, 1, lambda:False))

    def test_real_scenario_errors_are_not_hidden_as_deadlines(self):
        def observe(get):
            if get("/") is None: raise RuntimeError("Entity missing")
        with self.assertRaisesRegex(RuntimeError,"Entity missing"):
            observe_with_deadline(observe, lambda *_:None, time.monotonic()+5, lambda:False)
    def test_restage_bounds_do_not_turn_poll_latency_into_a_firmware_failure(self):
        for start, clear, explicit, expected, ambiguous in (
            (243000,3000,True,True,False), (241000,3000,True,False,True),
            (240000,3000,True,False,False), (243000,3000,False,True,False),
            (241000,3000,False,False,True), (243000,500,True,False,False)):
            with self.subTest(start=start, clear=clear, explicit=explicit):
                s = HeatingCurve.__new__(HeatingCurve)
                s.result = {"unit": 1, "checkpoints": {"defrost_held": {"guest_ms": 1000},
                    "defrost_clear": {"guest_ms": clear}}}
                if explicit: s.result["defrost_release_ms"] = 1000
                rows = [{**write(0,2,unit=2),"register":3999},write(start,2,unit=2)]
                s.finalize({"writes":rows})
                self.assertEqual(s.result["checks"]["restage_hold_proven"], expected)
                self.assertEqual("measurement_issue" in s.result, ambiguous)
                self.assertNotIn("violation",s.result)

    def test_boot_waits_for_unknown_frequency_instead_of_crashing(self):
        with tempfile.TemporaryDirectory() as tmp:
            fleet = Mock(now_ms=10000, lock=threading.RLock(), oil=False)
            fleet.read.side_effect = lambda *_: 0
            fleet.snapshot.return_value = [{"power_w":0},{"power_w":0}]
            s = OduResponse(Path(tmp)/"heat",fleet)
            s.result["stage"] = "standby"
            sample = {"guest_ms":10000,"observed_from_ms":10000,
                      "odus":[{"requested_level":0},{"requested_level":0}],
                      "firmware":{"ott_valid":True,"heat":False,"frequency_hz":[None,None]}}
            s.sample,s.no_flow = lambda _:sample, lambda _:True
            s.observe(lambda *a,**k:b"{}")
            self.assertEqual(s.result["stage"],"standby")
            sample.update(guest_ms=16000,observed_from_ms=16000)
            sample["firmware"]["frequency_hz"]=[0,0];fleet.now_ms=16000
            s.observe(lambda *a,**k:b"{}")
            self.assertEqual(s.result["stage"],"normal")

    def test_transport_exposes_lost_early_wire_evidence(self):
        from .transport import RtuPeer, frame
        import struct
        fleet=Mock(lock=threading.RLock(),online=[True,True],profiles=["V2 old","V2 old"],now_ms=0)
        fleet.write.return_value=True
        peer=RtuPeer(fleet)
        for i in range(2001):
            peer.feed(frame(bytes([1,6])+struct.pack(">HH",1999,i%2)))
        self.assertEqual(len(peer.write_history()),2000)
        self.assertFalse(peer.snapshot()["write_history_complete"])
        self.assertEqual(peer.snapshot()["write_history_dropped"],1)
    def test_normal_stream_deadline_is_not_a_cleanup_error(self):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                try:
                    for _ in range(100):
                        self.wfile.write(b"data: {}\n\n");self.wfile.flush();time.sleep(.01)
                except OSError:
                    pass
        server = ThreadingHTTPServer(("127.0.0.1",0), Handler)
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.01),daemon=True)
        thread.start()
        try:
            deadline = time.monotonic()+.2
            s = HttpsScenario(Mock(identity={}), lambda: 0, server.server_port, deadline)
            s.start_sse()
            time.sleep(.3)
            self.assertTrue(s.close())
            self.assertNotIn("sse_error", s.sse)
            self.assertGreater(s.sse["sse_events"],0)
        finally:
            server.shutdown();server.server_close();thread.join(1)
    def test_minimum_pair_duration_is_diagnostic(self):
        rows = [{**write(0, 2), "register": 3999}, {**write(0, 2, unit=2), "register": 3999},
                write(0, 1), write(0, 1, unit=2)]
        self.assertEqual(minimum_pair_hold(rows + [write(50000, 0)], 0, 70000)["ms"], 50000)
        self.assertEqual(minimum_pair_hold(rows + [write(60000, 0)], 0, 70000)["ms"], 60000)
        self.assertIsNone(minimum_pair_hold(rows + [write(30000, 2), write(60000, 0)], 0, 70000))
    def test_rundown_hold_does_not_require_both_units_at_minimum(self):
        rows = [{**write(0, 2), "register": 3999}, {**write(0, 2, unit=2), "register": 3999},
                write(0, 3), write(0, 3, unit=2), write(10000, 2), write(40000, 1),
                write(40000, 1, unit=2)]
        self.assertEqual(rundown_hold(rows + [write(70000, 0)], 5000, 80000)["ms"], 60000)
        self.assertEqual(minimum_pair_hold(rows + [write(70000, 0)], 5000, 80000)["ms"], 30000)
        self.assertEqual(rundown_hold(rows + [write(60000, 0)], 5000, 80000)["ms"], 50000)
        self.assertIsNone(rundown_hold(rows + [write(65000, 3), write(65000, 3, unit=2),
                                             write(70000, 0)], 5000, 80000))

    def test_recovery_needs_both_units_running_for_full_minute(self):
        s = OduResponse.__new__(OduResponse)
        def sample(ms, second=2):
            return {"stage":"restart", "guest_ms":ms,"observed_from_ms":ms,
                    "odus":[{"requested_mode":"heating","requested_level":2,"frequency_hz":26},
                            {"requested_mode":"heating","requested_level":second,"frequency_hz":26 if second else 0}],
                    "firmware":{"heat":True,"frequency_hz":[26,26 if second else 0]}}
        s.has_flow = lambda _:True
        samples = [sample(ms) for ms in range(1000,62000,5000)]
        s.result = {"samples":samples,"checkpoints":{"complete":samples[-1]}}
        s.finalize({"writes":[]})
        self.assertTrue(s.result["checks"]["both_running_recovery"])
        samples[-1] = sample(61000,0)
        s.finalize({"writes":[]})
        self.assertFalse(s.result["checks"]["both_running_recovery"])

    def test_early_shedding_cannot_hide_behind_later_valid_rundown(self):
        rows = [{**write(0, 2), "register": 3999}, {**write(0, 2, unit=2), "register": 3999},
                write(0, 3), write(0, 3, unit=2), {**write(6000, 0), "register":3999},
                {**write(7000, 2), "register":3999},write(10000,2),
                {**write(70000,0), "register":3999}]
        self.assertIsNone(rundown_hold(rows,5000,80000))

    def test_rundown_baseline_precedes_the_target_request(self):
        rows = [{**write(0, 2), "register": 3999}, {**write(0, 2, unit=2), "register": 3999},
                write(0,3),write(0,3,unit=2),write(4000,2),write(79000,0)]
        self.assertEqual(rundown_hold(rows,1000,80000)["ms"],75000)
    def test_holds_require_continuous_state_and_bounded_observation_gaps(self):
        scenario = HeatingCurve.__new__(HeatingCurve)
        scenario.result, scenario.holds = {"checkpoints": {}}, {}
        def sample(ms): return {"guest_ms": ms, "observed_from_ms": ms}
        self.assertFalse(scenario.held("maintain_near", sample(1000), True, 120000))
        self.assertFalse(scenario.held("maintain_near", sample(61000), False, 120000))
        self.assertFalse(scenario.held("maintain_near", sample(121000), True, 120000))
        for ms in range(126000, 241000, 5000):
            self.assertFalse(scenario.held("maintain_near", sample(ms), True, 120000))
        self.assertTrue(scenario.held("maintain_near", sample(241000), True, 120000))
        self.assertFalse(scenario.held("maintain_near", sample(261000), True, 120000))

    def test_stopped_active_unit_cannot_count_as_single_recovery(self):
        scenario = HeatingCurve.__new__(HeatingCurve)
        scenario.target, scenario.last_refresh, scenario.holds = 28.5, 100000, {}
        scenario.fleet = Mock(now_ms=100000)
        units = [{"requested_level": 2, "requested_mode": "standby", "frequency_hz": 0},
                 {"requested_level": 0, "requested_mode": "standby", "frequency_hz": 0}]
        sample = {"guest_ms": 100000, "observed_from_ms": 100000, "odus": units,
                  "firmware": {"frequency_hz": [0,0]}}
        scenario.result = {"stage": "single_stable", "samples": [], "unit": 1,
                           "checkpoints": {"single_low_load": {"guest_ms": 1000, "odus": units}}}
        scenario.sample, scenario.has_flow = lambda _: sample, lambda _: True
        scenario.observe(Mock())
        self.assertEqual(scenario.result["stage"], "failed")

    def test_active_mode_stop_between_http_samples_fails_wire_stability(self):
        scenario = HeatingCurve.__new__(HeatingCurve)
        scenario.result = {"unit": 1, "checkpoints": {
            "single_low_load": {"guest_ms": 1000, "odus": [{"requested_level": 2}, {"requested_level": 0}]},
            "single_stable": {"guest_ms": 62000}}}
        scenario.finalize({"writes": [{**write(30000, 0), "register": 3999}]})
        self.assertFalse(scenario.result["checks"]["single_stable_on_wire"])

    def test_only_successful_wire_transitions_count_for_timers(self):
        mode = {**write(0, 2), "register": 3999}
        on, off = command_intervals([mode, write(1000, 2), write(2000, 2), write(3000, 0, exception=True),
                                     write(301000, 0), write(541000, 2)])
        self.assertEqual(on, [{"hp": 1, "ms": 300000}])
        self.assertEqual(off, [{"hp": 1, "ms": 240000}])

    def test_mode_only_stops_and_restarts_cannot_escape_timing_checks(self):
        on, off = command_intervals([{**write(0, 2), "register": 3999}, write(1000, 2),
                                     {**write(2000, 0), "register": 3999},
                                     {**write(3000, 2), "register": 3999}])
        self.assertEqual(on, [{"hp": 1, "ms": 1000}])
        self.assertEqual(off, [{"hp": 1, "ms": 1000}])

    def test_defrost_wire_violation_is_not_hidden_by_clean_http_samples(self):
        scenario = HeatingCurve.__new__(HeatingCurve)
        scenario.result = {"unit": 1, "checkpoints": {
            "defrost_injected": {"observed_from_ms": 1000}, "defrost_clear": {"guest_ms": 5000}}}
        scenario.finalize({"writes": [write(2000, 2, unit=2)]})
        self.assertFalse(scenario.result["checks"]["peer_blocked_on_wire"])
        self.assertEqual(len(scenario.result["blocked_peer_writes"]), 1)

    def test_missing_recovery_and_stream_gap_cannot_pass(self):
        fixture = Mock(identity={}, snapshot=lambda: [])
        scenario = HttpsScenario(fixture, lambda: 0, 12345, time.monotonic()+1)
        scenario.result.update(stage="complete", sse_events=10)
        scenario.sse.update(sse_events=10, sse_times_ms=[0, 100000])
        scenario.result["checkpoints"] = {"weather": 1000, "after": 99000}
        scenario.finalize()
        self.assertFalse(scenario.result["checks"]["stream_covers_tls"])
        self.assertFalse(scenario.result["checks"]["retry_recovered"])
        self.assertFalse(scenario.result["checks"]["serialization_observed"])

    def test_https_rejects_unsupported_route_before_resources_start(self):
        with self.assertRaisesRegex(ValueError, "HTTPS requires"):
            run(Path("missing.bin"), scenario="https", network="wifi")
        with self.assertRaisesRegex(ValueError, "belongs"):
            run(Path("missing.bin"), https_fixture=Path("missing"))

    def test_failed_parallel_tls_handshake_is_still_overlap(self):
        def event(role, begin, end, ok=True):
            return {"role": role, "connected": {"host_s": begin}, "closed": {"host_s": end},
                    "outcome": "ok" if ok else "tls_or_io_error"}
        events = [event("weather", 1, 20), event("manifest", 2, 12, False), event("manifest", 21, 22)]
        fixture = Mock(identity={}, snapshot=lambda: events)
        scenario = HttpsScenario(fixture, lambda: 0, 12345, time.monotonic()+1)
        scenario.finalize()
        self.assertFalse(scenario.result["checks"]["serialization_observed"])
        events.pop(1)
        scenario.finalize()
        self.assertTrue(scenario.result["checks"]["serialization_observed"])

    def test_trust_rejection_cannot_hide_a_successful_handshake(self):
        events = [{"role": role, "outcome": "tls_or_io_error", "tls_alert": "TLSV1_ALERT_UNKNOWN_CA"}
                  for role in ("weather", "manifest")]
        fixture = Mock(identity={}, snapshot=lambda: events)
        scenario = HttpsScenario(fixture, lambda: 0, 12345, time.monotonic()+1, reject=True)
        scenario.finalize()
        self.assertTrue(scenario.result["checks"]["no_trust_bypass"])
        events.append({"role": "weather", "handshake": {}, "outcome": "tls_or_io_error"})
        scenario.finalize()
        self.assertFalse(scenario.result["checks"]["no_trust_bypass"])
        events[:] = [{"role": "weather", "outcome": "tls_or_io_error", "error": "timeout"}]
        scenario.finalize()
        self.assertFalse(scenario.result["checks"]["weather_rejected"])


class LocalTls(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.root = Path(cls.directory.name)
        (cls.root / "configs/heatpump_controller_q").mkdir(parents=True)
        (cls.root / "configs/heatpump_controller_q/duo.yaml").write_text("packages: {}\n")
        import subprocess
        subprocess.run(["git", "init", "-q", str(cls.root)], check=True)
        subprocess.run(["git", "-C", str(cls.root), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                        "commit", "--allow-empty", "-qm", "test"], check=True)
        cls.fixture_path = cls.root / "tls"
        prepare(cls.root, cls.fixture_path)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def setUp(self):
        self.fixture = TlsFixture(self.fixture_path, lambda: 10000)
        self.fixture.release_weather.set()
        self.addCleanup(self.fixture.close)

    def request(self, context):
        with socket.create_connection(("127.0.0.1", self.fixture.port), timeout=2) as raw:
            with context.wrap_socket(raw, server_hostname="github.com") as sock:
                sock.sendall(b"GET /local-manifest HTTP/1.1\r\nHost: github.com\r\n\r\n")
                out = b""
                while part := sock.recv(4096):
                    out += part
                return out

    def test_trust_failure_is_preserved(self):
        with self.assertRaises(ssl.SSLCertVerificationError):
            self.request(ssl.create_default_context())
        self.assertTrue(self.fixture.close())
        events = self.fixture.snapshot()
        self.assertEqual(len(events), 1)
        self.assertNotIn("request", events[0])
        self.assertNotIn("handshake", events[0])

    def test_trusted_download_recovers_after_disconnect(self):
        context = ssl.create_default_context(cafile=str(self.fixture_path / "ca.pem"))
        self.fixture.fail_manifest = True
        self.assertEqual(self.request(context), b"")
        body = self.request(context)
        self.assertIn(b"HTTP/1.1 200", body)
        self.assertIn(b"v0.58.1-hil-local", body)
        self.assertTrue(self.fixture.close())
        self.assertEqual([e["outcome"] for e in self.fixture.snapshot()], ["injected_disconnect", "ok"])

    def test_silent_client_cannot_keep_fixture_alive(self):
        sock = socket.create_connection(("127.0.0.1", self.fixture.port), timeout=2)
        self.addCleanup(sock.close)
        until = time.monotonic()+2
        while not self.fixture.snapshot() and time.monotonic()<until:
            time.sleep(.01)
        began = time.monotonic()
        self.assertTrue(self.fixture.close())
        self.assertLess(time.monotonic()-began, 2)
