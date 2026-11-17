"""Espressif 1.0.0 co-simulation peer for the Q-edition MAX31865."""
from __future__ import annotations

import ctypes
import socket
import struct
import threading

from .cosim_ethernet import W5500_CLK, W5500_CS, W5500_IRQ, W5500_MISO, W5500_MOSI, Ethernet

# esp-emulator v1.0.0, proto/esp_cosim.proto and docs/guides/HOST-INTERFACES.md.
MAX_FRAME = 256 * 1024
CS, CLK, MOSI, MISO = 5, 6, 7, 4


def varint(value):
    encoded = bytearray()
    while value >= 128:
        encoded.append((value & 127) | 128)
        value >>= 7
    encoded.append(value)
    return bytes(encoded)


def uint(number, value):
    return varint(number << 3) + varint(value)


def message(number, payload):
    return varint((number << 3) | 2) + varint(len(payload)) + payload


def fields(payload):
    offset = 0

    def read_varint():
        nonlocal offset
        value = 0
        for shift in range(0, 70, 7):
            if offset == len(payload):
                break
            byte = payload[offset]
            offset += 1
            value |= (byte & 127) << shift
            if byte < 128:
                return value
        raise ValueError("Ongeldige co-simulatie-varint")

    result = {}
    while offset < len(payload):
        number, wire = divmod(read_varint(), 8)
        if not number:
            raise ValueError("Ongeldig co-simulatieveld")
        if wire == 0:
            result[number] = read_varint()
            continue
        if wire not in (1, 2, 5):
            raise ValueError("Onbekend co-simulatieveldtype")
        size = read_varint() if wire == 2 else 8 if wire == 1 else 4
        end = offset + size
        if end > len(payload):
            raise ValueError("Onvolledig co-simulatieveld")
        result[number] = payload[offset:end]
        offset = end
    return result


def read_frame(stream):
    header = stream.read(4)
    if not header:
        return None
    if len(header) != 4:
        raise ValueError("Onvolledige co-simulatieframing")
    size = struct.unpack("<I", header)[0]
    if not 2 <= size <= MAX_FRAME:
        raise ValueError("Ongeldige co-simulatieframelengte")
    frame = stream.read(size)
    if len(frame) != size:
        raise ValueError("Onvolledig co-simulatieframe")
    topic_size = struct.unpack_from("<H", frame)[0]
    if 2 + topic_size >= size:
        raise ValueError("Leeg co-simulatiebericht")
    return fields(frame[2 + topic_size:])


def registration(ethernet=False):
    pins = b"".join(uint(index, pin) for index, pin in enumerate((CS, CLK, MOSI, MISO), 1))
    spi = message(3, pins)
    watch = 1 << CS
    drive = 0
    claims = [spi]
    if ethernet:
        eth_pins = (W5500_CS, W5500_CLK, W5500_MOSI, W5500_MISO)
        claims.append(message(3, b"".join(uint(index, pin) for index, pin in enumerate(eth_pins, 1))))
        drive |= 1 << W5500_IRQ
    claims.append(message(1, uint(1, drive) + uint(2, watch)))
    return message(4, uint(1, 1) + message(2, b"OpenQuatt Q Duo") + b"".join(message(3, claim) for claim in claims))


def spi_reply(correlation_id, data):
    return message(8, uint(1, correlation_id) + uint(2, 1) + message(3, data) + uint(4, 2))


