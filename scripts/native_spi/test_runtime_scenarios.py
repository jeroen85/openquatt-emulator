import socket
import unittest
from unittest.mock import Mock

from .energy_boundary import (
    closed_days, closed_totals_match, counters_recovered_once, fixture_time, hours_for, monotonic_day_samples, new_day_starts_clean, parse_history, reconciles,
)
from .mqtt_reconnect import QUIET_RECONNECT_MS, LocalBroker, MqttReconnect, qualifies_overlap
from .pr_workload import PrWebLoad, plan


class EnergyBoundaryTests(unittest.TestCase):
    def test_hour_and_day_edges_follow_amsterdam_local_time(self):
        for case, before, after in (("hour", (15, 11, 52), (15, 12)),
                                    ("day", (15, 23, 52), (16, 0))):
            with self.subTest(case=case):
                start, edge = fixture_time(case)
                self.assertEqual((start.day, start.hour, start.minute), before)
                self.assertEqual((edge.day, edge.hour), after)
                self.assertEqual(edge.timestamp() - start.timestamp(), 480)
                self.assertEqual(edge.utcoffset().total_seconds(), 3600)

    def test_live_hour_replaces_stored_revision_once_and_keeps_marker(self):
        snapshot = parse_history(b"""@schema|3
@enabled|1
@now|1768475100000
@current|20260115|14|14|0|28|0|0|28
4|20260115|1|10|10|0|20|0|0|20
@hour|2|20260115|11|8|8|0|16|0|0|16|0
@hour|3|20260115|12|2|2|0|4|0|0|4|0
@hour|2147483649|20260115|12|6|6|0|12|0|0|12|1
""")
        combined = hours_for(snapshot, 20260115)
        self.assertEqual(sorted(combined), [11, 12])
        self.assertTrue(combined[12]["recovered"])
        self.assertFalse(hours_for(snapshot, 20260115, stored_only=True)[12]["recovered"])
        self.assertTrue(reconciles(snapshot["current"]["wh"], combined))
        self.assertFalse(reconciles(snapshot["days"][0]["wh"], combined))
        self.assertEqual(closed_days(snapshot, 20260115), [])
        snapshot["hours"].append(dict(combined[12]))
        with self.assertRaises(ValueError):
            hours_for(snapshot, 20260115)

    def test_incomplete_hour_rows_do_not_qualify(self):
        with self.assertRaises(ValueError):
            parse_history(b"@schema|3\n@now|123\n@enabled|1\n@hour|5|20260115|12|4\n")

    def test_closed_hour_accepts_a_last_capture_and_rejects_missing_or_doubled_energy(self):
        before = [80, 80, 0, 290, 0, 0, 290]
        closed = [95, 95, 0, 353, 0, 0, 353]
        self.assertTrue(closed_totals_match(before, closed, 2000))
        self.assertFalse(closed_totals_match(before, [x * 2 for x in closed], 2000))
        self.assertFalse(closed_totals_match(before, [0] * 7, 2000))
        self.assertFalse(closed_totals_match([0] * 7, [0] * 7, 2000))

    def test_unclean_reset_loses_unsaved_tail_without_duplicating_checkpoint(self):
        saved = [95, 95, 0, 354, 0, 0, 333]
        before = [125, 125, 0, 480, 0, 0, 459]
        after = [109, 109, 0, 375, 0, 0, 354]
        self.assertTrue(counters_recovered_once(saved, before, after))
        self.assertFalse(counters_recovered_once(saved, before, [78, 78, 0, 273, 0, 0, 273]))
        self.assertFalse(counters_recovered_once(saved, before, [109, 109, 0, 540, 0, 0, 540]))

    def test_new_day_catches_old_totals_even_when_hour_sum_matches(self):
        old = [92, 92, 0, 378, 0, 0, 378]
        new = [3, 3, 0, 8, 0, 0, 8]
        self.assertTrue(reconciles(old, {0: {"wh": old}}))
        snapshot = {"epoch_ms": 1001000, "current": {"wh": old}}
        self.assertFalse(new_day_starts_clean(snapshot, 1000000))
        snapshot["current"]["wh"] = new
        self.assertTrue(new_day_starts_clean(snapshot, 1000000))
        self.assertFalse(monotonic_day_samples([{"wh": old}, {"wh": new}]))
        self.assertTrue(monotonic_day_samples([{"wh": new}, {"wh": old}]))

    def test_a_missing_measured_channel_or_doubled_hour_is_rejected(self):
        totals = [4, 4, 0, 9, 0, -1, 9]
        good = {"wh": [4, 4, 0, 9, 0, -1, 9]}
        self.assertTrue(reconciles(totals, {11: good}))
        self.assertFalse(reconciles(totals, {11: good, 12: good}))
        self.assertFalse(reconciles(totals, {11: {"wh": [4, 4, 0, -1, 0, -1, 9]}}))


