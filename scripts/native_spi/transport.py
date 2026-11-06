"""Bounded Modbus RTU frames over the emulator's UART TCP bridge.

This exercises the firmware UART/Modbus stack, not RS485 electrical/parity timing.
"""
from __future__ import annotations

import collections
import socket
import struct
import threading
import time


def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ (0xA001 if crc & 1 else 0)
    return crc


def frame(data: bytes) -> bytes:
    return data + struct.pack("<H", crc16(data))


class RtuPeer:
    def __init__(self, fleet, guest_clock=None):
        self.fleet = fleet
        self.guest_clock = guest_clock
        self.clock_last_progress_at = None
        self.lock = threading.RLock()
        self.buffer = bytearray()
        self.stats = {"requests": 0, "responses": 0, "crc_errors": 0, "dropped": 0,
                      "timeouts": 0, "exceptions": 0, "unknown_addresses": 0}
        self.faults = {"drop_every": 0, "corrupt_every": 0, "delay_ms": 2}
        self.recent = collections.deque(maxlen=80)
        self.writes = collections.deque(maxlen=2000)
        self.last_data_at = 0.0
        self.connected = False
        self.error = None
        self.stop = threading.Event()
        self.sock = None
        self.thread = None

    def configure_faults(self, **values):
        with self.lock:
            if set(values) - set(self.faults):
                raise ValueError("Unknown transport fault")
            for name, value in values.items():
                if type(value) is not int or not 0 <= value <= (5000 if name == "delay_ms" else 10000):
                    raise ValueError("Transport fault values must be bounded non-negative integers")
            self.faults.update(values)

    def snapshot(self):
        with self.lock:
            return {**self.stats, **self.faults, "connected": self.connected, "error": self.error,
                    "recent": list(self.recent), "uart": 1, "wire_timing": False}

    def write_history(self):
        with self.lock:
            return list(self.writes)

    def feed(self, data: bytes, now: float | None = None) -> list[bytes]:
        responses = []
        now = time.monotonic() if now is None else now
        with self.lock:
            expired = bool(self.buffer and now - self.last_data_at > 0.25)
            if data:
                self.last_data_at = now
            self.buffer.extend(data)
            if len(self.buffer) > 1024:
                del self.buffer[:-256]
                self.stats["timeouts"] += 1
            while len(self.buffer) >= 8:
                if self.buffer[0] not in (1, 2):
                    del self.buffer[0]
                    continue
                fc = self.buffer[1]
                size = 9 + self.buffer[6] if fc == 16 else 8
                if size > 255:
                    del self.buffer[0]
                    continue
                if len(self.buffer) < size:
                    if expired:
                        self.stats["timeouts"] += 1
                        del self.buffer[0]
                        continue
                    break
                candidate = bytes(self.buffer[:size])
                if crc16(candidate[:-2]) != int.from_bytes(candidate[-2:], "little"):
                    if candidate[0] in (1, 2) and fc in (3, 6, 16):
                        self.stats["crc_errors"] += 1
                    del self.buffer[0]
                    continue
                del self.buffer[:size]
                response = self._handle(candidate)
                if response is not None:
                    responses.append(response)
        return responses

    def _handle(self, data: bytes) -> bytes | None:
        address, fc = data[:2]
        if address not in (1, 2):
            self.stats["unknown_addresses"] += 1
            return None
        index = address - 1
        with self.fleet.lock:
            if not self.fleet.online[index] or self.fleet.profiles[index] == "Disabled":
                self.stats["dropped"] += 1
                return None
            self.stats["requests"] += 1
            sequence = self.stats["requests"]
            start, count_or_value = struct.unpack(">HH", data[2:6])
            count = 1 if fc == 6 else count_or_value
            dropped = bool(self.faults["drop_every"] and sequence % self.faults["drop_every"] == 0)
            # Receipt resets the ODU watchdog even when its ACK is deliberately lost.
            self.fleet.observe_request(index, address, fc, start, count, dropped)
            exception = 0
            payload = b""
            write_values = []
            if fc == 3:
                if not 1 <= count <= 125 or start + count > 65536:
                    exception = 3
                else:
                    values = [self.fleet.read(index, start + offset) for offset in range(count)]
                    if any(v is None for v in values):
                        exception = 2
                    else:
                        payload = bytes([count * 2]) + b"".join(struct.pack(">H", v) for v in values)
            elif fc == 6:
                write_values = [count_or_value]
                if not self.fleet.write(index, start, count_or_value):
                    exception = 3 if self.fleet.read(index, start) is not None else 2
                else:
                    payload = data[2:6]
            elif fc == 16:
                if not 1 <= count <= 123 or data[6] != count * 2 or start + count > 65536:
                    exception = 3
                else:
                    values = list(struct.unpack(">" + "H" * count, data[7:-2]))
                    write_values = values
                    if not self.fleet.write_many(index, start, values):
                        exception = 3
                    else:
                        payload = data[2:6]
            else:
                exception = 1
            self.recent.append({"unit": address, "fc": fc, "register": start, "count": count,
                                "at_ms": self.fleet.now_ms, "exception": exception, "dropped": dropped,
                                "write_values": write_values})
            if fc in (6, 16):
                self.writes.append({"unit": address, "fc": fc, "register": start,
                    "at_ms": self.fleet.now_ms, "values": list(write_values),
                    "exception": exception, "dropped": dropped})
            if exception:
                self.stats["exceptions"] += 1
                response = frame(bytes([address, fc | 0x80, exception]))
            else:
                response = frame(bytes([address, fc]) + payload)
            if dropped:
                self.stats["dropped"] += 1
                return None
            if self.faults["corrupt_every"] and sequence % self.faults["corrupt_every"] == 0:
                response = response[:-1] + bytes([response[-1] ^ 0xFF])
            self.stats["responses"] += 1
            return response

    def start(self, port: int):
        if self.thread:
            raise RuntimeError("UART worker already started")
        self.thread = threading.Thread(target=self._run, args=(port,), name="odu-uart", daemon=True)
        self.thread.start()

    def _advance_clock(self):
        if self.guest_clock is None:
            return
        previous = self.fleet.now_ms
        self.fleet.advance(self.guest_clock())
        if self.fleet.now_ms != previous:
            self.clock_last_progress_at = time.monotonic()

    def _run(self, port: int):
        pending = collections.deque()
        deadline = time.monotonic() + 30
        try:
            while not self.stop.is_set():
                try:
                    self.sock = socket.create_connection(("127.0.0.1", port), timeout=1)
                    break
                except OSError:
                    if time.monotonic() > deadline:
                        raise RuntimeError("UART bridge did not open")
                    self.stop.wait(0.1)
            if self.stop.is_set():
                return
            self.sock.settimeout(0.02)
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            with self.lock:
                self.connected = True
            while not self.stop.is_set():
                self._advance_clock()
                try:
                    # Wake when a queued reply is due, rather than holding it
                    # behind the general 20 ms receive polling interval.
                    timeout = 0.02
                    if pending:
                        timeout = max(0.0001, min(timeout, pending[0][0] - time.monotonic()))
                    self.sock.settimeout(timeout)
                    data = self.sock.recv(512)
                    if not data:
                        if not self.stop.is_set():
                            raise RuntimeError("Emulator UART bridge closed")
                        break
                    self._advance_clock()
                    for response in self.feed(data):
                        with self.lock:
                            delay = self.faults["delay_ms"] / 1000
                        pending.append((time.monotonic() + delay, response))
                except socket.timeout:
                    for response in self.feed(b""):
                        pending.append((time.monotonic(), response))
                while pending and pending[0][0] <= time.monotonic():
                    self.sock.sendall(pending.popleft()[1])
        except (OSError, RuntimeError, ValueError) as exc:
            if not self.stop.is_set():
                with self.lock:
                    self.error = str(exc)
        finally:
            with self.lock:
                self.connected = False
            if self.sock:
                self.sock.close()

    def close(self):
        self.stop.set()
        if self.sock:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        if self.thread:
            self.thread.join(timeout=3)
            if self.thread.is_alive():
                raise RuntimeError("UART worker did not stop")
