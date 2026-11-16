from pathlib import Path
import unittest
from unittest.mock import Mock

from .safety_scenarios import DewGuard, OduLinkLoss, SourceLoss, confirmed_stops, started_between


def command(unit, when, register, values, *, dropped=False, exception=0):
    return {"unit": unit, "at_ms": when, "register": register,
            "values": values, "dropped": dropped, "exception": exception}


class SafetyEvidence(unittest.TestCase):
    def test_room_temperature_is_refreshed_during_long_waits(self):
        fleet = Mock(now_ms=10000)
        scenario = SourceLoss(Path("heat"), fleet)
        get = Mock(return_value=b"")
        scenario.refresh_room_temperature(get)
        fleet.now_ms = 249999
        scenario.refresh_room_temperature(get)
        self.assertEqual(get.call_count, 1)
        fleet.now_ms = 250000
        scenario.refresh_room_temperature(get)
        self.assertEqual(get.call_count, 2)
        self.assertTrue(all(call.args[0].endswith("value=19") for call in get.call_args_list))

    def test_both_acknowledged_stop_commands_must_be_for_the_active_unit(self):
        rows = [command(1, 10, 1999, [0]), command(2, 11, 3999, [0])]
        self.assertFalse(confirmed_stops(rows, 1, 0, 20))
        rows[1]["unit"] = 1
        self.assertTrue(confirmed_stops(rows, 1, 0, 20))
        rows[1]["dropped"] = True
        self.assertFalse(confirmed_stops(rows, 1, 0, 20))

    def test_source_loss_fails_for_even_one_hidden_start_during_block(self):
        source = SourceLoss(Path("heat"), None)
        source.result.update(paused_at_ms=100, running_units=[1])
        source.result["checkpoints"].update(
            invalid={"guest_ms": 110, "firmware": {"frequency_hz": [30, 0]},
                     "odus": [{"requested_level": 1, "frequency_hz": 30},
                              {"requested_level": 0, "frequency_hz": 0}]},
            stopped={"guest_ms": 140}, blocked={"guest_ms": 180},
            api_selected={"guest_ms": 200}, api_restarted={}, restored_ott={},
        )
        writes = [command(1, 131, 1999, [0]), command(1, 135, 3999, [0])]
        source.finalize({"writes": writes})
        self.assertTrue(all(source.result["checks"].values()))
        source.finalize({"writes": writes + [command(2, 120, 1999, [1])]})
        self.assertFalse(source.result["checks"]["only_retained_minimum_after_loss"])
        writes.append(command(2, 190, 3998, [0, 2], dropped=True))
        source.finalize({"writes": writes})
        self.assertFalse(source.result["checks"]["no_enable_while_blocked"])

    def test_only_the_deliberately_disconnected_unit_can_time_out(self):
        scenario = OduLinkLoss(Path("heat"), None, None)
        frame = {"unit": 2, "at_ms": 120, "fc": 3, "register": 2099, "count": 1}
        recovery = {**frame, "at_ms": 1100, "dropped": False, "exception": 0}
        scenario.result.update(cut_ms=100, restore_ms=1000, hp1_max_read_gap_ms=10000, recovery_reply=recovery)
        scenario.result["checkpoints"].update(duo={}, watchdog_stop={}, recovered={})
        warning = {"line": "Stop waiting for response from 2 500000us after last send", "guest_ms": 650}
        loss = {"line": "HP2 - Modbus OFFLINE", "guest_ms": 700}
        restored = {"line": "HP2 - Modbus ONLINE", "guest_ms": 1120}
        report = {
            "peer": {"offline_frames": [frame], "dropped": 1, "recent": []},
            "modbus_timing_events": [warning], "modbus_timing_warnings": [warning["line"]],
            "modbus_link_events": [loss, restored],
        }
        scenario.finalize(report)
        self.assertTrue(all(scenario.result["checks"].values()))
        warning["line"] = warning["line"].replace("from 2", "from 1")
        loss["guest_ms"] = 99
        restored["guest_ms"] = 1099
        scenario.finalize(report)
        self.assertFalse(scenario.result["checks"]["planned_modbus_timeouts_only"])
        self.assertFalse(scenario.result["checks"]["firmware_detected_outage"])
        self.assertFalse(scenario.result["checks"]["second_unit_recovers_and_resumes_polling"])
        frame["at_ms"] = 1001
        scenario.finalize(report)
        self.assertFalse(scenario.result["checks"]["planned_drops_only"])
        frame.update(at_ms=120, count=40)
        scenario.finalize(report)
        self.assertTrue(scenario.result["checks"]["planned_drops_only"])
        self.assertFalse(scenario.result["checks"]["offline_probe_attempted"])

    def test_hp2_recovery_needs_an_answer_after_reconnection_then_a_normal_poll(self):
        reply = {"unit": 2, "at_ms": 1100, "fc": 3, "register": 2099, "count": 1,
                 "dropped": False, "exception": 0}
        poll = {**reply, "at_ms": 1200, "count": 40}
        before = [
            {"reads": 2, "watchdog_counter": 0, "fault_words": [0, 0, 0], "frequency_hz": 0},
            {"reads": 1, "watchdog_counter": 1200, "fault_words": [0, 0, 1], "frequency_hz": 0},
        ]
        after = [
            {**before[0], "reads": 3},
            {**before[1], "reads": 4, "fault_words": [0, 0, 0], "watchdog_counter": 1, "frequency_hz": 20},
        ]
        for frames, passes in (
            ([reply, poll], True),
            ([{**reply, "fc": 6, "register": 3999}, poll], True),
            ([{**reply, "fc": 6, "register": 3999, "unit": 1}, poll], False),
            ([{**reply, "at_ms": 999}, poll], False),
            ([{**reply, "exception": 2}, poll], False),
            ([reply, {**poll, "dropped": True}], False),
            ([reply], False),
        ):
            with self.subTest(frames=frames):
                fleet = Mock()
                fleet.snapshot.return_value = [{"protocol": {"reads": row["reads"]}, **row} for row in after]
                peer = Mock()
                peer.snapshot.return_value = {"recent": frames}
                scenario = OduLinkLoss(Path("heat"), fleet, peer)
                scenario.result.update(stage="restore", restore_ms=1000)
                scenario.result["checkpoints"]["watchdog_stop"] = {"odu_link": before}
                sample = {"observed_from_ms": 1150, "guest_ms": 1500,
                          "firmware": {"flow_lph": 800, "frequency_hz": [0, 20]}}
                scenario.sample = Mock(return_value=sample)
                scenario.observe(None)
                self.assertEqual(scenario.result["stage"], "restore")
                sample["observed_from_ms"] = 1400
                scenario.observe(None)
                self.assertEqual(scenario.result["stage"] == "complete", passes)
                if passes:
                    self.assertEqual(scenario.result["recovery_reply"], frames[0])
                    self.assertEqual(scenario.result["resumed_poll"], poll)

    def test_early_hp1_answers_do_not_cover_a_later_gap_in_modbus_polls(self):
        scenario = OduLinkLoss(Path("heat"), None, None)
        scenario.result["hp1_latest_read_ms"] = 100
        reads = [{"unit": 1, "at_ms": 1000}, {"unit": 1, "at_ms": 9000}, {"unit": 1, "at_ms": 11000}]
        scenario.observe_hp1_reads(reads, 30000)
        self.assertEqual(scenario.result["hp1_max_read_gap_ms"], 19000)
        with self.assertRaises(RuntimeError):
            scenario.observe_hp1_reads(reads + [{"unit": 2, "at_ms": 31000}], 32000)

    def dew_run(self):
        scenario = DewGuard(Path("heat"), Mock(), "rise")
        cooling = {
            "odus": [{"requested_mode": "cooling", "frequency_hz": 30}],
            "firmware": {"frequency_hz": [30, 0], "valid": True, "dew": 10, "supply": 18, "floor": 11},
        }
        scenario.result.update(unit=1, started_injection_ms=100000, stop_event={"uptime_s": 120})
        scenario.result["checkpoints"].update(
            cooling={"guest_ms": 10000}, unsafe={**cooling, "observed_from_ms": 109000, "guest_ms": 110000},
            stopped={"guest_ms": 121000}, blocked={"guest_ms": 150000},
            safe={"guest_ms": 175000, "firmware": dict(cooling["firmware"])}, restarted={**cooling, "guest_ms": 380000},
        )
        writes = [
            command(1, 5000, 3999, [1]), command(1, 118000, 1999, [0]),
            command(1, 119000, 3999, [0]), command(1, 360000, 3999, [1]),
        ]
        return scenario, writes

    def test_cooling_cannot_start_on_the_other_unit_during_minimum_off_time(self):
        scenario, writes = self.dew_run()
        scenario.finalize({"writes": writes})
        self.assertTrue(all(scenario.result["checks"].values()))
        too_early = command(2, 359999, 3999, [1])
        scenario.finalize({"writes": writes[:-1] + [too_early, writes[-1]]})
        self.assertFalse(scenario.result["checks"]["minimum_off_before_any_cooling_start"])
        self.assertEqual(started_between([too_early], 120000, 360000), [too_early])

    def test_dew_stop_evidence_must_follow_observed_unsafe_cooling(self):
        scenario, writes = self.dew_run()
        scenario.result["stop_event"]["uptime_s"] = 108
        writes[1]["at_ms"] = 107500
        writes[2]["at_ms"] = 108000
        scenario.finalize({"writes": writes})
        self.assertFalse(scenario.result["checks"]["stop_on_wire"])
        scenario.result["checkpoints"]["unsafe"]["observed_from_ms"] = 99000
        scenario.finalize({"writes": writes})
        self.assertFalse(scenario.result["checks"]["unsafe_point_observed"])

    def test_dew_minimum_runtime_is_measured_from_the_first_cooling_command(self):
        scenario, writes = self.dew_run()
        scenario.result["stop_event"]["uptime_s"] = 305
        self.assertLess(305000 - scenario.result["checkpoints"]["cooling"]["guest_ms"], 300000)
        scenario.finalize({"writes": writes})
        self.assertFalse(scenario.result["checks"]["confirmed_high_dew_overrides_minimum_runtime"])

    def test_late_dew_recovery_cannot_hide_a_restart_before_safe_readback(self):
        scenario, writes = self.dew_run()
        scenario.result["checkpoints"]["safe"]["guest_ms"] = 365000
        scenario.finalize({"writes": writes})
        self.assertTrue(scenario.result["checks"]["minimum_off_before_any_cooling_start"])
        self.assertFalse(scenario.result["checks"]["no_enable_during_guard"])
        self.assertFalse(scenario.result["checks"]["safe_cooling_restored"])

    def test_dew_recovery_command_must_match_the_restarted_unit(self):
        scenario, writes = self.dew_run()
        writes[-1]["unit"] = 2
        scenario.finalize({"writes": writes})
        self.assertFalse(scenario.result["checks"]["safe_cooling_restored"])

    def test_stop_writes_can_overlap_an_unsafe_sample_of_a_running_unit(self):
        scenario, writes = self.dew_run()
        writes[1]["at_ms"] = 109500
        writes[2]["at_ms"] = 109501
        scenario.finalize({"writes": writes})
        self.assertTrue(scenario.result["checks"]["stop_on_wire"])
        scenario.result["checkpoints"]["unsafe"]["odus"][0]["frequency_hz"] = 0
        scenario.finalize({"writes": writes})
        self.assertFalse(scenario.result["checks"]["unsafe_point_observed"])

    def test_safe_dew_must_still_be_valid_at_the_cooling_restart(self):
        scenario, writes = self.dew_run()
        scenario.result["checkpoints"]["restarted"]["firmware"] = {
            "frequency_hz": [30, 0], "valid": True, "dew": 10, "supply": 18, "floor": 19,
        }
        scenario.finalize({"writes": writes})
        self.assertFalse(scenario.result["checks"]["safe_cooling_restored"])

    def test_last_retained_minimum_inside_the_stop_second_is_not_a_start(self):
        scenario, writes = self.dew_run()
        scenario.case = "loss"
        scenario.result["checkpoints"]["unsafe"]["firmware"]["permitted"] = False
        retained = command(1, 120500, 1999, [1])
        scenario.finalize({"writes": writes[:-1] + [retained, writes[-1]]})
        self.assertTrue(all(scenario.result["checks"].values()))
        retained["unit"] = 2
        scenario.finalize({"writes": writes[:-1] + [retained, writes[-1]]})
        self.assertFalse(scenario.result["checks"]["no_enable_during_guard"])

        retained["unit"] = 1
        scenario.result["checkpoints"]["stopped"]["guest_ms"] = 120100
        scenario.finalize({"writes": writes[:-1] + [retained, writes[-1]]})
        self.assertFalse(scenario.result["checks"]["no_enable_during_guard"])
