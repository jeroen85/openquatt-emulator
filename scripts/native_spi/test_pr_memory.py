"""Focused regression checks for fixed-window PR memory evidence."""

import copy
import unittest

from .memory import FIELDS
from .memory_comparison import METRICS, runtime_fingerprint
from .pr_memory import assessment, markdown, repeat_assessment, summarize_pr_recording
from .pr_workload import WINDOWS, plan, summarize_workload


def stamp(ms):
    return {"host_ms": ms, "guest_ms": ms}


def workload(without_overlap=False):
    schedule = plan(without_overlap)
    begin, end = schedule["sse_window_ms"]
    sse = {
        "headers": stamp(begin + 30), "first_event": stamp(begin + 40), "last_event": stamp(end - 50),
        "closed": stamp(end), "outcome": "ok",
        "events": 180, "max_event_gap_ms": 9000,
    }
    gets = [
        {"target_ms": target, "start": stamp(target + 30), "last_byte": stamp(target + 80),
         "closed": stamp(target + 90), "outcome": "ok"}
        for target in schedule["get_targets_ms"]
    ]
    return schedule, sse, gets


def report(firmware="a" * 64, *, drop=0, separated=False, run_id=None):
    schedule, sse, gets = workload(separated)
    evidence = summarize_workload(schedule, sse, gets, [], 32000)
    return {
        "scenario": "pr-memory", "mode": "device", "network": {"mode": "wifi"}, "duration_s": 240,
        "fixture_temperature_c": 30.0, "input": {"chip": "esp32s3", "sha256": firmware},
        "sources": {name: "a" * 64 for name in
                    ("memory.py", "model.py", "network_probe.py", "transport.py",
                     "run.py", "pr_workload.py", "pr_memory.py", "http_response.py")},
        **dict.fromkeys(("executable_sha256", "extension_sha256", "odu_library_sha256"), "f" * 64),
        "native_build": {"slirp": "4.9.5"},
        "qualification": {"memory": True, "pr_memory_workload": evidence["valid"]},
        "failure_reasons": evidence["issues"],
        "pr_memory": {"run_id": run_id or firmware, "plan": schedule, "workload": evidence,
                      "hardware_profile": "q-duo", "reset_path": "fresh"},
        "memory": {
            "complete": True, "interval_ms": 10000, "source": "openquatt_debug_recorder",
            "capabilities": "MALLOC_CAP_INTERNAL", "samples": [],
            "phases": {name: {"covered": True, "sample_count": 3, "window_ms": list(window),
                              **dict(zip(METRICS, (70000 - drop, 40000 - drop, 60000 - drop)))}
                       for name, window in WINDOWS.items()},
        },
    }


def reference(value, sha="1" * 64):
    return {"report": value, "path": "/local/report.json", "report_sha256": sha}


