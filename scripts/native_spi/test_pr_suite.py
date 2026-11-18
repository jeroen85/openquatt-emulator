import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from .memory_comparison import METRICS
from .pr_suite import BASE, run_case, run_suite, select_cases, wait_for_case
from .pr_profiles import HOSTED_CASES, LAB_CASES


class PrSuiteTest(unittest.TestCase):
    def test_selects_all_contracts_without_repeating_the_base_run(self):
        self.assertEqual(select_cases("basis", "cosim"), (BASE,))
        self.assertEqual(select_cases("herstart", "cosim"), (BASE, "herstart"))
        all_cases = select_cases("alles", "native")
        self.assertEqual(all_cases, (BASE, "herstart", "modbus", "energie-uur", "energie-dag", "power-house", "lowflow"))
        with self.assertRaisesRegex(ValueError, "macOS"):
            select_cases("modbus", "cosim")

    def test_ubuntu_suite_runs_each_portable_case_once(self):
        network = ("ethernet-ntp", "ethernet-failover")
        self.assertEqual(select_cases("netwerk", "cosim"), (BASE, *network))
        self.assertEqual(select_cases("uitgebreid", "cosim"), HOSTED_CASES)
        with self.assertRaisesRegex(ValueError, "cosim"):
            select_cases("netwerk", "native")

    def test_failed_baseline_never_produces_a_comparison(self):
        memory = {"phases": {phase: dict.fromkeys(METRICS, 123)
                             for phase in ("before", "load", "after")}}

        def result(key, *_):
            passed = key != "geheugen-baseline"
            return {"title": key, "passed": passed, "reasons": [] if passed else ["Ontbrekende meetdata"],
                    "report": {"memory": memory}}

        with tempfile.TemporaryDirectory() as folder, patch("scripts.native_spi.pr_suite.run_case", side_effect=result) as run:
            root = Path(folder)
            self.assertEqual(run_suite("basis", root/"candidate.bin", "cosim", 19460, root, root/"baseline.bin"), 1)
            self.assertEqual(run.call_args.args[-1], ())
            summary = (root/"summary.md").read_text()
            self.assertIn("Ontbrekende meetdata", summary)
            self.assertIn("Q Duo-rooktest geslaagd", summary)
            self.assertEqual(json.loads((root / "pr-result.json").read_text()),
                             {"schema": 2, "profiles": ["basis"], "cases": {"geheugen-baseline": False, BASE: True}, "firmware": {}})

    def test_stop_gives_the_owned_runner_time_to_write_report(self):
        child = Mock()
        child.poll.return_value = None
        child.wait.return_value = 1
        with patch("scripts.native_spi.pr_suite.stop_requested", True), patch("os.killpg") as kill:
            self.assertEqual(wait_for_case(child), 1)
        child.terminate.assert_called_once_with()
        child.wait.assert_called_once_with(timeout=25)
        kill.assert_not_called()

    def test_successful_baseline_supplies_report_and_factory_hash(self):
        memory = {"phases": {phase: dict.fromkeys(METRICS, 123) for phase in ("before", "load", "after")}}
        result = {"title": "Geheugen", "passed": True, "reasons": [], "report": {"memory": memory}}
        with tempfile.TemporaryDirectory() as folder, patch("scripts.native_spi.pr_suite.run_case", return_value=result) as run:
            root = Path(folder)
            baseline = root / "baseline.bin"
            baseline.write_bytes(b"comparison firmware")
            self.assertEqual(run_suite("basis", root/"candidate.bin", "cosim", 19460, root, baseline), 0)
            self.assertEqual(run.call_args.args[-1], (
                "--memory-baseline", str(root/"geheugen-baseline/report.json"),
                "--memory-baseline-firmware-sha256", hashlib.sha256(b"comparison firmware").hexdigest(),
            ))


    def test_multiple_profiles_use_one_base_and_one_shared_lab_firmware(self):
        memory = {"phases": {phase: dict.fromkeys(METRICS, 123) for phase in ("before", "load", "after")}}
        def result(key, firmware, *args):
            sha = "b"*64 if firmware.name == "lab.bin" else "a"*64
            return {"title": key, "passed": True, "reasons": [], "report": {"memory": memory, "input": {"sha256": sha}}}
        with tempfile.TemporaryDirectory() as folder, patch("scripts.native_spi.pr_suite.run_case", side_effect=result) as run:
            root = Path(folder); production = root/"production.bin"; lab = root/"lab.bin"; tls = root/"tls"
            code = run_suite("https,regeling,stooklijn,herstart,netwerk", production, "cosim", 19460, root,
                             lab_firmware=lab, https_fixture=tls)
            self.assertEqual(code, 0)
            self.assertEqual([call.args[0] for call in run.call_args_list], list(HOSTED_CASES))
            for call in run.call_args_list:
                self.assertEqual(call.args[1], lab if call.args[0] in LAB_CASES else production)
                if call.args[0] == "https": self.assertEqual(call.args[-1], ("--https-fixture", str(tls)))
            report = json.loads((root/"pr-result.json").read_text())
            self.assertEqual(len({row["sha256"] for key,row in report["firmware"].items() if key in LAB_CASES}), 1)
            self.assertEqual(report["cases"], dict.fromkeys(HOSTED_CASES, True))

    def test_missing_lab_or_tls_fails_before_starting_any_probe(self):
        with tempfile.TemporaryDirectory() as folder, patch("scripts.native_spi.pr_suite.run_case") as run:
            root = Path(folder)
            with self.assertRaisesRegex(ValueError, "lab-firmware"):
                run_suite("regeling", root/"prod", "cosim", 19460, root)
            with self.assertRaisesRegex(ValueError, "https-fixture"):
                run_suite("https", root/"prod", "cosim", 19460, root, lab_firmware=root/"lab")
            run.assert_not_called()


    def test_missing_memory_samples_preserve_partial_suite_report_and_failure(self):
        base_memory = {"phases": {phase: dict.fromkeys(METRICS, 123) for phase in ("before", "load", "after")}}
        def result(key, *args):
            passed = key == BASE
            memory = base_memory if passed else {"phases": {"startup": {"sample_count": 0, "covered": False}}}
            return {"title": key, "passed": passed, "reasons": [] if passed else ["Onvoldoende heapmetingen"],
                    "report": {"memory": memory}}
        with tempfile.TemporaryDirectory() as folder, patch("scripts.native_spi.pr_suite.run_case", side_effect=result):
            root=Path(folder)
            self.assertEqual(run_suite("regeling", root/"prod.bin", "cosim", 19460, root,
                                       lab_firmware=root/"lab.bin"), 1)
            self.assertIn("Geen meting", (root/"summary.md").read_text())
            report=json.loads((root/"pr-result.json").read_text())
            self.assertTrue(report["cases"][BASE])
            self.assertFalse(report["cases"]["power-house"])
            self.assertFalse(report["cases"]["lowflow"])


    def test_truncated_report_is_an_explicit_failed_case(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); lab=root/"lab"; run=lab/"runs/github-q-duo-geheugen-0000000000-recorded"
            run.mkdir(parents=True);(run/"report.json").write_text('{"status":')
            with patch("scripts.native_spi.pr_suite.LAB",lab), patch("scripts.native_spi.pr_suite.uuid.uuid4") as identifier, \
                    patch("scripts.native_spi.pr_suite.subprocess.Popen"), patch("scripts.native_spi.pr_suite.wait_for_case",return_value=1):
                identifier.return_value.hex="0"*32
                result=run_case(BASE,root/"candidate.bin","cosim",19460,root/"out")
            self.assertFalse(result["passed"])
            self.assertIn("Onleesbaar eindrapport",result["reasons"][0])
            self.assertEqual(result["report"],{})
