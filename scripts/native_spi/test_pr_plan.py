import json
from pathlib import Path
import re
import unittest

from .pr_plan import LINUX_RUNNERS, PROFILE_LABELS, event_candidate_ref, event_selection, labeled_ref, workflow_plan
from .pr_profiles import COSIM_PROFILES, HIL_LABELS, HOSTED_CASES, selected_cases


class WorkflowPlanTest(unittest.TestCase):
    def test_all_selections_use_one_ubuntu_runner(self):
        for architecture, runner in LINUX_RUNNERS.items():
            for profile in COSIM_PROFILES:
                with self.subTest(architecture=architecture, profile=profile):
                    plan = workflow_plan(profile, architecture, "a" * 40)
                    self.assertEqual(plan["runner"], runner)
                    self.assertEqual(plan["profiles"], profile)
                    self.assertEqual(plan["baseline"], "")
                    self.assertEqual(plan["label"], PROFILE_LABELS[profile])
                    self.assertGreaterEqual(int(plan["timeout_minutes"]), 30)

    def test_selection_order_and_duplicates_do_not_change_build_identity(self):
        first = workflow_plan("netwerk,https,regeling,herstart,regeling", "arm64", "a" * 40, "b" * 40)
        second = workflow_plan("regeling,herstart,https,netwerk", "arm64", "a" * 40, "b" * 40)
        self.assertEqual(first, second)
        self.assertEqual(first["profiles"], "herstart,netwerk,https,regeling")
        self.assertEqual(first["lab"], "true")

    def test_all_probes_share_one_session_and_same_revision_needs_no_extra_baseline(self):
        plan = workflow_plan("uitgebreid", "x64", "a" * 40, "a" * 40)
        self.assertEqual(plan["baseline"], plan["candidate"])
        self.assertEqual(selected_cases("uitgebreid", "cosim"), HOSTED_CASES)
        self.assertEqual(plan["lab"], "true")
        self.assertGreater(int(plan["timeout_minutes"]), 145)
        self.assertLess(int(plan["timeout_minutes"]), 360)

    def test_basic_profiles_do_not_request_a_lab_build(self):
        for profile in ("basis", "herstart", "netwerk", "herstart,netwerk"):
            self.assertEqual(workflow_plan(profile, "x64", "a"*40)["lab"], "false")
        for profile in ("https", "regeling", "stooklijn"):
            self.assertEqual(workflow_plan(profile, "x64", "a"*40)["lab"], "true")

    def test_pr_source_pin_requires_one_full_sha(self):
        def event(*labels):
            return {"pull_request": {"labels": [{"name": label} for label in labels]}}

        self.assertEqual(event_candidate_ref(event("hil:regeling-kern")), "dev")
        self.assertEqual(event_candidate_ref(event("hil:ref:" + "a" * 40)), "a" * 40)
        for labels in (("hil:ref:dev",), ("hil:ref:" + "a" * 40, "hil:ref:" + "b" * 40)):
            with self.subTest(labels=labels), self.assertRaises(ValueError):
                event_candidate_ref(event(*labels))

    def test_baseline_pin_is_optional_and_independent_of_the_candidate(self):
        event = {"pull_request": {"labels": [{"name": "hil:ref:" + "a" * 40},
                                             {"name": "hil:base:" + "b" * 40}]}}
        self.assertEqual(labeled_ref(event, "base"), "b" * 40)
        self.assertEqual(event_candidate_ref(event), "a" * 40)
        self.assertEqual(labeled_ref({}, "base"), "")
        event["pull_request"]["labels"].append({"name": "hil:base:dev"})
        with self.assertRaisesRegex(ValueError, "hil:base"):
            labeled_ref(event, "base")

    def test_regulation_retry_selects_only_two_cases_and_the_lab_build(self):
        event = {"pull_request": {"labels": [{"name": "hil:regeling-kern"}]}}
        selection = event_selection(event)
        self.assertEqual(selected_cases(selection, "cosim"), ("power-house", "stooklijn"))
        self.assertEqual(workflow_plan(selection, "x64", "a" * 40)["lab"], "true")
        with self.assertRaisesRegex(ValueError, "geheugenreferentie"):
            workflow_plan(selection, "x64", "a" * 40, "b" * 40)

    def test_local_or_invalid_profiles_never_generate_hosted_jobs(self):
        for selection in ("", "basis,", "alles", "energie", "modbus", "basis,modbus",
                          "basis,uitgebreid", "unknown", "https;echo bad"):
            with self.subTest(selection=selection), self.assertRaises(ValueError):
                workflow_plan(selection, "x64", "a" * 40)
        with self.assertRaises(ValueError): workflow_plan("basis", "amd64", "a"*40)
        with self.assertRaises(ValueError): workflow_plan("basis", "x64", "dev")

    def test_pr_labels_select_the_same_profiles_without_shell_interpolation(self):
        def event(*labels): return {"pull_request": {"labels": [{"name": x} for x in labels]}}
        self.assertEqual(event_selection(event("documentation")), "basis")
        self.assertEqual(event_selection(event("hil:https", "hil:regeling")), "https,regeling")
        self.assertEqual(event_selection(event("hil:https", "hil:uitgebreid")), "uitgebreid")


    def test_workflow_labels_and_choices_match_the_actual_profile_contract(self):
        workflow = Path(__file__).resolve().parents[2]/".github/workflows/virtual-hil.yml"
        text = workflow.read_text()
        selectors = re.findall(r"fromJSON\('([^']+)'\)", text)
        label_lists = [json.loads(value) for value in selectors if value.startswith("[")]
        self.assertTrue(label_lists)
        for labels in label_lists: self.assertEqual(set(labels), set(HIL_LABELS))
        choices = [value.split(", ") for value in re.findall(r"options: \[([^]]+)\]", text)]
        self.assertIn(list(COSIM_PROFILES), choices)
        self.assertNotIn("macos-", text)
