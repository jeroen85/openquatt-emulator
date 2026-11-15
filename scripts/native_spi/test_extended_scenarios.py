import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from .export_memory import (
    ExportMeasurement, compare_exports, measure_window, payload_evidence, serial_probe_samples,
)
from .restart import Restart, odu_clock_continuous, reset_control
from .qualification import qualify
from . import test_tooling


class ExportEvidence(unittest.TestCase):
    def test_forecast_validates_payload_and_row_count(self):
        document = {"schema": 2, "kind": "openquatt-forecast-measurements",
                    "observation_columns": ["epoch", "watts"], "observations": [[0, 3000], [10, 2999]],
                    "issues": []}
        body = json.dumps(document).encode()
        self.assertEqual(payload_evidence("forecast", body, 2)["rows"], 2)
        with self.assertRaisesRegex(ValueError, "Ten minste 3"):
            payload_evidence("forecast", body, 3)
        document["observations"].append([20])
        with self.assertRaisesRegex(ValueError, "Onvolledige"):
            payload_evidence("forecast", json.dumps(document).encode(), 2)

    def test_probe_does_not_fill_an_unsampled_transfer_with_adjacent_values(self):
        rows = serial_probe_samples([
            "[I][hil.exp]: mem 160 100 80 80 15 200",
            "[I][hil.exp]: mem 310 99 70 79 10 180 15",
        ])
        phases = measure_window(rows, 200, 250, 100)
        self.assertEqual(phases["during"], {"sample_count": 0, "min_b": {}, "samples": []})
        self.assertNotIn("largest_psram_block_b", phases["before"]["min_b"])
        self.assertEqual(phases["after"]["min_b"]["largest_psram_block_b"], 15)

    def test_seed_waits_for_confirmed_row_count_and_posts_once(self):
        export = ExportMeasurement("forecast", 4321, True, seed=True)
        endpoint = Mock(side_effect=[b"{}", b'{"count":100}', b'{"count":4321}'])
        self.assertFalse(export.prepare(endpoint))
        self.assertTrue(export.prepare(endpoint))
        self.assertTrue(export.prepare(endpoint))
        posts = [call for call in endpoint.call_args_list if call.kwargs.get("method") == "POST"]
        self.assertEqual(len(posts), 1)
        self.assertTrue(export.seed_confirmed)

    def test_comparison_retains_zero_samples_as_missing_evidence(self):
        baseline = {"request": {"payload": {"rows": 16}}, "phases": {
            "during": {"sample_count": 0, "min_b": {}},
        }}
        candidate = {"request": {"payload": {"rows": 17}}, "phases": {
            "during": {"sample_count": 1, "min_b": {"httpd_stack_b": 512}},
        }}
        result = compare_exports(baseline, candidate)
        self.assertEqual(result["phases"]["during"], {
            "reference_samples": 0, "candidate_samples": 1, "delta_b": {},
        })

    def test_invalid_probe_sample_or_prior_boot_is_rejected(self):
        for last in ("300 99 100 79 10 180 15", "300 99 50 81 10 180 15", "30 99 50 79 10 180 15"):
            with self.subTest(last=last), self.assertRaisesRegex(ValueError, "inconsistente"):
                serial_probe_samples(["hil.exp mem 160 100 80 80 15 200", "hil.exp mem " + last])

    def test_missing_probe_samples_cannot_qualify_a_requested_probe(self):
        export = ExportMeasurement("forecast", 1, True)
        export.row = {"outcome": "ok", "start": {"guest_ms": 100, "host_ms": 15},
                      "last_byte": {"guest_ms": 101, "host_ms": 16}, "payload": {"rows": 1}}
        load = Mock(sse={"first_event": {"host_ms": 10}, "last_event": {"host_ms": 20}})
        with tempfile.TemporaryDirectory() as directory:
            serial = Path(directory) / "serial.log"
            serial.write_text("hil.exp mem 110 99 70 79 10 180 15\n")
            result = export.finalize(load, {}, serial)
        self.assertTrue(result["checks"]["export_during_sse"])
        self.assertFalse(result["checks"]["serial_probe_covers_transfer"])


class RestartEvidence(unittest.TestCase):
    def test_hard_reset_rejects_an_unacknowledged_control_command(self):
        with patch("scripts.native_spi.restart.socket.create_connection") as connect:
            client = connect.return_value.__enter__.return_value
            file = client.makefile.return_value.__enter__.return_value
            file.readline.return_value = b"error\n"
            with self.assertRaisesRegex(RuntimeError, "bevestigde"):
                reset_control(19779)
        connect.assert_called_once_with(("127.0.0.1", 19779), timeout=2)
        client.sendall.assert_called_once_with(b"reset\n")

    def test_only_restart_accepts_changes_to_private_flash(self):
        report = test_tooling.EvidenceGates().good()
        report["flash_file_unchanged"] = False
        report["flash_final_sha256"] = "a" * 64
        self.assertFalse(qualify(report)[0]["input"])
        report["scenario"] = "restart"
        self.assertTrue(qualify(report)[0]["input"])
        self.assertTrue(qualify(report)[0]["persistent_flash"])
        report["input_hash_unchanged"] = False
        self.assertFalse(qualify(report)[0]["input"])

    def test_odu_clock_can_continue_through_a_long_ota_and_rejects_a_stall(self):
        request = {"host_ms": 110200, "odu_ms": 110180}
        post = {"host_ms": 321200, "odu_ms": 321153}
        self.assertTrue(odu_clock_continuous(request, post, 0.02))
        self.assertFalse(odu_clock_continuous(request, {**post, "odu_ms": 150000}, 0.02))
        self.assertFalse(odu_clock_continuous(request, post, 11))

    def test_restart_requires_web_assets_and_sensor_of_the_new_boot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "serial.log").write_text("")
            restart = Restart(root / "thermostat", None, "hard", {}, None, None, None, None)
            restart.result["boot_observed"] = {"setting": 760}
            report = {"input": {"application_offset": 16}}
            restart.finalize(report)
            self.assertFalse(restart.result["checks"]["web_after_restart"])
            report.update(web_assets_verified=True, pt1000_verified=True)
            restart.finalize(report)
            self.assertTrue(restart.result["checks"]["web_after_restart"])

    def test_ota_receipt_needs_exact_bytes_in_the_new_booted_part(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = b"ota image"
            identity = {"sha256": hashlib.sha256(app).hexdigest(), "offset": 32, "bytes": len(app)}
            restart = Restart(root / "thermostat", None, "ota", {}, None, None, identity, None)
            restart.ota = Mock(returncode=0)
            report = {"input": {"application_offset": 16}, "setup_count": 2, "clock_last_progress_age_s": 0}
            (root / "serial.log").write_text("Loaded app from partition at offset 0x10\n"
                                             "Loaded app from partition at offset 0x20\n")
            content = root / "flash.bin"
            content.write_bytes(b"x" * 32 + app)
            restart.finalize(report)
            self.assertTrue(restart.result["checks"]["ota_receipt_and_image"])
            content.write_bytes(b"x" * 32 + b"bad image")
            restart.finalize(report)
            self.assertFalse(restart.result["checks"]["ota_receipt_and_image"])
