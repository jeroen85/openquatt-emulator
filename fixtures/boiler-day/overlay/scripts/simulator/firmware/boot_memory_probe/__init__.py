"""Test-only bounded early-boot evidence; no reset, retention or actuator API."""
import esphome.codegen as cg
import esphome.config_validation as cv
from esphome.components.esp32 import include_builtin_idf_component
from esphome.const import CONF_ID

DEPENDENCIES = ["esp32", "web_server", "diagnostics_component"]
AUTO_LOAD = ["web_server_base"]
namespace = cg.esphome_ns.namespace("boot_memory_probe")
BootMemoryProbe = namespace.class_("BootMemoryProbe", cg.Component)
CONFIG_SCHEMA = cv.All(
    cv.Schema({cv.GenerateID(): cv.declare_id(BootMemoryProbe)}).extend(cv.COMPONENT_SCHEMA),
    cv.only_on_esp32,
)


async def to_code(config):
    include_builtin_idf_component("app_update")
    include_builtin_idf_component("esp_http_server")
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)
