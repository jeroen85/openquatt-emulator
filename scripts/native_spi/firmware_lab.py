"""Prepare the shared GPIO OpenTherm and HTTPS test-CA firmware recipe."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

from .https_fixture import prepare

ASSETS = Path(__file__).with_name("lab_firmware")
COMPONENTS = ("components/opentherm/opentherm.cpp", "components/openquatt_ot_slave/OpenTherm.cpp")
PROFILE = "gpio_opentherm_https_lab"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prepare_lab(root, fixture):
    root, fixture = root.resolve(), fixture.resolve()
    config_dir = root / "configs/heatpump_controller_q"
    if not (config_dir / "duo_hil.yaml").is_file():
        raise ValueError("De gekozen OpenQuatt-bron mist duo_hil.yaml")
    assets = json.loads((ASSETS / "provenance.json").read_text())
    for filename, expected in assets["files"].items():
        if digest(ASSETS / filename) != expected:
            raise ValueError("De meegeleverde GPIO-labbron wijkt af van de herkomstgegevens")
    owned = root / ".virtual-hil"
    gpio = config_dir / ".virtual-gpio.yaml"
    if owned.exists() or gpio.exists() or fixture.exists() or (config_dir / ".virtual-https.yaml").exists():
        raise ValueError("Gebruik een eigen verse firmwarecheckout en fixturemap")
    patch = ASSETS / "gpio-rx.patch"
    checked = subprocess.run(["git", "apply", "--check", str(patch)], cwd=root, capture_output=True, text=True)
    if checked.returncode:
        raise ValueError("De GPIO-ontvangstpatch past niet op de gekozen bronrevisie: " + checked.stderr.strip())
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    before = {name: digest(root / name) for name in COMPONENTS}
    subprocess.run(["git", "apply", str(patch)], cwd=root, check=True)
    owned.mkdir()
    for name in ("lab_gpio_ot_rx.h", "lab_gpio_ot_rx.cpp"):
        shutil.copyfile(ASSETS / name, owned / name)
    gpio.write_text("packages:\n  q_duo_hil: !include " + json.dumps(str(config_dir / "duo_hil.yaml")) +
        "\nsubstitutions:\n  device_name: openquatt-ot-lab\n  friendly_name: OpenQuatt Virtual HIL Lab\n" +
        "  project_name: openquatt.gpio-ot-lab\nesphome:\n  build_flags:\n    - -DOQ_LAB_GPIO_OT_RX=1\n" +
        "  includes:\n    - " + json.dumps(str(owned / "lab_gpio_ot_rx.h")) +
        "\n    - " + json.dumps(str(owned / "lab_gpio_ot_rx.cpp")) + "\n")
    identity = prepare(root, fixture)
    overlay = Path(identity["overlay"])
    text = overlay.read_text()
    old = json.dumps(str(config_dir / "duo.yaml"))
    if text.count(old) != 1:
        raise ValueError("Onverwachte HTTPS-overlay")
    overlay.write_text(text.replace(old, json.dumps(str(gpio)), 1))
    recipe = {"schema": 1, "profile": PROFILE, "test_only": True, "source_revision": revision,
              "upstream_gpio_revision": assets["revision"], "patch_sha256": digest(patch),
              "components_before": before, "components_after": {name: digest(root / name) for name in COMPONENTS},
              "gpio_config_sha256": digest(gpio), "gpio_header_sha256": digest(owned / "lab_gpio_ot_rx.h"),
              "gpio_cpp_sha256": digest(owned / "lab_gpio_ot_rx.cpp")}
    (owned / "recipe.json").write_text(json.dumps(recipe, indent=2) + "\n")
    identity.update(profile=PROFILE, overlay_sha256=digest(overlay), gpio_recipe=recipe)
    (fixture / "fixture.json").write_text(json.dumps(identity, indent=2) + "\n")
    return {"config": str(overlay), "build_dir": str(root / ".esphome/build/virtual_https/build"),
            "fixture": str(fixture), "recipe": recipe}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--firmware-root", type=Path, required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(prepare_lab(args.firmware_root, args.fixture)))