class MqttReconnectTests(unittest.TestCase):
    def test_asset_with_missing_last_bytes_does_not_count_as_load(self):
        load = PrWebLoad(1, lambda: 0, 0, 0, lambda: False, False)
        response = Mock()
        response.read.return_value = b"x" * 1200
        response.getheader.return_value = "1201"
        with self.assertRaises(ValueError):
            load.read_asset({}, response)
        response.getheader.return_value = "1200"
        row = {}
        load.read_asset(row, response)
        self.assertEqual(row["wire_bytes"], 1200)

    def test_shifted_load_retains_old_pr_defaults(self):
        old = plan(False)
        new = plan(False, offset_ms=-60000, extend_ms=44000)
        self.assertEqual(old["sse_window_ms"], [120000, 179000])
        self.assertEqual(new["sse_window_ms"], [60000, 163000])
        self.assertEqual(new["get_targets_ms"][0], old["get_targets_ms"][0] - 60000)
        self.assertEqual(new["get_targets_ms"][-1], old["get_targets_ms"][-1] - 60000 + 44000)

    def test_overlap_must_cover_both_sides_and_the_outage(self):
        gets = [
            {"target_ms": t, "start": {"guest_ms": t}, "last_byte": {"guest_ms": t + 1}}
            for t in (5, 12, 16, 35, 39)
        ]
        workload = {"valid": True, "gets": gets, "confirmed_overlap_targets_ms": [5, 12, 16, 35, 39]}
        self.assertTrue(qualifies_overlap(workload, 10, 30, 34))
        self.assertFalse(qualifies_overlap(workload, 10, 30, 38))
        workload["confirmed_overlap_targets_ms"].remove(12)
        self.assertFalse(qualifies_overlap(workload, 10, 30, 34))

    def test_reconnected_session_stays_quiet_before_the_next_publish(self):
        broker = Mock()
        broker.evidence.return_value = {"sessions": [{"id": 2, "subscriptions": {"topic": 1000}}]}
        broker.publish.return_value = {"session": 2, "value": False}
        clock = Mock(return_value=1000)
        scenario = MqttReconnect(None, None, broker, None, clock)
        scenario.result["stage"] = "resubscribe"
        scenario.result["checkpoints"]["initial_publish"] = {"session": 1}
        scenario.topic = "topic"
        scenario.heating_enable = Mock(return_value={"valid": False, "enabled": False})
        get = Mock(return_value=b'{"connected":true}')
        scenario.observe(get)
        clock.return_value += QUIET_RECONNECT_MS - 1
        scenario.observe(get)
        broker.publish.assert_not_called()
        clock.return_value += 1
        scenario.observe(get)
        broker.publish.assert_called_once_with("topic", False, 1)
        self.assertEqual(scenario.result["stage"], "fresh_applied")

    def test_mqtt_failed_status_in_completion_tail_is_rejected(self):
        scenario = MqttReconnect(None, None, Mock(), None, lambda: 1000)
        scenario.result["stage"] = "complete"
        scenario.heating_enable = Mock(return_value={"valid": False, "enabled": False})
        with self.assertRaisesRegex(RuntimeError, "sessie bleef niet geldig"):
            scenario.observe(Mock(return_value=b'{"connected":false}'))

    def test_broker_requires_a_fresh_subscription_and_never_replays_its_value(self):
        broker = LocalBroker(0, lambda: 42)
        self.addCleanup(broker.close)
        broker.start()
        port = broker.listener.getsockname()[1]

        def connect():
            peer = socket.create_connection(("127.0.0.1", port), timeout=1)
            self.addCleanup(peer.close)
            data = peer.makefile("rb")
            self.addCleanup(data.close)
            peer.sendall(b"\x10\x0d\x00\x04MQTT\x04\x02\x00\x1e\x00\x01x")
            self.assertEqual(data.read(4), b"\x20\x02\x00\x00")
            return peer, data

        def subscribe(peer, data):
            peer.sendall(b"\x82\x06\x00\x01\x00\x01t\x00")
            self.assertEqual(data.read(5), b"\x90\x03\x00\x01\x00")

        first, data = connect()
        self.assertIsNone(broker.publish("t", True))
        subscribe(first, data)
        self.assertEqual(broker.publish("t", True)["session"], 1)
        self.assertEqual(data.read(9), b"\x30\x07\x00\x01ttrue")
        broker.set_online(False)
        self.assertEqual(data.read(1), b"")
        self.assertIsNone(broker.publish("t", False, 1))
        broker.set_online(True)
        second, data = connect()
        self.assertIsNone(broker.publish("t", False, 1))
        subscribe(second, data)
        self.assertEqual(broker.publish("t", False, 1)["session"], 2)
        self.assertEqual(data.read(10), b"\x30\x08\x00\x01tfalse")
        self.assertTrue(broker.close())
        evidence = broker.evidence()
        self.assertEqual([item["value"] for item in evidence["publications"]], [True, False])
        self.assertEqual(evidence["session_at_shutdown"], 2)
