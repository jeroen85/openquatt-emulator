"""Boundary checks for factory admission and the loopback control surface."""
import hashlib
import http.client
import json
from pathlib import Path
import struct
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from http.server import ThreadingHTTPServer
from . import factory, server, build


def image(payload, address=0x3c200020):
    header = bytearray(24)
    header[0:2] = bytes((0xe9, 1))
    header[12:14] = (9).to_bytes(2, "little")
    header[23] = 1
    result = header + struct.pack("<II", address, len(payload)) + payload
    checksum = 0xef
    for value in payload:
        checksum ^= value
    result += b"\0" * ((len(result) | 15) - len(result)) + bytes((checksum,))
    return bytes(result) + hashlib.sha256(result).digest()


def merged():
    desc = bytearray(256)
    struct.pack_into("<I", desc, 0, 0xabcd5432)
    desc[16:20] = b"test"
    desc[48:52] = b"test"
    desc[112:116] = b"idf5"
    app = image(desc)
    data = bytearray(b"\xff" * (0x20000 + len(app)))
    boot = image(b"boot", 0x403c0000)
    data[:len(boot)] = boot
    for index, (kind, subtype, offset, size, label) in enumerate(((1, 2, 0x9000, 0x10000, b"nvs"), (0, 0x10, 0x20000, 0x10000, b"app0"))):
        row = struct.pack("<HBBII16sI", 0x50aa, kind, subtype, offset, size, label, 0)
        data[0x8000 + index * 32:0x8020 + index * 32] = row
    data[0x8040:0x8060] = b"\xeb\xeb" + b"\xff" * 14 + hashlib.md5(data[0x8000:0x8040]).digest()
    data[0x20000:] = app
    return data


