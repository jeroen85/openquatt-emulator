"""Private local TLS fixture and explicit Q Duo test-CA build preparation."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import threading
import time


def prepare(root, output):
    root, output = root.resolve(), output.resolve()
    config = root / "configs/heatpump_controller_q/duo.yaml"
    if not config.is_file():
        raise ValueError("Q Duo source configuration missing")
    output.mkdir(parents=True, exist_ok=False)
    os.chmod(output, 0o700)
    ca, key = output / "ca.pem", output / "ca.key"
    server, server_key = output / "server.pem", output / "server.key"
    ca_config = output / "ca.cnf"
    ca_config.write_text("[req]\ndistinguished_name=dn\nx509_extensions=ca\nprompt=no\n"
                         "[dn]\nCN=OpenQuatt local HIL test CA\n[ca]\n"
                         "basicConstraints=critical,CA:TRUE\nkeyUsage=critical,keyCertSign,cRLSign\n"
                         "subjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid:always\n")
    commands = [
        ["req", "-x509", "-newkey", "rsa:2048", "-nodes", "-sha256", "-days", "30",
         "-config", str(ca_config), "-keyout", str(key), "-out", str(ca)],
        ["req", "-new", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=github.com",
         "-keyout", str(server_key), "-out", str(output / "server.csr")],
    ]
    (output / "extensions.cnf").write_text("subjectAltName=DNS:github.com,DNS:api.open-meteo.com\nbasicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\nsubjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid,issuer\n")
    commands.append(["x509", "-req", "-in", str(output / "server.csr"), "-CA", str(ca), "-CAkey", str(key),
                     "-CAserial", str(output / "ca.srl"), "-CAcreateserial", "-out", str(server), "-days", "30", "-sha256", "-extfile", str(output / "extensions.cnf")])
    for command in commands:
        subprocess.run(["openssl", *command], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=True)
    for private in (key, server_key):
        os.chmod(private, 0o600)
    # This is deliberately a separate configuration, never a production image.
    overlay = root / "configs/heatpump_controller_q/.virtual-https.yaml"
    if overlay.exists():
        raise ValueError("Local HTTPS configuration already exists")
    overlay.parent.mkdir(parents=True, exist_ok=True)
    overlay.write_text("packages:\n  production: !include " + json.dumps(str(config)) +
                       "\nesphome:\n  build_path: " + json.dumps(str(root / ".esphome/build/virtual_https")) +
                       "\nhttp_request:\n  ca_certificate_path: " + json.dumps(str(ca)) +
                       "\nesp32:\n  framework:\n    sdkconfig_options:\n" +
                       "      CONFIG_MBEDTLS_CUSTOM_CERTIFICATE_BUNDLE: y\n" +
                       "      CONFIG_MBEDTLS_CUSTOM_CERTIFICATE_BUNDLE_PATH: " + json.dumps('"' + str(ca) + '"') + "\n")
    identity = {"schema": 1, "source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip(),
                "production_config_sha256": hashlib.sha256(config.read_bytes()).hexdigest(),
                "ca_sha256": hashlib.sha256(ca.read_bytes()).hexdigest(),
                "overlay_sha256": hashlib.sha256(overlay.read_bytes()).hexdigest(),
                "profile": "local_https_test_ca", "overlay": str(overlay)}
    (output / "fixture.json").write_text(json.dumps(identity, indent=2) + "\n")
    return identity


class TlsFixture:
    def __init__(self, directory, clock):
        directory = Path(directory)
        self.identity = json.loads((directory / "fixture.json").read_text())
        if hashlib.sha256((directory / "ca.pem").read_bytes()).hexdigest() != self.identity["ca_sha256"]:
            raise ValueError("Local test CA differs from its fixture manifest")
        self.epoch, self.clock = int(time.time()), clock
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.minimum_version = ssl.TLSVersion.TLSv1_2
        self.context.load_cert_chain(directory / "server.pem", directory / "server.key")
        self.context.set_servername_callback(self._sni)
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        self.listener.listen(8)
        self.listener.settimeout(.2)
        self.stop, self.release_weather = threading.Event(), threading.Event()
        self.lock = threading.RLock()
        self.events, self.workers, self.sockets = [], [], set()
        self.fail_manifest = False
        self.thread = threading.Thread(target=self._accept, name="local-tls", daemon=True)
        self.thread.start()

    @staticmethod
    def _sni(sock, name, _context):
        if name not in ("github.com", "api.open-meteo.com"):
            raise ssl.SSLError("Unexpected fixture SNI")
        sock.fixture_role = "weather" if name == "api.open-meteo.com" else "manifest"

    def stamp(self):
        return {"host_s": time.monotonic(), "guest_ms": self.clock()}

    def snapshot(self):
        with self.lock:
            return json.loads(json.dumps(self.events))

    def _accept(self):
        while not self.stop.is_set():
            try:
                sock, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with self.lock:
                if len(self.workers) >= 32:
                    sock.close()
                    continue
                self.sockets.add(sock)
                worker = threading.Thread(target=self._serve, args=(sock,), daemon=True)
                self.workers.append(worker)
            worker.start()

    def _serve(self, raw):
        row = {"connected": self.stamp(), "role": "unknown", "outcome": "pending"}
        sock = raw
        with self.lock:
            self.events.append(row)
        try:
            raw.setblocking(False)
            with self.lock:
                if self.stop.is_set():
                    raise OSError("TLS fixture stopped")
                sock = self.context.wrap_socket(raw, server_side=True, do_handshake_on_connect=False)
                self.sockets.discard(raw)
                self.sockets.add(sock)
            until = time.monotonic() + 12
            while not self.stop.is_set():
                try:
                    sock.do_handshake()
                    break
                except (ssl.SSLWantReadError, ssl.SSLWantWriteError):
                    if time.monotonic() >= until:
                        raise TimeoutError("Local TLS handshake deadline")
                    self.stop.wait(.01)
            if self.stop.is_set():
                raise OSError("TLS fixture stopped")
            sock.settimeout(.2)
            with self.lock:
                row.update(role=sock.fixture_role, handshake=self.stamp())
            data = b""
            while b"\r\n\r\n" not in data and len(data) <= 8192:
                if self.stop.is_set() or time.monotonic() >= until:
                    raise TimeoutError("Local TLS request deadline")
                try:
                    part = sock.recv(2048)
                except socket.timeout:
                    continue
                if not part:
                    raise EOFError("TLS client closed before HTTP headers")
                data += part
            if not data.startswith(b"GET ") or len(data) > 8192:
                raise ValueError("Invalid local HTTPS request")
            with self.lock:
                row["request"] = self.stamp()
                row["path"] = data.split(b" ", 2)[1].decode("ascii")
                fail = self.fail_manifest and row["role"] == "manifest"
                if fail:
                    self.fail_manifest = False
            if fail:
                with self.lock:
                    row["outcome"] = "injected_disconnect"
                return
            if row["role"] == "weather":
                until = time.monotonic() + 8
                while not self.stop.is_set() and not self.release_weather.is_set() and time.monotonic() < until:
                    self.stop.wait(.05)
                body = self.weather()
            else:
                body = {"name": "OpenQuatt local HTTPS fixture", "version": "v0.58.1-hil-local",
                        "builds": [{"chipFamily": "ESP32-S3", "ota": {
                            "path": "https://github.com/never-install.bin", "md5": "0" * 32}}]}
            payload = json.dumps(body).encode()
            if self.stop.is_set():
                raise OSError("TLS fixture stopped")
            sock.sendall(f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {len(payload)}\r\nConnection: close\r\n\r\n".encode() + payload)
            with self.lock:
                row.update(outcome="ok", bytes=len(payload), response=self.stamp())
        except (OSError, EOFError, ValueError) as exc:
            with self.lock:
                row.update(outcome="tls_or_io_error", error=str(exc), tls_alert=getattr(exc, "reason", None),
                           role=getattr(sock, "fixture_role", row["role"]))
        finally:
            sock.close()
            with self.lock:
                self.sockets.discard(sock)
                self.sockets.discard(raw)
                row["closed"] = self.stamp()

    def weather(self):
        now = self.epoch + self.clock() // 1000
        hours = [now // 3600 * 3600 + i * 3600 for i in range(36)]
        return {"latitude": 52.1, "longitude": 5.1, "utc_offset_seconds": 0, "timezone": "GMT",
                "current_units": {"time": "unixtime", "interval": "seconds", "temperature_2m": "°C", "wind_speed_10m": "m/s", "wind_direction_10m": "°",
                                  "relative_humidity_2m": "%", "dew_point_2m": "°C"},
                "current": {"time": now, "interval": 900, "temperature_2m": 8, "wind_speed_10m": 3, "wind_direction_10m": 270,
                            "relative_humidity_2m": 70, "dew_point_2m": 3},
                "hourly_units": {"time": "unixtime", "temperature_2m": "°C", "wind_speed_10m": "m/s", "wind_direction_10m": "°"},
                "hourly": {"time": hours, "temperature_2m": [8] * 36,
                           "wind_speed_10m": [3] * 36, "wind_direction_10m": [270] * 36}}

    def close(self):
        self.stop.set()
        try:
            self.listener.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.listener.close()
        self.thread.join(2)
        for worker in self.workers:
            worker.join(1)
        return not self.thread.is_alive() and all(not w.is_alive() for w in self.workers)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--firmware-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--bind-firmware", type=Path)
    args = parser.parse_args()
    if args.bind_firmware:
        from .factory import factory_info
        path = args.output / "fixture.json"
        identity = json.loads(path.read_text())
        if hashlib.sha256(Path(identity["overlay"]).read_bytes()).hexdigest() != identity["overlay_sha256"]:
            raise ValueError("HTTPS test overlay changed")
        if subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=args.firmware_root, text=True).strip() != identity["source_commit"]:
            raise ValueError("HTTPS firmware source revision changed")
        identity["factory_sha256"] = factory_info(args.bind_firmware)["sha256"]
        path.write_text(json.dumps(identity, indent=2) + "\n")
        print(json.dumps(identity))
    else:
        print(json.dumps(prepare(args.firmware_root, args.output)))
