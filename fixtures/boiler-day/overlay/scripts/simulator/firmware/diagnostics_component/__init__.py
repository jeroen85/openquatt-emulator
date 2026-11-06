"""Early setup for the isolated, test-only firmware diagnostics probe."""
import esphome.codegen as cg
import esphome.config_validation as cv
from esphome.const import CONF_ID

DEPENDENCIES = ["esp32"]

namespace = cg.esphome_ns.namespace("diagnostics_component")
LabDiagnosticsComponent = namespace.class_("LabDiagnosticsComponent", cg.Component)

CONFIG_SCHEMA = cv.Schema({cv.GenerateID(): cv.declare_id(LabDiagnosticsComponent)}).extend(cv.COMPONENT_SCHEMA)


async def to_code(config):
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)
