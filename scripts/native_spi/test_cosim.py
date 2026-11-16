import io
import struct
import unittest

from .cosim import fields, MAX_FRAME, read_frame, spi_reply, uint, message
from .qualification import qualify
from .run import run


def frame(payload, topic=b"SIM/CLOCK"):
    body = struct.pack("<H", len(topic)) + topic + payload
    return struct.pack("<I", len(body)) + body


class CosimProtocolTest(unittest.TestCase):
    def test_framed_clock_time_and_spi_reply(self):
        packet = read_frame(io.BytesIO(frame(uint(1, 400_000_000) + message(2, b""))))
        self.assertEqual(packet, {1: 400_000_000, 2: b""})
        response = fields(fields(spi_reply(513, b"\xbe\xef"))[8])
        self.assertEqual(response, {1: 513, 2: 1, 3: b"\xbe\xef", 4: 2})

    def test_rejects_truncated_and_oversized_frames(self):
        with self.assertRaises(ValueError):
            read_frame(io.BytesIO(frame(uint(1, 2))[:-1]))
        with self.assertRaises(ValueError):
            read_frame(io.BytesIO(struct.pack("<I", MAX_FRAME + 1)))
        with self.assertRaises(ValueError):
            read_frame(io.BytesIO(frame(b"\x80")))

    def test_unsupported_modes_fail_before_loading_firmware(self):
        for options in ({"network": "ethernet"}, {"measure_memory": True}, {"scenario": "lowflow"}):
            with self.subTest(options=options), self.assertRaisesRegex(ValueError, "Q Duo-rooktest"):
                run(None, backend="cosim", **options)

    def test_retained_web_value_cannot_hide_stale_spi_data(self):
        report = {"duration_s": 90, "backend": "cosim", "pt1000_verified": True, "guest_uptime_ms": 90_000,
                  "cosim": {"registered": True, "spi_replies": 10, "last_spi_reply_ms": 60_000}}
        self.assertFalse(qualify(report)[0]["spi"])
        report["cosim"]["last_spi_reply_ms"] = 89_000
        self.assertTrue(qualify(report)[0]["spi"])
