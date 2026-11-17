"""Publish a completed HIL run on its still-current emulator pull request."""
from __future__ import annotations

import io
import json
import os
import re
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen
import zipfile
import zlib

from .pr_suite import CASES, PROFILES

WORKFLOW = ".github/workflows/virtual-hil.yml"
BOT = "github-actions[bot]"
MARKER = re.compile(r"<!-- openquatt-virtual-hil run=(\d+) attempt=(\d+) -->")
ARTIFACT = re.compile(r"virtual-hil-pr-result-[0-9a-f]{16}-([a-z]+)-(\d+)")
SHA = re.compile(r"[0-9a-f]{40}")
LIMIT = 16 * 1024
LABELS = {key: case[0] for key, case in CASES.items()}
LABELS["geheugen-baseline"] = "Referentiemeting voor webgeheugen"
CONCLUSIONS = {
    "success": "Geslaagd", "failure": "Afgekeurd", "cancelled": "Afgebroken",
    "timed_out": "Tijdslimiet bereikt", "action_required": "Actie vereist",
}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GitHub:
    def __init__(self, url, repository, token):
        self.url = f"{url}/repos/{repository}"
        self.headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
        self.opener = build_opener(NoRedirect())

    def request(self, path, *, method="GET", body=None):
        data = json.dumps(body).encode() if body is not None else None
        headers = {**self.headers, "Content-Type": "application/json"} if data is not None else self.headers
        return self.opener.open(Request(self.url + path, data=data, headers=headers, method=method), timeout=12)

    def json(self, path, *, method="GET", body=None):
        with self.request(path, method=method, body=body) as response:
            return json.load(response)

    def download(self, artifact_id):
        # Do not forward the repository token to the signed storage URL.
        try:
            with self.request(f"/actions/artifacts/{artifact_id}/zip") as response:
                return response.read(LIMIT + 1)
        except HTTPError as exc:
            target = exc.headers.get("Location", "")
            exc.close()
            if exc.code != 302 or urlsplit(target).scheme != "https":
                raise
        with urlopen(target, timeout=12) as response:
            return response.read(LIMIT + 1)

    def comments(self, number):
        page = 1
        while True:
            rows = self.json(f"/issues/{number}/comments?per_page=100&page={page}")
            yield from rows
            if len(rows) < 100:
                return
            page += 1


def same_pr_identity(pr, run, repository_id):
    head = pr.get("head", {})
    base = pr.get("base", {})
    return (
        pr.get("state") == "open" and (base.get("repo") or {}).get("id") == repository_id
        and (head.get("repo") or {}).get("id") == (run.get("head_repository") or {}).get("id")
        and head.get("ref") == run.get("head_branch")
    )


def matches(pr, run, repository_id):
    recorded = next((candidate for candidate in run.get("pull_requests") or []
                     if candidate["number"] == pr["number"]), None)
    return (
        same_pr_identity(pr, run, repository_id)
        and pr["head"]["sha"] == run.get("head_sha") and recorded is not None
        and all(pr["base"].get(key) == recorded.get("base", {}).get(key) for key in ("ref", "sha"))
    )


def find_pr(api, run, repository_id):
    if not run.get("head_repository") or not run.get("head_branch"):
        return None
    associated = run.get("pull_requests") or []
    candidates = [api.json(f"/pulls/{pr['number']}") for pr in associated]
    current = [pr for pr in candidates if same_pr_identity(pr, run, repository_id)]
    return current[0] if len(current) == 1 else None


def version(run):
    return run["run_number"], run["run_attempt"]


def latest_for_pr(api, run, pr, repository_id):
    head_sha = pr["head"]["sha"]
    if not SHA.fullmatch(head_sha):
        return None
    query = urlencode({"event": "pull_request", "status": "completed", "head_sha": head_sha, "per_page": 100})
    path = f"/actions/workflows/{run['workflow_id']}/runs?{query}"
    candidates = api.json(path)["workflow_runs"]
    return max((candidate for candidate in [run, *candidates]
                if candidate.get("path") == WORKFLOW and candidate.get("event") == "pull_request"
                and candidate.get("status") == "completed" and matches(pr, candidate, repository_id)),
               key=version, default=None)


