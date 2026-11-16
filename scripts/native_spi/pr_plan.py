"""Plan GitHub HIL jobs and share the firmware builds within one workflow call."""
from __future__ import annotations

import hashlib
import json
import os
import re

from .pr_suite import PROFILES, select_cases

LINUX_RUNNERS = {"x64": "ubuntu-24.04", "arm64": "ubuntu-24.04-arm"}


def workflow_plan(selection, architecture, candidate, baseline=None):
    if architecture not in LINUX_RUNNERS:
        raise ValueError("Kies x64 of arm64 voor de Linux-runner.")
    selected = {part.strip() for part in selection.split(",")}
    if not selected <= PROFILES.keys() or ("alles" in selected and len(selected) > 1):
        raise ValueError("Kies geldige, kommagescheiden profielen of kies alleen alles.")
    if not re.fullmatch(r"[0-9a-f]{40}", candidate) or (baseline and not re.fullmatch(r"[0-9a-f]{40}", baseline)):
        raise ValueError("Het bouwplan vereist volledige OpenQuatt-commit-SHA's.")

    jobs = []
    for profile in PROFILES:
        if profile not in selected:
            continue
        try:
            select_cases(profile, "cosim")
        except ValueError:
            runner = "macos-15"
        else:
            runner = LINUX_RUNNERS[architecture]
        jobs.append({"profile": profile, "runner": runner, "compare": False})

    if baseline:
        reference_job = next((job for job in jobs if job["runner"] == LINUX_RUNNERS[architecture]), jobs[0])
        reference_job["compare"] = True

    key = hashlib.sha256(json.dumps([jobs, architecture, candidate, baseline], sort_keys=True).encode()).hexdigest()[:16]
    return {
        "matrix": json.dumps({"include": jobs}, separators=(",", ":")),
        "candidate": candidate,
        "baseline": baseline or "",
        "artifact_key": key,
    }


if __name__ == "__main__":
    result = workflow_plan(os.environ["PROFILES"], os.environ["ARCHITECTURE"],
                           os.environ["CANDIDATE_SHA"], os.environ["BASELINE_SHA"])
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        for key, value in result.items():
            print(f"{key}={value}", file=output)
