import asyncio
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, Mock

from .frost_pump import FixedFrostPump, PHASES
from .ha_mqtt_probe import NativeApiProbe, ha_overlap
from .mqtt_reconnect import MqttReconnect
from .odu_profile_recovery import P276, PLATE, PLATE_WORDS, OduProfileRecovery, ProfilePeer, p276_value
from .provenance import emulator_checkout, firmware_source
from .pump_postflow import PostflowFleet, approximate_flow, no_restart_commands


class ProvenanceTests(unittest.TestCase):
    def test_only_records_complete_declared_firmware_source(self):
        revision = "a" * 40
        config = "configs/heatpump_controller_q/duo.yaml"
        self.assertIsNone(firmware_source(None, None))
        self.assertEqual(firmware_source(revision, config),
                         {"revision": revision, "config": config, "source": "declared"})
        for rev, path in ((revision[:7], config), (revision, None), (None, config),
                          (revision, "configs/../outside.yaml")):
            with self.subTest(revision=rev, path=path), self.assertRaises(ValueError):
                firmware_source(rev, path)

    def test_git_worktree_status_and_export_without_git(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.assertEqual(emulator_checkout(root), {"revision": None, "dirty": None})
            subprocess.run(("git", "init", "-q", str(root)), check=True)
            git = ("git", "-C", str(root), "-c", "user.name=HIL test", "-c", "user.email=hil@localhost")
            subprocess.run((*git, "commit", "--quiet", "--allow-empty", "-m", "test"), check=True)
            clean = emulator_checkout(root)
            self.assertRegex(clean["revision"], r"^[0-9a-f]{40}$")
            self.assertIs(clean["dirty"], False)
            (root / "local-fixture.txt").write_text("fixture")
            self.assertEqual(emulator_checkout(root), {**clean, "dirty": True})


class FrostTests(unittest.TestCase):
    def test_each_flow_phase_and_both_real_commands_are_required(self):
        frost = FixedFrostPump(None, None)
        for name, flow, span in PHASES:
            frost.samples[name] = [
                {"guest_ms": 20000 + span * i // 4, "mode": "CM98", "outside_c": -2,
                 "flow_lph": flow, "r2015": [800, 800], "pumps_running": [True, True],
                 "compressor_hz": [0, 0]}
                for i in range(5)
            ]
        report = {"writes": [{"unit": u, "at_ms": 22000, "values": [800], "register": 2015,
                              "dropped": False, "exception": 0} for u in (1, 2)]}
        frost.finalize(report)
        self.assertTrue(all(frost.result["checks"].values()))
        frost.samples["zero"][2]["r2015"] = [800, 50]
        report["writes"].pop()
        frost.finalize(report)
        self.assertFalse(frost.result["checks"]["zero_qualified"])
        self.assertFalse(frost.result["checks"]["both_units_received_800"])

    def test_nominal_phase_waits_for_both_commands_after_entering_cm98(self):
        fleet = Mock()
        fleet.lock = threading.RLock()
        fleet.now_ms = 10000
        fleet._pump_commands.return_value = ([True, True], [800, 50], [True, True])
        fleet.snapshot.return_value = [{"frequency_hz": 0}] * 2
        frost = FixedFrostPump(None, fleet)
        frost.result["stage"] = "nominal"

        flow = 0

        def get(path, **kwargs):
            for suffix, value in (("Outside%20Temperature%20%28Selected%29", -2),
                                  ("Control%20Mode%20%28Label%29", "CM98"),
                                  ("Flow%20average%20%28Selected%29", flow)):
                if path.endswith(suffix):
                    return json.dumps({"value": value})
            self.fail(f"unexpected path {path}")

        frost.observe(get)
        self.assertEqual(frost.cm98_entered_ms, 10000)
        self.assertFalse(frost.samples["nominal"])
        flow = 800
        fleet.now_ms = 30000
        fleet._pump_commands.return_value = ([True, True], [800, 800], [True, True])
        frost.observe(get)
        self.assertEqual(frost.samples["nominal"][0]["guest_ms"], 30000)

        late = FixedFrostPump(None, fleet)
        late.result["stage"] = "nominal"
        late.cm98_entered_ms = 10000
        fleet.now_ms = 30001
        with self.assertRaisesRegex(RuntimeError, "20 gastseconden"):
            late.observe(get)
        self.assertFalse(late.samples["nominal"])

    def test_postflow_fixture_interpolates_and_stops_at_800(self):
        self.assertEqual(approximate_flow(440), 558)
        self.assertEqual(approximate_flow(800), 0)
        self.assertEqual(approximate_flow(None), 0)
        self.assertAlmostEqual(approximate_flow(755), 110)


class PostflowTests(unittest.TestCase):
    def test_arming_frequency_replay_does_not_accept_stop_before_withdrawal(self):
        fleet = object.__new__(PostflowFleet)
        fleet.armed = True
        fleet.demand_withdrawal_ms = None
        fleet.stop_ms = None
        fleet.now_ms = 10000
        self.assertTrue(fleet.record_stop(0, 3999, [0], True))
        self.assertIsNone(fleet.stop_ms)
        fleet.demand_withdrawal_ms = fleet.now_ms
        self.assertTrue(fleet.record_stop(1, 3999, [0], True))
        self.assertIsNone(fleet.stop_ms)
        fleet.now_ms += 1
        self.assertTrue(fleet.record_stop(0, 1999, [0], True))
        self.assertEqual(fleet.stop_ms, 10001)

    def test_restart_mode_is_detected_even_at_level_zero(self):
        stop_level = {"register": 1999, "values": [0]}
        self.assertTrue(no_restart_commands([stop_level, {"register": 3999, "values": [0]}]))
        self.assertFalse(no_restart_commands([stop_level, {"register": 3999, "values": [2]}]))
        self.assertFalse(no_restart_commands([{"register": 1999, "values": [1]}]))


class ProfileRecoveryTests(unittest.TestCase):
    def test_peer_reboots_only_between_polls_and_restores_on_guest_clock(self):
        fleet = Mock()
        fleet.lock = threading.RLock()
        fleet.handles = [11, 12]
        fleet.profiles = ["V1.5", "V1.5"]
        fleet.lib.oq_odu_configure.return_value = 1
        fleet.now_ms = 1000
        fleet.advance.side_effect = lambda ms: setattr(fleet, "now_ms", ms)
        clock = Mock(return_value=1000)
        peer = ProfilePeer(fleet, clock)
        peer.arm_reset(400)
        peer.last_hp2_regular_ms, peer.last_hp2_ms = 2000, 2100

        clock.return_value = 3500
        peer.advance_clock()
        self.assertFalse(peer.reset)
        clock.return_value = 3800
        peer.advance_clock()
        self.assertEqual(peer.reset["cut_ms"], 3800)
        fleet.set_odu.assert_called_once_with({"index": 1, "online": False})
        clock.return_value = 4199
        peer.advance_clock()
        self.assertNotIn("restore_ms", peer.reset)
        clock.return_value = 4200
        peer.advance_clock()
        self.assertEqual(peer.reset["restore_ms"], 4200)
        fleet.set_odu.assert_called_with({"index": 1, "online": True})
        self.assertEqual(fleet.lib.oq_odu_configure.call_args.args[::2], (12, 4200))


    def test_requires_ordered_read_write_read_and_no_offline_transition(self):
        def read(unit, register, at, *values):
            return {"unit": unit, "fc": 3, "address": register, "at_ms": at, "ok": True,
                    "count_or_value": len(values), "values": list(values)}

        def write(register, at, value):
            return {"unit": 2, "fc": 6, "address": register, "at_ms": at, "ok": True,
                    "count_or_value": value}

        rows = [read(u, reg, 1000, *vals) for u in (1, 2)
                for reg, vals in ((P276, (4,)), (PLATE, PLATE_WORDS))]
        rows += [read(1, P276, 601000, 4), read(1, PLATE, 601100, *PLATE_WORDS),
                 read(2, P276, 601000, 0), write(P276, 602000, 4), read(2, P276, 603000, 4),
                 read(2, PLATE, 603000, 3, 34, 3)]
        rows += [write(PLATE + i, 604000 + i, value) for i, value in enumerate(PLATE_WORDS)]
        rows += [read(2, PLATE, 605000, *PLATE_WORDS)]
        reset = {"cut_ms": 3000, "restore_ms": 3400,
                 "before": {"defrost": 4, "bottom-plate": PLATE_WORDS},
                 "after": {"defrost": 0, "bottom-plate": [3, 34, 3]}}
        peer = Mock()
        peer.evidence.return_value = (reset, rows)
        scenario = object.__new__(OduProfileRecovery)
        scenario.peer = peer
        scenario.units = (1, 2)
        scenario.result = {"armed_ms": 2000, "samples": [
            {"at_ms": 5000, "hp2_online": True, "cm": "CM98", "operating_mode": 0}
        ]}
        report = {"modbus_link_events": [], "peer": {"offline_frames": []}}
        scenario.finalize(report)
        self.assertTrue(all(scenario.result["checks"].values()), scenario.result["checks"])

        rows[4]["values"] = [0]
        rows[5]["values"] = [3, 34, 3]
        scenario.finalize(report)
        self.assertFalse(scenario.result["checks"]["unchanged_unit_exactly_two_reads_and_no_write"])
        rows[4]["values"], rows[5]["values"] = [4], PLATE_WORDS

        no_change_reset = {**reset, "after": {**reset["after"], "bottom-plate": PLATE_WORDS}}
        unchanged_plate_rows = [row for row in rows if row["fc"] != 6 or row["address"] == P276]
        unchanged_plate_rows[9] = read(2, PLATE, 603000, *PLATE_WORDS)
        peer.evidence.return_value = (no_change_reset, unchanged_plate_rows)
        scenario.finalize(report)
        self.assertFalse(scenario.result["checks"]["bottom_plate_writes_only_changed_values_and_verifies"])
        peer.evidence.return_value = (reset, rows)

        rows[7]["at_ms"] = 610000
        report["modbus_link_events"].append({"guest_ms": 4500})
        scenario.finalize(report)
        self.assertFalse(scenario.result["checks"]["defrost_restored_once_and_verified"])
        self.assertFalse(scenario.result["checks"]["short_reset_without_offline"])

    def test_p276_must_actually_be_in_the_readback_block(self):
        self.assertIsNone(p276_value({"address": P276 - 2, "values": [1, 2]}))
        self.assertEqual(p276_value({"address": P276 - 2, "values": [1, 2, 4]}), 4)


class ConcurrentLoadTests(unittest.TestCase):
    def test_only_full_downloads_inside_confirmed_api_mqtt_sse_sessions_count(self):
        sessions = [{"subscribed_ms": 20, "end_ms": 55}, {"subscribed_ms": 80, "end_ms": 100}]

        def get(start, end):
            return {"target_ms": start, "start": {"guest_ms": start}, "last_byte": {"guest_ms": end}}

        workload = {"valid": True, "confirmed_overlap_targets_ms": [25, 39, 43, 75, 81, 98],
                    "gets": [get(25, 27), get(26, 28), get(39, 41), get(43, 46), get(75, 79), get(81, 89), get(98, 101)]}
        observed = ha_overlap(sessions, workload, 24, 40, 67, 69)
        self.assertEqual([x["target_ms"] for x in observed["before"]], [25])
        self.assertEqual([x["target_ms"] for x in observed["outage"]], [43])
        self.assertEqual([x["target_ms"] for x in observed["reconnected"]], [81])
        self.assertEqual(ha_overlap(sessions[:1], workload, 24, 40, 67, 69),
                         {"before": [], "outage": [], "reconnected": []})

    def test_partial_mqtt_sample_cannot_hide_a_contradiction(self):
        for stage, connected, valid, enabled, reason in (
            ("outage", True, None, None, "broker nog onbereikbaar"),
            ("outage", None, True, None, "broker nog onbereikbaar"),
            ("resubscribe", True, None, True, "oude MQTT-vrijgave"),
            ("complete", False, None, None, "sessie bleef niet geldig"),
        ):
            with self.subTest(stage=stage, connected=connected, valid=valid, enabled=enabled):
                scenario = MqttReconnect(None, None, Mock(), None, lambda: 1000)
                scenario.result["stage"] = stage
                scenario.result["checkpoints"]["outage_confirmed"] = {"guest_ms": 500}
                scenario.heating_enable = Mock(return_value={"valid": valid, "enabled": enabled, "source": None})
                with self.assertRaisesRegex(RuntimeError, reason):
                    scenario.observe(Mock(return_value=json.dumps({"connected": connected})))

    def test_missing_probe_cannot_span_recovery_or_completion(self):
        for stage in ("fresh_applied", "complete"):
            with self.subTest(stage=stage):
                scenario = MqttReconnect(None, None, Mock(), None, lambda: 13500)
                scenario.result["stage"] = stage
                scenario.last_valid_probe_ms = 500
                with self.assertRaisesRegex(RuntimeError, "Te lang geen volledige MQTT-status"):
                    scenario.observe(Mock(return_value=None))

    def test_web_timeout_is_missing_evidence_not_a_valid_mqtt_command(self):
        scenario = MqttReconnect(None, None, Mock(), None, lambda: 10500)
        scenario.result["stage"] = "outage"
        scenario.result["checkpoints"]["outage_confirmed"] = {"guest_ms": 8000}
        scenario.last_valid_probe_ms = 8000
        scenario.heating_enable = Mock(return_value={"valid": None, "enabled": None, "source": "None"})
        get = Mock(return_value=json.dumps({"connected": False}))
        scenario.observe(get)
        self.assertEqual(scenario.result["stage"], "outage")
        self.assertEqual(scenario.result["incomplete_web_samples"], 1)
        self.assertEqual(scenario.last_valid_probe_ms, 8000)
        scenario.heating_enable.return_value = {"valid": True, "enabled": True, "source": "MQTT"}
        with self.assertRaisesRegex(RuntimeError, "MQTT werd geldig"):
            scenario.observe(get)


class ApiTeardownTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_during_provisioning_cleans_up_the_pending_client(self):
        probe = object.__new__(NativeApiProbe)
        probe.stopped = threading.Event()
        probe.zero_psk = "unused-test-key"
        probe.connection_error = OSError
        started = asyncio.Event()

        async def connect(*args, **kwargs):
            started.set()
            await asyncio.Future()

        client = Mock()
        client.connect = connect
        probe.client = Mock(return_value=client)
        probe.disconnect = AsyncMock()
        task = asyncio.create_task(probe.provision())
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        probe.disconnect.assert_awaited_once_with(client)
