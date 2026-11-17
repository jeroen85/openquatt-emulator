import io
import struct
import unittest

from .cosim import fields, MAX_FRAME, read_frame, spi_reply, uint, message
from .memory_comparison import require_match, runtime_fingerprint
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
        for options in ({"network": "ethernet", "scenario": "pr-memory"}, {"memory_export": "recorder"}, {"scenario": "lowflow"}):
            with self.subTest(options=options), self.assertRaisesRegex(ValueError, "Q Duo-rooktest"):
                run(None, backend="cosim", **options)

    def test_ntp_fixture_requires_both_a_reply_and_observed_guest_time(self):
        report = {"duration_s": 90, "ntp_fixture": {"epoch": 1768474320}}
        self.assertFalse(qualify(report)[0]["local_ntp"])
        report["local_services"] = {"ntp": 1}
        self.assertFalse(qualify(report)[0]["local_ntp"])
        report["ntp_fixture"]["guest_epoch_ms"] = 1768474364000
        self.assertTrue(qualify(report)[0]["local_ntp"])

    def test_ethernet_memory_comparisons_bind_slirp_and_the_adapter(self):
        sources = dict.fromkeys(("memory.py", "model.py", "network_probe.py", "transport.py",
                                 "cosim.py", "cosim_ethernet.py"), "a" * 64)
        report = {"backend": "cosim", "mode": "device", "network": {"mode": "ethernet"},
                  "fixture_temperature_c": 30, "duration_s": 90, "input": {"chip": "esp32s3"},
                  "executable_sha256": "b" * 64, "odu_library_sha256": "c" * 64,
                  "native_build": {"slirp": "4.7.0"}, "sources": sources}
        original = runtime_fingerprint(report)
        sources["cosim_ethernet.py"] = "d" * 64
        report["native_build"]["slirp"] = "4.9.5"
        with self.assertRaisesRegex(ValueError, "cosim_ethernet.py, slirp"):
            require_match(original, runtime_fingerprint(report))

    def test_retained_web_value_cannot_hide_stale_spi_data(self):
        report = {"duration_s": 90, "backend": "cosim", "pt1000_verified": True, "guest_uptime_ms": 90_000,
                  "cosim": {"registered": True, "spi_replies": 10, "last_spi_reply_ms": 60_000}}
        self.assertFalse(qualify(report)[0]["spi"])
        report["cosim"]["last_spi_reply_ms"] = 89_000
        self.assertTrue(qualify(report)[0]["spi"])
