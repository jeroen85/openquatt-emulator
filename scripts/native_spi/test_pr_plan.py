import json
import unittest

from .pr_plan import PROFILE_LABELS, workflow_plan
from .pr_suite import PROFILES


class WorkflowPlanTest(unittest.TestCase):
    def test_each_profile_has_its_own_check_name(self):
        self.assertEqual(PROFILE_LABELS.keys(), PROFILES.keys())
        self.assertEqual(len(set(PROFILE_LABELS.values())), len(PROFILES))

    def test_profiles_share_artifact_key_and_compare_on_one_runner(self):
        plan = workflow_plan("modbus, herstart, energie, herstart", "arm64", "a" * 40, "b" * 40)
        jobs = json.loads(plan["matrix"])["include"]
        self.assertEqual(jobs, [
            {"label": "softwareherstart", "profile": "herstart", "runner": "ubuntu-24.04-arm", "compare": True},
            {"label": "Modbus-uitval", "profile": "modbus", "runner": "macos-15", "compare": False},
            {"label": "energiehistorie", "profile": "energie", "runner": "macos-15", "compare": False},
        ])
        reordered = workflow_plan("energie,modbus,herstart", "arm64", "a" * 40, "b" * 40)
        self.assertEqual(plan["artifact_key"], reordered["artifact_key"])

    def test_same_revision_can_be_measured_with_the_candidate_artifact(self):
        plan = workflow_plan("alles", "x64", "a" * 40, "a" * 40)
        self.assertEqual(plan["baseline"], plan["candidate"])
        self.assertEqual(json.loads(plan["matrix"])["include"], [
            {"label": "alle proeven", "profile": "alles", "runner": "macos-15", "compare": True},
        ])

    def test_absent_baseline_does_not_schedule_a_comparison(self):
        plan = workflow_plan("basis", "x64", "a" * 40)
        self.assertEqual(plan["baseline"], "")
        job, = json.loads(plan["matrix"])["include"]
        self.assertEqual(job["label"], "rooktest en geheugen")
        self.assertFalse(job["compare"])

    def test_invalid_selections_never_generate_jobs(self):
        for selection in ("", "basis,", "alles,energie", "unknown"):
            with self.subTest(selection=selection), self.assertRaises(ValueError):
                workflow_plan(selection, "x64", "a" * 40)
        with self.assertRaises(ValueError):
            workflow_plan("basis", "amd64", "a" * 40)
        with self.assertRaises(ValueError):
            workflow_plan("basis", "x64", "dev")
