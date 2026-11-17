import io
import json
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
import zipfile

from .pr_comment import GitHub, WORKFLOW, parse_result, publish

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
            return {"workflow_runs": self.runs}
        if path == "/actions/runs/42/artifacts?per_page=100":
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
