"""Install and verify the official esp-emu used for Q Duo co-simulation."""
from __future__ import annotations

import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request

from .build import LAB, ROOT, SOURCES, digest

VERSION = "1.1.0"
RUNTIME = LAB / "runtime" / "esp-emu.cosim"
ASSETS = {
    ("Darwin", "arm64"): (
        "aarch64-apple-darwin", "8bf2bd236c3bf472e8327e3b784bc2901e0ec5c65b5cbf68b4cdd6d0974264bc",
        "633a35f993300a765634ed78a2dbcfd5ffcd5c3f2f354a2dddabeb6108240828",
    ),
    ("Linux", "x86_64"): (
        "x86_64-unknown-linux-gnu", "bcb940dcf48aa6a1b62dbdc4abc37f05431710f4040d9e2504723a5ea4fd5de9",
        "952ff6ed45475f45089e26acc9926c9781fdd7a72925a24e613f035aaf084822",
    ),
    ("Linux", "aarch64"): (
        "aarch64-unknown-linux-gnu", "425f78e54470e46fc523f44787659a0c6f845295a42694fa51fbefa87c4b7ce7",
        "b18769263229d32395659a7ad5afe167351779f83c25b188f31aea0879c329b0",
    ),
}
MODEL_SOURCES = tuple(name for name in SOURCES if name.startswith(("native/", "vendor/odu/"))) + (
    "device_model.h", "w5500.h", "ethernet_lan.h", "local_services.h", "native/cosim_devices.cpp",
)


def platform_asset():
    key = platform.system(), platform.machine()
    if key not in ASSETS:
        raise ValueError("Co-simulatie vereist Linux x64/arm64 of macOS arm64. Gebruik op Windows WSL2.")
    return ASSETS[key]


def install():
    platform_tag, archive_sha, executable_sha = platform_asset()
    if RUNTIME.is_file() and digest(RUNTIME) == executable_sha:
        return RUNTIME
    name = f"esp-emu-{VERSION}-{platform_tag}"
    url = f"https://github.com/espressif/esp-emulator/releases/download/v{VERSION}/{name}.tar.gz"
    with urllib.request.urlopen(url, timeout=30) as response:
        archive = response.read(12 * 1024 * 1024 + 1)
    if hashlib.sha256(archive).hexdigest() != archive_sha:
        raise ValueError("Checksum van het co-simulatiearchief klopt niet")
    RUNTIME.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as bundle:
        member = bundle.getmember(f"{name}/esp-emu")
        if not member.isfile() or not 0 < member.size <= 64 * 1024 * 1024:
            raise ValueError("Ongeldig esp-emu-bestand in het geverifieerde archief")
        with bundle.extractfile(member) as executable, tempfile.TemporaryDirectory(dir=RUNTIME.parent) as folder:
            stage = Path(folder) / "esp-emu"
            stage.write_bytes(executable.read())
            if digest(stage) != executable_sha:
                raise ValueError("Checksum van het co-simulatieprogramma klopt niet")
            stage.chmod(0o755)
            os.replace(stage, RUNTIME)
    return RUNTIME


def ensure_cosim():
    platform_tag, _, executable_sha = platform_asset()
    if not RUNTIME.is_file() or digest(RUNTIME) != executable_sha:
        raise ValueError("Installeer esp-emu voor co-simulatie met python3 -m scripts.native_spi.cosim_build")
    mac = platform.system() == "Darwin"
    compiler = subprocess.check_output(["xcrun", "--find", "clang++"], text=True).strip() if mac else shutil.which("c++")
    if not compiler:
        raise ValueError("Geen C++-compiler beschikbaar voor het buitenunitmodel")
    flags = ["-dynamiclib"] if mac else ["-shared", "-fPIC"]
    if mac:
        sdk = subprocess.check_output(["xcrun", "--sdk", "macosx", "--show-sdk-path"], text=True).strip()
        flags += ["-isysroot", sdk, "-isystem", str(Path(sdk) / "usr/include/c++/v1")]
    base = ROOT / "scripts/native_spi"
    sources = {name: (base / name).read_bytes() for name in MODEL_SOURCES}
    try:
        slirp_version = subprocess.check_output(["pkg-config", "--modversion", "slirp"], text=True).strip()
        slirp_flags = shlex.split(subprocess.check_output(["pkg-config", "--cflags", "--libs", "slirp"], text=True))
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        packages = "pkgconf libslirp" if mac else "pkg-config libslirp-dev libglib2.0-dev"
        raise ValueError(f"Installeer {packages} voor de co-simulatiebackend") from exc
    identity = {"slirp": slirp_version, "slirp_flags": slirp_flags, "backend": "cosim", "platform": platform_tag, "version": VERSION, "original_sha256": executable_sha,
                "compiler": subprocess.check_output([compiler, "--version"], text=True), "compiler_flags": flags,
                "source_sha256": {name: hashlib.sha256(source).hexdigest() for name, source in sources.items()}}
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:24]
    builds = RUNTIME.parent / "builds"
    builds.mkdir(exist_ok=True)
    folder = builds / f"cosim-{key}"
    filename = "odu.dylib" if mac else "odu.so"
    library = folder / filename
    with (RUNTIME.parent / ".build.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not folder.exists():
            with tempfile.TemporaryDirectory(dir=builds) as stage:
                staged = Path(stage)
                for name, source in sources.items():
                    output = staged / name
                    output.parent.mkdir(parents=True, exist_ok=True)
                    output.write_bytes(source)
                subprocess.run([compiler, *flags, "-std=c++17", "-O2", "-Wall", "-Wextra", "-Werror",
                                "native/odu_bridge.cpp", "native/cosim_devices.cpp", *slirp_flags, "-o", filename],
                               cwd=staged, check=True, timeout=90)
                result = {**identity, "artifact_sha256": {filename: digest(staged / filename)}}
                (staged / "build.json").write_text(json.dumps(result))
                os.replace(staged, folder)
        result = json.loads((folder / "build.json").read_text())
        if (any(result.get(name) != value for name, value in identity.items())
                or digest(library) != result.get("artifact_sha256", {}).get(filename)):
            raise ValueError("Co-simulatiecache faalt de checksumcontrole")
    return RUNTIME, library, result


if __name__ == "__main__":
    print(install())
