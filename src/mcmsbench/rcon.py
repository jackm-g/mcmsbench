"""Minimal Minecraft RCON client (Source RCON protocol). No dependencies.

Used by the arena manager for resets, gamerules, teleports and inventory
stocking. The agent never gets a handle to this — that is deliberate.
"""
from __future__ import annotations

import socket
import struct

_LOGIN = 3
_COMMAND = 2
_RESPONSE = 0


def _pack(req_id: int, kind: int, payload: str) -> bytes:
    body = struct.pack("<ii", req_id, kind) + payload.encode("utf-8") + b"\x00\x00"
    return struct.pack("<i", len(body)) + body


class RconError(RuntimeError):
    pass


class Rcon:
    def __init__(self, host: str, port: int, password: str, timeout: float = 10.0):
        self.host, self.port, self.password, self.timeout = host, port, password, timeout
        self._sock: socket.socket | None = None
        self._id = 0

    # -- lifecycle ---------------------------------------------------------
    def connect(self) -> "Rcon":
        self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        rid, kind, _ = self._roundtrip(_LOGIN, self.password)
        if rid == -1:
            raise RconError("RCON authentication failed")
        return self

    def close(self) -> None:
        if self._sock:
            self._sock.close()
            self._sock = None

    def __enter__(self) -> "Rcon":
        return self.connect()

    def __exit__(self, *exc) -> None:
        self.close()

    # -- protocol ----------------------------------------------------------
    def _recv_exact(self, n: int) -> bytes:
        assert self._sock
        buf = b""
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise RconError("connection closed")
            buf += chunk
        return buf

    def _roundtrip(self, kind: int, payload: str) -> tuple[int, int, str]:
        assert self._sock, "not connected"
        self._id += 1
        self._sock.sendall(_pack(self._id, kind, payload))
        (length,) = struct.unpack("<i", self._recv_exact(4))
        body = self._recv_exact(length)
        rid, rkind = struct.unpack("<ii", body[:8])
        return rid, rkind, body[8:-2].decode("utf-8", errors="replace")

    def command(self, cmd: str) -> str:
        """Run a server command (without leading slash) and return its output."""
        _, _, out = self._roundtrip(_COMMAND, cmd.lstrip("/"))
        return out

    __call__ = command
