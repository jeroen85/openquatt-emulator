import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from . import firmware_lab


class SharedLabFirmwareTests(unittest.TestCase):
    def source(self, root):
        config = root/"configs/heatpump_controller_q"
        config.mkdir(parents=True)
        (config/"duo.yaml").write_text("esphome: {}\n")
        (config/"duo_hil.yaml").write_text("packages:\n  production: !include duo.yaml\n")
        # Minimal preimages for the real receiver patch; no product code is executed.
        contents = {
            "components/openquatt_ot_slave/OpenTherm.cpp": "\n"*8 +
                '#include "esp_err.h"\n#include "esp_private/esp_clk.h"\n#include "esp_timer.h"\n'
                'static const char* TAG = "oq.ot.thermostat.transport";\nusing namespace esphome;\n\n',
            "components/opentherm/opentherm.cpp": "\n"*16 +
                '#endif\n#include <string>\n\nnamespace esphome::opentherm {\n\nusing std::string;\n',
        }
        for name, text in contents.items():
            (root/name).parent.mkdir(parents=True, exist_ok=True);(root/name).write_text(text)
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        subprocess.run(["git", "add", "."], cwd=root, check=True)
        subprocess.run(["git", "-c", "user.name=HIL", "-c", "user.email=hil@example.invalid",
                        "commit", "-qm", "fixture"], cwd=root, check=True)
        return config

    def test_one_recipe_keeps_production_config_and_shares_gpio_with_https_trust(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)/"source";root.mkdir();config=self.source(root)
            original=(config/"duo.yaml").read_bytes()
            result=firmware_lab.prepare_lab(root,Path(temporary)/"private-tls")
            self.assertEqual((config/"duo.yaml").read_bytes(), original)
            self.assertTrue(result["recipe"]["test_only"])
            self.assertEqual(result["recipe"]["profile"], firmware_lab.PROFILE)
            self.assertIn(str(config/".virtual-gpio.yaml"), Path(result["config"]).read_text())
            self.assertIn("openquatt-ot-lab", (config/".virtual-gpio.yaml").read_text())
            self.assertIn("OQ_LAB_GPIO_OT_RX", (root/firmware_lab.COMPONENTS[0]).read_text())
            fixture=Path(result["fixture"])
            self.assertEqual((fixture/"server.key").stat().st_mode & 0o777, 0o600)
            self.assertFalse(any(p.suffix==".key" for p in (root/".virtual-hil").iterdir()))
            self.assertEqual(json.loads((fixture/"fixture.json").read_text())["profile"], firmware_lab.PROFILE)
            with self.assertRaisesRegex(ValueError,"verse firmwarecheckout"):
                firmware_lab.prepare_lab(root,Path(temporary)/"other-tls")

    def test_incompatible_source_fails_before_any_patch_or_ca_creation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)/"source";root.mkdir();self.source(root)
            path=root/firmware_lab.COMPONENTS[0];path.write_text("incompatible source\n")
            before={name:(root/name).read_bytes() for name in firmware_lab.COMPONENTS}
            with self.assertRaisesRegex(ValueError,"past niet"):
                firmware_lab.prepare_lab(root,Path(temporary)/"private-tls")
            self.assertEqual(before,{name:(root/name).read_bytes() for name in firmware_lab.COMPONENTS})
            self.assertFalse((root/".virtual-hil").exists())
            self.assertFalse((Path(temporary)/"private-tls").exists())

    def test_modified_vendor_source_is_rejected_before_touching_firmware(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)/"source";root.mkdir();self.source(root)
            assets=Path(temporary)/"assets";assets.mkdir()
            (assets/"changed.h").write_text("corrupt")
            (assets/"provenance.json").write_text(json.dumps({"files":{"changed.h":"0"*64}}))
            with patch.object(firmware_lab,"ASSETS",assets), self.assertRaisesRegex(ValueError,"herkomstgegevens"):
                firmware_lab.prepare_lab(root,Path(temporary)/"private-tls")
            self.assertFalse((root/".virtual-hil").exists())
