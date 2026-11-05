"""Check the actual HTTP route before and after a virtual W5500 link loss."""
import json
import time

MODES = ("wifi", "ethernet", "failover")
ETHERNET_IP = "10.42.0.15"
CONNECTION_ENTITY = "/text_sensor/OpenQuatt%20Connection"
IP_ENTITY = "/text_sensor/IP%20Address"
AUTOMATIC_REQUEST = "/select/Preferred%20Connection/set?option=Automatic"


class NetworkProbe:
    def __init__(self, mode, ports, cable):
        self.ports = ports
        self.cable = cable
        phase = {"wifi": "wifi", "ethernet": "ethernet", "failover": "provision"}[mode]
        self.result = {
            "mode": mode, "phase": phase, "ethernet_checks": 0, "wifi_checks": 0, "wifi_recovery_checks": 0,
            "ethernet_ip": None, "wifi_ip": None, "cable_cut_s": None,
            "wifi_restored_s": None, "ethernet_http_lost": False,
        }
        if mode != "wifi":
            self.set_cable(mode == "ethernet")

    @property
    def port(self):
        return self.ports["ethernet"] if self.result["phase"] == "ethernet" else self.ports["http"]

    def set_cable(self, connected):
        pending = self.cable.with_suffix(".next")
        pending.write_text("1\n" if connected else "0\n")
        pending.replace(self.cable)

    @staticmethod
    def entity(get, port, path):
        body = get(path, port=port, timeout=1)
        return json.loads(body).get("value") if body else None

    def observe(self, get, started):
        """Return true when the current HTTP response belongs to the expected interface."""
        state = self.result
        if state["mode"] == "wifi":
            return True
        port = self.port
        active = self.entity(get, port, CONNECTION_ENTITY)
        expected = "Ethernet" if state["phase"] == "ethernet" else "WiFi"
        if active != expected:
            return False
        ip = self.entity(get, port, IP_ENTITY)
        if expected == "Ethernet":
            if ip != ETHERNET_IP:
                return False
            state["ethernet_checks"] += 1
            state["ethernet_ip"] = ip
            if state["mode"] == "failover" and state["ethernet_checks"] == 3:
                cut_time = time.monotonic() - started
                self.set_cable(False)
                state["phase"] = "recovery"
                state["cable_cut_s"] = cut_time
        elif not ip or ip in ("0.0.0.0", ETHERNET_IP):
            return False
        else:
            state["wifi_checks"] += 1
            state["wifi_ip"] = ip
            if state["phase"] == "provision":
                self.set_cable(True)
                state["automatic_request_ack"] = get(AUTOMATIC_REQUEST, port=port, method="POST") is not None
                state["phase"] = "ethernet"
            else:
                state["wifi_recovery_checks"] += 1
                if state["wifi_restored_s"] is None:
                    state["wifi_restored_s"] = time.monotonic() - started
        state["active"] = active
        return True

    def verify_cut(self, get):
        state = self.result
        if state["phase"] == "recovery" and not state["ethernet_http_lost"]:
            state["ethernet_http_lost"] = get(CONNECTION_ENTITY, port=self.ports["ethernet"], timeout=0.5, any_status=True) is None
