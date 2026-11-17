"""Timed GPIO OpenTherm peers for the explicit Q Duo lab image."""
import struct

from . import cosim

HALF_NS = 500_000
SECOND = 1_000_000_000


def word(kind, data_id, data):
    raw = (kind << 28) | (data_id << 16) | data
    return raw | ((raw.bit_count() & 1) << 31)


def fields(raw):
    return (raw >> 28) & 7, (raw >> 16) & 255, raw & 65535


def waveform(raw, start):
    previous = False
    bits = [1, *((raw >> shift) & 1 for shift in range(31, -1, -1)), 1]
    for index, bit in enumerate(bits):
        for half, level in enumerate((bit, 1 - bit)):
            if level != previous:
                yield start + (index * 2 + half) * HALF_NS, level
                previous = level


class Outgoing:
    """Read the controller's active-low pulses and check each decoded frame."""

    def __init__(self):
        self.edges = []

    def edge(self, at, level):
        if self.edges and at - self.edges[-1][0] > 2 * HALF_NS + 5_000:
            self.edges.clear()
        if not self.edges and level:
            return None
        self.edges.append((at, level))
        start = self.edges[0][0]
        if abs(at - start - 67 * HALF_NS) > 5_000 or level != 1:
            return None
        halves = []
        for (before, value), (after, _) in zip(self.edges, self.edges[1:]):
            duration = after - before
            count = round(duration / HALF_NS)
            if count not in (1, 2) or abs(duration - count * HALF_NS) > 5_000:
                self.edges.clear()
                return None
            halves.extend([value] * count)
        self.edges.clear()
        halves.append(1)
        if len(halves) != 68 or halves[:2] != [0, 1] or halves[-2:] != [0, 1]:
            return None
        if any(halves[i] == halves[i + 1] for i in range(0, 68, 2)):
            return None
        value = int("".join(str(1 - bit) for bit in halves[2:-2:2]), 2)
        if value.bit_count() & 1 or value & 0x0F000000:
            return None
        return start, value


class OpenTherm:
    def __init__(self, thermostat):
        self.thermostat = thermostat
        self.events = []
        self.next_ott = 6 * SECOND
        self.index = 0
        self.target = 0
        self.modulation = 25600
        self.decoders = {14: Outgoing(), 48: Outgoing()}

    def record(self, link, direction, at, raw):
        if len(self.events) >= 100000:
            raise RuntimeError("OpenTherm wire history limit reached")
        self.events.append({"kind": "opentherm", "link": link, "direction": direction,
                            "guest_us": at // 1000, "word": raw})

    def delivered(self, until_ms):
        """Exclude frames whose scheduled stop bit has not reached the guest yet."""
        return [event for event in self.events if event["guest_us"] + 34000 <= until_ms * 1000]

    def send(self, peer, link, pin, at, raw):
        payload = bytearray()
        for when, level in [(at - 20_000_000, 0), *waveform(raw, at)]:
            event = cosim.uint(1, when) + cosim.message(6, cosim.uint(1, pin) + cosim.uint(2, level))
            payload.extend(struct.pack("<I", len(event)))
            payload.extend(event)
        peer.socket.sendall(payload)
        self.record(link, "peer-tx", at, raw)

    def tick(self, peer, at):
        if at < self.next_ott:
            return
        heat = self.thermostat.read_text().strip()
        if heat not in ("0", "1"):
            raise ValueError("Unknown thermostat heat state")
        raw = (word(0, 0, int(heat) << 8), word(1, 16, 21 * 256),
               word(1, 24, 19 * 256))[self.index % 3]
        self.send(peer, "ott", 21, at + 80_000_000, raw)
        self.index += 1
        self.next_ott = at + SECOND

    def boiler_reply(self, kind, data_id, data):
        if kind == 1:
            if data_id == 1:
                self.target = data
            if data_id == 14:
                self.modulation = data
            return word(5 if data_id in (1, 2, 14, 124, 126) else 7, data_id, data)
        values = {0: data & 0xff00, 1: self.target, 3: 0x0101, 5: 0, 14: self.modulation,
                  15: 0x1414, 17: 0, 18: 384, 25: 10880, 26: 12288, 28: 8960,
                  48: 0x3c23, 49: 0x3c14, 56: 12800, 57: 15360, 115: 0, 125: 563, 127: 0x0101}
        return word(4 if data_id in values else 7, data_id, values.get(data_id, 0))

    def edge(self, peer, pin, at, level):
        if pin not in self.decoders:
            return
        decoded = self.decoders[pin].edge(at, level)
        if decoded is None:
            return
        start, raw = decoded
        link = "ott" if pin == 14 else "otb"
        self.record(link, "controller-tx", start, raw)
        kind, data_id, data = fields(raw)
        if link == "otb" and kind in (0, 1):
            reply = self.boiler_reply(kind, data_id, data)
            self.send(peer, link, 47, start + 110_000_000, reply)
