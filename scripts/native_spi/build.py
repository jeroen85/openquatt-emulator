"""Build and attest a private native emulator extension, never firmware."""
from pathlib import Path
import fcntl
import hashlib
import json
import os
import platform
import shlex
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
LAB = ROOT / ".tmp/native-spi"
EXPECTED = "162761d52bcee36fe8be5fd039202fed5873ca550a83a12229038311a4bbc18c"
PROTOCOL = 3
SOURCES = (
    "native_extension.cpp", "device_model.h", "w5500.h", "ethernet_lan.h", "local_services.h", "rmt_peer.h", "ot_protocol.h",
    "native/odu_bridge.cpp", "native/odu_bridge.h",
    "vendor/odu/quatt_odu_simulator_model.h",
    "vendor/odu/quatt_odu_communication_watchdog.h",
    "vendor/odu/quatt_odu_heating_limits.h",
    "vendor/odu/quatt_odu_performance.generated.h",
    "vendor/odu/quatt_odu_profiles.generated.h",
    "vendor/odu/quatt_odu_registers.generated.h",
)
ARTIFACTS = {"esp-emu", "native-spi.dylib", "odu.dylib"}


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def verify_original(path):
    if digest(path) != EXPECTED:
        raise ValueError("Unsupported emulator binary; native extension requires the pinned 0.51.0 arm64 build")


def ensure():
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise ValueError("This native extension currently requires macOS arm64")
    runtime = LAB / "runtime"
    original = runtime / "esp-emu.original"
    verify_original(original)
    source_bytes = {name: (ROOT / "scripts/native_spi" / name).read_bytes() for name in SOURCES}
    compiler = subprocess.check_output(["xcrun", "--find", "clang++"], text=True).strip()
    sdk = subprocess.check_output(["xcrun", "--sdk", "macosx", "--show-sdk-path"], text=True).strip()
    compiler_version = subprocess.check_output([compiler, "--version"], text=True)
    slirp_version = subprocess.check_output(["pkg-config", "--modversion", "slirp"], text=True).strip()
    slirp_flags = shlex.split(subprocess.check_output(["pkg-config", "--cflags", "--libs", "slirp"], text=True))
    identity = {"slirp": slirp_version, "slirp_flags": slirp_flags, "protocol": PROTOCOL, "original_sha256": EXPECTED,
                "source_sha256": {n: hashlib.sha256(b).hexdigest() for n, b in source_bytes.items()},
                "compiler": compiler_version, "sdk": sdk, "sysroot_explicit": True}
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:24]
    builds = runtime / "builds"
    builds.mkdir(parents=True, exist_ok=True)
    target = builds / key
    # One host compiler at a time, including concurrent independent CLI runs.
    with (runtime / ".build.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        manifest = target / "build.json"
        if manifest.exists():
            result = json.loads(manifest.read_text())
            hashes = result["artifact_sha256"]
            valid = (
                result.get("key") == key and set(hashes) == ARTIFACTS
                and all(result.get(field) == value for field, value in identity.items())
                and all((target / name).is_file() and digest(target / name) == hashes[name] for name in ARTIFACTS)
            )
            if not valid:
                raise ValueError("Native emulator cache failed checksum verification; refusing to run")
            return target / "esp-emu", target / "native-spi.dylib", result
        stage = Path(tempfile.mkdtemp(prefix=".build-", dir=builds))
        try:
            for name, data in source_bytes.items():
                path = stage / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
            flags = [compiler, "-isysroot", sdk, "-isystem", str(Path(sdk) / "usr/include/c++/v1"),
                     "-std=c++17", "-O2", "-Wall", "-Wextra", "-Werror", "-dynamiclib"]
            extension = stage / "native-spi.dylib"
            subprocess.run([*flags, str(stage / "native_extension.cpp"), *slirp_flags, "-o", str(extension)], check=True)
            odu = stage / "odu.dylib"
            subprocess.run([*flags, str(stage / "native/odu_bridge.cpp"), "-o", str(odu)], check=True)
            for library in (extension, odu):
                subprocess.run(["codesign", "--force", "--sign", "-", str(library)], check=True)
            executable = stage / "esp-emu"
            shutil.copyfile(original, executable)
            executable.chmod(0o755)
            entitlements = stage / "entitlements.plist"
            entitlements.write_text("""<?xml version="1.0" encoding="UTF-8"?><!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd"><plist version="1.0"><dict><key>com.apple.security.cs.allow-unsigned-executable-memory</key><true/><key>com.apple.security.cs.disable-library-validation</key><true/></dict></plist>""")
            subprocess.run(["codesign", "--force", "--sign", "-", "--options", "0",
                            "--entitlements", str(entitlements), str(executable)], check=True)
            result = {**identity, "key": key,
                      "artifact_sha256": {name: digest(stage / name) for name in sorted(ARTIFACTS)},
                      "firmware_modified": False, "firmware_elf_used": False, "debugger_used": False}
            (stage / "build.json").write_text(json.dumps(result, indent=2) + "\n")
            if target.exists():
                raise ValueError("Incomplete native build cache exists; refusing to replace it")
            os.replace(stage, target)
            print(json.dumps({"native_build": key, "cache": str(target)}), flush=True)
            return target / "esp-emu", target / "native-spi.dylib", result
        finally:
            if stage.exists():
                shutil.rmtree(stage)


if __name__ == "__main__":
    executable, extension, result = ensure()
    print(json.dumps(result), flush=True)