class Admission(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "factory.bin"
        self.data = merged()

    def info(self, data=None):
        self.path.write_bytes(self.data if data is None else data)
        return factory.factory_info(self.path)

    def test_valid_factory_checks_every_layer(self):
        info = self.info()
        self.assertEqual(info["application_offset"], 0x20000)
        self.assertTrue(info["partition_md5_verified"])
        self.assertTrue(info["boot"]["appended_sha256_verified"])
        self.assertTrue(info["application"]["appended_sha256_verified"])
        self.assertEqual(info["sha256"], hashlib.sha256(self.data).hexdigest())

    def test_corruption_and_truncation_fail_closed(self):
        for offset in (0, 12, 32, 63, 0x805f, 0x20020, len(self.data) - 1):
            with self.subTest(offset=offset):
                bad = self.data.copy()
                bad[offset] ^= 1
                with self.assertRaises(ValueError):
                    self.info(bad)
        for end in (32, 0x9000, 0x20020, len(self.data) - 1):
            with self.subTest(end=end), self.assertRaises(ValueError):
                self.info(self.data[:end])

    def test_ota_only_rejected(self):
        with self.assertRaises(ValueError):
            self.info(self.data[0x20000:])

    def test_overlapping_partitions_rejected_even_with_valid_md5(self):
        struct.pack_into("<I", self.data, 0x8024, 0x10000)
        self.data[0x8050:0x8060] = hashlib.md5(self.data[0x8000:0x8040]).digest()
        with self.assertRaisesRegex(ValueError, "Overlapping"):
            self.info()

    def test_unknown_emulator_rejected(self):
        self.path.write_bytes(b"different emulator")
        with self.assertRaisesRegex(ValueError, "Unsupported emulator"):
            build.verify_original(self.path)


class Loopback(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.lab_patch = patch.object(server, "LAB", Path(self.directory.name))
        self.lab_patch.start()
        self.addCleanup(self.lab_patch.stop)
        self.manager = server.Manager()
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.http.manager = self.manager
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.finish)

    def finish(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(2)
        self.manager.stop()

    def request(self, path, body=b"{}", **headers):
        connection = http.client.HTTPConnection("127.0.0.1", self.http.server_port, timeout=3)
        default = {"X-Simulator-Token": self.manager.token}
        default.update(headers)
        connection.request("POST", path, body=body, headers=default)
        response = connection.getresponse()
        result = response.status, json.loads(response.read())
        connection.close()
        return result

    def test_foreign_origin_host_and_missing_token_rejected(self):
        for headers in ({"Origin": "https://foreign.invalid"}, {"Host": "foreign.invalid"}, {"X-Simulator-Token": ""}):
            with self.subTest(headers=headers):
                self.assertEqual(self.request("/api/start", **headers)[0], 403)
        self.assertIsNone(self.manager.process)

    def test_invalid_json_types_are_client_errors(self):
        for path in ("/api/start", "/api/stop"):
            for body in (b"[]", b"null", b"true", b"\"text\"", b"{"):
                with self.subTest(path=path, body=body):
                    self.assertEqual(self.request(path, body)[0], 400)
        self.assertIsNone(self.manager.process)

    def test_upload_sanitizes_name_and_resets_previous_results(self):
        self.manager.run_path = Path(self.directory.name) / "old-result"
        status, result = self.request("/api/upload", merged(), **{"X-Filename": "..%2FQ%20Duo.factory.bin"})
        self.assertEqual(status, 200)
        self.assertEqual(result["name"], "Q Duo.factory.bin")
        self.assertIsNone(self.manager.state()["details"])
        self.assertIsNone(self.manager.run_path)
        status, _ = self.request("/api/upload", b"broken", **{"X-Filename": "broken.bin"})
        self.assertEqual(status, 400)
        self.assertEqual(self.manager.selected["sha256"], result["sha256"])

    def test_tampered_selected_input_cannot_start(self):
        self.manager.choose(merged(), "valid.bin")
        self.manager.selected["path"].chmod(0o600)
        self.manager.selected["path"].write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "gewijzigd"):
            self.manager.start("12345678-1234-1234-1234-123456789abc", 90, 30)
        self.assertIsNone(self.manager.process)

    def test_stale_stop_cannot_target_current_job(self):
        self.manager.job_id = "new-job"
        with self.assertRaises(server.Conflict):
            self.manager.stop("old-job")


    def test_duplicate_stop_does_not_signal_process_twice(self):
        process = Mock()
        process.poll.return_value = None
        self.manager.process = process
        self.manager.stop()
        self.manager.stop()
        process.terminate.assert_called_once()


class EvidenceGates(unittest.TestCase):
    def good(self):
        return {"setup_complete": True, "web_checks": 2, "pt1000_verified": True,
                "peer": {"recent": [{"unit": n, "fc": 3, "exception": 0, "dropped": False} for n in (1, 2)],
                         "error": None, "crc_errors": 0, "exceptions": 0, "dropped": 0, "unknown_addresses": 0},
                "duration_s": 90, "elapsed_wall_s": 90.2, "web_last_ok_s": 89,
                "emulator_stop_requested": True, "cleanup_exit_code": 0,
                "input_hash_unchanged": True, "flash_file_unchanged": True,
                "cleanup": {"process_stopped": True, "reader_stopped": True, "peer_stopped": True}}

    def test_healthy_complete_run_qualifies(self):
        from .qualification import qualify
        gates, reasons = qualify(self.good())
        self.assertTrue(all(gates.values()))
        self.assertEqual(reasons, [])

    def test_partial_success_and_lifecycle_errors_do_not_qualify(self):
        from .qualification import qualify
        mutations = ({"cancelled": True}, {"elapsed_wall_s": 30}, {"cleanup": {}},
                     {"emulator_stop_requested": False}, {"exit_before_cleanup": 0}, {"cleanup_exit_code": 78},
                     {"web_last_ok_s": 20}, {"modbus_timing_warnings": ["late reply"]},
                     {"model_errors": [{"message": "DMA write failed"}]}, {"flash_file_unchanged": False})
        for change in mutations:
            with self.subTest(change=change):
                gates, reasons = qualify({**self.good(), **change})
                self.assertFalse(all(gates.values()))
                self.assertTrue(reasons)
        report = self.good()
        report["peer"]["error"] = "Unexpected UART EOF"
        self.assertFalse(qualify(report)[0]["uart"])

    def test_ethernet_gate_requires_dhcp_ack_beyond_ip_and_frames(self):
        from .qualification import qualify

        report = self.good()
        report["network"] = {
            "mode": "ethernet", "ethernet_checks": 2, "ethernet_ip": "10.42.0.15",
            "wire": {"tx": 5, "rx": 3, "dhcp_acks": 0},
        }
        self.assertFalse(qualify(report)[0]["ethernet"])
        report["network"]["wire"]["dhcp_acks"] = 1
        self.assertTrue(qualify(report)[0]["ethernet"])


    def test_failover_requires_recovery_after_the_cable_cut(self):
        from .qualification import qualify

        report = self.good()
        report.update(ports={"http": 19600}, web_port=19600, wifi_recovery_event_s=62)
        network = {
            "mode": "failover", "ethernet_checks": 3, "ethernet_ip": "10.42.0.15",
            "wire": {"tx": 5, "rx": 3, "dhcp_acks": 1, "cable": False, "link": False},
            "wifi_checks": 5, "wifi_recovery_checks": 2, "automatic_request_ack": True,
            "cable_cut_s": 60, "wifi_restored_s": 65, "ethernet_http_lost": True,
        }
        report["network"] = network
        self.assertTrue(qualify(report)[0]["failover"])
        self.assertFalse(qualify({**report, "wifi_recovery_event_s": 59})[0]["failover"])
        self.assertFalse(qualify({**report, "wifi_recovery_event_s": 95})[0]["failover"])
        for change in ({"wifi_recovery_checks": 1}, {"wifi_restored_s": 59}, {"ethernet_http_lost": False}):
            self.assertFalse(qualify({**report, "network": {**network, **change}})[0]["failover"])
        network["wire"]["link"] = True
        self.assertFalse(qualify(report)[0]["failover"])


    def test_device_gate_rejects_a_frozen_model_clock(self):
        from .qualification import qualify

        report = self.good()
        report.update(mode="device", odu_uptime_ms=3900, guest_uptime_ms=4000, clock_last_progress_age_s=0.1)
        first, second = report["peer"]["recent"]
        first["at_ms"] = 3650
        second["at_ms"] = 3650
        self.assertFalse(qualify(report)[0]["odu_clock"])
        first["at_ms"] = 3000
        self.assertTrue(qualify(report)[0]["odu_clock"])
        self.assertFalse(qualify({**report, "clock_last_progress_age_s": 55})[0]["odu_clock"])
        report["guest_uptime_ms"] = 6000
        self.assertFalse(qualify(report)[0]["odu_clock"])

    def test_opentherm_checks_require_the_correct_direction_and_timely_write_echo(self):
        from .dynamics import wire_evidence

        request = {"link": "ott", "direction": "peer-tx", "guest_us": 100000, "word": 0x10101500}
        reply = {"link": "ott", "direction": "controller-tx", "guest_us": 195000, "word": 0x50101500}
        self.assertEqual(wire_evidence([request, reply])["ott"]["ack_ids"], [16])
        for changed in ({"word": 0x50101400}, {"link": "otb"}, {"guest_us": 900000},
                        {"guest_us": 120000}, {"direction": "peer-tx"}):
            self.assertEqual(wire_evidence([request, {**reply, **changed}])["ott"]["ack_ids"], [])

    def test_dynamics_gate_requires_both_links_at_the_end_of_the_run(self):
        from .qualification import qualify

        report = self.good()
        report.update(guest_uptime_ms=428000, dynamics={"stage": "complete", "wire": {
            "ott": {"ack_ids": [0, 16, 24], "last_ack_ms": 427000},
            "otb": {"ack_ids": [0, 1, 25], "last_ack_ms": 426000},
        }})
        self.assertTrue(all(qualify(report)[0].values()))
        for link in ("ott", "otb"):
            evidence = report["dynamics"]["wire"][link]
            for stale in (None, 345000, 429000):
                with self.subTest(link=link, stale=stale):
                    previous = evidence["last_ack_ms"]
                    evidence["last_ack_ms"] = stale
                    self.assertFalse(qualify(report)[0][link])
                    evidence["last_ack_ms"] = previous


class LowflowEvidence(unittest.TestCase):
    @staticmethod
    def write(when, register, values, *, unit=1, exception=0, dropped=False):
        return {"unit": unit, "at_ms": when, "register": register, "values": values,
                "exception": exception, "dropped": dropped}

    def setUp(self):
        self.dynamics = {
            "unit": 1,
            "checkpoints": {
                "fault": {"guest_ms": 381000, "firmware": {"defrost": True, "frequency_hz": [26, 20]}},
                "stopped": {"guest_ms": 393000, "firmware": {"defrost": True}},
                "fault_held": {},
                "restart": {},
            },
            "lowflow": {
                "fault_event": {"uptime_s": 380}, "stop_event": {"uptime_s": 392},
                "clear_window_ms": [501000, 503000],
                "fault_elapsed_window_ms": [59000, 63000],
                "recovery_elapsed_window_ms": [29000, 33000],
            },
        }
        self.writes = [self.write(382000, 3999, [0]), self.write(383000, 1999, [0]),
                       self.write(633000, 1999, [1]), self.write(633500, 1999, [1], unit=2)]

    def gates(self):
        from .lowflow import lowflow_gates

        return {key: passed for key, (passed, _) in lowflow_gates(self.dynamics, self.writes).items()}

    def test_stop_hold_and_automatic_restart(self):
        self.assertTrue(all(self.gates().values()))

    def test_stop_requires_both_acknowledged_registers_with_defrost_active(self):
        self.writes[0]["exception"] = 4
        self.assertFalse(self.gates()["lowflow_defrost_stop"])
        self.writes[0]["exception"] = 0
        self.dynamics["checkpoints"]["stopped"]["firmware"]["defrost"] = False
        self.assertFalse(self.gates()["lowflow_defrost_stop"])

    def test_multi_register_enable_to_either_unit_is_prohibited_while_latched(self):
        self.writes.insert(2, self.write(400000, 3998, [0, 2], unit=2, dropped=True))
        self.assertFalse(self.gates()["lowflow_held"])

    def test_other_unit_cannot_restart_during_release_window_or_minimum_off_time(self):
        for when, gate in ((502000, "lowflow_held"), (505000, "lowflow_restart")):
            with self.subTest(when=when):
                self.writes[-1]["at_ms"] = when
                self.assertFalse(self.gates()[gate])

    def test_restart_fails_during_minimum_off_time(self):
        for when in (631000, 632998):
            with self.subTest(when=when):
                self.writes[-2]["at_ms"] = when
                self.assertFalse(self.gates()["lowflow_restart"])

    def test_defrost_feedback_cannot_delay_recording_an_early_fault_clear(self):
        from .lowflow import Lowflow, matches_timer, RECOVER_MS

        def sample(when, flow, fault):
            return {
                "observed_from_ms": when - 100, "guest_ms": when,
                "firmware": {"ott_valid": True, "heat": True, "flow_lph": flow, "lowflow_fault": fault,
                             "defrost": True, "valve": True, "frequency_hz": [0, 0]},
                "odus": [{"requested_level": 0, "requested_mode": "standby", "frequency_hz": 0}] * 2,
            }

        scenario = Lowflow(None, None)
        scenario.result["stage"] = "wait_clear"
        scenario.previous = sample(100000, 0, True)
        scenario.evidence["restore_ms"] = 100000
        scenario.observe_flow(None, "wait_clear", sample(102000, 600, True), 0)
        scenario.observe_flow(None, "wait_clear", sample(104000, 600, False), 0)

        self.assertEqual(scenario.result["stage"], "wait_clear")
        self.assertFalse(matches_timer(scenario.evidence["recovery_elapsed_window_ms"], RECOVER_MS))
        running = sample(106000, 600, False)
        running["firmware"]["frequency_hz"][0] = 26
        with self.assertRaisesRegex(RuntimeError, "Een compressor kwam op gang"):
            scenario.observe_flow(None, "wait_clear", running, 0)
        with self.assertRaisesRegex(RuntimeError, "keerde terug"):
            scenario.observe_flow(None, "wait_clear", sample(108000, 600, True), 0)
        released = sample(136000, 600, False)
        released["firmware"].update(defrost=False, valve=False)
        scenario.observe_flow(None, "wait_clear", released, 0)
        self.assertEqual(scenario.result["stage"], "wait_restart")
        self.assertFalse(matches_timer(scenario.evidence["recovery_elapsed_window_ms"], RECOVER_MS))

    def test_timer_intervals_reject_early_late_and_imprecise_measurements(self):
        from .lowflow import matches_timer

        for interval in ([52000, 59000], [67000, 68000], [60000, 59000], [53000, 66000]):
            with self.subTest(interval=interval):
                self.assertFalse(matches_timer(interval, 60000))


class MemoryEvidence(unittest.TestCase):
    def setUp(self):
        from .memory import FIELDS

        self.recording = {
            "format": "openquatt-debug-device-v1", "encoding": "device-psram-delta-json-v1",
            "columns": list(FIELDS), "units": [[index, unit] for index, (_, unit) in enumerate(FIELDS.values())],
            "initial": [[0, 1000], [1, 100000], [2, 40000], [3, 50000]],
            "recording": {"interval_s": 10, "sample_count": 15, "active": True},
            "samples": [[0, []]] + [[i * 10, [[0, i * 10000 + 1000]]] for i in range(1, 15)],
        }
        self.recording["samples"][4][1].extend([[1, 90000], [2, 35000], [3, 45000]])
        self.dynamics = {
            "checkpoints": {
                "standby": {"observed_from_ms": 25000},
                "defrost": {"guest_ms": 30000},
                "restart": {"guest_ms": 110000},
            },
            "lowflow": {"fault_event": {"uptime_s": 64}, "restore_ms": 70000, "clear_window_ms": [104000, 106000]},
        }

    def summarize(self, guest_ms=142000, windows=None):
        from .memory import lowflow_windows, summarize

        return summarize(self.recording, lowflow_windows(self.dynamics) if windows is None else windows, guest_ms)

    def test_phase_summaries_decode_deltas_and_keep_the_since_boot_watermark(self):
        result = self.summarize()
        self.assertTrue(result["complete"])
        startup, defrost, recovery, restart = result["phases"].values()
        self.assertEqual([row["sample_count"] for row in (startup, defrost, recovery, restart)], [3, 4, 4, 3])
        self.assertEqual(startup["heapmin_at_last_sample_b"], 40000)
        self.assertEqual(defrost["lowest_free_heap_b"], 90000)
        self.assertEqual(recovery["lowest_largest_block_b"], 45000)
        self.assertEqual(restart["heapmin_at_last_sample_b"], 35000)

    def test_normal_run_covers_startup_and_runtime_without_lowflow_checkpoints(self):
        from .memory import normal_windows

        self.recording["samples"] = self.recording["samples"][:6]
        self.recording["recording"]["sample_count"] = 6
        windows = normal_windows(9000, 58000)
        self.assertEqual(windows, {"startup": (0, 20000), "normal": (20000, 58000)})
        result = self.summarize(58000, windows)
        self.assertTrue(result["complete"])
        self.assertEqual(result["phases"]["startup"]["sample_count"], 2)
        self.assertEqual(result["phases"]["normal"]["sample_count"], 4)
        self.assertEqual(result["phases"]["normal"]["lowest_free_heap_b"], 90000)

    def test_normal_run_needs_thirty_seconds_after_late_web_readiness(self):
        from .memory import normal_windows

        self.assertIsNone(normal_windows(45000, 74999))
        self.assertEqual(normal_windows(45000, 75000)["normal"], (45000, 75000))

    def test_missing_measurements_inside_recovery_prevent_qualification(self):
        self.recording["samples"].pop(8)
        self.recording["recording"]["sample_count"] -= 1
        result = self.summarize()
        self.assertFalse(result["phases"]["lowflow_recovery"]["covered"])
        from .qualification import qualify

        report = EvidenceGates().good()
        report["memory"] = result
        self.assertFalse(qualify(report)[0]["memory"])

    def test_missing_heap_field_or_invalid_clock_cannot_supply_evidence(self):
        with self.assertRaisesRegex(ValueError, "firmwareklok"):
            self.summarize(160000)
        self.recording["initial"].pop()
        with self.assertRaisesRegex(ValueError, "onvolledig"):
            self.summarize()

    def test_slower_recorder_cadence_cannot_supply_comparable_evidence(self):
        self.recording["recording"]["interval_s"] = 20
        self.recording["samples"] = self.recording["samples"][::2]
        self.recording["recording"]["sample_count"] = len(self.recording["samples"])
        with self.assertRaisesRegex(ValueError, "elke 10 seconden"):
            self.summarize()

    def test_invalid_value_or_watermark_is_rejected(self):
        changes = self.recording["samples"][8][1]
        for value in (True, None, 40000):
            with self.subTest(value=value):
                changes.append([2, value])
                with self.assertRaises(ValueError):
                    self.summarize()
                changes.pop()


if __name__ == "__main__":
    unittest.main()
