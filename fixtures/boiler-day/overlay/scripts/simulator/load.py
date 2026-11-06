"""Bounded concurrent requests and SSE reconnects against the owned local firmware."""
from __future__ import annotations

import collections
import http.client
import socket
import threading
import time


class WebLoad:
    def __init__(self, port: int):
        if type(port) is not int or not 1024 <= port <= 65535:
            raise ValueError("Invalid local firmware port")
        self.port = port
        self.stop = threading.Event()
        self.lock = threading.RLock()
        self.threads = []
        self.watchdog = None
        self.connections = {}
        self.errors = collections.deque(maxlen=8)
        self.completed_paths = collections.Counter()
        self.stats = {"http_requests": 0, "sse_connections": 0, "sse_events": 0,
                      "sse_delivering_connections": 0, "sse_bytes": 0}

    def snapshot(self):
        with self.lock:
            return {**self.stats, "errors": list(self.errors),
                    "workers_alive": sum(thread.is_alive() for thread in self.threads)}

    @staticmethod
    def _shutdown(sock):
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def _open(self, path, *, headers=None, seconds=8):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
        # Only connect is inside the lock (bounded 2s). Register the raw socket
        # before any blocking protocol I/O; auto_open=False prevents reopening
        # an already cancelled connection between registration and request().
        with self.lock:
            if self.stop.is_set():
                raise InterruptedError("Load stopped")
            connection.connect()
            connection.auto_open = False
            record = {"socket": connection.sock, "deadline": time.monotonic() + seconds, "expired": False}
            self.connections[connection] = record
            if self.stop.is_set():
                self._shutdown(record["socket"])
                connection.close()
                self.connections.pop(connection, None)
                raise InterruptedError("Load stopped during connect")
        return connection, record

    def _release(self, connection, response=None):
        with self.lock:
            self.connections.pop(connection, None)
        if response:
            response.close()
        connection.close()

    def _deadlines(self):
        while not self.stop.wait(.05):
            with self.lock:
                for record in self.connections.values():
                    if not record["expired"] and time.monotonic() >= record["deadline"]:
                        record["expired"] = True
                        self._shutdown(record["socket"])

    def _http(self, paths=("/", "/0.js", "/0.css", "/text_sensor/ESPHome%20Version")):
        while not self.stop.is_set():
            for path in paths:
                if self.stop.is_set():
                    return
                connection, record = self._open(path)
                response = None
                try:
                    connection.request("GET", path)
                    response = connection.getresponse()
                    body = response.read(2 * 1024 * 1024 + 1)
                    if record["expired"] or response.status != 200 or not body or len(body) > 2 * 1024 * 1024:
                        raise RuntimeError(f"Invalid/expired HTTP load response {path}: {response.status}")
                    with self.lock:
                        self.stats["http_requests"] += 1
                        self.completed_paths[path] += 1
                finally:
                    self._release(connection, response)
                if self.stop.wait(.25):
                    return

    def _sse(self):
        while not self.stop.is_set():
            connection, record = self._open("/events", seconds=6)
            response = None
            try:
                connection.request("GET", "/events", headers={"Accept": "text/event-stream"})
                response = connection.getresponse()
                if response.status != 200 or "text/event-stream" not in response.getheader("Content-Type", ""):
                    raise RuntimeError("Firmware SSE endpoint did not open")
                with self.lock:
                    self.stats["sse_connections"] += 1
                has_data, delivered = False, False
                while not self.stop.is_set() and not record["expired"]:
                    try:
                        line = response.readline(65537)
                    except (socket.timeout, TimeoutError):
                        break  # A quiet stream reconnects without inventing an event.
                    except (OSError, http.client.HTTPException):
                        if record["expired"] or self.stop.is_set():
                            break
                        raise
                    if not line:
                        break
                    if len(line) > 65536:
                        raise RuntimeError("Oversized firmware SSE event line")
                    with self.lock:
                        self.stats["sse_bytes"] += len(line)
                    if not line.endswith(b"\n"):
                        break  # Truncated lines and unterminated events never count.
                    if line.startswith(b"data:"):
                        has_data = True
                    elif line in (b"\n", b"\r\n"):
                        if has_data:
                            with self.lock:
                                self.stats["sse_events"] += 1
                                if not delivered:
                                    self.stats["sse_delivering_connections"] += 1
                            delivered = True
                        has_data = False
            finally:
                self._release(connection, response)
            self.stop.wait(.5)

    def _worker(self, operation):
        try:
            operation()
        except (OSError, RuntimeError, ValueError, http.client.HTTPException, InterruptedError) as exc:
            if not self.stop.is_set():
                with self.lock:
                    self.errors.append(type(exc).__name__ + ": " + str(exc))

    def start(self):
        if self.threads:
            raise RuntimeError("Web load already started")
        self.watchdog = threading.Thread(target=self._deadlines, name="lab-web-deadlines", daemon=True)
        self.watchdog.start()
        for index, operation in enumerate((self._http, self._http, self._sse)):
            worker = threading.Thread(target=self._worker, args=(operation,), name=f"lab-web-load-{index}", daemon=True)
            self.threads.append(worker)
            worker.start()

    def close(self):
        self.stop.set()
        with self.lock:
            records = list(self.connections.values())
        # A raw socket stays tracked even when HTTPConnection has handed it to
        # HTTPResponse (HTTP/1.0 or Connection: close). Interrupt readers first.
        for record in records:
            self._shutdown(record["socket"])
        workers = self.threads + ([self.watchdog] if self.watchdog else [])
        for worker in workers:
            worker.join(timeout=4)
        if any(worker.is_alive() for worker in workers):
            raise RuntimeError("Web load worker did not stop")