class PrMemoryEvidence(unittest.TestCase):
    def test_boundary_sample_belongs_to_only_the_next_phase(self):
        recording = {
            "format": "openquatt-debug-device-v1", "encoding": "device-psram-delta-json-v1",
            "columns": list(FIELDS), "units": [[i, unit] for i, (_, unit) in enumerate(FIELDS.values())],
            "initial": [[0, 10000], [1, 70000], [2, 40000], [3, 50000]],
            "recording": {"interval_s": 10, "sample_count": 4, "active": True},
            "samples": [[0, []], [10, [[0, 20000]]],
                        [20, [[0, 30000], [1, 60000]]], [30, [[0, 40000]]]],
        }
        phases = summarize_pr_recording(recording, {"before": (10000, 30000), "load": (30000, 50000)}, 40000)["phases"]
        self.assertEqual(phases["before"]["window_ms"], [10000, 30000])
        self.assertEqual(phases["before"]["lowest_free_heap_b"], 70000)
        self.assertEqual(phases["load"]["lowest_free_heap_b"], 60000)
        self.assertTrue(all(phase["covered"] for phase in phases.values()))
        edge = summarize_pr_recording(recording, {"before": (10000, 32501)}, 40000)["phases"]["before"]
        self.assertTrue(edge["covered"])
        edge = summarize_pr_recording(recording, {"load": (30000, 52501)}, 40000)["phases"]["load"]
        self.assertFalse(edge["covered"])

    def test_comparison_can_be_retried_but_requires_valid_measurements(self):
        baseline, candidate = report(), report("b" * 64)
        candidate["qualification"]["pr_memory_comparison"] = False
        candidate["failure_reasons"] = ["De oude baselinehash week af."]
        self.assertEqual(assessment(candidate, reference(baseline), "a" * 64)["status"], "measured")
        for invalid in (False, "False"):
            with self.subTest(invalid=invalid):
                candidate["qualification"]["memory"] = invalid
                self.assertEqual(assessment(candidate, reference(baseline), "a" * 64)["status"], "uncomparable")

    def test_delivering_stream_and_successful_gets_prove_overlap(self):
        schedule, sse, gets = workload()
        result = summarize_workload(schedule, sse, gets, [], 32000)
        self.assertTrue(result["valid"], result["issues"])
        self.assertEqual(result["confirmed_overlap_targets_ms"], schedule["get_targets_ms"])

    def test_sequential_clients_are_uncomparable_even_when_both_work(self):
        schedule, sse, gets = workload(without_overlap=True)
        result = summarize_workload(schedule, sse, gets, [], 32000)
        self.assertFalse(result["valid"])
        self.assertEqual(result["socket_overlap_targets_ms"], [])
        self.assertEqual(result["confirmed_overlap_targets_ms"], [])
        self.assertIn("na elkaar", result["issues"][0])

    def test_queued_or_undelivered_stream_cannot_count_as_overlap(self):
        schedule, sse, gets = workload()
        late = {**sse, "first_event": stamp(179000)}
        self.assertFalse(summarize_workload(schedule, late, gets, [], 32000)["valid"])
        stalled = {**sse, "max_event_gap_ms": 30000}
        self.assertFalse(summarize_workload(schedule, stalled, gets, [], 32000)["valid"])
        self.assertFalse(summarize_workload(schedule, sse, gets, [[89000, 91000]], 32000)["valid"])
        self.assertFalse(summarize_workload(schedule, sse, gets[1:], [], 32000)["valid"])
        self.assertFalse(summarize_workload(schedule, sse, gets, [], 91000)["valid"])
        late_close = {**sse, "closed": stamp(WINDOWS["after"][0] + 1)}
        self.assertFalse(summarize_workload(schedule, late_close, gets, [], 32000)["valid"])

    def test_comparison_requires_expected_firmware_and_same_realized_load(self):
        baseline, candidate = report(), report("b" * 64)
        ref = reference(baseline)
        self.assertEqual(assessment(candidate, ref, "c" * 64)["status"], "uncomparable")
        self.assertEqual(assessment(candidate, ref, "a" * 64)["status"], "measured")
        candidate["pr_memory"]["workload"]["sse"]["events"] *= 2
        self.assertEqual(assessment(candidate, ref, "a" * 64)["status"], "uncomparable")
        candidate = report("b" * 64)
        candidate["pr_memory"]["reset_path"] = "warm"
        self.assertEqual(assessment(candidate, ref, "a" * 64)["status"], "uncomparable")
        self.assertIn("pr_workload.py", runtime_fingerprint(baseline))

    def test_single_drop_requests_repeat_and_shared_baseline_counts_once(self):
        baseline, candidate = report(), report("b" * 64, drop=4096)
        first = assessment(candidate, reference(baseline), "a" * 64, 2048)
        self.assertEqual(first["status"], "repeat")
        shared = repeat_assessment(first, {**first, "candidate_run_id": "candidate-2"})
        self.assertEqual(shared["independent_baselines"], 1)
        self.assertEqual(shared["status"], "repeat")
        second = assessment(report("b" * 64, drop=4096, run_id="candidate-2"),
                            reference(report(run_id="baseline-2"), "2" * 64), "a" * 64, 2048)
        repeated = repeat_assessment(first, second)
        self.assertEqual(repeated["status"], "regression_signal")
        self.assertEqual(repeated["repeat_spread_b"]["load/lowest_free_heap_b"], [-4096, -4096])

        mixed = copy.deepcopy(second)
        mixed["exceeded"] = []
        self.assertEqual(repeat_assessment(first, mixed)["status"], "repeat")
        reused_boot = {**second, "candidate_run_id": first["reference_run_id"]}
        self.assertEqual(repeat_assessment(first, reused_boot)["status"], "repeat")
        other_profile = {**second, "measurement_profile": {}}
        self.assertEqual(repeat_assessment(first, other_profile)["status"], "uncomparable")

    def test_report_states_coverage_and_unknown_metrics(self):
        candidate = report(separated=True)
        result = assessment(candidate, reference(report()), "a" * 64)
        output = markdown(result, candidate["pr_memory"])
        self.assertIn("Onvergelijkbaar", output)
        self.assertIn("manifestcheck na 300 s", output)
        self.assertIn("Stackmarges en allocatiefouten onbekend", output)
        self.assertIn("elke 10 s", output)
