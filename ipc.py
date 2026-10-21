"""How a second start of the app reaches the running app (no GTK).

Linux: a Unix socket at SOCKET_PATH (mode 0600). Python has no AF_UNIX on
Windows, so there the app listens on 127.0.0.1 with a random port, and every
request must carry a random token; address and token are in ENDPOINT_FILE.

A request is one JSON object plus a newline, e.g. {"type": "show", "token"}.
"""
from __future__ import annotations

import hmac
import json
import secrets
import socket
from pathlib import Path

from config import ENDPOINT_FILE, SOCKET_PATH, WINDOWS, write_private_json


def connect(address: str, timeout: float | None = None) -> socket.socket:
    """`address` is a socket path, or host:port on Windows."""
    if WINDOWS:
        host, _, port = address.rpartition(":")
        return socket.create_connection((host, int(port)), timeout=timeout)
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.settimeout(timeout)
    try:
        conn.connect(address)
    except OSError:
        conn.close()
        raise
    return conn


def running_endpoint() -> tuple[str, str]:
    """Address and token of a running app, as recorded at its start."""
    if not WINDOWS:
        return str(SOCKET_PATH), ""
    try:
        data = json.loads(ENDPOINT_FILE.read_text(encoding="utf-8"))
        return str(data["address"]), str(data["token"])
    except (OSError, ValueError, KeyError, TypeError):
        return "", ""


def token_ok(request: dict, token: str) -> bool:
    return hmac.compare_digest(str(request.get("token") or ""), token)


class Listener:
    """The app's end: listen() binds and records the endpoint, close() removes it."""

    def __init__(self) -> None:
        self.address = ""
        # Unix sockets are protected by their file mode; no token there.
        self.token = secrets.token_urlsafe(32) if WINDOWS else ""
        self.sock: socket.socket | None = None

    def listen(self) -> socket.socket:
        if WINDOWS:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.bind(("127.0.0.1", 0))
            self.address = f"127.0.0.1:{self.sock.getsockname()[1]}"
            write_private_json(ENDPOINT_FILE, {"address": self.address, "token": self.token})
        else:
            SOCKET_PATH.parent.mkdir(parents=True, exist_ok=True)
            try:
                SOCKET_PATH.unlink()  # stale socket of a crashed instance
            except FileNotFoundError:
                pass
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.bind(str(SOCKET_PATH))
            SOCKET_PATH.chmod(0o600)
            self.address = str(SOCKET_PATH)
        self.sock.listen()
        return self.sock

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
        path = ENDPOINT_FILE if WINDOWS else Path(self.address)
        try:
            if not WINDOWS or json.loads(path.read_text(encoding="utf-8")).get("token") == self.token:
                path.unlink()
        except (OSError, ValueError):
            pass
