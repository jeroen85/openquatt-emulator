import io
import json
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
import zipfile

from .pr_comment import GitHub, SKIPPED_TITLE, WORKFLOW, parse_result, publish

REPO = "example/emulator"
SERVER = "https://github.com"
HASH = "a" * 40
FIRMWARE = "b" * 40
PR = {"number": 7, "state": "open", "base": {"ref": "main", "sha": "c" * 40, "repo": {"id": 1}},
      "head": {"ref": "topic", "sha": HASH, "repo": {"id": 2}}}


def event():
    return {"repository": {"id": 1, "full_name": REPO}, "workflow_run": {
        "id": 42, "workflow_id": 5, "run_number": 3, "run_attempt": 2, "path": WORKFLOW, "event": "pull_request",
        "status": "completed", "conclusion": "failure", "head_repository": {"id": 2, "full_name": "fork/emulator"},
        "head_branch": "topic", "head_sha": HASH, "pull_requests": [{"number": 7, "base": PR["base"]}],
    }}


def bundle(cases, profile="basis", firmware=FIRMWARE):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr("pr-result.json", json.dumps({"profile": profile, "cases": cases}))
        archive.writestr("source-candidate.json", json.dumps({
            "repository": "OpenQuatt/OpenQuatt", "target": "configs/heatpump_controller_q/duo.yaml", "revision": firmware,
        }))
    return data.getvalue()


class FakeGitHub:
    def __init__(self):
        self.pr = PR
        self.runs = []
        self.queries = []
        self.comment_rows = []
        self.artifacts = []
        self.writes = []
        self.downloads = {}

    def json(self, path, *, method="GET", body=None):
        if method != "GET":
            self.writes.append((method, path, body["body"]))
            return {}
        if path == "/pulls/7":
            return self.pr
        if path.startswith("/actions/workflows/5/runs?event=pull_request&status=completed&head_sha="):
            self.queries.append(path)
            return {"workflow_runs": self.runs}
        if path.startswith("/actions/runs/") and path.endswith("/artifacts?per_page=100"):
            return {"artifacts": self.artifacts}
        raise AssertionError(path)

    def comments(self, number):
        assert number == 7
        return iter(self.comment_rows)

    def download(self, artifact_id):
        return self.downloads[artifact_id]


def artifact(artifact_id, profile="basis", attempt=2):
    return {"id": artifact_id, "name": f"virtual-hil-pr-result-{'5' * 16}-{profile}-{attempt}"}