def parse_result(payload, expected_profile):
    if len(payload) > LIMIT:
        raise ValueError("Resultaatarchief is te groot")
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        for filename in ("pr-result.json", "source-candidate.json"):
            if archive.getinfo(filename).file_size > LIMIT:
                raise ValueError("Resultaatbestand is te groot")
        result = json.loads(archive.read("pr-result.json"))
        source = json.loads(archive.read("source-candidate.json"))

    if not isinstance(result, dict) or result.get("profile") != expected_profile or expected_profile not in PROFILES:
        raise ValueError("Onbekend profiel in het resultaat")
    cases = result.get("cases")
    allowed = set(PROFILES[expected_profile]) | {"geheugen-baseline"}
    if (not isinstance(cases, dict) or not cases or not cases.keys() <= allowed
            or any(type(status) is not bool for status in cases.values())):
        raise ValueError("Ongeldige proefuitslagen")
    revision = source.get("revision") if isinstance(source, dict) else None
    if (not isinstance(revision, str) or not SHA.fullmatch(revision)
            or source.get("repository") != "OpenQuatt/OpenQuatt"
            or source.get("target") != "configs/heatpump_controller_q/duo.yaml"):
        raise ValueError("Ongeldige firmwareherkomst")
    return revision, cases


def details(api, run):
    artifacts = api.json(f"/actions/runs/{run['id']}/artifacts?per_page=100")["artifacts"]
    profiles = {}
    for artifact in artifacts:
        name = ARTIFACT.fullmatch(artifact["name"])
        if not name or artifact.get("expired") or name[1] not in PROFILES or int(name[2]) != run["run_attempt"]:
            continue
        try:
            result = parse_result(api.download(artifact["id"]), name[1])
        except (OSError, zipfile.BadZipFile, zlib.error, ValueError, KeyError, RuntimeError):
            print(f"Onbruikbaar uitslagartefact {artifact['id']}")
            continue
        if name[1] in profiles:
            return {}  # More than one result per profile has ambiguous provenance.
        profiles[name[1]] = result
    return profiles


def render(run, repository, server, profiles):
    marker = f"<!-- openquatt-virtual-hil run={run['run_number']} attempt={run['run_attempt']} -->"
    label = CONCLUSIONS.get(run["conclusion"], "Onbekende uitslag")
    lines = [marker, "### Virtuele HIL · Q Duo", "", f"**{label}**", "",
             f"Geteste PR-commit `{run['head_sha'][:7]}`."]
    if profiles:
        lines += ["", "Proefdetails zoals gerapporteerd door de run.", "",
                  "| Profiel | Proef | Uitslag |", "| --- | --- | --- |"]
        for profile, (_, cases) in sorted(profiles.items()):
            for key, passed in cases.items():
                lines.append(f"| {profile} | {LABELS[key]} | {'Geslaagd' if passed else 'Afgekeurd'} |")
        revisions = ", ".join(f"`{rev[:8]}`" for rev in sorted({rev for rev, _ in profiles.values()}))
        lines += ["", f"OpenQuatt-firmware volgens de run {revisions}."]
    else:
        lines += ["", "De workflow leverde geen bruikbare proefdetails op."]
    lines += ["", f"[GitHub-run en volledige rapporten]({server}/{repository}/actions/runs/{run['id']})"]
    return "\n".join(lines)


def publish(event, repository, server, api):
    run = event["workflow_run"]
    owner = event["repository"]
    if (owner["full_name"] != repository or run.get("path") != WORKFLOW
            or run.get("event") != "pull_request" or run.get("status") != "completed"):
        return False

    pr = find_pr(api, run, owner["id"])
    if pr is None:
        print("Geen unieke open PR voor deze commit")
        return False

    number = pr["number"]
    run = latest_for_pr(api, run, pr, owner["id"])
    if run is None:
        print("Geen voltooide run voor de huidige PR-commit")
        return False
    existing = None
    revision = version(run)
    for comment in api.comments(number):
        marker = MARKER.match(comment.get("body") or "")
        if (comment.get("user") or {}).get("login") != BOT or not marker:
            continue
        if tuple(map(int, marker.groups())) >= revision:
            return False
        existing = comment["id"]

    body = render(run, repository, server, details(api, run))
    if not matches(api.json(f"/pulls/{number}"), run, owner["id"]):
        return False
    path = f"/issues/{number}/comments" if existing is None else f"/issues/comments/{existing}"
    api.json(path, method="POST" if existing is None else "PATCH", body={"body": body})
    return True


if __name__ == "__main__":
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    repository = os.environ["GITHUB_REPOSITORY"]
    api = GitHub(os.environ["GITHUB_API_URL"], repository, os.environ["GH_TOKEN"])
    publish(event, repository, os.environ["GITHUB_SERVER_URL"], api)
