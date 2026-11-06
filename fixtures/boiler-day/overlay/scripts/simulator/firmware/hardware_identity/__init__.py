"""Read-only identity markers for the isolated Q hardware qualification build."""
import esphome.codegen as cg
import esphome.config_validation as cv
from esphome.const import CONF_ID
DEPENDENCIES = ["esp32"]
identity_ns = cg.esphome_ns.namespace("hardware_identity")
HardwareIdentity = identity_ns.class_("HardwareIdentity", cg.PollingComponent)
CONFIG_SCHEMA = cv.Schema({cv.GenerateID(): cv.declare_id(HardwareIdentity)}).extend(cv.polling_component_schema("5s"))

async def to_code(config):
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)
