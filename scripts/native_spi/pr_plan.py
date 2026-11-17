"""Plan GitHub HIL jobs and share the firmware builds within one workflow call."""
from __future__ import annotations

import hashlib
import json
import os
import re

from .pr_suite import COSIM_PROFILES

LINUX_RUNNERS = {"x64": "ubuntu-24.04", "arm64": "ubuntu-24.04-arm"}
PROFILE_LABELS = {
    "basis": "rooktest en geheugen",
    "herstart": "softwareherstart",
    "netwerk": "Ethernet en Wi-Fi-terugval",
    "uitgebreid": "alle Ubuntu-proeven",
}


def workflow_plan(selection, architecture, candidate, baseline=None):
    if architecture not in LINUX_RUNNERS:
        raise ValueError("Kies x64 of arm64 voor de Linux-runner.")
    selected = {part.strip() for part in selection.split(",")}
    if not selected <= set(COSIM_PROFILES):
        raise ValueError("GitHub gebruikt Ubuntu. Kies basis, herstart, netwerk of uitgebreid. "
                         "Draai alles, modbus, energie en regeling lokaal op macOS.")
    if "uitgebreid" in selected and len(selected) > 1:
        raise ValueError("Kies uitgebreid afzonderlijk. Dat profiel bevat alle Ubuntu-proeven.")
    if not re.fullmatch(r"[0-9a-f]{40}", candidate) or (baseline and not re.fullmatch(r"[0-9a-f]{40}", baseline)):
        raise ValueError("Het bouwplan vereist volledige OpenQuatt-commit-SHA's.")

    jobs = [
        {"profile": profile, "runner": LINUX_RUNNERS[architecture], "compare": False}
        for profile in COSIM_PROFILES
        if profile in selected
    ]
    if baseline:
        jobs[0]["compare"] = True

    key = hashlib.sha256(json.dumps([jobs, architecture, candidate, baseline], sort_keys=True).encode()).hexdigest()[:16]
    matrix = {"include": [{**job, "label": PROFILE_LABELS[job["profile"]]} for job in jobs]}
    return {
        "matrix": json.dumps(matrix, separators=(",", ":")),
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
