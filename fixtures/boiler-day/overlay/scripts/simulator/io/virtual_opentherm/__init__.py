"""Test-only RMT peers; no production sensor or control state is published here."""
import hashlib
from pathlib import Path
from esphome.components.esp32 import include_builtin_idf_component
from esphome.components import switch
import esphome.codegen as cg
import esphome.config_validation as cv
from esphome.const import CONF_ID

DEPENDENCIES = ["esp32"]
peer_ns = cg.esphome_ns.namespace("virtual_opentherm")
VirtualOpenTherm = peer_ns.class_("VirtualOpenTherm", cg.Component)

def validate_role(config):
    guarded = "disabled_thermostat_switch" in config
    if config["role"] == "boiler" and not guarded:
        raise cv.Invalid("boiler peer requires disabled_thermostat_switch to reserve its RX channel safely")
    if config["role"] == "thermostat" and guarded:
        raise cv.Invalid("thermostat ingress must not disable its production receiver")
    return config


CONFIG_SCHEMA = cv.All(cv.Schema({
    cv.GenerateID(): cv.declare_id(VirtualOpenTherm),
    cv.Required("role"): cv.one_of("thermostat", "boiler", lower=True),
    cv.Optional("disabled_thermostat_switch"): cv.use_id(switch.Switch),
}).extend(cv.COMPONENT_SCHEMA), validate_role)


async def to_code(config):
    include_builtin_idf_component("esp_driver_rmt")
    root = Path(__file__).resolve().parents[4]
    sources = sorted(path for path in Path(__file__).resolve().parent.glob("*.*") if path.is_file())
    sources += [root / "components/opentherm" / name for name in (
        "opentherm.cpp", "opentherm.h", "opentherm_rmt_encoder.h",
        "opentherm_rmt_decoder.h")]
    sources += [root / "components/openquatt_ot_slave" / name for name in (
        "OpenTherm.cpp", "OpenTherm.h", "OpenThermRmtDecoder.h",
        "OpenQuattOTSlave.cpp", "OpenQuattOTSlave.h", "room_signal_freshness.h")]
    fingerprint = hashlib.sha256()
    for path in sources:
        fingerprint.update(str(path.relative_to(root)).encode() + b"\0" + path.read_bytes() + b"\0")
    cg.add_define("OQ_VIRTUAL_OT_SOURCE_SHA256", fingerprint.hexdigest())
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)
    cg.add(var.set_boiler_role(config["role"] == "boiler"))
    if "disabled_thermostat_switch" in config:
        disabled = await cg.get_variable(config["disabled_thermostat_switch"])
        cg.add(disabled.set_internal(True))
        cg.add(var.set_disabled_thermostat_switch(disabled))
