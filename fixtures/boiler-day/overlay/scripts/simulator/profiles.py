"""Explicit test targets; physical profiles can only be built, never emulated."""
from __future__ import annotations
from dataclasses import dataclass


@dataclass(frozen=True)
class LabProfile:
    name: str
    identity: str
    config: str
    build: str
    backend: str = "esp-emu"
    loopbacks: str | None = None
    peer: str | None = None


LAB_PROFILES = {
    "duo": LabProfile("duo", "q-duo-local-lab-v1", "configs/emulator/lab_duo.yaml", ".esphome/build/simulator_q_duo/build"),
    "ot-thermostat": LabProfile("ot-thermostat", "q-duo-ot-thermostat-v1", "configs/emulator/opentherm_lab.yaml",
        ".esphome/build/simulator_ot_thermostat/build", loopbacks="1:4", peer="thermostat"),
    "ot-boiler": LabProfile("ot-boiler", "q-duo-ot-boiler-v1", "configs/emulator/opentherm_boiler_lab.yaml",
        ".esphome/build/simulator_ot_boiler/build", loopbacks="0:4,1:2", peer="boiler"),
    "local-flow": LabProfile("local-flow", "q-duo-local-flow-v1", "configs/emulator/flow_lab.yaml",
        ".esphome/build/simulator_flow_lab/build"),
    "hardware-memory": LabProfile("hardware-memory", "q-duo-hardware-memory-v1", "configs/hil/memory_qualification_duo_wifi.yaml",
        ".esphome/build/hardware_memory_q_duo/build", backend="hardware"),
}


def get_profile(name):
    if not isinstance(name, str) or name not in LAB_PROFILES:
        raise ValueError("Unknown lab profile")
    return LAB_PROFILES[name]
