"""Test-only pin-level flow generator for the real Q pulse_meter input."""
import esphome.codegen as cg
import esphome.config_validation as cv
from esphome.components.esp32 import include_builtin_idf_component
from esphome.const import CONF_ID

DEPENDENCIES = ["esp32"]
namespace = cg.esphome_ns.namespace("virtual_flow")
VirtualFlow = namespace.class_("VirtualFlow", cg.Component)
CONFIG_SCHEMA = cv.Schema({cv.GenerateID(): cv.declare_id(VirtualFlow)}).extend(cv.COMPONENT_SCHEMA)


async def to_code(config):
    include_builtin_idf_component("esp_driver_gpio")
    include_builtin_idf_component("esp_timer")
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)
