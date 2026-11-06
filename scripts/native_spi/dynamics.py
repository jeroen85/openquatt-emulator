"""Exercise the stock controller through OpenTherm and its Modbus feedback."""
import json
from urllib.parse import quote, urlencode


def entity(domain, name):
    return f"/{domain}/" + quote(name, safe="")


def value(get, domain, name):
    response = get(entity(domain, name), timeout=1)
    return json.loads(response).get("value") if response else None


def at_least(value, minimum):
    return type(value) in (int, float) and value >= minimum


def near(value, target, margin=0.1):
    return type(value) in (int, float) and abs(value - target) <= margin


class Dynamics:
    """Progress only after both the fixture and firmware show the expected state."""

    settings = (
        ("switch", "OpenTherm Enabled", True),
        ("select", "Boiler connection", "OpenTherm"),
        ("select", "Room Temperature Source", "OT thermostat"),
        ("select", "Room Setpoint Source", "OT thermostat"),
        ("select", "Heating Enable Source", "OT thermostat"),
        ("select", "Outside Temperature Source", "API input"),
        ("number", "api_input_outside_temperature", 8),
        ("select", "Flow Source", "Outdoor unit"),
        ("select", "Q Flow Source", "Outdoor unit"),
        ("select", "Outdoor Unit Flow Mode", "Flowmeter HP1"),
        ("select", "Flow Control Mode", "Flow Setpoint"),
        ("number", "Flow Setpoint", 800),
        ("select", "CM Override", "Auto"),
    )

    def __init__(self, thermostat, fleet):
        self.thermostat = thermostat
        self.fleet = fleet
        self.result = {"stage": "configure", "checkpoints": {}, "current": {}, "unit": None}
        self.settings_pending = list(self.settings)

    def set_heat(self, enabled):
        pending = self.thermostat.with_suffix(".next")
        pending.write_text("1\n" if enabled else "0\n")
        pending.replace(self.thermostat)

    def configure(self, get):
        for domain, name, expected in self.settings_pending[:]:
            current = value(get, domain, name)
            if domain == "number" and current is not None:
                current = float(current)
            if current == expected:
                self.settings_pending.remove((domain, name, expected))
                continue
            if current is None:
                break
            path = entity(domain, name)
            if domain == "switch":
                path += "/turn_on"
            else:
                key = "option" if domain == "select" else "value"
                path += "/set?" + urlencode({key: expected})
            if get(path, timeout=2, method="POST") is None:
                break
        self.result["pending_settings"] = [name for _, name, _ in self.settings_pending]
        return not self.settings_pending

    def sample(self, get):
        observed_from_ms = self.fleet.now_ms
        unit_number = self.result["unit"]
        firmware = {
            "ott_valid": value(get, "binary_sensor", "OT - Thermostat Status Valid"),
            "heat": value(get, "binary_sensor", "OT - Thermostat CH Enable"),
            "room_c": value(get, "sensor", "OT - Room Temperature"),
            "setpoint_c": value(get, "sensor", "OT - Room Setpoint"),
            "otb_link": value(get, "binary_sensor", "OTB - Boiler Link Available"),
            "boiler_c": value(get, "sensor", "OTB - Boiler Water Temperature"),
            "mode": value(get, "text_sensor", "Control Mode (Label)"),
            "flow_lph": value(get, "sensor", "Flow average (Selected)"),
            "odu_flow_lph": [value(get, "sensor", f"HP{n} - Flow") for n in (1, 2)],
            "frequency_hz": [value(get, "sensor", f"HP{n} - Compressor frequency") for n in (1, 2)],
            "lowflow_fault": value(get, "binary_sensor", "Lowflow fault active"),
        }
        if unit_number:
            firmware.update(
                defrost=value(get, "binary_sensor", f"HP{unit_number} - Defrost"),
                valve=value(get, "binary_sensor", f"HP{unit_number} - 4-Way valve"),
            )
        with self.fleet.lock:
            model = [
                {key: unit[key] for key in ("address", "flow_lph", "frequency_hz", "requested_level",
                                           "inlet_c", "outlet_c", "defrost", "requested_mode")}
                for unit in self.fleet.snapshot()
            ]
            return {"observed_from_ms": observed_from_ms, "guest_ms": self.fleet.now_ms,
                    "firmware": firmware, "odus": model}

    @staticmethod
    def heating(sample, index):
        unit = sample["odus"][index]
        return (unit["requested_level"] > 0 and unit["frequency_hz"] > 3
                and at_least(sample["firmware"]["frequency_hz"][index], 3))

    @staticmethod
    def has_flow(sample):
        units = sample["odus"]
        firmware = sample["firmware"]
        return (at_least(units[0]["flow_lph"], 600) and near(units[0]["flow_lph"], units[1]["flow_lph"])
                and near(units[0]["outlet_c"], units[1]["inlet_c"])
                and all(at_least(flow, 600) for flow in (*firmware["odu_flow_lph"], firmware["flow_lph"])))

    @staticmethod
    def no_flow(sample):
        firmware = sample["firmware"]
        return (all(near(unit["flow_lph"], 0, 1) for unit in sample["odus"])
                and all(near(flow, 0, 1) for flow in (*firmware["odu_flow_lph"], firmware["flow_lph"])))

    def observe(self, get):
        stage = self.result["stage"]
        if stage == "complete":
            return
        if stage == "configure":
            if self.configure(get):
                self.result["stage"] = "standby"
            return
        sample = self.sample(get)
        self.result["current"] = sample
        firmware = sample["firmware"]
        points = self.result["checkpoints"]
        unit_number = self.result["unit"]
        index = unit_number - 1 if unit_number else 0
        if stage == "standby":
            if (firmware["ott_valid"] is True and firmware["heat"] is False
                    and near(firmware["room_c"], 19) and near(firmware["setpoint_c"], 21)
                    and firmware["otb_link"] is True and near(firmware["boiler_c"], 42.5)
                    and self.no_flow(sample) and all(near(freq, 0) for freq in firmware["frequency_hz"])):
                points["standby"] = sample
                self.set_heat(True)
                self.result["stage"] = "heating"
        elif stage == "heating":
            running = [i for i in (0, 1) if self.heating(sample, i)]
            if firmware["heat"] is True and self.has_flow(sample) and running:
                index = max(running, key=lambda i: sample["odus"][i]["frequency_hz"])
                points["heating"] = sample
                self.result["unit"] = index + 1
                self.fleet.set_odu({"index": index, "defrost": True})
                self.result["stage"] = "defrost"
        elif stage == "defrost":
            if (firmware["defrost"] is True and firmware["valve"] is True
                    and sample["odus"][index]["defrost"] and self.has_flow(sample)):
                points["defrost"] = sample
                self.cut_flow(index)
        else:
            self.observe_flow(get, stage, sample, index)

    def cut_flow(self, index):
        self.fleet.set_odu({"index": index, "defrost": False, "no_flow": True})
        self.result["stage"] = "flow_loss"

    def observe_flow(self, get, stage, sample, index):
        firmware = sample["firmware"]
        points = self.result["checkpoints"]
        if stage == "flow_loss":
            if (self.no_flow(sample) and firmware["defrost"] is False
                    and firmware["valve"] is False and not sample["odus"][index]["defrost"]):
                points["flow_loss"] = sample
                self.fleet.set_odu({"index": index, "no_flow": False})
                self.result["stage"] = "recovery"
        elif stage == "recovery":
            if (firmware["otb_link"] is True and firmware["heat"] is True and firmware["lowflow_fault"] is False
                    and self.has_flow(sample) and self.heating(sample, index)):
                points["recovery"] = sample
                self.result["stage"] = "complete"


def wire_evidence(events):
    """Match requests and acknowledgements by direction, ID and virtual time."""
    results = {"ott": {}, "otb": {}}
    for link, report in results.items():
        request_source, reply_source = ("peer-tx", "controller-tx") if link == "ott" else ("controller-tx", "peer-tx")
        requests = {}
        acknowledged = set()
        last_ack_ms = None
        for event in events:
            if event["link"] != link:
                continue
            word = event["word"]
            message = (word >> 28) & 7
            data_id = (word >> 16) & 255
            timestamp = event["guest_us"]
            direction = event["direction"]
            if direction == request_source and message in (0, 1):
                requests[data_id] = (word, timestamp)
            elif direction == reply_source and data_id in requests:
                request, sent = requests[data_id]
                request_type = (request >> 28) & 7
                payload_matches = request_type == 0 or (request & 0xffff) == (word & 0xffff)
                if message == request_type + 4 and payload_matches and 34000 <= timestamp - sent <= 750000:
                    acknowledged.add(data_id)
                    last_ack_ms = timestamp // 1000
                    requests.pop(data_id)
        report["ack_ids"] = sorted(acknowledged)
        report["last_ack_ms"] = last_ack_ms
        report["observed"] = sum(e["link"] == link for e in events)
    return results