class PrCommentTest(unittest.TestCase):
    def test_post_reports_failed_run_and_verified_scenarios(self):
        api = FakeGitHub()
        api.artifacts = [artifact(10, "uitgebreid")]
        api.downloads[10] = bundle({"geheugen": True, "ethernet-failover": False}, profile="uitgebreid")
        self.assertTrue(publish(event(), REPO, SERVER, api))
        method, path, text = api.writes[0]
        self.assertEqual((method, path), ("POST", "/issues/7/comments"))
        self.assertIn("**Afgekeurd**", text)
        self.assertIn("| uitgebreid | Webbelasting en geheugen | Geslaagd |", text)
        self.assertIn("| uitgebreid | Automatische terugval van Ethernet naar Wi-Fi | Afgekeurd |", text)
        self.assertIn("`bbbbbbbb`", text)
        self.assertIn("/actions/runs/42", text)

    def test_no_reports_still_posts_actual_run_status(self):
        api = FakeGitHub()
        api.artifacts = [artifact(10, attempt=1)]
        self.assertTrue(publish(event(), REPO, SERVER, api))
        self.assertIn("geen bruikbare proefdetails", api.writes[0][2])

    def test_only_the_bot_comment_updates_and_older_attempts_are_ignored(self):
        api = FakeGitHub()
        api.comment_rows = [
            {"id": 11, "user": {"login": "reviewer"}, "body": "<!-- openquatt-virtual-hil run=999 attempt=1 -->"},
            {"id": 12, "user": {"login": "github-actions[bot]"}, "body": "<!-- openquatt-virtual-hil run=3 attempt=1 -->"},
        ]
        self.assertTrue(publish(event(), REPO, SERVER, api))
        self.assertEqual(api.writes[0][:2], ("PATCH", "/issues/comments/12"))
        api.comment_rows[1]["body"] = "<!-- openquatt-virtual-hil run=3 attempt=2 -->"
        self.assertFalse(publish(event(), REPO, SERVER, api))
        self.assertEqual(len(api.writes), 1)

    def test_skips_changed_pr_head_or_base(self):
        for changed in ({**PR, "head": {**PR["head"], "sha": "d" * 40}},
                        {**PR, "base": {**PR["base"], "ref": "dev"}}):
            with self.subTest(changed=changed):
                api = FakeGitHub()
                api.pr = changed
                self.assertFalse(publish(event(), REPO, SERVER, api))
                self.assertEqual(api.writes, [])

    def test_missing_trusted_pr_association_never_guesses_the_target(self):
        api = FakeGitHub()
        payload = event()
        payload["workflow_run"]["pull_requests"] = []
        self.assertFalse(publish(payload, REPO, SERVER, api))
        self.assertEqual(api.writes, [])

    def test_older_completion_still_reports_latest_associated_run(self):
        api = FakeGitHub()
        run = event()["workflow_run"]
        api.runs = [
            {**run, "id": 990, "run_number": 10, "pull_requests": [{"number": 8, "base": PR["base"]}]},
            {**run, "conclusion": "success", "run_attempt": 3},
        ]
        self.assertTrue(publish(event(), REPO, SERVER, api))
        self.assertIn("run=3 attempt=3", api.writes[0][2])
        self.assertIn("**Geslaagd**", api.writes[0][2])

    def test_unrelated_label_does_not_publish_or_replace_the_test_report(self):
        api = FakeGitHub()
        skipped = {**event()["workflow_run"], "id": 43, "run_number": 4,
                   "display_title": SKIPPED_TITLE, "conclusion": "success"}
        api.runs = [skipped]
        self.assertFalse(publish({**event(), "workflow_run": skipped}, REPO, SERVER, api))
        self.assertEqual(api.writes, [])
        self.assertTrue(publish(event(), REPO, SERVER, api))
        self.assertIn("/actions/runs/42", api.writes[0][2])

    def test_late_event_for_previous_head_reports_the_current_head(self):
        api = FakeGitHub()
        current_sha = "d" * 40
        api.pr = {**PR, "head": {**PR["head"], "sha": current_sha}}
        run = event()["workflow_run"]
        api.runs = [{**run, "head_sha": current_sha, "run_number": 4, "id": 43, "conclusion": "success"}]
        self.assertTrue(publish(event(), REPO, SERVER, api))
        self.assertIn(f"head_sha={current_sha}", api.queries[0])
        self.assertIn("**Geslaagd**", api.writes[0][2])
        self.assertIn("/actions/runs/43", api.writes[0][2])

    def test_never_uses_a_pr_number_from_an_artifact_or_other_workflow(self):
        api = FakeGitHub()
        payload = event()
        payload["workflow_run"]["path"] = ".github/workflows/another.yml"
        self.assertFalse(publish(payload, REPO, SERVER, api))
        for cases, firmware in (({"@reviewer": True}, FIRMWARE), ({"geheugen": "yes"}, FIRMWARE),
                                ({"geheugen": True}, "[misleidend](https://example.org)")):
            with self.assertRaises(ValueError):
                parse_result(bundle(cases, firmware=firmware), "basis")

    def test_download_uses_no_auth_for_signed_storage_url(self):
        api = GitHub("https://api.github.com", REPO, "test-token")
        url = "https://storage.example.org/download?sig=fixture"
        redirection = HTTPError(api.url, 302, "Found", {"Location": url}, None)
        api.request = Mock(side_effect=redirection)
        with patch("scripts.native_spi.pr_comment.urlopen", return_value=io.BytesIO(b"report")) as download:
            self.assertEqual(api.download(10), b"report")
        api.request.assert_called_once_with("/actions/artifacts/10/zip")
        download.assert_called_once_with(url, timeout=12)


