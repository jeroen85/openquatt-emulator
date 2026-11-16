import json
import unittest

from .pr_plan import workflow_plan


class WorkflowPlanTest(unittest.TestCase):
    def test_profiles_share_artifact_key_and_compare_on_one_runner(self):
        plan = workflow_plan("modbus, herstart, energie, herstart", "arm64", "a" * 40, "b" * 40)
        jobs = json.loads(plan["matrix"])["include"]
        self.assertEqual(jobs, [
            {"profile": "herstart", "runner": "ubuntu-24.04-arm", "compare": True},
            {"profile": "modbus", "runner": "macos-15", "compare": False},
            {"profile": "energie", "runner": "macos-15", "compare": False},
        ])
        reordered = workflow_plan("energie,modbus,herstart", "arm64", "a" * 40, "b" * 40)
        self.assertEqual(plan["artifact_key"], reordered["artifact_key"])

    def test_same_revision_can_be_measured_with_the_candidate_artifact(self):
        plan = workflow_plan("alles", "x64", "a" * 40, "a" * 40)
        self.assertEqual(plan["baseline"], plan["candidate"])
        self.assertEqual(json.loads(plan["matrix"])["include"], [
            {"profile": "alles", "runner": "macos-15", "compare": True},
        ])

    def test_absent_baseline_does_not_schedule_a_comparison(self):
        plan = workflow_plan("basis", "x64", "a" * 40)
        self.assertEqual(plan["baseline"], "")
        self.assertFalse(json.loads(plan["matrix"])["include"][0]["compare"])

    def test_invalid_selections_never_generate_jobs(self):
        for selection in ("", "basis,", "alles,energie", "unknown"):
            with self.subTest(selection=selection), self.assertRaises(ValueError):
                workflow_plan(selection, "x64", "a" * 40)
        with self.assertRaises(ValueError):
            workflow_plan("basis", "amd64", "a" * 40)
        with self.assertRaises(ValueError):
            workflow_plan("basis", "x64", "dev")
