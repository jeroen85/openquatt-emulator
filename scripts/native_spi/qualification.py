"""Explicit evidence gates for a complete bin-only compatibility run."""
import signal

from .lowflow import lowflow_gates, command_value
from .network_probe import ETHERNET_IP


FUNCTIONAL_COMPLETION = {"restart", "defrost-assist", "ott-setpoint", "source-loss", "odu-link-loss", "dew-guard"}


def qualify(report):
    peer = report.get("peer", {})
    units = {row["unit"] for row in peer.get("recent", []) if row["fc"] == 3 and not row["exception"] and not row["dropped"]}
    scenario = report.get("scenario")
    checks = report.get("dynamics", {}).get("checks", {})
    loss_test = scenario == "odu-link-loss"
    gates = {
        "setup": (report.get("setup_complete") is True, "De firmware heeft setup niet afgerond."),
        "web": (report.get("web_checks", 0) >= 2, "De web-app is onvoldoende bereikbaar geweest."),
        "pt1000": (report.get("pt1000_verified") is True, "De PT1000-meetwaarde wijkt af van de fixture."),
        "outside_units": (units == {1, 2}, "Niet beide buitenunits hebben registeruitlezingen beantwoord."),
        "firmware": (not report.get("panics") and (not report.get("modbus_timing_warnings")
                     or (loss_test and checks.get("planned_modbus_timeouts_only") is True)), "De firmware meldde een crash of Modbus-timingwaarschuwing."),
        "runner": (not report.get("errors"), "De testrunner meldde een fout."),
        "uart": (not peer.get("error") and all(peer.get(k) == 0 for k in ("crc_errors", "exceptions", "unknown_addresses"))
                 and (peer.get("dropped") == 0 or (loss_test and checks.get("planned_drops_only") is True)), "De buitenunitcommunicatie bevat fouten."),
        "native_exit": (report.get("exit_before_cleanup") is None and report.get("emulator_stop_requested") is True and report.get("cleanup_exit_code") in (0, -signal.SIGTERM), "De emulator stopte met een foutcode."),
        "duration": (report.get("elapsed_wall_s", 0) >= report["duration_s"], "De volledige testduur is niet voltooid."),
        "fresh_web": (report.get("elapsed_wall_s", 0) - report.get("web_last_ok_s", -999) <= 10, "De web-app antwoordde niet meer aan het einde van de proef."),
        "input": (report.get("input_hash_unchanged") is True and report.get("flash_file_unchanged") is True, "Een firmwarebestand is gewijzigd."),
        "cleanup": (all(report.get("cleanup", {}).get(k) is True for k in ("process_stopped", "reader_stopped", "peer_stopped")), "Niet alle eigen testprocessen zijn gestopt."),
        "spi": (not report.get("model_errors"), "Het native SPI/DMA-model meldde een fout."),
        "completed": (not report.get("cancelled"), "De proef is handmatig gestopt."),
    }
    restart = report.get("scenario") == "restart"
    if restart:
        gates["input"] = (report.get("input_hash_unchanged") is True,
                          "De oorspronkelijke factory-bin is gewijzigd.")
        gates["persistent_flash"] = (report.get("flash_file_unchanged") is False
                                      and report.get("flash_final_sha256") is not None,
                                      "De schrijfbare testflash bevat geen aantoonbare wijziging.")
    if scenario in FUNCTIONAL_COMPLETION:
        gates["duration"] = (report.get("functional_completion") is True,
                             "De fase en haar eindvenster zijn niet voltooid binnen de ingestelde duur.")
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
    if report.get("mode") == "device" and not restart:
        samples = [row["at_ms"] for row in peer.get("recent", []) if "at_ms" in row]
        odu_ms = report.get("odu_uptime_ms", 0)
        guest_ms = report.get("guest_uptime_ms", 0)
        clock_age = report.get("clock_last_progress_age_s", float("inf"))
        gates["odu_clock"] = (
            len(samples) >= 2 and max(samples) - min(samples) >= 250 and odu_ms >= max(samples)
            and 0 <= guest_ms - odu_ms <= 1000 and 0 <= clock_age <= 10,
            "De ODU-modelklok liep onvoldoende mee met de firmware.",
        )
    if dynamics := report.get("dynamics"):
        wire = dynamics.get("wire", {})
        for link, ids, description in (
            ("ott", {0, 16, 24}, "thermostaatuitwisseling op OTT"),
            ("otb", {0, 1, 25}, "keteluitwisseling op OTB"),
        ):
            if restart and dynamics.get("case") != "running":
                continue  # Standby reset scenarios exercise flash and network lifecycle.
            if link == "otb" and report.get("scenario") == "defrost-assist" and dynamics.get("case") != "ot":
                continue  # Virtual OpenTherm boiler is physically absent in relay profiles.
            evidence = wire.get(link, {})
            last_ack_ms = evidence.get("last_ack_ms")
            gates[link] = (
                ids.issubset(evidence.get("ack_ids", [])) and last_ack_ms is not None
                and 0 <= report.get("guest_uptime_ms", 0) - last_ack_ms <= 10000,
                f"De {description} is onvolledig of stopte vóór het einde van de proef.",
            )
        gates["dynamics"] = (dynamics["stage"] == "complete", "De dynamische proef is onvolledig.")
        if "version" in dynamics or restart:
            gates["wire_history"] = (peer.get("write_history_complete") is True,
                                     "De Modbus-schrijfhistorie is onvolledig; vroege opdrachten ontbreken.")
            if report.get("scenario") in ("heating-curve", "odu-response", "odu-transients", "power-house"):
                gates["heating_modes"] = (all(command_value(row, 3999) in (None, 0, 2)
                    for row in report.get("writes", []) if not row["exception"]),
                    "Een verwarmingsscenario bevat een opdracht voor een andere ODU-modus dan Standby of Heating.")
            checks = dynamics.get("checks", {})
            gates["scenario_evidence"] = (bool(checks), "De scenario-eindcontrole ontbreekt.")
            for key, ok in checks.items():
                gates["scenario_" + key] = (ok is True, f"Scenariocontrole faalt: {key}.")
            if dynamics.get("violation"):
                gates["scenario_violation"] = (False, dynamics["violation"])
        if "lowflow" in dynamics:
            gates["wire_history"] = (peer.get("write_history_complete") is True,
                                     "De Modbus-schrijfhistorie is onvolledig; vroege opdrachten ontbreken.")
            gates.update(lowflow_gates(dynamics, report.get("writes", [])))
    if "memory" in report:
        memory = report["memory"]
        gaps = [name for name, phase in memory.get("phases", {}).items() if not phase["covered"]]
        reason = f"Onvoldoende heapmetingen voor {', '.join(gaps)}." if gaps else "De heapmeting is onvolledig."
        gates["memory"] = (memory.get("complete") is True, memory.get("error", reason))
    if "https" in report:
        checks = report["https"].get("checks", {})
        gates["https_evidence"] = (bool(checks), "De HTTPS-eindcontrole ontbreekt.")
        for key, ok in checks.items():
            gates["https_" + key] = (ok is True, f"HTTPS-controle faalt: {key}.")
    if "export_memory" in report:
        for key, ok in report["export_memory"]["checks"].items():
            gates["export_" + key] = (ok is True, f"Exportcontrole faalt: {key}.")
    if "pr_memory" in report:
        pr = report["pr_memory"]
        workload = pr["workload"]
        gates["pr_memory_workload"] = (workload["valid"], " ".join(workload["issues"]))
        if "assessment" in pr:
            assessment = pr["assessment"]
            gates["pr_memory_comparison"] = (assessment["status"] != "uncomparable", assessment["reason"])
    if "memory_comparison" in report:
        comparison = report["memory_comparison"]
        gates["memory_comparison"] = (
            comparison["status"] == "compared", comparison.get("reason", "De geheugenvergelijking is onvolledig."),
        )
    return {key: bool(value[0]) for key, value in gates.items()}, [reason for ok, reason in gates.values() if not ok]
