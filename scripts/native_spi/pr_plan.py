"""Plan one Ubuntu session with shared production and lab firmware builds."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re

from .pr_profiles import (COSIM_PROFILES, COSIM_SECONDS, HIL_LABELS, LAB_CASES, PROFILE_LABELS,
                          selected_cases, selected_profiles)

LINUX_RUNNERS = {"x64": "ubuntu-24.04", "arm64": "ubuntu-24.04-arm"}


def workflow_plan(selection, architecture, candidate, baseline=None):
    if architecture not in LINUX_RUNNERS:
        raise ValueError("Kies x64 of arm64 voor de Linux-runner.")
    baseline = baseline or None
    profiles = selected_profiles(selection)
    cases = selected_cases(selection, "cosim")
    if not re.fullmatch(r"[0-9a-f]{40}", candidate) or (baseline and not re.fullmatch(r"[0-9a-f]{40}", baseline)):
        raise ValueError("Het bouwplan vereist volledige OpenQuatt-commit-SHA's.")
    label = PROFILE_LABELS[profiles[0]] if len(profiles) == 1 else "geselecteerde Ubuntu-proeven"
    lab = bool(set(cases) & LAB_CASES)
    seconds = sum(COSIM_SECONDS.get(key, {"geheugen": 285, "herstart": 340,
                  "ethernet-ntp": 90, "ethernet-failover": 180}.get(key, 0)) for key in cases)
    if baseline:
        seconds += 285
    # Include provisioning, builds and teardown. Test timers are never shortened.
    timeout = math.ceil(seconds / 60) + 35 + (25 if lab else 0) + (25 if baseline and baseline != candidate else 0)
    key = hashlib.sha256(json.dumps([profiles, architecture, candidate, baseline]).encode()).hexdigest()[:16]
    return {"profiles": ",".join(profiles), "runner": LINUX_RUNNERS[architecture], "label": label,
            "candidate": candidate, "baseline": baseline or "", "lab": str(lab).lower(),
            "artifact_key": key, "timeout_minutes": str(timeout)}


def event_selection(event):
    labels = {row["name"] for row in event.get("pull_request", {}).get("labels", [])}
    if "hil:uitgebreid" in labels:
        return "uitgebreid"
    selected = [profile for label, profile in HIL_LABELS.items() if label in labels]
    return ",".join(selected) or "basis"


if __name__ == "__main__":
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    selection = os.environ.get("PROFILES") or event_selection(event)
    result = workflow_plan(selection, os.environ["ARCHITECTURE"],
                           os.environ["CANDIDATE_SHA"], os.environ["BASELINE_SHA"])
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        for key, value in result.items():
            print(f"{key}={value}", file=output)