class CosimPeer:
    def __init__(self, library, temperature, ethernet=None):
        self.lib = ctypes.CDLL(str(library))
        self.lib.oq_cosim_rtd_create.argtypes = [ctypes.c_double]
        self.lib.oq_cosim_rtd_create.restype = ctypes.c_void_p
        self.lib.oq_cosim_rtd_select.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self.lib.oq_cosim_rtd_select.restype = None
        self.lib.oq_cosim_rtd_exchange.argtypes = [ctypes.c_void_p, ctypes.c_uint8]
        self.lib.oq_cosim_rtd_exchange.restype = ctypes.c_int
        self.lib.oq_cosim_rtd_destroy.argtypes = [ctypes.c_void_p]
        self.lib.oq_cosim_rtd_destroy.restype = None
        self.rtd = self.lib.oq_cosim_rtd_create(temperature)
        if not self.rtd:
            raise RuntimeError("Kon de MAX31865-fixture niet maken")
        self.lan = None
        if ethernet is not None:
            try:
                self.lan = Ethernet(self.lib, *ethernet, self.drive)
            except BaseException:
                self.lib.oq_cosim_rtd_destroy(self.rtd)
                raise
        self.socket = self.thread = None
        self.stopping = threading.Event()
        self.clock_ms = 0
        self.registered = False
        self.transfers = self.replies = self.clock_frames = 0
        self.last_spi_reply_ms = None
        self.error = None

    def start(self, port):
        self.socket = socket.create_connection(("127.0.0.1", port), timeout=2)
        self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.socket.settimeout(4)
        worker = threading.Thread(target=self.serve, name="esp-cosim", daemon=True)
        worker.start()
        self.thread = worker

    def send(self, payload):
        self.socket.sendall(struct.pack("<I", len(payload)) + payload)

    def drive(self, at, pin, level):
        self.send(uint(1, at) + message(6, uint(1, pin) + uint(2, level)))

    def serve(self):
        try:
            self.send(registration(self.lan is not None))
            with self.socket.makefile("rb") as stream:
                while not self.stopping.is_set():
                    packet = read_frame(stream)
                    if packet is None:
                        raise EOFError("Co-simulatieverbinding gesloten")
                    self.observe(packet)
        except Exception as exc:
            if not self.stopping.is_set():
                self.error = str(exc)

    def observe(self, packet):
        at = packet.get(1, 0)
        event_ms = at // 1_000_000
        if 9 in packet:  # ChipReset starts a new guest uptime.
            self.clock_ms = event_ms
            self.last_spi_reply_ms = None
            self.lib.oq_cosim_rtd_select(self.rtd, False)
            if self.lan:
                self.lan.irq = None
            return
        self.clock_ms = max(self.clock_ms, event_ms)
        if 5 in packet:
            ack = fields(packet[5])
            if ack.get(1) != 1 or ack.get(2) != b"esp32s3" or not ack.get(5):
                raise RuntimeError(f"Co-simulatie geweigerd: {ack.get(6, b'').decode(errors='replace')}")
            self.registered = True
        elif 2 in packet:
            self.clock_frames += 1
            if self.lan and self.registered:
                self.lan.service(at)
        elif 3 in packet:
            gpio = fields(packet[3])
            if gpio.get(1) == CS:
                self.lib.oq_cosim_rtd_select(self.rtd, not gpio.get(2))
        elif 7 in packet:
            request = fields(packet[7])
            spi = fields(request[3])
            tx, read_size = spi.get(3, b""), spi.get(4, 0)
            if self.registered and self.lan and spi.get(1) == 2:
                cmd = fields(spi.get(6, b""))
                addr = fields(spi.get(7, b""))
                register, control = cmd.get(3, b""), addr.get(3, b"")
                if ((cmd.get(1), cmd.get(2), addr.get(1), addr.get(2)) != (16, 1, 8, 1)
                        or len(register) != 2 or len(control) != 1 or spi.get(8) or spi.get(11)):
                    raise ValueError(f"Onverwachte W5500 SPI2-header: {cmd!r}, {addr!r}")
                reply = self.lan.transfer(int.from_bytes(register, "big"), control[0], tx, read_size, at)
                if read_size:
                    self.send(spi_reply(request.get(1, 0), reply))
                return
            transfer_size = max(len(tx), read_size)
            if not self.registered or spi.get(1) != 3 or not 1 <= transfer_size <= 64:
                raise ValueError("Onverwachte MAX31865 SPI3-transactie")
            self.lib.oq_cosim_rtd_select(self.rtd, True)
            reply = bytearray()
            for index in range(transfer_size):
                value = self.lib.oq_cosim_rtd_exchange(self.rtd, tx[index] if index < len(tx) else 0)
                if value < 0:
                    raise RuntimeError("MAX31865 weigerde de SPI3-transactie")
                reply.append(value)
            self.transfers += 1
            if read_size:
                self.send(spi_reply(request.get(1, 0), bytes(reply[:read_size])))
                self.replies += 1
                self.last_spi_reply_ms = event_ms

    def close(self):
        self.stopping.set()
        if self.socket is not None:
            try:
                self.socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.socket.close()
        if self.thread is not None:
            self.thread.join(timeout=5)
            if self.thread.is_alive():
                return False
        if self.lan:
            self.lan.close()
        self.lib.oq_cosim_rtd_destroy(self.rtd)
        self.rtd = None
        return True

    def evidence(self):
        return {"registered": self.registered, "clock_frames": self.clock_frames, "spi_transfers": self.transfers,
                "spi_replies": self.replies, "last_spi_reply_ms": self.last_spi_reply_ms, "error": self.error,
                "w5500_spi_transfers": self.lan.transfers if self.lan else 0}
