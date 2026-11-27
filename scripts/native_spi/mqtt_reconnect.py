"""Guest MQTT ingress under real, bounded SSE and web asset loads."""
import json
import socket
import threading
from urllib.parse import urlencode

from .defrost_assist import write_setting
from .dynamics import Dynamics, entity, near, value
from .pr_workload import READY_BY_MS, PrWebLoad, plan, summarize_workload

BROKER_IP = "10.42.0.2"
TOPIC_KEY = "heating_enable"
OUTAGE_MS = 26000
QUIET_RECONNECT_MS = 6000
MAX_FINAL_STATUS_AGE_MS = 7000
MAX_CHECK_GAP_MS = 12000


def receive_exact(connection, length):
    data = bytearray()
    while len(data) < length:
        try:
            chunk = connection.recv(length - len(data))
        except TimeoutError:
            continue
        if not chunk:
            raise EOFError
        data.extend(chunk)
    return bytes(data)


def receive_packet(connection):
    kind = receive_exact(connection, 1)[0]
    length = 0
    for shift in range(0, 28, 7):
        byte = receive_exact(connection, 1)[0]
        length |= (byte & 127) << shift
        if length > 4096:
            break
        if byte < 128:
            return kind, receive_exact(connection, length)
    raise ValueError("MQTT-testfixture kreeg een ongeldige pakketlengte")


def topic_field(body, start):
    length = int.from_bytes(body[start:start + 2], "big")
    end = start + 2 + length
    if not length or end > len(body):
        raise ValueError("MQTT-testfixture kreeg een ongeldige topic")
    return body[start + 2:end].decode("utf8"), end


class LocalBroker:
    """Only the MQTT 3.1.1 packet types used by the stock ingress client."""

    def __init__(self, port, clock):
        self.port, self.clock = port, clock
        self.listener = self.connection = self.thread = None
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.online = True
        self.session_at_shutdown = None
        self.sessions = []
        self.publications = []
        self.errors = []

    def start(self):
        self.listener = socket.socket()
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", self.port))
        self.listener.listen(2)
        self.listener.settimeout(0.2)
        self.thread = threading.Thread(target=self.serve, daemon=True, name="hil-mqtt-broker")
        self.thread.start()

    def set_online(self, online, *, shutdown=False):
        with self.lock:
            if shutdown and self.session_at_shutdown is None and self.online and self.connection and self.sessions:
                last = self.sessions[-1]
                if "closed_ms" not in last:
                    self.session_at_shutdown = last["id"]
            self.online = online
            if not online and self.connection:
                try:
                    self.connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            return self.clock()

    def close(self):
        self.stop.set()
        self.set_online(False, shutdown=True)
        if self.listener:
            self.listener.close()
        if self.thread:
            self.thread.join(timeout=2)
        return not self.thread or not self.thread.is_alive()

    def serve(self):
        while not self.stop.is_set():
            try:
                connection, _ = self.listener.accept()
            except TimeoutError:
                continue
            except OSError:
                if not self.stop.is_set():
                    with self.lock:
                        self.errors.append("MQTT-luistersocket stopte onverwacht")
                return
            with connection:
                connection.settimeout(0.25)
                with self.lock:
                    if not self.online:
                        continue
                    self.connection = connection
                try:
                    self.serve_session(connection)
                except (EOFError, OSError):
                    pass  # A disconnected peer or an injected broker outage.
                except ValueError as exc:
                    with self.lock:
                        if len(self.errors) < 10:
                            self.errors.append(str(exc))
                finally:
                    with self.lock:
                        self.connection = None
                        if self.sessions and "closed_ms" not in self.sessions[-1]:
                            self.sessions[-1]["closed_ms"] = self.clock()

    def serve_session(self, connection):
        kind, body = receive_packet(connection)
        if kind != 0x10 or len(body) < 12 or body[:7] != b"\x00\x04MQTT\x04" or body[7] != 2:
            raise ValueError("MQTT-testfixture verwacht een schone MQTT 3.1.1-sessie")
        _, end = topic_field(body, 10)
        if end != len(body):
            raise ValueError("MQTT-testfixture kreeg onverwachte CONNECT-velden")
        with self.lock:
            if len(self.sessions) >= 32:
                raise ValueError("Te veel MQTT-sessies tijdens de proef")
            connection.sendall(b"\x20\x02\x00\x00")
            session = {"id": len(self.sessions) + 1, "connected_ms": self.clock(), "subscriptions": {}}
            self.sessions.append(session)
        while not self.stop.is_set():
            kind, body = receive_packet(connection)
            if kind == 0xC0 and not body:
                with self.lock:
                    connection.sendall(b"\xD0\x00")
            elif kind == 0xE0 and not body:
                return
            elif kind == 0x82 and len(body) >= 6:
                topics = {}
                offset = 2
                while offset < len(body):
                    topic, end = topic_field(body, offset)
                    if end >= len(body) or body[end] > 2 or len(topics) >= 16:
                        raise ValueError("Ongeldig MQTT-abonnement")
                    topics[topic] = min(body[end], 1)
                    offset = end + 1
                with self.lock:
                    connection.sendall(bytes([0x90, 2 + len(topics)]) + body[:2] + bytes(topics.values()))
                    session["subscriptions"].update((topic, self.clock()) for topic in topics)
            else:
                raise ValueError(f"Onverwacht MQTT-pakket van de ingressclient ({kind:#x})")

    def publish(self, topic, enabled, after_session=0):
        with self.lock:
            if not self.online or not self.connection or not self.sessions:
                return None
            session = self.sessions[-1]
            if ("closed_ms" in session or session["id"] <= after_session
                    or topic not in session["subscriptions"]):
                return None
            topic_bytes = topic.encode("utf8")
            body = len(topic_bytes).to_bytes(2, "big") + topic_bytes + (b"true" if enabled else b"false")
            if len(body) >= 128:
                raise ValueError("MQTT-inputtopic is te lang voor de fixture")
            try:
                self.connection.sendall(bytes([0x30, len(body)]) + body)
            except OSError:
                return None
            publication = {"session": session["id"], "guest_ms": self.clock(), "value": enabled}
            self.publications.append(publication)
            return publication

    def evidence(self):
        with self.lock:
            return {
                "sessions": [{**item, "subscriptions": dict(item["subscriptions"])} for item in self.sessions],
                "publications": list(self.publications),
                "errors": list(self.errors),
                "session_at_shutdown": self.session_at_shutdown,
            }


