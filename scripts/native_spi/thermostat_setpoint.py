"""Exercise the production OpenTherm setpoint boundaries without heating."""
import json

from .dynamics import Dynamics, entity, near, value
from .lowflow import command_value

CASES = ((21.0, 19.0, True), (0.0, 0.0, False), (4.5, 19.0, False),
         (5.0, 19.0, True), (35.0, 19.0, True), (35.5, 19.0, False), (21.0, 19.0, True))


def set_input(path, value):
    pending = path.with_suffix(".next")
    pending.write_text(f"{value}\n")
    pending.replace(path)


class ThermostatSetpoint(Dynamics):
    def __init__(self, thermostat, fleet):
        super().__init__(thermostat, fleet)
        self.setpoint = thermostat.parent / "ott-setpoint"
        self.room = thermostat.parent / "ott-room"
        self.index = 0
        self.started = 0
        self.result.update(version=1, measurements=[])

    def inject(self):
        setpoint, room, _ = CASES[self.index]
        set_input(self.setpoint, setpoint)
        set_input(self.room, room)
        self.started = self.fleet.now_ms
        self.result["stage"] = f"setpoint_{self.index}"

    def observe(self, get):
        if self.result["stage"] == "complete":
            return
        if self.result["stage"] == "configure":
            if self.configure(get):
                self.inject()
            return
        setpoint, room, valid = CASES[self.index]
        raw = value(get, "sensor", "OT - Room Setpoint")
        selected = get(entity("sensor", "Room Setpoint (Selected)"), timeout=1)
        selected_reading = json.loads(selected) if selected else {}
        selected_value = selected_reading.get("value")
        selected_ok = near(selected_value, setpoint) if valid else (
            "value" in selected_reading and selected_value in (None, "NaN"))
        room_value = value(get, "sensor", "Room Temperature (Selected)")
        linked = value(get, "binary_sensor", "OT - Thermostat Status Valid")
        now = self.fleet.now_ms
        if (now - self.started >= 12000 and near(raw, setpoint) and selected_ok
                and near(room_value, room) and linked is True):
            self.result["measurements"].append({
                "setpoint_c": setpoint, "selected_c": selected_value, "room_c": room_value,
                "link_valid": linked, "injected_ms": self.started, "confirmed_ms": now,
            })
            self.index += 1
            if self.index == len(CASES):
                self.result["stage"] = "complete"
            else:
                self.inject()

    def finalize(self, report):
        self.result["checks"] = {
            "all_setpoints_confirmed": len(self.result["measurements"]) == len(CASES),
            "heating_stays_disabled": all(
                command_value(row, 1999) in (None, 0) and command_value(row, 3999) in (None, 0)
                for row in report["writes"]
            ),
        }
