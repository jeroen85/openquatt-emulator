import json
import unittest

from .pr_plan import LINUX_RUNNERS, PROFILE_LABELS, workflow_plan
from .pr_suite import COSIM_PROFILES


class WorkflowPlanTest(unittest.TestCase):
    def test_each_hosted_profile_has_its_own_check_name(self):
        self.assertEqual(PROFILE_LABELS.keys(), set(COSIM_PROFILES))
        self.assertEqual(len(set(PROFILE_LABELS.values())), len(COSIM_PROFILES))

    def test_profiles_share_one_build_and_compare_on_one_ubuntu_runner(self):
        plan = workflow_plan("netwerk, herstart, herstart", "arm64", "a" * 40, "b" * 40)
        jobs = json.loads(plan["matrix"])["include"]
        self.assertEqual(jobs, [
            {"profile": "herstart", "runner": "ubuntu-24.04-arm", "compare": True, "label": "softwareherstart"},
            {"profile": "netwerk", "runner": "ubuntu-24.04-arm", "compare": False, "label": "Ethernet en Wi-Fi-terugval"},
        ])
        reordered = workflow_plan("herstart,netwerk", "arm64", "a" * 40, "b" * 40)
        self.assertEqual(plan["artifact_key"], reordered["artifact_key"])

    def test_complete_ubuntu_suite_is_one_job_and_reuses_same_revision_baseline(self):
        plan = workflow_plan("uitgebreid", "x64", "a" * 40, "a" * 40)
        self.assertEqual(plan["baseline"], plan["candidate"])
        self.assertEqual(json.loads(plan["matrix"])["include"], [
            {"profile": "uitgebreid", "runner": "ubuntu-24.04", "compare": True, "label": "alle Ubuntu-proeven"},
        ])

    def test_each_hosted_profile_uses_only_ubuntu(self):
        for architecture, runner in LINUX_RUNNERS.items():
            for profile in COSIM_PROFILES:
                with self.subTest(architecture=architecture, profile=profile):
                    plan = workflow_plan(profile, architecture, "a" * 40)
                    self.assertEqual(plan["baseline"], "")
                    self.assertEqual(json.loads(plan["matrix"])["include"], [
                        {"profile": profile, "runner": runner, "compare": False, "label": PROFILE_LABELS[profile]},
                    ])

    def test_local_or_invalid_profiles_never_generate_hosted_jobs(self):
        for selection in ("", "basis,", "alles", "energie", "regeling", "modbus", "basis,modbus",
                          "basis,uitgebreid", "unknown"):
            with self.subTest(selection=selection), self.assertRaises(ValueError):
                workflow_plan(selection, "x64", "a" * 40)
        with self.assertRaises(ValueError):
            workflow_plan("basis", "amd64", "a" * 40)
        with self.assertRaises(ValueError):
            workflow_plan("basis", "x64", "dev")
