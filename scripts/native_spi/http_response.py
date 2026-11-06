"""Bound local HTTP requests across headers and body."""
from contextlib import closing, contextmanager, suppress
import http.client
import socket
import threading
import time


@contextmanager
def bounded_response(port, path, deadline, cancelled, *, headers=None, connect_timeout=None):
    def check_deadline():
        if cancelled():
            raise ConnectionAbortedError("HTTP request cancelled")
        if time.monotonic() >= deadline:
            raise TimeoutError("HTTP request deadline exceeded")

    check_deadline()
    timeout = max(0.001, deadline - time.monotonic())
    with closing(http.client.HTTPConnection("127.0.0.1", port, timeout=min(timeout, connect_timeout or timeout))) as connection:
        connection.connect()
        check_deadline()
        connection.sock.settimeout(max(0.001, deadline - time.monotonic()))
        # HTTPConnection can release its own socket when a response will close.
        # Keep a descriptor that can interrupt the headers or body at any time.
        with connection.sock.dup() as interrupt_socket:
            finished = threading.Event()

            def interrupt():
                while not finished.is_set():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or cancelled():
                        with suppress(OSError):
                            interrupt_socket.shutdown(socket.SHUT_RDWR)
                        return
                    finished.wait(min(remaining, 0.05))

            watchdog = threading.Thread(target=interrupt, name="http-deadline", daemon=True)
            watchdog.start()
            try:
                connection.request("GET", path, headers={"Connection": "close", **(headers or {})})
                with connection.getresponse() as response:
                    yield response
                check_deadline()
            except (OSError, http.client.HTTPException):
                check_deadline()
                raise
            finally:
                finished.set()
                watchdog.join()
