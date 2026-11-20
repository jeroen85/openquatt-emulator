"""Shared, trusted selection of Ubuntu HIL cases and firmware roles."""
BASE = "geheugen"
CASES = {
    BASE: ("Webbelasting en geheugen", "wifi", "pr-memory", "--seconds", "285"),
    "herstart": ("Instellingen behouden na softwareherstart", "ethernet", "restart", "--restart-case", "software"),
    "modbus": ("Modbus-uitval en herstel van HP2", "wifi", "odu-link-loss"),
    "energie-uur": ("Energiehistorie bij uurwisseling en herstart", "ethernet", "energy-boundary", "--energy-case", "hour"),
    "energie-dag": ("Energiehistorie bij dagwisseling", "ethernet", "energy-boundary", "--energy-case", "day"),
    "power-house": ("Power House-vraag en Single/Duo", "wifi", "power-house", "--power-house-climate", "mild"),
    "lowflow": ("Flowverlies tijdens defrost en herstel", "wifi", "lowflow"),
    "stooklijn": ("Stooklijn, defrostblokkade en Duo-afbouw", "wifi", "heating-curve"),
    "https": ("HTTPS, afgebroken verbinding en herstel onder webbelasting", "ethernet", "https"),
    "ethernet-ntp": ("W5500-Ethernet en lokale tijdsynchronisatie", "ethernet", "compatibility",
                     "--ntp-epoch", "1768474320", "--seconds", "90"),
    "ethernet-failover": ("Automatische terugval van Ethernet naar Wi-Fi", "failover", "compatibility",
                          "--seconds", "180"),
}
NETWORK_CASES = ("ethernet-ntp", "ethernet-failover")
LAB_CASES = frozenset({"https"})
COSIM_SECONDS = {"power-house": 3300, "lowflow": 1100, "stooklijn": 3000, "https": 420}
COSIM_PROFILES = ("basis", "herstart", "netwerk", "https", "regeling", "stooklijn", "regeling-kern", "uitgebreid")
HOSTED_CASES = (BASE, "herstart", *NETWORK_CASES, "https", "power-house", "lowflow", "stooklijn")
PROFILES = {
    "basis": (BASE,),
    "herstart": (BASE, "herstart"),
    "modbus": (BASE, "modbus"),
    "energie": (BASE, "energie-uur", "energie-dag"),
    "regeling": (BASE, "power-house", "lowflow"),
    "https": (BASE, "https"),
    "stooklijn": (BASE, "stooklijn"),
    "regeling-kern": ("power-house", "stooklijn"),
    "alles": (BASE, "herstart", "modbus", "energie-uur", "energie-dag", "power-house", "lowflow"),
    "netwerk": (BASE, *NETWORK_CASES),
    "uitgebreid": HOSTED_CASES,
}
PROFILE_LABELS = {"basis": "rooktest en geheugen", "herstart": "softwareherstart",
                  "netwerk": "Ethernet en Wi-Fi-terugval", "https": "HTTPS en herstel",
                  "regeling": "Power House en lowflowherstel", "stooklijn": "stooklijn",
                  "regeling-kern": "Power House en stooklijn",
                  "uitgebreid": "alle Ubuntu-proeven"}
HIL_LABELS = {"hil:" + name: name for name in COSIM_PROFILES}


def selected_profiles(selection, backend="cosim"):
    if backend not in ("cosim", "native"):
        raise ValueError("Onbekende backend")
    selected = {part.strip() for part in selection.split(",")}
    cosim_only = {"netwerk", "uitgebreid", "https", "stooklijn", "regeling-kern"}
    allowed = set(COSIM_PROFILES) if backend == "cosim" else set(PROFILES) - cosim_only
    if backend == "native" and selected & cosim_only:
        raise ValueError("Kies --backend cosim voor dit profiel.")
    if not selected or not selected <= allowed:
        raise ValueError("Ongeldige profielen voor deze backend; overige lokale proeven vereisen macOS.")
    if "uitgebreid" in selected and len(selected) > 1:
        raise ValueError("Kies uitgebreid afzonderlijk; dat profiel bevat alle Ubuntu-proeven.")
    order = COSIM_PROFILES if backend == "cosim" else tuple(PROFILES)
    return tuple(name for name in order if name in selected)


def selected_cases(selection, backend):
    profiles = selected_profiles(selection, backend)
    if backend == "cosim":
        wanted = {key for profile in profiles for key in PROFILES[profile]}
        return tuple(key for key in HOSTED_CASES if key in wanted)
    return tuple(dict.fromkeys(key for profile in profiles for key in PROFILES[profile]))
