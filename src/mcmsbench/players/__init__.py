"""A task's scripted players: other people on the server, played by the bench.

A task with `players: [{name: Pat, at: "x y z"}]` has each of them logged in before its setup runs (so setup can give
them things), each as its own mineflayer client in a Node child process (player.js, spoken to in the observer's JSON
lines). The runner makes them ready over RCON: placed at `at` in adventure mode (they break nothing, and can still pick
up what the agent tosses them), and out of harm's way (resistance V, saturation). They talk through the task's events
(`say:`, `as:`) and go where its events `tp` them; what they hold is read over RCON like any player's.

Every chat line on the server is heard by them, the agent's included: that is the trial's chat record, [{t, from,
text}] with t in seconds since the trial started, and the `chat` grader reads it.
"""
from __future__ import annotations

import contextlib
import threading
from pathlib import Path

from ..observer import ObserverClient

SCRIPT = Path(__file__).with_name("player.js")
SAFE = ("effect give {name} minecraft:resistance infinite 4 true",     # V: no damage but the void's and /kill
        "effect give {name} minecraft:saturation infinite 0 true")      # never hungry


class ScriptedPlayers:
    """The scripted players of one trial, logged in and made ready; `close()` logs them out."""

    def __init__(self, arena, host: str, port: int, version: str, plot,
                 players: list[tuple[str, tuple[int, int, int], str]], t0: float, log=None):
        self.arena, self.t0, self.log = arena, t0, log
        self.clients: dict[str, ObserverClient] = {}
        self._chat: list[dict] = []
        self._lock = threading.Lock()
        try:
            for name, at, gamemode in players:
                client = ObserverClient(host, port, name, version, script=SCRIPT)
                self.clients[name] = client
                client.connect()
                arena.wait_for_player(name)
                arena.prepare_player(name, plot, {}, gamemode=gamemode, spawn=at)
                for cmd in SAFE:
                    arena.rcon(cmd.format(name=name))
        except Exception:
            self.close()
            raise

    @property
    def names(self) -> list[str]:
        return list(self.clients)

    def say(self, name: str, text: str) -> str:
        """`name` says `text` in chat; what happened, for the event record."""
        client = self.clients.get(name)
        if client is None:
            return f"error: no scripted player {name!r}"
        client.call("say", timeout=10, text=text)
        return "said"

    def chat(self) -> list[dict]:
        """Every chat line heard so far, the trial's whole record: [{t, from, text}] in the order heard. Public chat
        reaches every scripted player, so it is taken from the first; a whisper only reaches its own (`to`)."""
        with self._lock:
            for i, (name, client) in enumerate(self.clients.items()):
                try:
                    lines = client.call("drain_heard", timeout=10)
                except Exception:  # noqa: BLE001 — a player that dropped hears nothing more
                    continue
                for t_ms, who, text, kind in lines:
                    if i and kind != "whisper":
                        continue                # public chat: the first player heard it already
                    rec = {"t": round(t_ms / 1000.0 - self.t0, 1), "from": who, "text": text, **({"to": name} if i else {})}
                    self._chat.append(rec)
                    if self.log is not None:
                        self.log.emit("chat", **rec)
            return list(self._chat)

    def final(self) -> dict:
        """Where each scripted player ended and what they hold (server truth), for the trial record."""
        out = {}
        for name in self.clients:
            with contextlib.suppress(Exception):
                out[name] = {"position": self.arena.server_position(name), "inventory": self.arena.server_inventory(name)}
        return out

    def close(self) -> None:
        for client in self.clients.values():
            with contextlib.suppress(Exception):
                client.close()