def qualifies_overlap(workload, cut, restored, fresh):
    confirmed = set(workload["confirmed_overlap_targets_ms"])
    gets = [row for row in workload["gets"] if row["target_ms"] in confirmed]
    return (
        workload["valid"]
        and sum(cut <= row["start"]["guest_ms"] and row["last_byte"]["guest_ms"] <= restored for row in gets) >= 2
        and sum(fresh <= row["start"]["guest_ms"] for row in gets) >= 2
        and any(row["last_byte"]["guest_ms"] <= cut for row in gets)
    )


class MqttReconnect(Dynamics):
    def __init__(self, thermostat, fleet, broker, port, clock):
        super().__init__(thermostat, fleet)
        self.broker, self.port, self.clock = broker, port, clock
        self.result.update(version=1)
        self.load = None
        self.topic = ""
        self.last_valid_probe_ms = None

    def start(self, started, deadline, cancelled):
        self.started, self.deadline, self.cancelled = started, deadline, cancelled
        self.broker.start()

    @staticmethod
    def save(get, status, path, **fields):
        response = get(path, method="POST", data=urlencode({"csrf_token": status["csrf_token"], **fields}).encode())
        return response is not None and json.loads(response).get("ok") is True

    @staticmethod
    def heating_enable(get):
        return {
            "valid": value(get, "binary_sensor", "Heating Enable Valid"),
            "enabled": value(get, "binary_sensor", "Heating Enable (Selected)"),
            "source": value(get, "text_sensor", "Heating Enable Effective Source"),
        }

    def observe(self, get):
        stage = self.result["stage"]
        raw = get("/mqtt/status")
        if not raw:
            self.check_probe_age(stage, self.clock())
            self.record_missing_probe()
            return
        status = json.loads(raw)
        self.result["current"] = {"connected": status.get("connected"), "guest_ms": self.clock()}
        if stage == "configure":
            if not (status["enabled"] and status["broker"] == BROKER_IP and status["port"] == 1883):
                self.save(get, status, "/mqtt/save", enabled="true", broker=BROKER_IP, port=1883)
            elif not status["input_enabled"][TOPIC_KEY]:
                self.save(get, status, "/mqtt/input/save", enabled="true", input=TOPIC_KEY)
            elif status["input_accept_retained"][TOPIC_KEY]:
                self.save(get, status, "/mqtt/input/retained/save", accept_retained="false", input=TOPIC_KEY)
            elif (write_setting(get, "select", "Heating Enable Source", "MQTT")
                  and write_setting(get, "select", "Room Temperature Source", "API input")
                  and write_setting(get, "number", "api_input_room_temperature", 19)
                  and write_setting(get, "switch", "OpenTherm Enabled", True)
                  and write_setting(get, "select", "Boiler connection", "OpenTherm")):
                self.topic = status["input_topics"][TOPIC_KEY]
                self.result["topic"] = self.topic
                self.result["stage"] = "initial_publish"
            return
        points = self.result["checkpoints"]
        heat = self.heating_enable(get)
        connected = status.get("connected")
        sample = {"guest_ms": self.clock(), "connected": connected, "heating_enable": heat}
        self.result["current"] = sample
        self.check_probe_age(stage, sample["guest_ms"])
        # A field that was read successfully can contradict the expected
        # state even if another GET in the same observation failed.
        active = heat["valid"] is True or heat["enabled"] is True
        if stage == "outage" and "outage_confirmed" in points and (connected is True or active):
            raise RuntimeError("MQTT werd geldig terwijl de broker nog onbereikbaar was")
        if stage == "resubscribe" and active:
            raise RuntimeError("De oude MQTT-vrijgave werd geldig vóór een nieuw MQTT-bericht")
        if stage in ("measure", "complete") and (
            connected is False or heat["valid"] is False or heat["enabled"] is True
            or (isinstance(heat.get("source"), str) and heat["source"] != "MQTT")
        ):
            raise RuntimeError("De herstelde MQTT-sessie bleef niet geldig")
        if (type(connected) is not bool or type(heat["valid"]) is not bool or type(heat["enabled"]) is not bool
                or not isinstance(heat.get("source"), str)):
            self.record_missing_probe()
            return
        self.last_valid_probe_ms = sample["guest_ms"]
        if stage == "initial_publish":
            if connected and (sent := self.broker.publish(self.topic, True)):
                points["initial_publish"] = sent
                self.result["stage"] = "initial_applied"
        elif stage == "initial_applied":
            room = value(get, "sensor", "Room Temperature (Selected)")
            if connected and heat == {"valid": True, "enabled": True, "source": "MQTT"} and near(room, 19):
                points["initial_applied"] = sample
                offset = self.clock() + 1500 - READY_BY_MS
                schedule = plan(False, offset_ms=offset, extend_ms=44000)
                self.load = PrWebLoad(self.port, self.clock, self.started, self.deadline, self.cancelled, False,
                                      schedule=schedule, sse_timeout_s=180, get_timeout_s=8)
                self.load.ready(self.clock())
                self.result["workload_plan"] = schedule
                self.result["stage"] = "wait_load"
        elif stage == "wait_load":
            if not connected or heat != {"valid": True, "enabled": True, "source": "MQTT"}:
                raise RuntimeError("MQTT-vrijgave viel weg vóór de geplande brokeruitval")
            if self.load.sse.get("first_event") and any(row.get("outcome") == "ok" for row in self.load.gets):
                points["cut_ms"] = self.broker.set_online(False)
                self.result["stage"] = "outage"
        elif stage == "outage":
            if connected or active:
                return
            points.setdefault("outage_confirmed", sample)
            if write_setting(get, "number", "api_input_room_temperature", 20):
                room = value(get, "sensor", "Room Temperature (Selected)")
                if near(room, 20):
                    points["control_progress"] = {"guest_ms": self.clock(), "selected_room_c": room}
            if self.clock() - points["cut_ms"] >= OUTAGE_MS and "control_progress" in points:
                points["restore_ms"] = self.broker.set_online(True)
                self.result["stage"] = "resubscribe"
        elif stage == "resubscribe":
            sessions = self.broker.evidence()["sessions"]
            initial_session = points["initial_publish"]["session"]
            if not (connected and sessions and sessions[-1]["id"] > initial_session
                    and "closed_ms" not in sessions[-1] and self.topic in sessions[-1]["subscriptions"]):
                return
            observed = points.setdefault("reconnected_without_inputs", sample)
            if sample["guest_ms"] - observed["guest_ms"] >= QUIET_RECONNECT_MS:
                if sent := self.broker.publish(self.topic, False, initial_session):
                    points["fresh_publish"] = sent
                    self.result["stage"] = "fresh_applied"
        elif stage == "fresh_applied":
            if connected and heat == {"valid": True, "enabled": False, "source": "MQTT"}:
                points["fresh_applied"] = sample
                self.result["stage"] = "measure"
        elif stage in ("measure", "complete"):
            self.result["last_confirmed_session_ms"] = sample["guest_ms"]

    def check_probe_age(self, stage, now):
        if (stage in ("outage", "resubscribe", "fresh_applied", "measure", "complete") and self.last_valid_probe_ms is not None
                and now - self.last_valid_probe_ms > MAX_CHECK_GAP_MS):
            raise RuntimeError("Te lang geen volledige MQTT-status via de web-app")

    def record_missing_probe(self):
        self.result["incomplete_web_samples"] = self.result.get("incomplete_web_samples", 0) + 1

    def windows(self):
        points = self.result["checkpoints"]
        if self.result["stage"] != "measure":
            return None
        schedule = self.load.schedule["windows_ms"]
        return {
            "before": schedule["before"],
            "outage": [points["cut_ms"], points["restore_ms"]],
            "reconnected": [points["fresh_applied"]["guest_ms"], schedule["load"][1]],
        }

    def stop_workers(self):
        self.result["teardown_guest_ms"] = self.clock()
        self.result["clients_stopped"] = self.load is None or self.load.close()
        self.result["broker_stopped"] = self.broker.close()

    def finalize(self, report):
        points = self.result["checkpoints"]
        evidence = self.result["broker"] = self.broker.evidence()
        load = self.load
        workload = self.result["web_load"] = (
            summarize_workload(load.schedule, load.sse, load.gets, [], load.ready_ms) if load else None
        )
        sessions = evidence["sessions"]
        got_baseline = "initial_applied" in points
        got_outage = {"cut_ms", "restore_ms", "outage_confirmed", "control_progress"}.issubset(points)
        got_recovery = {"fresh_publish", "fresh_applied"}.issubset(points)
        progress = points.get("control_progress", {}).get("guest_ms")
        quiet = points.get("reconnected_without_inputs")
        teardown = self.result.get("teardown_guest_ms")
        last_confirmed = self.result.get("last_confirmed_session_ms")
        self.result["checks"] = {
            "local_workers_stopped": self.result.get("clients_stopped") is True
            and self.result.get("broker_stopped") is True and not evidence["errors"],
            "no_unexpected_reboot": report.get("setup_count") == 1,
            "initial_receipt": got_baseline,
            "outage_and_control_progress": got_outage
            and points["cut_ms"] <= points["outage_confirmed"]["guest_ms"] <= progress <= points["restore_ms"],
            "new_session_applies_fresh_input": got_baseline and got_outage and got_recovery
            and [session["id"] for session in sessions] == [
                points["initial_publish"]["session"], points["fresh_publish"]["session"]
            ]
            and points["cut_ms"] <= sessions[0].get("closed_ms", -1) <= points["restore_ms"]
            and bool(quiet) and points["restore_ms"] <= sessions[1]["connected_ms"] <= quiet["guest_ms"]
            and points["fresh_publish"]["guest_ms"] - quiet["guest_ms"] >= QUIET_RECONNECT_MS
            and points["fresh_publish"]["guest_ms"] <= points["fresh_applied"]["guest_ms"],
            "restored_session_survives_until_teardown": got_recovery and teardown is not None
            and last_confirmed is not None and 0 <= teardown - last_confirmed <= MAX_FINAL_STATUS_AGE_MS
            and evidence["session_at_shutdown"] == points["fresh_publish"]["session"],
            "sse_asset_overlap_through_reconnect": bool(workload) and got_outage and got_recovery
            and qualifies_overlap(workload, points["cut_ms"], points["restore_ms"], points["fresh_applied"]["guest_ms"]),
        }
