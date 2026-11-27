"""Exercise a real encrypted ESPHome API client alongside the MQTT fixture."""

import asyncio
import base64
import os
import threading

from .mqtt_reconnect import MqttReconnect

VALID = "Heating Enable Valid"
SELECTED = "Heating Enable (Selected)"
PROBE_TEMPERATURE = "Water Supply Temp (PT1000)"


def api_dependencies():
    try:
        from aioesphomeapi import APIClient, APIConnectionError, ZERO_NOISE_PSK
        if not hasattr(APIClient, "noise_encryption_set_key"):
            raise ImportError("Noise provisioning is missing")
    except ImportError as exc:
        raise ValueError("--ha-api vereist aioesphomeapi met Noise-provisioning") from exc
    return APIClient, APIConnectionError, ZERO_NOISE_PSK


class NativeApiProbe:
    def __init__(self, port, guest_clock):
        self.api_client, self.connection_error, self.zero_psk = api_dependencies()
        self.port = port
        self.clock = guest_clock
        self.key = base64.b64encode(os.urandom(32))
        self.ready = threading.Event()
        self.stopped = threading.Event()
        self.reconnect = threading.Event()
        self.recovery_ready = threading.Event()
        self.result = {"provisioned": False, "sessions": [], "unexpected_disconnects": []}
        self.worker = threading.Thread(target=self.run, name="ha-noise-api", daemon=True)
        self.loop = self.task = None
        self.last_valid_ms = -1
        self.failure = None
        self.lock = threading.Lock()

    def close(self):
        self.stopped.set()
        with self.lock:
            if self.loop and self.task:
                self.loop.call_soon_threadsafe(self.task.cancel)
        if self.worker.ident is not None:
            self.worker.join(timeout=4)
        return not self.worker.is_alive()

    async def owned_session(self):
        with self.lock:
            self.loop = asyncio.get_running_loop()
            self.task = asyncio.current_task()
        try:
            if not self.stopped.is_set():
                await self.exercise()
        finally:
            with self.lock:
                self.loop = self.task = None

    def client(self, key):
        return self.api_client(
            "127.0.0.1", self.port, None, client_info="OpenQuatt HIL HA API",
            noise_psk=key, keepalive=10,
        )

    def run(self):
        try:
            asyncio.run(self.owned_session())
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            with self.lock:
                self.failure = type(exc).__name__
            self.result["error_type"] = self.failure

    async def disconnect(self, client):
        await asyncio.wait_for(client.disconnect(force=True), 2)

    async def provision(self):
        while not self.stopped.is_set():
            bootstrap = self.client(self.zero_psk)
            try:
                try:
                    await asyncio.wait_for(bootstrap.connect(login=True), 3)
                except (self.connection_error, OSError, TimeoutError):
                    pass
                else:
                    info = await asyncio.wait_for(bootstrap.device_info(), 5)
                    if not info.api_encryption_provisionable:
                        raise RuntimeError("API provisioning closed on fresh flash")
                    self.result["provisioned"] = await asyncio.wait_for(
                        bootstrap.noise_encryption_set_key(self.key), 5
                    )
                    self.result["provisioned_ms"] = self.clock()
                    if not self.result["provisioned"]:
                        raise RuntimeError("API refused the test key")
                    return
            finally:
                await self.disconnect(bootstrap)
            await asyncio.sleep(1)

    async def exercise(self):
        await self.provision()
        # ESPHome activates a stored key on the guest loop after acknowledging it.
        await asyncio.sleep(2)
        if self.stopped.is_set() or not self.result["provisioned"]:
            return
        for ordinal in (1, 2):
            session = None
            client = self.client(self.key.decode())
            try:
                await asyncio.wait_for(client.connect(login=True), 8)
                info, entities, _ = await asyncio.wait_for(
                    client.device_info_and_list_entities(), 20
                )
                names = {entity.key: entity.name for entity in entities
                         if entity.name in (VALID, SELECTED, PROBE_TEMPERATURE)}
                if len(names) != 3:
                    self.result["matched_entities"] = sorted(names.values())
                    raise RuntimeError("Expected HA entities not exposed")
                session = {
                    "ordinal": ordinal, "subscribed_ms": self.clock(),
                    "esphome_version": info.esphome_version, "entity_count": len(entities),
                    "states": [],
                }
                self.result["sessions"].append(session)

                def receive(state):
                    name = names.get(state.key)
                    if name and len(session["states"]) < 10000 and not getattr(state, "missing_state", False):
                        now = self.clock()
                        session["states"].append({"guest_ms": now, "entity": name, "state": state.state})
                        if ordinal == 1 and name == VALID and state.state is True:
                            with self.lock:
                                self.last_valid_ms = now

                client.subscribe_states(receive)
                if ordinal == 1:
                    self.ready.set()
                else:
                    self.recovery_ready.set()
                while not self.stopped.is_set():
                    if client.api_version is None:
                        return
                    if ordinal == 1 and self.reconnect.is_set():
                        break
                    await asyncio.sleep(0.2)
            finally:
                if session is not None:
                    if client.api_version is None:
                        self.result["unexpected_disconnects"].append(ordinal)
                    session["end_ms"] = self.clock()
                await self.disconnect(client)
            if self.stopped.is_set():
                break
            await asyncio.sleep(1)


