from contextlib import contextmanager
import gzip
import random
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from .cosim import CosimPeer, fields as bus_fields, message, registration, uint
from .transport import RtuPeer, frame
from .cosim_opentherm import HALF_NS, SECOND, OpenTherm, Outgoing, fields, waveform, word
from .dynamics import wire_evidence
from .qualification import qualify
from .run import run
from .https_scenario import HttpsScenario
import time
from .scenario_load import ScenarioLoad, read_web_asset, regulation_windows


class PortableOpenThermTests(unittest.TestCase):
    def test_gpio_registration_keeps_spi_and_adds_both_links(self):
        packet = bus_fields(registration(ethernet=True, opentherm=True))
        payload = bus_fields(packet[4])
        claim = bus_fields(bus_fields(payload[3])[1])
        self.assertEqual(claim[1], (1 << 9) | (1 << 21) | (1 << 47))
        self.assertEqual(claim[2], (1 << 5) | (1 << 14) | (1 << 48))

    def test_modbus_claim_and_fragmented_uart_reply(self):
        claim = bus_fields(bus_fields(bus_fields(registration(modbus=True))[4])[3])
        self.assertEqual(bus_fields(claim[4]), {1: 40, 2: 42})
        fleet = Mock(now_ms=0, lock=threading.RLock(), online=[True, True],
                     profiles=["V2", "V2"])
        fleet.read.return_value = 0x1234
        cosim = SimpleNamespace(clock_ms=0, modbus=None, registered=True, uart_tx=0,
                                uart_replies=0, send=Mock())
        cosim.modbus = RtuPeer(fleet, lambda: cosim.clock_ms)
        request = frame(bytes([1, 3, 8, 54, 0, 1]))
        for at, chunk in ((50_000_000, request[:3]), (51_000_000, request[3:])):
            CosimPeer.observe(cosim, {1: at, 10: uint(1, 1) + message(2, chunk)})
        reply = frame(bytes([1, 3, 2, 0x12, 0x34]))
        cosim.send.assert_called_once_with(uint(1, 51_000_000) + message(11, message(1, reply)))
        self.assertEqual(cosim.uart_replies, 1)
        with self.assertRaisesRegex(ValueError, "Onverwacht Modbus-frame"):
            CosimPeer.observe(cosim, {1: 90_000_000, 10: uint(1, 2) + message(2, request)})

    def test_complete_valid_frames_and_bad_parity(self):
        for kind in (0, 1, 4, 5, 7):
            for data in (0, 1, 0x5555, 0xaaaa, 0xffff):
                good = word(kind, 24, data)
                for raw, expected in ((good, [(SECOND, good)]), (good ^ (1 << 31), [])):
                    with self.subTest(kind=kind, data=data, raw=raw):
                        decoder = Outgoing()
                        found = []
                        for at, level in waveform(raw, SECOND):
                            decoded = decoder.edge(at, not level)
                            if decoded:
                                found.append(decoded)
                        self.assertEqual(found, expected)

    def test_malformed_pulse_is_not_accepted(self):
        decoder = Outgoing()
        edges = list(waveform(word(5, 16, 5376), SECOND))
        edges[2] = (edges[2][0] + HALF_NS // 3, edges[2][1])
        self.assertFalse(any(decoder.edge(at, not level) for at, level in edges))

    def test_boiler_request_gets_correlated_reply_and_real_wire_evidence(self):
        fixture = OpenTherm(Path("unused"))
        peer = SimpleNamespace(socket=Mock())
        for at, level in waveform(word(0, 25, 0), SECOND):
            fixture.edge(peer, 48, at, not level)
        self.assertEqual([(e["direction"], fields(e["word"])) for e in fixture.events],
                         [("controller-tx", (0, 25, 0)), ("peer-tx", (4, 25, 10880))])
        self.assertEqual(wire_evidence(fixture.events)["otb"]["ack_ids"], [25])

    def test_pending_wire_reply_is_not_received_evidence(self):
        fixture = OpenTherm(Path("unused"))
        fixture.record("otb", "controller-tx", SECOND, word(0, 25, 0))
        fixture.record("otb", "peer-tx", SECOND + 110_000_000, word(4, 25, 10880))
        self.assertEqual(wire_evidence(fixture.delivered(1143))["otb"]["ack_ids"], [])
        self.assertEqual(wire_evidence(fixture.delivered(1144))["otb"]["ack_ids"], [25])
        self.assertEqual(len(fixture.events), 2)

    def test_heat_control_changes_the_transmitted_status_frame(self):
        with tempfile.TemporaryDirectory() as directory:
            control = Path(directory) / "thermostat"
            control.write_text("0\n")
            fixture = OpenTherm(control)
            peer = SimpleNamespace(socket=Mock())
            fixture.tick(peer, 6 * SECOND)
            self.assertEqual(fields(fixture.events[-1]["word"]), (0, 0, 0))
            control.write_text("1\n")
            for index in (7, 8, 9):
                fixture.tick(peer, index * SECOND)
            self.assertEqual(fields(fixture.events[-1]["word"]), (0, 0, 256))
            control.write_text("invalid")
            with self.assertRaises(ValueError):
                fixture.tick(peer, 10 * SECOND)

    def test_regular_firmware_is_rejected_before_starting_a_gpio_scenario(self):
        for scenario in ("dynamics", "power-house", "heating-curve", "lowflow"):
            with self.subTest(scenario=scenario), patch(
                    "scripts.native_spi.run.factory_info", return_value={"project": "openquatt"}), patch(
                    "scripts.native_spi.run.ensure_cosim") as backend:
                with self.assertRaisesRegex(ValueError, "GPIO-labbin"):
                    run(None, backend="cosim", scenario=scenario)
                backend.assert_not_called()

    def test_wire_history_cannot_grow_without_limit(self):
        fixture = OpenTherm(Path("unused"))
        fixture.events = [None] * 100000
        with self.assertRaises(RuntimeError):
            fixture.record("ott", "peer-tx", 0, 0)


class ScenarioLoadTests(unittest.TestCase):
    def test_plain_and_gzipped_web_bundle_are_validated(self):
        plain = b"OpenQuatt" + random.Random(0).randbytes(2048)
        for encoding, body in (("identity", plain), ("gzip", gzip.compress(plain))):
            with self.subTest(encoding=encoding):
                response = Mock(status=200)
                response.read.return_value = body
                headers = {"Content-Length": str(len(body)), "Content-Encoding": encoding}
                response.getheader.side_effect = lambda key, default=None: headers.get(key, default)
                result = read_web_asset(response)
                self.assertTrue(result["ok"])
                self.assertEqual(result["bytes"], len(body))
                self.assertEqual(result["decoded_bytes"], len(plain))

    def test_partial_or_invalid_compressed_bundle_is_rejected(self):
        plain = b"OpenQuatt" + random.Random(0).randbytes(2048)
        compressed = gzip.compress(plain)
        bomb = gzip.compress(plain + b"x" * (4 * 1024 * 1024))
        corrupt = compressed[:10] + b"\xff" + compressed[11:]
        for body, length, encoding in ((compressed[:-8], len(compressed), "gzip"),
                (compressed[:-8], len(compressed) - 8, "gzip"), (bomb, len(bomb), "gzip"),
                (plain, len(plain), "br"), (b"x" * 2048, 2048, "identity"),
                (corrupt, len(corrupt), "gzip")):
            with self.subTest(length=length, encoding=encoding):
                response = Mock(status=200)
                response.read.return_value = body
                headers = {"Content-Length": str(length), "Content-Encoding": encoding}
                response.getheader.side_effect = lambda key, default=None: headers.get(key, default)
                with self.assertRaises((ValueError, EOFError)):
                    read_web_asset(response)

    def test_download_is_not_successful_when_context_deadline_expires(self):
        @contextmanager
        def late_deadline(*args):
            yield Mock()
            raise TimeoutError("HTTP request deadline exceeded")
        deadline = time.monotonic() + 10
        load = ScenarioLoad(0, lambda: 1000, deadline)
        https = HttpsScenario(Mock(identity={}), lambda: 1000, 0, deadline)
        for instance, target, module, rows in (
                (load, load.assets, "scenario_load", load.result["gets"]),
                (https, https.download_assets, "https_scenario", https.assets)):
            with self.subTest(module=module), patch(
                    f"scripts.native_spi.{module}.bounded_response", late_deadline), patch(
                    f"scripts.native_spi.{module}.read_web_asset", return_value={"ok": True}), patch.object(
                    instance.stop, "wait", side_effect=[False, True]):
                target()
                self.assertEqual(len(rows), 1)
                self.assertFalse(rows[0]["ok"])
                self.assertIn("deadline", rows[0]["error"])

    def test_empty_or_nonoverlapping_load_cannot_qualify(self):
        load = ScenarioLoad(12345, lambda: 0, 100)
        load.result.update(clients_stopped=True, sse_events=10, sse_first_ms=0, sse_last_ms=20, sse_times_ms=[0, 10, 20])
        load.result["gets"] = [{"ok": True, "begin_ms": 30, "end_ms": 35}]
        result = load.finalize({"single": (30, 50)})
        self.assertFalse(result["checks"]["overlapping_downloads"])
        self.assertFalse(result["checks"]["single"])

    def test_each_measured_phase_needs_a_download_with_sse(self):
        load = ScenarioLoad(12345, lambda: 0, 100)
        load.result.update(clients_stopped=True, sse_events=10, sse_first_ms=0, sse_last_ms=100, sse_times_ms=[0, 30, 40, 50, 100])
        load.result["gets"] = [{"ok": True, "begin_ms": 30, "end_ms": 35},
                              {"ok": True, "begin_ms": 40, "end_ms": 45}]
        result = load.finalize({"single": (30, 50), "recovered": (60, 80)})
        self.assertTrue(result["checks"]["single"])
        self.assertFalse(result["checks"]["recovered"])
        self.assertFalse(qualify({"duration_s": 0, "scenario_load": result})[0]["scenario_web_load"])

    def test_stalled_stream_cannot_qualify_from_its_endpoints(self):
        load = ScenarioLoad(12345, lambda: 0, 100)
        load.result.update(clients_stopped=True, sse_events=2, sse_first_ms=1,
                           sse_last_ms=600000, sse_times_ms=[1, 600000])
        load.result["gets"] = [{"ok": True, "begin_ms": t, "end_ms": t + 10}
                               for t in (100000, 500000)]
        checks = load.finalize({"duo": (90000, 110000), "recovered": (490000, 510000)})["checks"]
        self.assertFalse(checks["sse_continuous"])
        self.assertFalse(checks["overlapping_downloads"])
        self.assertFalse(checks["duo"])
        self.assertFalse(checks["recovered"])

    def test_sse_must_cover_the_entire_measured_window(self):
        load = ScenarioLoad(12345, lambda: 0, 100)
        load.result.update(clients_stopped=True, sse_events=4, sse_times_ms=[0, 10000, 20000, 30000])
        load.result["gets"] = [{"ok": True, "begin_ms": t, "end_ms": t + 1000}
                               for t in (11000, 21000)]
        self.assertFalse(load.finalize({"recovered": (0, 600000)})["checks"]["recovered"])
        self.assertTrue(all(load.finalize({"recovered": (0, 30000)})["checks"].values()))
        self.assertFalse(load.finalize({"recovered": (-20000, 30000)})["checks"]["recovered"])

    def test_silent_sse_reconnects_and_records_the_failed_session(self):
        load = ScenarioLoad(12345, lambda: 0, 100)
        now = 0
        calls = 0

        def read_line(_):
            nonlocal now
            now += 4.1
            self.assertTrue(cancelled())
            raise ConnectionAbortedError("HTTP request cancelled")

        @contextmanager
        def open_stream(port, path, deadline, cancelled_, **kwargs):
            nonlocal calls, cancelled
            calls += 1
            cancelled = cancelled_
            self.assertEqual(path, "/events")
            if calls == 1:
                yield Mock(status=200, readline=read_line)
            else:
                load.stop.set()
                yield Mock(status=200, readline=Mock(side_effect=[b"data: x", b""]))

        cancelled = None
        with patch("scripts.native_spi.scenario_load.bounded_response", open_stream), patch(
                "scripts.native_spi.scenario_load.time.monotonic", side_effect=lambda: now):
            load.sse()
        first, second = load.result["sse_sessions"]
        self.assertEqual(first["error"], "SSE idle timeout")
        self.assertEqual(second["end_index"], 1)

    def test_overlap_cannot_cross_sse_reconnect_and_tail_stall_fails(self):
        load = ScenarioLoad(12345, lambda: 0, 100)
        load.result.update(clients_stopped=True, sse_events=6, sse_times_ms=[0, 5, 10, 15, 20, 25],
                           sse_sessions=[{"begin_index": 0}, {"begin_index": 3}])
        load.result["gets"] = [{"ok": True, "begin_ms": start, "end_ms": end}
                              for start, end in ((1, 4), (11, 14), (16, 19))]
        checks = load.finalize({"recovered": (0, 25)}, end_ms=12026)["checks"]
        self.assertEqual([row["sse_overlap"] for row in load.result["gets"]], [True, False, True])
        self.assertTrue(checks["recovered"])
        self.assertFalse(checks["sse_continuous"])

    def test_https_requires_real_downloads_within_the_tls_interval(self):
        scenario = HttpsScenario(Mock(identity={}, snapshot=lambda: []), lambda: 0, 0, time.monotonic() + 1)
        scenario.result["checkpoints"] = {"weather": 100, "after": 200}
        scenario.assets = [{"begin_ms": 10, "end_ms": 20, "ok": True},
                           {"begin_ms": 30, "end_ms": 40, "ok": True}]
        scenario.finalize()
        self.assertFalse(scenario.result["checks"]["web_downloads_during_tls"])
        scenario.assets = [{"begin_ms": 110, "end_ms": 120, "ok": True},
                           {"begin_ms": 130, "end_ms": 140, "ok": True}]
        scenario.finalize()
        self.assertTrue(scenario.result["checks"]["web_downloads_during_tls"])
        scenario.assets[-1]["ok"] = False
        scenario.finalize()
        self.assertFalse(scenario.result["checks"]["web_downloads_during_tls"])

    def test_power_house_windows_use_completed_holds(self):
        phases = {name: {"end_ms": end} for name, end in (
            ("low_up", 400000), ("high", 1000000), ("stop", 1400000), ("restart", 1800000))}
        result = {"stage": "complete", "phase_results": phases}
        windows = regulation_windows("power-house", result)
        self.assertEqual(windows["duo"], (970000, 1000000))
        self.assertEqual(windows["recovered"], (1770000, 1800000))
        self.assertIsNone(regulation_windows("power-house", {"stage": "high"}))
