import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from .memory_comparison import METRICS
from .pr_suite import BASE, run_suite, select_cases, wait_for_case


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
        self.assertEqual(select_cases("uitgebreid", "cosim"), (BASE, "herstart", *network))
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
                             {"profile": "basis", "cases": {"geheugen-baseline": False, BASE: True}})

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
