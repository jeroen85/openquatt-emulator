"""Regression tests for Power House levels and GPIO flow beside OpenTherm."""
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts.simulator import provenance
from scripts.simulator.runtime import LabCapture, Session, validate_control_state


LEVEL_FIELDS = ("requested_level1", "requested_level2", "applied_level1", "applied_level2")


def flow_marker(tick, edges):
    return {
        "schema": 1, "profile": "oq-local-flow-v1", "pin": 15, "uptime_ms": tick,
        "requested_flow_lph": 800, "enabled": True, "active": True, "generation": 2,
        "requested_generation": 2, "applied_flow_lph": 800, "applied_enabled": True,
        "half_period_us": 1875, "generated_rising_edges": edges, "generated_falling_edges": edges,
        "read_mismatches": 0, "gpio_level": 0, "driver_error": 0,
    }


class BoilerFlowRegression(unittest.TestCase):
    def test_control_levels_accept_0_to_20(self):
        state = {
            "state_schema": 2, "uptime_ms": 1000, "control_mode": 3,
            "hp1_generation": 3, "hp2_generation": 4, "hp1_online": True, "hp2_online": True,
            "flow_valid": True, "flow_lph": 800, "lowflow_fault": False,
            **dict.fromkeys(LEVEL_FIELDS, 0),
        }
        for key in LEVEL_FIELDS:
            for level in range(21):
                with self.subTest(field=key, level=level):
                    self.assertEqual(validate_control_state({**state, key: level})[key], level)
            for level in (-1, 21):
                with self.subTest(field=key, level=level), self.assertRaises(ValueError):
                    validate_control_state({**state, key: level})

    def test_boiler_capture_preserves_both_streams(self):
        session = Session(lab_profile="ot-boiler")
        with tempfile.TemporaryDirectory() as output:
            capture = LabCapture(Path(output), session)
            capture.set_phase("day-measurement")
            boiler = {
                "schema": 1, "profile": "ot-boiler-duplex-v1", "tx_channel": 1, "rx_channel": 6,
                "source_sha256": capture.ot_evidence.source_sha256, "self_test": True, "loopback": "0:4,1:2",
            }
            records = [
                ("OQ_LAB_FLOW", flow_marker(1000, 100)),
                ("OQ_OT_PEER_BUILD", boiler),
                ("OQ_LAB_FLOW", flow_marker(2000, 366)),
            ]
            capture.consume(io.StringIO("".join(f"{tag} {json.dumps(row)}\n" for tag, row in records)))
            self.assertEqual(capture.state()[1], [])
            self.assertEqual(session.ot_telemetry["OQ_OT_PEER_BUILD"], boiler)
            self.assertEqual(session.flow_telemetry["generated_rising_edges"], 366)
            self.assertEqual([row["phase"] for row in session.flow_history], ["day-measurement"] * 2)

    def test_boiler_flow_is_optional_until_seen_and_then_must_stay_fresh(self):
        session = Session(lab_profile="ot-boiler")
        session.process = Mock()
        session.process.poll.return_value = None
        session.telemetry = {"uptime_ms": 12000}
        session.ot_telemetry = {
            "OQ_OT_PEER_BUILD": {}, "OQ_OT_PEER": {"uptime_ms": 12000},
            "OQ_OT_CONTROLLER": {"uptime_ms": 12000},
        }
        with tempfile.TemporaryDirectory() as output:
            capture = session.capture = LabCapture(Path(output), session)
            capture.first_tick_ms = 1000
            capture.state = Mock(return_value=([{"uptime_ms": 12000, "wall_seconds": 0}], [], 1))
            capture.diagnostics.latest = Mock(return_value={"uptime_ms": 12000})
            session._check()
            session.flow_telemetry = flow_marker(7000, 100)
            session._check()
            session.flow_telemetry = flow_marker(6999, 100)
            with self.assertRaisesRegex(RuntimeError, "Local flow fixture telemetry"):
                session._check()

    def test_other_profiles_reject_unexpected_gpio_flow(self):
        for profile in ("duo", "ot-thermostat"):
            with self.subTest(profile=profile), tempfile.TemporaryDirectory() as output:
                session = Session(lab_profile=profile)
                capture = LabCapture(Path(output), session)
                capture.consume(io.StringIO("OQ_LAB_FLOW " + json.dumps(flow_marker(1000, 100)) + "\n"))
                self.assertTrue(any("Unexpected local flow fixture" in error for error in capture.state()[1]))
                self.assertFalse(session.flow_history)

    def test_boiler_build_manifest_detects_changed_gpio_and_clock_inputs(self):
        source_root = Path(provenance.__file__).resolve().parents[2]
        paths = (
            "configs/emulator/cv_virtual_clock.h",
            "scripts/simulator/io/virtual_flow/virtual_flow.cpp",
        )
        sources = provenance.firmware_sources(source_root, "ot-boiler")
        for name in paths:
            with self.subTest(path=name):
                self.assertEqual(sources[name], provenance.digest(source_root / name))

        with tempfile.TemporaryDirectory() as output:
            build = Path(output)
            for name in ("firmware.factory.bin", "firmware.elf"):
                (build / name).touch()
            provenance.write_build_manifest(build, sources, esphome_version="test", root=source_root, profile="ot-boiler")
            provenance.verified_build(build, source_root, "ot-boiler")
            for name in paths:
                with self.subTest(path=name):
                    changed = {**sources, name: "changed"}
                    with patch.object(provenance, "firmware_sources", return_value=changed):
                        with self.assertRaisesRegex(RuntimeError, "Firmwarebronnen gewijzigd"):
                            provenance.verified_build(build, source_root, "ot-boiler")
