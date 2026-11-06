"""Bind observations to loaded harness sources and a verified firmware build."""
from __future__ import annotations

import hashlib
import json
from scripts.simulator.profiles import get_profile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CORE_PYTHON = ("lab.py", "controller.py", "diagnostics.py", "load.py", "model.py", "runtime.py", "scenarios.py", "transport.py", "provenance.py", "profiles.py", "opentherm.py", "flow.py")
CONFIGS = ("configs/emulator/duo_wifi.yaml", "configs/emulator/lab_duo.yaml", "configs/emulator/diagnostics.yaml")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def hashes(paths, root):
    return {str(path.relative_to(root)): digest(path) for path in sorted(set(paths))}


def process_sources(root=ROOT):
    paths = [root / "scripts/simulator" / name for name in CORE_PYTHON]
    paths += [root / "scripts/emulator/run.py"]
    for directory in ("native", "vendor/odu"):
        paths += [p for p in (root / "scripts/simulator" / directory).glob("*") if p.suffix in (".h", ".cpp", ".json")]
    return hashes(paths, root)


def firmware_sources(root=ROOT, profile="duo"):
    spec = get_profile(profile)
    paths = [root / name for name in CONFIGS]
    selected = root / spec.config
    if selected not in paths:
        paths.append(selected)
    fixtures = ["virtual_opentherm"] if spec.peer else []
    if spec.name in ("local-flow", "ot-boiler"):
        fixtures.append("virtual_flow")
    if spec.name == "ot-boiler":
        clock = root / "configs/emulator/cv_virtual_clock.h"
        if clock.is_file():
            paths.append(clock)
    for fixture in fixtures:
        paths += [p for p in (root / "scripts/simulator/io" / fixture).rglob("*")
                  if p.is_file() and p.suffix in (".h", ".cpp", ".py", ".yaml", ".json", ".md")]
    paths += [p for p in (root / "scripts/simulator/firmware").rglob("*") if p.is_file() and p.suffix in (".h", ".cpp", ".py")]
    for directory in ("openquatt", "components", "configs/heatpump_controller_q"):
        paths += [p for p in (root / directory).rglob("*") if p.is_file() and p.suffix in (".yaml", ".h", ".cpp", ".py", ".csv")]
    paths += [root / name for name in (".github/requirements-esphome.txt", "package-lock.json",
             "openquatt/web/js/openquatt-app.js", "openquatt/web/css/openquatt-app.css") if (root / name).is_file()]
    return hashes(paths, root)


def assert_unchanged(expected, actual, label):
    changed = sorted(key for key in expected.keys() | actual.keys() if expected.get(key) != actual.get(key))
    if changed:
        raise RuntimeError(label + ": " + ", ".join(changed[:8]))


def write_build_manifest(build, before, *, esphome_version, root=ROOT, profile="duo"):
    spec = get_profile(profile)
    after = firmware_sources(root, profile)
    assert_unchanged(before, after, "Firmwarebronnen gewijzigd tijdens bouwen; bouw opnieuw")
    value = {"schema": 1, "lab_profile": profile, "profile": spec.identity, "instrumentation_schema": "oq-lab-diagnostics-v1", "firmware_sha256": digest(build / "firmware.factory.bin"),
             "elf_sha256": digest(build / "firmware.elf"), "esphome_version": esphome_version,
             "compiler_jobs": 1, "sources": before}
    temporary = build / "lab-manifest.json.tmp"
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(build / "lab-manifest.json")
    return value


def verified_build(build, root=ROOT, profile="duo"):
    path = build / "lab-manifest.json"
    if not path.is_file():
        raise RuntimeError("Lab-buildmanifest ontbreekt; voer lab.py build uit")
    value = json.loads(path.read_text())
    if not isinstance(value, dict) or value.get("schema") != 1 or not isinstance(value.get("sources"), dict):
        raise RuntimeError("Ongeldig lab-buildmanifest; bouw opnieuw")
    if value.get("lab_profile", "duo") != profile or value.get("profile", get_profile(profile).identity) != get_profile(profile).identity:
        raise RuntimeError("Buildmanifest hoort bij een ander labprofiel")
    assert_unchanged(value["sources"], firmware_sources(root, profile), "Firmwarebronnen gewijzigd na bouwen; bouw opnieuw")
    if value.get("firmware_sha256") != digest(build / "firmware.factory.bin") or value.get("elf_sha256") != digest(build / "firmware.elf"):
        raise RuntimeError("Firmware/ELF wijkt af van het lab-buildmanifest; bouw opnieuw")
    return value