class SharedSuiteCommentTest(unittest.TestCase):
    @staticmethod
    def suite_bundle(*, mutate=None):
        cases = {"geheugen": True, "https": True, "power-house": True, "lowflow": True, "stooklijn": True}
        identities = {key: {"role": "candidate" if key == "geheugen" else "lab",
                            "sha256": ("c" if key == "geheugen" else "d")*64} for key in cases}
        result = {"schema": 2, "profiles": ["https", "regeling", "stooklijn"], "cases": cases, "firmware": identities}
        if mutate: mutate(result)
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as archive:
            archive.writestr("pr-result.json", json.dumps(result))
            archive.writestr("source-candidate.json", json.dumps({"repository": "OpenQuatt/OpenQuatt",
                "target": "configs/heatpump_controller_q/duo.yaml", "revision": FIRMWARE}))
        return data.getvalue()

    def test_automatic_comment_includes_all_new_probes_and_shared_lab_hash(self):
        api = FakeGitHub(); api.artifacts = [artifact(10, "suite")]
        api.downloads[10] = self.suite_bundle()
        self.assertTrue(publish(event(), REPO, SERVER, api))
        text = api.writes[0][2]
        for title in ("HTTPS, afgebroken verbinding", "Power House-vraag", "Flowverlies tijdens defrost", "Stooklijn"):
            self.assertIn(title, text)
        self.assertIn("d"*64, text)
        self.assertIn("GPIO/test-CA-labbin", text)

    def test_hosted_lowflow_comment_reports_only_the_selected_cases(self):
        def lowflow_only(result):
            result["profiles"] = ["lowflow"]
            cases = ("geheugen", "lowflow")
            result["cases"] = dict.fromkeys(cases, True)
            result["firmware"] = {key: {"role": "candidate", "sha256": "c" * 64} for key in cases}

        api = FakeGitHub()
        api.artifacts = [artifact(10, "suite")]
        api.downloads[10] = self.suite_bundle(mutate=lowflow_only)
        self.assertTrue(publish(event(), REPO, SERVER, api))
        text = api.writes[0][2]
        self.assertIn("| lowflow | Flowverlies tijdens defrost en herstel | Geslaagd |", text)
        self.assertNotIn("Power House-vraag", text)
        self.assertNotIn("Stooklijn", text)

    def test_comment_formats_heap_values_and_signed_differences(self):
        def measured(result):
            result["cases"]["geheugen-baseline"] = True
            result["firmware"]["geheugen-baseline"] = {"role": "baseline", "sha256": "e" * 64}
            result["memory"] = {"geheugen": [98100, 62000, 71000], "geheugen-baseline": [98000, 62500, 71000],
                                "https": [96000, 60000, 70000]}
        api = FakeGitHub()
        api.artifacts = [artifact(10, "suite")]
        api.downloads[10] = self.suite_bundle(mutate=measured)
        self.assertTrue(publish(event(), REPO, SERVER, api))
        text = api.writes[0][2]
        self.assertIn("| Webbelasting | 98100 (+100) | 62000 (-500) | 71000 (+0) |", text)
        self.assertIn("| HTTPS, afgebroken verbinding en herstel onder webbelasting | 96000 | 60000 | 70000 |", text)

    def test_rejects_invalid_or_unpaired_memory_evidence(self):
        for memory in (
            {"geheugen": ["@reviewer", 0, 0]}, {"geheugen": [20, True, 0]},
            {"geheugen": [20, 0, 21]}, {"geheugen": [2**24, 0, 0]},
            {"geheugen-baseline": [20, 0, 0]}, {"@reviewer": [20, 0, 0]},
        ):
            with self.subTest(memory=memory), self.assertRaises(ValueError):
                parse_result(self.suite_bundle(mutate=lambda r: r.update(memory=memory)), "suite")

    def test_unknown_selection_missing_cases_or_firmware_never_publish_details(self):
        mutations = (
            lambda r: r.update(profiles=["https", "@reviewer"]),
            lambda r: r["cases"].pop("https"),
            lambda r: r["firmware"].pop("https"),
            lambda r: r["firmware"]["https"].update(role="production"),
            lambda r: r.update(schema=True),
            lambda r: r.update(profiles=["https", "https"]),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate), self.assertRaises(ValueError):
                parse_result(self.suite_bundle(mutate=mutate), "suite")

    def test_regular_regulation_firmware_and_https_only_lab_are_reported(self):
        def regular(result):
            for key in ("power-house", "lowflow", "stooklijn"):
                result["firmware"][key] = result["firmware"]["geheugen"].copy()
        api = FakeGitHub()
        api.artifacts = [artifact(10, "suite")]
        api.downloads[10] = self.suite_bundle(mutate=regular)
        self.assertTrue(publish(event(), REPO, SERVER, api))
        body = api.writes[0][2]
        self.assertIn("HTTPS-testbin met lokale CA", body)
        self.assertIn("Power House-vraag en Single/Duo", body.split("Gewone Q Duo-bin", 1)[1].split("\n", 1)[0])

    def test_mixed_regulation_firmware_layout_is_rejected(self):
        def mixed(result):
            result["firmware"]["power-house"] = result["firmware"]["geheugen"].copy()
        with self.assertRaisesRegex(ValueError, "geen firmwaretype"):
            parse_result(self.suite_bundle(mutate=mixed), "suite")

    def test_different_lab_images_cannot_be_reported_as_shared_firmware(self):
        with self.assertRaisesRegex(ValueError, "gedeelde firmware"):
            parse_result(self.suite_bundle(mutate=lambda r: r["firmware"]["https"].update(sha256="e"*64)), "suite")

    def test_failed_unstarted_cases_can_be_reported_without_an_invented_firmware_hash(self):
        def mutation(result):
            result["cases"]["https"] = False
            result["firmware"].pop("https")
        result = parse_result(self.suite_bundle(mutate=mutation), "suite")
        self.assertFalse(result["cases"]["https"])
        self.assertNotIn("https", result["firmware"])
