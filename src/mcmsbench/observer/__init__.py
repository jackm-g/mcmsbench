"""The observer client: a spectator mineflayer bot in a Node child process (observer.js), spoken to in JSON lines.

It is the bench's eyes. It reads blocks (the ground-truth snapshots every grade starts from) and a column's top (where
a start on terrain lands); it never touches the world. Everything else the bench knows about the player comes from the
server over RCON. (The bench's other clients, a task's scripted players, speak the same JSON lines: players/.)
"""
from __future__ import annotations

import itertools
import json
import queue
import shutil
import subprocess
import threading
from pathlib import Path

SCRIPT = Path(__file__).with_name("observer.js")


class ObserverDead(RuntimeError):
    """The observer's Node process is gone: nothing more can be scanned in this run."""


class ObserverClient:
    def __init__(self, host: str, port: int, username: str, version: str, node: str | None = None,
                 script: Path = SCRIPT):
        self.host, self.port, self.username, self.version = host, int(port), username, version
        node = node or shutil.which("node")
        if not node:
            raise SystemExit("node is not on PATH (the observer is a mineflayer client: `npm install` in the repo root)")
        self._proc = subprocess.Popen([node, str(script)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=None, text=True, bufsize=1)
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self._replies: queue.Queue = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        for line in self._proc.stdout:
            try:
                self._replies.put(json.loads(line))
            except json.JSONDecodeError:
                continue
        self._replies.put(None)

    def call(self, op: str, timeout: float = 60.0, **args):
        with self._lock:
            if self._proc.poll() is not None:
                raise ObserverDead(f"observer process exited ({self._proc.returncode})")
            rid = next(self._ids)
            try:
                self._proc.stdin.write(json.dumps({"id": rid, "op": op, **args}) + "\n")
                self._proc.stdin.flush()
            except (BrokenPipeError, OSError) as e:
                raise ObserverDead(f"observer process is gone: {e}") from e
            while True:
                try:
                    r = self._replies.get(timeout=timeout)
                except queue.Empty:
                    raise TimeoutError(f"observer: {op} took over {timeout:.0f}s") from None
                if r is None:
                    raise ObserverDead(f"observer process exited during {op}")
                if r.get("id") == rid:
                    break                       # a stale reply (an earlier call that timed out) is dropped
        if not r.get("ok"):
            raise RuntimeError(f"observer {op}: {r.get('error')}")
        return r.get("result")

    # -- the surface the runner uses

    def connect(self, timeout: float = 30.0) -> "ObserverClient":
        self.call("connect", timeout=timeout + 5, host=self.host, port=self.port, username=self.username,
                  version=self.version, timeout_ms=int(timeout * 1000))
        return self

    def connected(self) -> bool:
        try:
            return bool(self.call("connected", timeout=10))
        except (ObserverDead, TimeoutError):
            return False

    def disconnect(self) -> None:
        try:
            self.call("disconnect", timeout=10)
        except Exception:  # noqa: BLE001 — a disconnect never fails
            pass

    def close(self) -> None:
        self.disconnect()
        try:
            self._proc.stdin.close()
            self._proc.wait(5)
        except Exception:  # noqa: BLE001
            self._proc.kill()

    def position(self) -> tuple[int, int, int]:
        return tuple(self.call("position", timeout=10))

    def scan_raw(self, min_xyz, max_xyz, with_states: bool = False):
        """Non-air blocks in an inclusive box as {(x, y, z): name}, the count of cells not loaded, and (with_states)
        the state properties of redstone/rail-like blocks. Returns (blocks, unloaded[, states])."""
        r = self.call("scan", timeout=120, min=list(min_xyz), max=list(max_xyz), with_states=bool(with_states))
        blocks = {(x, y, z): n for x, y, z, n in r["blocks"]}
        if with_states:
            return blocks, int(r.get("unloaded", 0)), {(x, y, z): props for x, y, z, props in r.get("states", [])}
        return blocks, int(r.get("unloaded", 0))

    def column_top(self, x: int, z: int, span: int = 40, mode: str = "any") -> tuple[int | None, str | None]:
        """Highest block in a column, searched `span` blocks above and below the observer's own y."""
        y0 = int(self.position()[1])
        r = self.call("column_top", timeout=20, x=int(x), z=int(z), y_top=y0 + span, y_bottom=y0 - span, mode=mode)
        return r[0], r[1]

    def watch_breaks(self, min_xyz, max_xyz) -> None:
        """Record, from now on, every block in the box that goes from a block to air or a liquid (whoever broke it)."""
        self.call("watch_breaks", timeout=10, min=list(min_xyz), max=list(max_xyz))

    def drain_breaks(self) -> list[tuple[float, int, int, int, str]]:
        """The breaks since the last drain: [(epoch seconds, x, y, z, what it was)]."""
        return [(t / 1000.0, x, y, z, was) for t, x, y, z, was in self.call("drain_breaks", timeout=10)]

    def find_blocks(self, names: str, max_distance: int = 32, count: int = 10) -> list[tuple[int, int, int]]:
        """Nearest blocks with any of these names (comma-separated), nearest first."""
        ns = [n.strip().removeprefix("minecraft:") for n in str(names).split(",") if n.strip()]
        return [tuple(p) for p in self.call("find_blocks", timeout=60, names=ns, max_distance=max_distance, count=count)]
