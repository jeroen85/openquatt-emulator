"""Explicit evidence gates for a complete bin-only compatibility run."""
import signal

from .network_probe import ETHERNET_IP


def qualify(report):
    peer = report.get("peer", {})
    units = {row["unit"] for row in peer.get("recent", []) if row["fc"] == 3 and not row["exception"] and not row["dropped"]}
    gates = {
        "setup": (report.get("setup_complete") is True, "De firmware heeft setup niet afgerond."),
        "web": (report.get("web_checks", 0) >= 2, "De web-app is onvoldoende bereikbaar geweest."),
        "pt1000": (report.get("pt1000_verified") is True, "De PT1000-meetwaarde wijkt af van de fixture."),
        "outside_units": (units == {1, 2}, "Niet beide buitenunits hebben registeruitlezingen beantwoord."),
        "firmware": (not report.get("panics") and not report.get("modbus_timing_warnings"), "De firmware meldde een crash of Modbus-timingwaarschuwing."),
        "runner": (not report.get("errors"), "De testrunner meldde een fout."),
        "uart": (not peer.get("error") and all(peer.get(k) == 0 for k in ("crc_errors", "exceptions", "dropped", "unknown_addresses")), "De buitenunitcommunicatie bevat fouten."),
        "native_exit": (report.get("exit_before_cleanup") is None and report.get("emulator_stop_requested") is True and report.get("cleanup_exit_code") in (0, -signal.SIGTERM), "De emulator stopte met een foutcode."),
        "duration": (report.get("elapsed_wall_s", 0) >= report["duration_s"], "De volledige testduur is niet voltooid."),
        "fresh_web": (report.get("elapsed_wall_s", 0) - report.get("web_last_ok_s", -999) <= 10, "De web-app antwoordde niet meer aan het einde van de proef."),
        "input": (report.get("input_hash_unchanged") is True and report.get("flash_file_unchanged") is True, "Een firmwarebestand is gewijzigd."),
        "cleanup": (all(report.get("cleanup", {}).get(k) is True for k in ("process_stopped", "reader_stopped", "peer_stopped")), "Niet alle eigen testprocessen zijn gestopt."),
        "spi": (not report.get("model_errors"), "Het native SPI/DMA-model meldde een fout."),
        "completed": (not report.get("cancelled"), "De proef is handmatig gestopt."),
    }
    network = report.get("network", {})
    if network.get("mode") in ("ethernet", "failover"):
        wire = network.get("wire", {})
        gates["ethernet"] = (
            not report.get("ethernet_driver_error") and network.get("ethernet_checks", 0) >= 2
            and network.get("ethernet_ip") == ETHERNET_IP and wire.get("dhcp_acks", 0) > 0
            and wire.get("rx", 0) > 0 and wire.get("tx", 0) > 0,
            "De Ethernetproef heeft onvoldoende bewijs voor DHCP, frames en webtoegang.",
        )
    if network.get("mode") == "failover":
        cut = network.get("cable_cut_s")
        restored = network.get("wifi_restored_s")
        recovery_event = report.get("wifi_recovery_event_s")
        gates["failover"] = (
            cut is not None and restored is not None and recovery_event is not None
            and cut < restored and cut <= recovery_event <= report["elapsed_wall_s"]
            and network.get("automatic_request_ack") is True
            and network.get("wifi_recovery_checks", 0) >= 2 and network.get("ethernet_http_lost")
            and report.get("web_port") == report["ports"]["http"]
            and wire.get("cable") is False and wire.get("link") is False,
            "De terugval van Ethernet naar Wi-Fi is niet aangetoond.",
        )
    return {key: bool(value[0]) for key, value in gates.items()}, [reason for ok, reason in gates.values() if not ok]