class HaMqttReconnect(MqttReconnect):
    def __init__(self, *args, api_port):
        super().__init__(*args)
        self.api = NativeApiProbe(api_port, self.clock)

    def start(self, *args):
        super().start(*args)
        self.api.worker.start()

    def observe(self, get):
        with self.api.lock:
            error = self.api.failure
            last_valid_ms = self.api.last_valid_ms
        if error:
            raise RuntimeError(f"Native HA API failed in {error}")
        if not self.api.ready.is_set():
            return
        super().observe(get)
        fresh = self.result["checkpoints"].get("fresh_applied")
        if fresh and last_valid_ms >= self.result["checkpoints"]["fresh_publish"]["guest_ms"]:
            self.api.reconnect.set()

    def windows(self):
        return super().windows() if self.api.recovery_ready.is_set() else None

    def stop_workers(self):
        try:
            self.api.result["client_stopped"] = self.api.close()
        finally:
            super().stop_workers()

    def finalize(self, report):
        super().finalize(report)
        api = self.result["ha_api"] = (
            self.api.result if self.api.result["client_stopped"] else
            {"client_stopped": False, "provisioned": False, "sessions": [], "unexpected_disconnects": []}
        )
        points = self.result["checkpoints"]
        sessions = api["sessions"]
        first = sessions[0]["states"] if sessions else []
        second = sessions[1]["states"] if len(sessions) > 1 else []
        init = points.get("initial_publish", {}).get("guest_ms", -1)
        cut = points.get("cut_ms", -1)
        restore = points.get("restore_ms", -1)
        fresh = points.get("fresh_publish", {}).get("guest_ms", -1)

        def got(events, name, expected, start, end):
            return 0 <= start <= end and any(
                item["entity"] == name and item["state"] is expected
                and start <= item["guest_ms"] <= end for item in events
            )

        overlap = ha_overlap(sessions, self.result["web_load"], init, cut, restore, fresh)
        self.result["client_overlap"] = overlap
        self.result["checks"].update(
            ha_mqtt_http_sse_overlap=bool(overlap["before"] and overlap["reconnected"]),
            ha_http_sse_during_mqtt_outage=len(overlap["outage"]) >= 2,
            native_api_key_and_login=api["provisioned"] and bool(sessions),
            native_api_mqtt_initial=got(first, VALID, True, init, cut)
            and got(first, SELECTED, True, init, cut),
            native_api_mqtt_outage=got(first, VALID, False, cut, restore),
            native_api_mqtt_recovery=got(first, VALID, True, fresh, sessions[0].get("end_ms", -1))
            if sessions else False,
            native_api_reconnect_states=len(sessions) == 2
            and sessions[1]["subscribed_ms"] >= fresh >= 0
            and got(second, VALID, True, sessions[1]["subscribed_ms"], sessions[1].get("end_ms", -1))
            and got(second, SELECTED, False, sessions[1]["subscribed_ms"], sessions[1].get("end_ms", -1)),
            native_api_teardown=api["client_stopped"] and not api["unexpected_disconnects"]
            and "error_type" not in api,
        )


def ha_overlap(sessions, workload, initial, cut, restored, fresh):
    """Keep complete GET/SSE overlaps within one API session and MQTT phase."""
    result = {"before": [], "outage": [], "reconnected": []}
    if len(sessions) != 2 or not workload or not workload["valid"] or not 0 <= initial <= cut <= restored <= fresh:
        return result
    confirmed = set(workload["confirmed_overlap_targets_ms"])
    for row in workload["gets"]:
        if row["target_ms"] not in confirmed:
            continue
        begin, end = row["start"]["guest_ms"], row["last_byte"]["guest_ms"]
        for name, session, mqtt_begin, mqtt_end in (
            ("before", sessions[0], initial, cut),
            ("outage", sessions[0], cut, restored),
            ("reconnected", sessions[1], fresh, None),
        ):
            window_end = session.get("end_ms", -1) if mqtt_end is None else min(
                session.get("end_ms", -1), mqtt_end)
            if max(session["subscribed_ms"], mqtt_begin) <= begin <= end <= window_end:
                result[name].append({"target_ms": row["target_ms"], "start_ms": begin, "end_ms": end})
    return result
