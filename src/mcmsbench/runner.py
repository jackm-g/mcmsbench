"""The trial runner.

Per trial: reset the plot -> observer snapshot -> the player made ready (start, inventory, setup) -> the player handed to
the agent (protocol.py) -> the player taken back -> server reads + observer snapshot -> grade -> save the record.
Ground truth always comes from the observer and the server, never from the agent.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import math
import re
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

import yaml

from . import config, loops
from .arena import Arena, Plot
from .config import ROOT, Settings
from .events import EventLog, git_sha
from .goalspec import check_agreement, from_grader
from .graders import Context, Frame, built, grade
from .observer import ObserverClient, ObserverDead
from .players import ScriptedPlayers
from .protocol import PROTOCOL_VERSION, Agent, TrialContext
from .rcon import Rcon
from .render import render_iso, render_topdown, timelapse
from .render.report import run_index, trial_report
from .start import resolve_start, situation_commands
from .stats import Stats
from .tasks import Say, Task, load, load_all, with_profile
from .world.volume import Snapshot, diff, snapshot_to_json


@dataclass
class TrialRecord:
    task: str
    trial: int
    mode: str                                # the agent's name
    started: str
    seconds: float = 0.0
    result: dict | None = None
    error: str | None = None
    trace: dict = field(default_factory=dict)
    before: list = field(default_factory=list)
    after: list = field(default_factory=list)
    diff: dict = field(default_factory=dict)
    final_position: list | None = None
    final_inventory: list = field(default_factory=list)
    final_stats: dict = field(default_factory=dict)
    final_health: float | None = None
    final_time: int | None = None
    final_dimension: str | None = None
    final_respawn: dict | None = None        # {pos, bed}: the server's respawn point at the end (None = world spawn)
    final_food: int | None = None            # the hunger bar at the end (server truth)
    final_equipment: dict | None = None      # {slot: {name, count}}: worn and held at the end, when the grader reads it
    start: list | None = None
    report: str | None = None
    frame_diffs: list = field(default_factory=list)   # per-frame changes vs `before` (compact; enough to re-render)
    plot: dict = field(default_factory=dict)
    bench: dict = field(default_factory=dict)         # what ran: bench sha, protocol, task hash, agent (reproducibility)
    caveats: list = field(default_factory=list)       # what makes this trial's score not comparable (a missing capability)

    def to_dict(self) -> dict:
        return self.__dict__


class Observer:
    """Read-only spectator that provides ground-truth snapshots. Reconnects if the server restarted underneath it
    (survival arena resets do that)."""

    def __init__(self, host: str, port: int, username: str, version: str, arena: Arena):
        self.host, self.port, self.username, self.version = host, port, username, version
        self.arena = arena
        self.client = ObserverClient(host, port, username, version)
        self.ensure_connected()

    def ensure_connected(self) -> None:
        if self.client.connected():
            return
        self.client.connect()
        self.arena.wait_for_player(self.username)
        self.arena.rcon(f"gamemode spectator {self.username}")

    def move_to(self, plot: Plot) -> None:
        self.ensure_connected()
        cx, cy, cz = plot.center()
        self.arena.teleport(self.username, cx, cy + 6, cz, getattr(plot, "dimension", "minecraft:overworld"))
        time.sleep(1.5)  # chunk data

    def snapshot(self, plot: Plot, timeout: float = 30) -> Snapshot:
        """Ground-truth scan; waits until every cell of the plot is in a loaded chunk."""
        return self.snapshot_with_states(plot, timeout)[0]

    def snapshot_with_states(self, plot: Plot, timeout: float = 30) -> tuple[Snapshot, dict]:
        self.ensure_connected()
        deadline = time.time() + timeout
        snap, unloaded, states = self.client.scan_raw(plot.volume.min, plot.volume.max, with_states=True)
        while unloaded and time.time() < deadline:
            time.sleep(1.0)
            snap, unloaded, states = self.client.scan_raw(plot.volume.min, plot.volume.max, with_states=True)
        if unloaded:
            raise RuntimeError(f"observer: {unloaded} cells still unloaded after {timeout}s — "
                               "is the plot within the observer's view distance?")
        return snap, states

    def close(self) -> None:
        self.client.close()


class StandIn:
    """The trial's player while no agent holds it: logged in so the server will place, equip and set up the player
    (RCON reaches only players online), logged out for the agent, and back in for the final reads."""

    def __init__(self, host: str, port: int, username: str, version: str, arena: Arena):
        self.username, self.arena = username, arena
        self.client = ObserverClient(host, port, username, version)

    def login(self, attempts: int = 3) -> None:
        for i in range(attempts):
            try:
                self.client.connect()
                self.arena.wait_for_player(self.username)
                return
            except Exception as e:  # noqa: BLE001 — the server may still be dropping the agent's session
                if i == attempts - 1:
                    raise
                print(f"[stand-in] login {i + 1} failed: {e}")
                time.sleep(3.0)

    def logout(self) -> None:
        self.client.disconnect()
        time.sleep(1.0)              # the server drops the session before the agent logs in under the same name

    def close(self) -> None:
        self.client.close()


def render_trial(rec: TrialRecord, plot: Plot, before: Snapshot, after: Snapshot, d,
                 frames: list[Frame], out_dir: Path) -> None:
    """Isometric frames, a timelapse GIF, final views, and the HTML report."""
    fdir = out_dir / f"trial_{rec.trial}_frames"
    fdir.mkdir(exist_ok=True)
    vol = plot.volume
    fy = plot.floor_y if plot.flat else None   # terrain snapshots carry their own ground
    paths: dict[str, str] = {}
    seq: list[Path] = []
    for label, snap, pos in [("before", before, None)] + [(f.label, f.snapshot, f.position) for f in frames]:
        pth = render_iso(snap, vol, fdir / f"{label}.png", bot=pos, floor_y=fy, title=f"{rec.task} t{rec.trial} {label}")
        paths[label] = str(pth.relative_to(out_dir)); seq.append(pth)
    pos = tuple(rec.final_position) if rec.final_position else None
    hl = set(d.placed) | set(d.replaced)
    for label, kw in (("final_se", {"corner": "se"}), ("final_nw", {"corner": "nw"})):
        pth = render_iso(after, vol, fdir / f"{label}.png", bot=pos, floor_y=fy, highlight=hl,
                         title=f"{rec.task} t{rec.trial} final ({kw['corner']}), placed blocks outlined", **kw)
        paths[label] = str(pth.relative_to(out_dir))
    seq.append(fdir / "final_se.png")
    pth = render_topdown(after, vol, fdir / "final_top.png", bot=pos, floor_y=fy, title="top-down")
    paths["final_top"] = str(pth.relative_to(out_dir))
    if len(seq) > 1 and timelapse(seq, fdir / "timelapse.gif"):
        paths["timelapse"] = str((fdir / "timelapse.gif").relative_to(out_dir))
    rep = trial_report(rec.to_dict(), paths, out_dir / f"trial_{rec.trial}.html")
    rec.report = str(rep)


# graders that act on the server (run commands, speed random ticks): they may run once, at the end, never mid-trial
ACTING_GRADERS = {"functional", "hydrated"}
# MCMSBENCH_LIVE_GRADE=0: the agent plays on to its own stop or the clock, never stopped by a passing grade
LIVE_GRADE = __import__("os").environ.get("MCMSBENCH_LIVE_GRADE", "1") != "0"


def grader_kinds(spec) -> set[str]:
    """Every grader kind a spec uses, milestone steps and nested checks included."""
    out: set[str] = set()
    if isinstance(spec, dict):
        if isinstance(spec.get("kind"), str):
            out.add(spec["kind"])
        for v in spec.values():
            out |= grader_kinds(v)
    elif isinstance(spec, list):
        for v in spec:
            out |= grader_kinds(v)
    return out


def live_gradable(spec) -> bool:
    """Whether the grader can be run on the live server mid-trial without changing it."""
    return bool(spec) and not (grader_kinds(spec) & ACTING_GRADERS)


def stops_on_pass(task: Task) -> bool:
    """Whether the bench grades the trial live and stops the agent once it passes (the goal file's graderStops): not for
    a grader that acts on the server, nor a task that runs to a dawn (a pass on the first day is not its end)."""
    return live_gradable(task.grader) and not task.end_at_dawn


class PositionTracker:
    """Samples the player's server-side position every `every` seconds on its own RCON connection, for the whole
    trial: milestones a step passes through (a waypoint on a patrol) and the distance travelled no longer depend on
    where frames fall. With `dimensions`, each sample's dimension is read too, and `dims` keeps each change of it,
    [(t, "minecraft:the_nether")]: a Nether x, z is not an overworld one (the `leg` grader reads them together)."""

    def __init__(self, host: str, port: int, password: str, player: str, t0: float, every: float = 2.0,
                 dimensions: bool = False):
        self.rcon = Rcon(host, port, password).connect()
        self.player, self.t0, self.every = player, t0, every
        self.track: list[tuple[float, float, float, float]] = []
        self.dims: list[tuple[float, str]] | None = [] if dimensions else None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self.every):
            try:
                out = self.rcon(f"data get entity {self.player} Pos")
                m = re.search(r"\[(-?[\d.]+)d, (-?[\d.]+)d, (-?[\d.]+)d\]", out)
                if m:
                    t = round(time.time() - self.t0, 1)
                    if self.dims is not None:
                        d = re.search(r'"(minecraft:[a-z_]+)"', self.rcon(f"data get entity {self.player} Dimension"))
                        if d and (not self.dims or self.dims[-1][1] != d.group(1)):
                            self.dims.append((t, d.group(1)))
                    self.track.append((t, *(round(float(v), 1) for v in m.groups())))
            except Exception:  # noqa: BLE001 — a missed sample is fine (the player is between clients, say)
                pass

    def stop(self) -> list:
        self._stop.set()
        self._thread.join(self.every + 5)
        with contextlib.suppress(Exception):
            self.rcon.close()
        return self.track


class EventRunner:
    """Runs a task's timed `events` (RCON commands at t0 + seconds) on its own connection while the agent works, and
    records what each returned. An event with a test (at, cmd, when) waits past its time, trying the test every POLL
    seconds, until it passes; the events after it wait with it. A test that is a tuple runs its commands in order and
    tests the last. A Say in the command's place is a scripted player's chat line, said through `speak(player, text)`.
    With `listener` (the bot's name) and `handed` (set once the stand-in has logged out), the clock starts when the
    agent is on the server instead (`joined`, seconds after t0): what the task's players say is never said to no one,
    however long an agent takes to start. `stop()` cancels what has not fired yet."""

    POLL = 1.0

    def __init__(self, host: str, port: int, password: str, events: list[tuple], t0: float, rcon=None,
                 speak: Callable[[str, str], str] | None = None, listener: str | None = None,
                 handed: threading.Event | None = None):
        self.events = sorted(events, key=lambda e: e[0])
        self.t0 = t0
        self.speak = speak
        self.listener, self.handed = listener, handed
        self.joined: float | None = None
        self.fired: list[dict] = []
        self.own = rcon is None
        self.rcon = rcon if rcon is not None else Rcon(host, port, password).connect()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        base = self.t0
        if self.listener:
            while not (self.handed is None or self.handed.is_set()) or not self._online(self.listener):
                if self._stop.wait(self.POLL):
                    return
            base = time.time()
            self.joined = round(base - self.t0, 1)
            print(f"[event] {self.listener} is on the server (t+{self.joined:.0f}s): the events' clock starts")
        for at, cmd, *when in self.events:
            wait = base + at - time.time()
            if wait > 0 and self._stop.wait(wait):
                return
            if self._stop.is_set():
                return
            while when and not self._passes(when[0]):
                if self._stop.wait(self.POLL):
                    return
            said = isinstance(cmd, Say)
            try:
                if said:
                    out = self.speak(cmd.player, cmd.text) if self.speak else "error: no scripted players to speak"
                else:
                    out = self.rcon(cmd)
            except Exception as e:  # noqa: BLE001 — a failed event is recorded, never a crashed trial
                out = f"error: {type(e).__name__}: {e}"
            what = {"say": cmd.text, "as": cmd.player} if said else {"run": cmd}
            self.fired.append({"at": at, "t": round(time.time() - self.t0, 1), **what, "out": out[:200]})
            print(f"[event t+{at:.0f}s] {f'<{cmd.player}> {cmd.text}' if said else cmd} -> {out[:80]}")

    def _online(self, name: str) -> bool:
        try:
            out = self.rcon("list")             # "There are 2 of a max of 8 players online: Pat, player"
        except Exception:  # noqa: BLE001
            return False
        return name in [n.strip() for n in out.partition(":")[2].split(",")]

    def _passes(self, test: str | tuple) -> bool:
        try:
            *first, last = (test,) if isinstance(test, str) else test
            for cmd in first:
                self.rcon(cmd)
            return "passed" in self.rcon(last).lower()     # "Test passed" / "Test failed"
        except Exception:  # noqa: BLE001 — a test that cannot be asked holds the event
            return False

    def stop(self) -> list[dict]:
        self._stop.set()
        self._thread.join(5)
        if self.own:
            with contextlib.suppress(Exception):
                self.rcon.close()
        return self.fired


BABY = '{condition:"minecraft:entity_properties",entity:"this",predicate:{flags:{is_baby:true}}}'   # an inline predicate


class HerdWatcher:
    """A task's `herd_watch`: the babies of the watched kinds, followed on the server for the whole trial on its own
    RCON connection. Each poll, per kind: babies that grew up lose the `bv_baby` tag; new babies get it; the tagged ones
    that vanished died. A baby death in a poll where the player's `killed:<kind>` counter rose by more than the adults
    that died is the player's (`baby_kills`, which the `herd` grader's max_baby_kills reads); the rest were mobs, falls,
    drowning (`baby_lost`). `grow` > 1 ages the babies that many times as fast (their Age, through a scoreboard): a calf
    takes 20 real minutes to grow, longer than most trials. The livestock there at the start and every baby since are
    the herd (`bv_herd`); with `strays`, an adult of a watched kind that is not (a natural spawn: Normal spawns animals
    as well as monsters, and 26.x has no rule for one without the other; a chicken jockey's mount) drops into the void
    as soon as it is seen, so the world's livestock is what the task put out and what the player bred, no more."""

    POLL = 2.0
    TAG, NEW, GROWN, AGE, HERD, STRAY = "bv_baby", "bv_new", "bv_grown", "bv_age", "bv_herd", "bv_stray"

    def __init__(self, host: str, port: int, password: str, player: str, types: list[str], grow: float = 1.0,
                 poll: float | None = None, strays: bool = False, rcon=None, thread: bool = True):
        self.types = [str(t).removeprefix("minecraft:") for t in types]
        self.grow, self.poll, self.strays = float(grow or 1.0), float(poll or self.POLL), bool(strays)
        self.own = rcon is None
        self.rcon = rcon if rcon is not None else Rcon(host, port, password).connect()
        self.kills = Stats(self.rcon, player, [f"killed:{t}" for t in self.types])   # read only: the runner's Stats resets them
        self.tally = {"baby_kills": 0, "baby_lost": 0, "born": 0, "grown": 0, "adults_died": 0, "strays_removed": 0,
                      "errors": 0}
        self.by_type = {t: {"baby_kills": 0, "baby_lost": 0, "born": 0, "grown": 0} for t in self.types}
        self._babies = {t: 0 for t in self.types}
        self._adults: dict[str, int | None] = {t: None for t in self.types}
        self._killed = {t: 0 for t in self.types}
        self._last = time.time()
        self.rcon(f"scoreboard objectives add {self.AGE} dummy")      # "already exists" is fine
        for tag in (self.TAG, self.NEW, self.GROWN, self.HERD, self.STRAY):
            self.rcon(f"tag @e[tag={tag}] remove {tag}")                 # a previous trial's, on a world not reset
        for t in self.types:
            self.rcon(f"tag {self._sel(t)} add {self.HERD}")              # the livestock the task put out
        self.poll_once()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True) if thread else None
        if self._thread is not None:
            self._thread.start()

    def _sel(self, t: str, *filters: str) -> str:
        return "@e[" + ",".join([f"type=minecraft:{t}", *filters]) + "]"

    def _count(self, sel: str) -> int:
        from .graders import herd_count
        return herd_count(self.rcon, sel) or 0

    def poll_once(self) -> None:
        killed = self.kills.read()
        now = time.time()
        elapsed, self._last = now - self._last, now
        for t in self.types:
            sel = lambda *f: self._sel(t, *f)   # noqa: E731
            self.rcon(f"execute as {sel(f'tag={self.TAG}')} unless predicate {BABY} run tag @s add {self.GROWN}")
            grown = self._count(sel(f"tag={self.GROWN}"))
            if grown:
                self.rcon(f"tag {sel(f'tag={self.GROWN}')} remove {self.TAG}")
                self.rcon(f"tag {sel(f'tag={self.GROWN}')} remove {self.GROWN}")
            self.rcon(f"execute as {sel(f'tag=!{self.TAG}')} if predicate {BABY} run tag @s add {self.NEW}")
            new = self._count(sel(f"tag={self.NEW}"))
            old = self._count(sel(f"tag={self.TAG}"))
            if new:
                self.rcon(f"tag {sel(f'tag={self.NEW}')} add {self.HERD}")
            if self.strays:
                stray = sel(f"tag=!{self.HERD}")                      # every baby is herd by now: these are adults
                n = self._count(stray)
                if n:                                                 # down out of reach, then gone: drops and all
                    self.rcon(f"tag {stray} add {self.STRAY}")
                    self.rcon(f"execute as {sel(f'tag={self.STRAY}')} at @s run tp @s ~ -400 ~")
                    self.rcon(f"kill {sel(f'tag={self.STRAY}')}")
                    self.tally["strays_removed"] += n
            adults = self._count(sel(f"tag={self.HERD}", f"tag=!{self.TAG}", f"tag=!{self.NEW}"))
            if new:
                self.rcon(f"tag {sel(f'tag={self.NEW}')} add {self.TAG}")
                self.rcon(f"tag {sel(f'tag={self.NEW}')} remove {self.NEW}")
            self._account(t, grown, new, old, adults, int(killed.get(f"killed:{t}") or 0))
            if self.grow > 1 and old + new:
                ticks = int((self.grow - 1) * elapsed * 20)
                babies = sel(f"tag={self.TAG}")
                self.rcon(f"execute as {babies} store result score @s {self.AGE} run data get entity @s Age")
                self.rcon(f"scoreboard players add {babies} {self.AGE} {ticks}")
                self.rcon(f"execute as {babies} if score @s {self.AGE} matches 1.. run scoreboard players set @s {self.AGE} 0")
                self.rcon(f"execute as {babies} store result entity @s Age int 1 run scoreboard players get @s {self.AGE}")

    def _account(self, t: str, grown: int, new: int, old: int, adults: int, killed: int) -> None:
        """One poll's numbers for one kind: `grown` babies became adults, `new` babies appeared, `old` tagged babies are
        still babies, `adults` live, and the player's kill counter for the kind stands at `killed`."""
        baby_died = max(0, self._babies[t] - grown - old)
        prev_adults = self._adults[t]
        adult_died = max(0, prev_adults + grown - adults) if prev_adults is not None else 0
        kill_delta = max(0, killed - self._killed[t])
        mine = min(baby_died, max(0, kill_delta - adult_died))
        for d in (self.tally, self.by_type[t]):
            d["baby_kills"] += mine
            d["baby_lost"] += baby_died - mine
            d["born"] += new
            d["grown"] += grown
        self.tally["adults_died"] += adult_died
        self._babies[t], self._adults[t], self._killed[t] = old + new, adults, killed

    def _run(self) -> None:
        while not self._stop.wait(self.poll):
            try:
                self.poll_once()
            except Exception:  # noqa: BLE001 — a missed poll is caught up by the next one
                self.tally["errors"] += 1

    def stop(self) -> dict:
        """Stop polling, after a last look (a kill in the final seconds), and return the tally."""
        if self._thread is not None:
            self._stop.set()
            self._thread.join(self.poll + 5)
        try:
            self.poll_once()
        except Exception:  # noqa: BLE001
            self.tally["errors"] += 1
        if self.own:
            with contextlib.suppress(Exception):
                self.rcon.close()
        return {**self.tally, "by_type": self.by_type, "babies_now": dict(self._babies), "adults_now": dict(self._adults)}


NIGHT_SKIP_TO = 1000     # the morning a fast-forwarded night jumps to: full day, so the undead at the shelter burn


def night_skipper(host: str, port: int, password: str, elog: EventLog | None = None):
    """The fast-night hook (an agent's `night_skip_request`, once its shelter has held a while): the clock jumps to
    morning on its own RCON connection, and the event log says so, so a report never mistakes a skipped night for a
    real one."""
    def skip() -> None:
        r = Rcon(host, port, password).connect()
        try:
            before = r("time query day")       # 26.x: "daytime" is not a query (a timeline error)
            out = r(f"time set {NIGHT_SKIP_TO}")
        finally:
            r.close()
        if elog is not None:
            elog.emit("night_skipped", before=before[:80], out=out[:80])
        print(f"[bench] night fast-forwarded ({before.strip()[:40]} -> {NIGHT_SKIP_TO})")
    return skip


class DayClock:
    """Counts dawns from the time of day, read every few seconds: a dawn is the clock coming out of the evening or the
    night (12000 on) into day (before 12000), whether the night ran its course past 24000, was fast-forwarded to
    morning, or was slept through. A bed takes a player from 12542, so a night slept through never reads 13000: the
    clock went 12658 to 56 and the dawn was missed (two_nights_hard, 2026-10-06). The day counter of `time query day`
    is not used: `time set` (a skipped night) resets it. `observe` is pure; `poll` reads the server."""

    DAY_BEFORE = 12000

    def __init__(self, read_time: Callable[[], int | None] | None = None, t0: float | None = None):
        self.read_time, self.t0 = read_time, t0
        self.dawns: list[float] = []         # seconds since the start of each dawn seen
        self._night = False

    def observe(self, tod: int | None, at: float | None = None) -> bool:
        """One reading of the time of day (ticks, 0..23999); True when it is a new dawn."""
        if tod is None:
            return False
        if tod >= self.DAY_BEFORE:
            self._night = True
        elif tod < self.DAY_BEFORE and self._night:
            self._night = False
            self.dawns.append(round(at if at is not None else time.time() - (self.t0 or time.time()), 1))
            return True
        return False

    def poll(self) -> int:
        self.observe(self.read_time() if self.read_time else None)
        return len(self.dawns)

    @property
    def day(self) -> int:
        return len(self.dawns) + 1


def needs_day_clock(task: Task) -> bool:
    """A day clock runs for a task that ends at a dawn or grades by one (a `day` check, a milestone `at_dawn`), and
    only with the day/night cycle on."""
    if not task.world.daylight:
        return False
    steps = (task.grader.get("steps") or []) if isinstance(task.grader, dict) else []
    return bool(task.end_at_dawn) or "day" in grader_kinds(task.grader) or any("at_dawn" in m for m in steps)


CONTAINERS = ("chest", "trapped_chest", "barrel")


def needs_containers(spec) -> bool:
    """Whether a grader reads what the bot's chests hold (a food_stock with in_containers)."""
    if isinstance(spec, dict):
        if spec.get("kind") == "food_stock" and spec.get("in_containers"):
            return True
        return any(needs_containers(v) for v in spec.values())
    if isinstance(spec, list):
        return any(needs_containers(v) for v in spec)
    return False


TUNNEL_MARGIN = 16     # blocks round a task's tunnels kept loaded for setup: the room at a tunnel's end, its mobs


@contextlib.contextmanager
def area_loaded(rcon, box: tuple[int, int, int, int] | None, plot: Plot, timeout: float = 60.0, log=print,
                dimension: str = "minecraft:overworld"):
    """The chunks over `box` (x0, z0, x1, z1, grown by TUNNEL_MARGIN) force-loaded, and generated, while the block
    runs: a setup that fills and summons 200 blocks from the bot gets "position is not loaded" otherwise. Let go
    after, and the plot's own force-load (Arena.reset's) put back over any chunk the two shared. No box: nothing.
    `dimension`: the box is in that dimension (the Nether side of a portal, which a restored world has never held)."""
    if box is None:
        yield
        return
    from .survival import chunk_blocks
    inn = "" if dimension == "minecraft:overworld" else f"execute in {dimension} run "
    cx0, cz0 = (box[0] - TUNNEL_MARGIN) >> 4, (box[1] - TUNNEL_MARGIN) >> 4
    cx1, cz1 = (box[2] + TUNNEL_MARGIN) >> 4, (box[3] + TUNNEL_MARGIN) >> 4
    loads = list(chunk_blocks(cx0, cz0, cx1, cz1))
    for a, b, c, d in loads:
        rcon(f"{inn}forceload add {a * 16} {b * 16} {c * 16 + 15} {d * 16 + 15}")
    pending = [(cx, cz) for cx in range(cx0, cx1 + 1) for cz in range(cz0, cz1 + 1)]
    test = "execute if loaded" if not inn else f"execute in {dimension} if loaded"
    deadline = time.time() + timeout
    while pending:                       # a chunk the golden world never held generates first
        pending = [(cx, cz) for cx, cz in pending if "passed" not in rcon(f"{test} {cx * 16} 0 {cz * 16}")]
        if not pending or time.time() > deadline:
            break
        time.sleep(1.0)
    if pending:
        log(f"[setup] {len(pending)} of the chunks to load ({dimension}) still not loaded after {timeout:.0f}s")
    try:
        yield
    finally:
        for a, b, c, d in loads:
            rcon(f"{inn}forceload remove {a * 16} {b * 16} {c * 16 + 15} {d * 16 + 15}")
        if not inn:
            (x0, _, z0), (x1, _, z1) = plot.volume.min, plot.volume.max
            rcon(f"forceload add {x0 - 16} {z0 - 16} {x1 + 16} {z1 + 16}")


class far_surface:
    """`surface(x, z)` for resolve_start on terrain. A start hundreds of blocks from the plot (start.distance) is outside
    the observer's view, so the observer flies there first, which also generates the chunks; `moved` tells the caller
    to send it back over the plot."""

    NEAR = 40   # columns within this many blocks of the observer are loaded already

    def __init__(self, arena: Arena, observer: Observer, plot: Plot):
        self.arena, self.observer, self.plot = arena, observer, plot
        self.moved = False

    def __call__(self, x: int, z: int) -> tuple[int | None, str | None]:
        ob = self.observer.client
        ox, _, oz = ob.position()
        if max(abs(ox - x), abs(oz - z)) > self.NEAR:
            self.arena.teleport(self.observer.username, x, self.plot.center()[1] + 6, z)
            self.moved = True
            for _ in range(12):            # chunk generation + delivery
                time.sleep(1.0)
                y, top = ob.column_top(x, z, span=120, mode="ground")
                if top != "unloaded":
                    return y, top
            return None, None
        return ob.column_top(x, z, span=120, mode="ground")

    def floor(self, x: int, z: int) -> tuple[int | None, str | None]:
        """Highest solid block (under water, if any). Only called after __call__ loaded the column."""
        return self.observer.client.column_top(x, z, span=120, mode="floor")


def block_position(arena: Arena, username: str) -> tuple[int, int, int] | None:
    """The block the player stands in, as the server has it (floored)."""
    p = arena.server_position(username)
    return (math.floor(p[0]), math.floor(p[1]), math.floor(p[2])) if p is not None else None


# ------------------------------------------------------------------ the goal file

def spawn_protection(task: Task, plot: Plot, start: tuple[int, int, int] | None = None) -> dict | None:
    """The server's protected square around the world spawn, or None. The world spawn is the plot's centre in the
    survival arena, or the start when the task moves it there (`start.world_spawn`)."""
    radius = int(getattr(task.world, "spawn_protection", 0) or 0)
    if radius <= 0:
        return None
    x, _, z = start if task.start.world_spawn and start is not None else plot.center()
    return {"x": int(x), "z": int(z), "radius": radius}


def world_border(task: Task, plot: Plot, s: Settings) -> dict | None:
    """The survival arena's /worldborder (the task's `world.border`, else [survival].border; a diameter, centred on the
    spawn the plot is centred on), or None on a flat plot."""
    if task.world.type != "survival":
        return None
    diameter = task.world.border or s.survival.border
    if not diameter:
        return None
    x, _, z = plot.center()
    return {"x": int(x), "z": int(z), "radius": int(diameter) / 2}


def build_goal(task: Task, plot: Plot, start: tuple[int, int, int], s: Settings, trial: int, *, seed: int | None,
               fast_nights: bool, grader_stops: bool, max_seconds: float, max_cost: float) -> dict:
    """The goal file for one trial (PROTOCOL.md, "Goal file")."""
    check = None if task.check is False else (task.check or from_grader(task.grader, plot=plot))
    if isinstance(check, dict):
        container_checks(check, task, plot, start)
    lo, hi = plot.volume.min, plot.volume.max
    return {
        "protocol": PROTOCOL_VERSION,
        "task_id": task.id,
        "trial": trial,
        "prompt": task.render(plot, start),
        "check": check if isinstance(check, dict) else None,
        "movements": dict(task.movements or {}),
        "profile": {"multiplayer": bool(task.players), "players": [str(p["name"]) for p in task.players],
                    "longRun": False, "spawnProtection": spawn_protection(task, plot, start),
                    "border": world_border(task, plot, s), "fastNights": fast_nights, "graderStops": grader_stops},
        "world": {"type": task.world.type, "seed": seed, "difficulty": task.world.difficulty,
                  "daylight": task.world.daylight, "dimension": task.world.dimension_id,
                  "keep_inventory": task.world.keep_inventory},
        "plot": {"min": list(lo), "max": list(hi), "center": list(plot.center()),
                 "floor_y": plot.floor_y if plot.flat else None,
                 "anchor": [lo[0] + task.anchor[0], lo[1] + task.anchor[1], lo[2] + task.anchor[2]],
                 **({"border": b} if (b := border_of(task, plot, s)) else {})},
        "start": list(start),
        "inventory": dict(task.inventory or {}),
        "budget": {"max_seconds": max_seconds, "max_cost_usd": max_cost,
                   **({"ends": {"at_dawn": int(task.end_at_dawn)}} if task.end_at_dawn else {})},
        "options": agent_options(task, plot, start),
        "tags": list(task.tags),
    }


def container_checks(check: dict, task: Task, plot: Plot, start) -> None:
    """An uncovered `container` part carries what it asks in `check`: the chest's `at` resolved for this trial ([x, y, z])
    and the `items` it must hold, so an agent that keeps a record of what it put where can score it, as a `stat` part
    carries its bound. ("the lighthouse chest holds all the cargo" alone did not say where, or what.) A `herd` part
    carries its kinds, its count each and the pen's inside (`within`, six numbers); a `block_state` part its block's
    `at`, `block` and `state`: an agent that can see animals and blocks scores them as it goes. A `leg` part carries its
    dimension and `min_travel` (and `exit_near`, [x, z] in that dimension, when it asks one). An `intact` part carries
    what it keeps (`of`: setup, or the agent's build) and what it lets move (`exclude`).

    A step whose grader an agent cannot copy (a `functional` test of server commands) may say what it asks in terms an
    agent can score, as its `agent_check`: that goes in `check` as written, an `at` resolved like the others. ("a lit
    beacon" in words alone was a part no agent served.) The grader stays the step's own check."""
    all_steps = list((task.grader or {}).get("steps") or [])
    steps = {str(m.get("name")): dict(m.get("check") or {}) for m in all_steps}
    agent = {str(m.get("name")): dict(m["agent_check"]) for m in all_steps if isinstance(m.get("agent_check"), dict)}
    xyz = lambda text: [int(v) for v in task._fmt(str(text), plot, start).split()]      # noqa: E731
    for u in check.get("uncovered") or []:
        spec = steps.get(str(u.get("name")))
        if not spec:
            continue
        with contextlib.suppress(KeyError, ValueError, IndexError, TypeError):
            if str(u.get("name")) in agent:
                own = dict(agent[str(u.get("name"))])
                if "at" in own:
                    own["at"] = xyz(own["at"])
                u["check"] = own
            elif u.get("kind") == "intact":
                u["check"] = {"kind": "intact", "of": str(spec.get("of", "build")),
                              **({"exclude": [str(b) for b in spec["exclude"]]} if spec.get("exclude") else {})}
            elif u.get("kind") == "container" and "at" in spec:
                items = dict(spec["items"]) if isinstance(spec.get("items"), dict) else {str(spec.get("item")): int(spec.get("count", 1))}
                u["check"] = {"kind": "container", "at": xyz(spec["at"]), "items": {str(k): int(v) for k, v in items.items()}}
            # a herd in a pen: the kinds, the count each, and the pen's inside resolved ([x1, y1, z1, x2, y2, z2])
            elif u.get("kind") == "herd" and spec.get("within"):
                within = xyz(spec["within"])
                if len(within) == 6:
                    u["check"] = {"kind": "herd", "types": [str(t) for t in spec.get("types") or []],
                                  "min_each": int(spec.get("min_each", 1)), "within": within}
            # a block's state (a gate shut): where, which block, what state
            elif u.get("kind") == "block_state" and "at" in spec:
                u["check"] = {"kind": "block_state", "at": xyz(spec["at"]), "block": str(spec.get("block", "*")),
                              "state": dict(spec.get("state") or {})}
            # a leg of the trip in one dimension: which, how far from where it came in, and (when asked) the point it
            # leaves by, in that dimension's coordinates ("80 blocks across the Nether" alone was words)
            elif u.get("kind") == "leg":
                leg = {"kind": "leg", "dimension": str(spec.get("dimension", "the_nether")),
                       "min_travel": float(spec.get("min_travel", 0))}
                if spec.get("exit_near"):
                    leg["exit_near"] = xyz(spec["exit_near"])
                    leg["tolerance"] = float(spec.get("tolerance", 16))
                u["check"] = leg


def agent_options(task: Task, plot: Plot, start) -> dict:
    """What a task asks of an agent's own harness beyond the goal, for an agent that has such things (an agent without
    them ignores this): X-ray off, a guard on its digging, notes it starts with, the setup blocks that count as its own
    build, its own turn and step limits."""
    out: dict = {"xray": bool(task.xray), "guard": bool(task.guard), "journal": bool(task.journal)}
    if task.memories:
        out["memories"] = [{"text": text, "at": list(at) if at else None, **{k: v for k, v in m.items()
                            if k not in ("text", "at")}}
                           for m, (text, at) in zip(task.memories, task.render_memories(plot, start))]
    if task.own:
        out["own"] = [[int(v) for v in task._fmt(str(box), plot, start).split()] for box in task.own]
    for k in ("curfew", "spawn_first", "max_turns", "exec_timeout", "effort"):
        v = getattr(task, k)
        if v is not None:
            out[k] = v
    return out


# ------------------------------------------------------------------ reproducibility

def task_hash(task_id: str) -> str:
    """A hash of the task as it ran: its YAML with its profile merged underneath, so a changed profile changes it. A
    task with params is hashed as its public instance (filled with its defaults; its domains are `params_hash`): the
    instance a trial drew is in the record's `params`."""
    from . import variants
    from .config import TASK_DIR
    text = (TASK_DIR / f"{task_id}.yaml").read_text()
    if "${" in text:
        text = variants.render(text, variants.defaults(variants.params_of(text)))
    d = with_profile(yaml.safe_load(text))
    d.pop("params", None)
    return hashlib.sha256(json.dumps(d, sort_keys=True, default=str).encode()).hexdigest()[:16]


def border_of(task: Task, plot: Plot, s: Settings) -> dict | None:
    """The world border the trial runs inside, as {min: [x, z], max: [x, z]}: a flat overworld plot's fence, or a
    survival world's border round its spawn. None for a Nether room (walled) or an unfenced plot. A player sees it."""
    if plot.flat:
        if not plot.fence or plot.dimension != "minecraft:overworld":
            return None
        (x0, _, z0), (x1, _, z1) = plot.volume.min, plot.volume.max
        f = plot.fence
        return {"min": [x0 - f, z0 - f], "max": [x1 + f, z1 + f]}
    cx, _, cz = plot.center()
    half = (task.world.border or s.survival.border) // 2
    return {"min": [cx - half, cz - half], "max": [cx + half, cz + half]}


def bench_info(task: Task, agent: Agent) -> dict:
    m = agent.manifest
    manifest_text = m.path.read_text() if m.path and m.path.exists() else ""
    out = {"sha": git_sha(), "protocol": PROTOCOL_VERSION, "task_hash": task_hash(task.id),
           "agent": agent.name, "provider": agent.provider or None, "model": agent.model,
           "manifest_hash": hashlib.sha256(manifest_text.encode()).hexdigest()[:16],
           "capabilities": sorted(agent.capabilities), "split": task.split}
    if task.params:
        out["params"] = dict(task.param_values)
        out["params_hash"] = hashlib.sha256(json.dumps(task.params, sort_keys=True, default=str).encode()).hexdigest()[:16]
    if task.split == "heldout":
        from .variants import heldout_key, key_id
        out["key_id"] = key_id(heldout_key() or "")
        out["pack_id"] = task.pack          # None: drawn from the task file's own domains (caveats_for says so)
    return out


def caveats_for(task: Task, agent: Agent) -> list[dict]:
    """What makes this agent's trial of this task not comparable: a capability the grading depends on that the agent
    does not declare (the `guard` grader counts refusals only an agent with a guard reports)."""
    out = []
    missing = sorted(set(task.requires) - agent.capabilities)
    if missing:
        out.append({"missing_capabilities": missing,
                    "says": f"the task requires {', '.join(missing)}, which {agent.name} does not declare: "
                            "the parts graded on it are not comparable"})
    if task.split == "heldout" and task.params and not task.pack:
        # a held-out draw from the file's own domains is a value an agent may have been developed on (variants.py)
        out.append({"tag": "public_heldout",
                    "says": f"{task.id}'s held-out instance is drawn from the domains in its task file, which are public: "
                            "no held-out pack gives it its own (MCMSBENCH_HELDOUT_DIR)"})
    return out


# ------------------------------------------------------------------ one trial

def run_trial(s: Settings, arena: Arena, observer: Observer, stand_in: StandIn, task: Task, trial: int, agent: Agent,
              out_dir: Path, progress=print, render: bool = True) -> TrialRecord:
    a = s.survival if task.world.type == "survival" else s.arena
    rec = TrialRecord(task.id, trial, agent.name, datetime.now().isoformat(timespec="seconds"))
    rec.bench = bench_info(task, agent)
    rec.caveats = caveats_for(task, agent)
    if task.world.type == "survival":
        from .survival import WorldArena, WorldRules
        assert isinstance(arena, WorldArena)
        arena.configure(task.seed_for(trial), task.world.radius, task.world.daylight, task.world.difficulty,
                        border=task.world.border, rules=WorldRules.of(task.world))
        rec.trace["seed"] = task.seed_for(trial)
        plot = arena.plot(trial)
    else:
        w = task.world
        if w.spawn_protection or not w.keep_inventory or w.biome or not w.passive_mobs or task.start.world_spawn:
            raise ValueError(f"{task.id}: spawn_protection, keep_inventory, biome, passive_mobs and start.world_spawn "
                             f"need a survival world")
        plot = arena.plot(trial, w.dimension, w.size, w.height)
        arena.set_difficulty(w.difficulty)
    arena.reset(plot)
    fenced = plot.flat and arena.fence(plot) is not None      # the plot's edge is the world's for the trial
    observer.move_to(plot)
    before_setup = observer.snapshot(plot)
    before = before_setup                   # until the task's setup has run (a trial that fails before then still grades)
    setup_placed: Snapshot = {}
    rec.before = snapshot_to_json(before)
    frames: list[Frame] = []
    broke: list = []

    # where/when the player starts (seeded; surface-resolved on terrain)
    seed = task.seed_for(trial) if task.world.type == "survival" else 0
    surface = far_surface(arena, observer, plot) if task.world.type == "survival" else None
    start = resolve_start(task.start, plot.center(), task.id, trial, seed, surface,
                          floor=surface.floor if surface is not None else None)
    rec.start = list(start)
    elog = EventLog(out_dir / f"trial_{trial}.jsonl", task=task.id, trial=trial, bot=a.bot_username)
    elog.emit("trial_start", agent=agent.name, model=agent.model, seed=seed, start=list(start),
              world=task.world.type, plot=plot.describe()[:600], bench=rec.bench)
    if surface is not None and surface.moved:
        observer.move_to(plot)      # back over the plot: frames scan it
    for cmd in situation_commands(task.start, start):   # carve the cave / raise the shell before the player lands
        arena.rcon(cmd)
    if task.start.world_spawn:          # the server's spawn (and so its spawn protection) moves to where the bot starts
        arena.rcon("setworldspawn {} {} {}".format(*start))
    if task.start.time is not None:
        arena.set_time(task.start.time)
    if task.start.weather:
        arena.set_weather(task.start.weather)

    stand_in.login()
    t0 = time.time()
    herd_types = [str(t) for t in (task.herd_watch or {}).get("types") or []]
    stats = Stats(arena.rcon, a.bot_username, task.stats + [f"killed:{t}" for t in herd_types]
                  + (["deaths"] if task.fail_on_death and "deaths" not in task.stats else []))   # the watcher reads them
    tracker: PositionTracker | None = None
    events: EventRunner | None = None
    herd: HerdWatcher | None = None
    players: ScriptedPlayers | None = None
    handed = threading.Event()              # the stand-in has logged out: the player is the agent's
    start_food = None
    day_clock = DayClock(arena.server_time, t0) if needs_day_clock(task) else None

    def drain_breaks() -> None:
        with contextlib.suppress(Exception):
            for t, x, y, z, was in observer.client.drain_breaks():
                broke.append((round(t - t0, 1), x, y, z, was, None))

    try:
        arena.prepare_player(a.bot_username, plot, task.inventory, gamemode=task.start.gamemode, spawn=start)
        stats.setup()
        start_food = arena.drain_food(a.bot_username, task.start.food) if task.start.food is not None else None
        tracker = PositionTracker(a.host, a.rcon_port, a.rcon_password, a.bot_username, t0,
                                  dimensions="leg" in grader_kinds(task.grader))
        if task.players:        # the other people on the server, there before setup (which may give them things)
            players = ScriptedPlayers(arena, a.host, a.port, s.arena.version, plot, task.render_players(plot, start),
                                      t0, elog)
        tunnel_cmds, tunnel_box = task.render_tunnels(plot, start)
        far = task.render_load_area(plot, start, also=tunnel_box)
        nether = task.render_load_nether(plot, start)
        with area_loaded(arena.rcon, far, plot, log=print), \
                area_loaded(arena.rcon, nether, plot, log=print, dimension="minecraft:the_nether"):
            # tunnels, and what setup builds out of the plot's reach (in the Nether too)
            for cmd in tunnel_cmds + task.render_setup(plot, start, a.bot_username):
                out = arena.rcon(cmd)
                elog.emit("setup", cmd=cmd[:300], out=out[:300])
                if any(w in out for w in ("Unknown", "Incorrect", "Expected", "Could not", "rror", "nvalid", "Malformed",
                                          "not loaded")):
                    print(f"[setup] {cmd!r} -> {out[:160]}")      # spreadplayers with no ground, a bad NBT tag
        # the baseline is the world as the player finds it: what setup (and the start situation) placed is not the
        # agent's work. Graders that check a structure the task provides take `with_setup: true`.
        time.sleep(1.0)                     # the setup's block updates reach the observer
        before = observer.snapshot(plot)
        rec.before = snapshot_to_json(before)
        setup_placed = built(diff(before_setup, before))
        observer.client.watch_breaks(plot.volume.min, plot.volume.max)   # every block broken from here on, by anyone
        if task.events:
            events = EventRunner(a.host, a.rcon_port, a.rcon_password, task.render_events(plot, start, a.bot_username), t0,
                                 speak=players.say if players else None,
                                 listener=a.bot_username if players else None, handed=handed)
        if herd_types:
            hw = task.herd_watch or {}
            herd = HerdWatcher(a.host, a.rcon_port, a.rcon_password, a.bot_username, herd_types,
                               grow=float(hw.get("grow", 1)), poll=hw.get("poll"), strays=bool(hw.get("strays")))
        time.sleep(1.0)

        refusals: list = []
        wears = "equipped" in grader_kinds(task.grader)       # read what it wears with each frame only when graded

        def after_step(label: str) -> None:
            drain_breaks()
            frames.append(Frame(label, observer.snapshot(plot), block_position(arena, a.bot_username),
                                arena.server_inventory(a.bot_username), t=round(time.time() - t0, 1),
                                stats=stats.read(), food=arena.server_food(a.bot_username),
                                equipment=arena.server_equipment(a.bot_username) if wears else None))

        def live_context(after_now, states_now) -> Context:
            """The grading context on the world as it stands, read as the final capture reads it."""
            pos = block_position(arena, a.bot_username)
            return Context(before, after_now, diff(before, after_now), plot.volume, plot.floor_y if plot.flat else None,
                           tuple(pos) if pos else None, arena.server_inventory(a.bot_username), list(frames),
                           stats.read(), arena.server_health(a.bot_username), arena.server_time(), plot.center(),
                           states_now, arena.rcon,
                           lambda text: task._fmt(text, plot, start).replace("{bot}", a.bot_username),
                           track=list(tracker.track) if tracker else [], seconds=round(time.time() - t0, 1),
                           dims=list(tracker.dims or []) if tracker else [],
                           refusals=list(refusals),
                           respawn=arena.server_respawn(a.bot_username) if grader_kinds(task.grader) & {"respawn"} else None,
                           setup=setup_placed, start=tuple(rec.start), broke=list(broke),
                           food=arena.server_food(a.bot_username), herd=dict(herd.tally) if herd else None,
                           equipment=arena.server_equipment(a.bot_username) if wears else None,
                           day=day_clock.day if day_clock else None, events=list(events.fired) if events else [],
                           chat=players.chat() if players else [])

        def goal_met() -> bool:
            # the grader itself on the world as it stands, read as the final capture reads it; never for a grader
            # that acts on the server (live_gradable)
            drain_breaks()
            after_now, states_now = observer.snapshot_with_states(plot)
            r = grade(task.grader, live_context(after_now, states_now))
            elog.emit("live_grade", passed=r.passed, score=round(r.score, 3), checks=r.checks, t=round(time.time() - t0, 1))
            return r.passed

        def on_event(ev: dict) -> None:
            if ev.get("event") == "guard_refusal":
                refusals.append({k: v for k, v in ev.items() if k != "event"})

        live = stops_on_pass(task) and LIVE_GRADE
        fast = bool(task.fast_nights and task.world.daylight)
        skipper = night_skipper(a.host, a.rcon_port, a.rcon_password, elog) if fast else None

        def night_skip() -> None:
            if day_clock is not None:
                day_clock.poll()            # the night the skip ends, seen before the clock jumps past it
            skipper()
        def hand_off() -> None:
            stand_in.logout()
            handed.set()
        max_seconds = float(task.max_seconds or s.trial.max_seconds)
        max_cost = float(task.max_cost_usd or s.trial.max_cost_usd or 0.0)
        goal = build_goal(task, plot, start, s, trial, seed=seed if task.world.type == "survival" else None,
                          fast_nights=fast, grader_stops=live, max_seconds=max_seconds, max_cost=max_cost)
        ctx = TrialContext(task.id, goal, a.host, a.port, a.bot_username, s.arena.version, max_seconds, max_cost,
                           out_dir / f"trial_{trial}", hand_off=hand_off, take_back=stand_in.login,
                           progress=progress, after_step=after_step, goal_met=goal_met if live else None,
                           night_skip=night_skip if fast else None, on_event=on_event, log=elog,
                           dawns=day_clock.poll if day_clock else None,
                           end_at_dawn=task.end_at_dawn if day_clock else None,
                           lost=(lambda: "the player died" if (stats.read() or {}).get("deaths", 0) > 0 else None) if task.fail_on_death else None)
        rec.trace = {"agent": agent.name, "model": agent.model, "start_food": start_food}
        rec.trace.update(agent.run(ctx))
        if rec.trace.get("error"):          # the agent ended with stop = "error", or never wrote a trace
            rec.error = str(rec.trace["error"])
    except ObserverDead:
        raise
    except Exception as e:  # an agent or setup failure is a failed trial, not a crashed run
        rec.error = f"{type(e).__name__}: {e}"
        elog.emit("exception", where="trial", error=rec.error[:500], traceback=traceback.format_exc()[-4000:])
        traceback.print_exc()
    finally:
        rec.seconds = round(time.time() - t0, 1)
        rec.trace["track"] = tracker.stop() if tracker else []
        if tracker is not None and tracker.dims is not None:
            rec.trace["dims"] = [list(d) for d in tracker.dims]
        rec.trace["events"] = events.stop() if events else []
        if events is not None and events.listener:
            rec.trace["events_clock"] = events.joined      # when the agent was on and the events' clock started
        if players is not None:
            rec.trace["chat"] = players.chat()
        if herd is not None:
            rec.trace["herd"] = herd.stop()

        # each read on its own: one that fails must not cost the others (all of them server truth)
        def final(what: str, fn):
            try:
                return fn()
            except Exception as e:  # noqa: BLE001
                print(f"(final {what} not captured: {type(e).__name__}: {e})")
                return None
        rec.final_stats = final("stats", stats.read) or {}
        rec.final_health = final("health", lambda: arena.server_health(a.bot_username))
        rec.final_food = final("food", lambda: arena.server_food(a.bot_username))
        rec.final_time = final("time", arena.server_time)
        pos = final("position", lambda: block_position(arena, a.bot_username))
        rec.final_position = list(pos) if pos else None
        rec.final_dimension = final("dimension", lambda: arena.server_dimension(a.bot_username))
        rec.final_inventory = final("inventory", lambda: arena.server_inventory(a.bot_username)) or []
        if "equipped" in grader_kinds(task.grader):
            rec.final_equipment = final("equipment", lambda: arena.server_equipment(a.bot_username))
        if day_clock is not None:
            final("day", day_clock.poll)            # a dawn in the last seconds of the agent's run
            rec.trace["dawns"] = list(day_clock.dawns)
        rec.final_respawn = final("respawn", lambda: arena.server_respawn(a.bot_username))   # last: it force-loads the bed's chunk
        rec.trace.setdefault("guard_refusals", [])
        drain_breaks()
        rec.trace["broke"] = [list(b) for b in broke]
        if players is not None:
            rec.trace["players"] = final("players", players.final) or {}
            players.close()
        stand_in.logout()
        if fenced:
            final("unfence", arena.unfence)

    time.sleep(0.5)
    after, after_states = observer.snapshot_with_states(plot)
    rec.after = snapshot_to_json(after)
    d = diff(before, after)
    rec.diff = d.summary()
    containers = None
    if needs_containers(task.grader):
        # the chests and barrels the bot placed, read from the server (the plot's chunks are loaded for the observer)
        where = sorted(p for p, b in built(d).items() if b in CONTAINERS)
        try:
            containers = arena.server_containers(where)
        except Exception as e:  # noqa: BLE001
            print(f"(final containers not captured: {type(e).__name__}: {e})")
        rec.trace["containers"] = [[*p, items] for p, items in (containers or {}).items()]
    if task.grader:
        gctx = Context(before, after, d, plot.volume, plot.floor_y if plot.flat else None,
                       tuple(rec.final_position) if rec.final_position else None, rec.final_inventory, frames,
                       rec.final_stats, rec.final_health, rec.final_time, plot.center(),
                       after_states, arena.rcon, lambda text: task._fmt(text, plot, start).replace("{bot}", a.bot_username),
                       track=rec.trace["track"], seconds=rec.seconds, refusals=rec.trace["guard_refusals"],
                       dims=[tuple(d) for d in rec.trace.get("dims") or []],
                       respawn=rec.final_respawn, setup=setup_placed, start=tuple(rec.start), broke=list(broke),
                       food=rec.final_food, herd=rec.trace.get("herd"), equipment=rec.final_equipment,
                       containers=containers, day=day_clock.day if day_clock else None, events=rec.trace["events"],
                       chat=rec.trace.get("chat") or [])
        rec.result = grade(task.grader, gctx).to_dict()
        agent_check = rec.trace.get("check")
        if isinstance(agent_check, dict) and agent_check.get("parts"):
            # the agent's last word on each part of the goal file's check, beside the grader's verdict
            agree = check_agreement(agent_check, rec.result, task.grader)
            rec.trace["check_agreement"] = agree
            off = {k: v for k, v in agree.items() if v.get("agree") is False}
            if off:
                print(f"[check] {task.id}: the agent's check and the grader disagree on "
                      + ", ".join(f"{k} (agent {v['live']}, grader {v['grader']})" for k, v in off.items()))
    done = rec.trace.get("done")
    if isinstance(done, dict) and rec.result is not None:
        rec.trace["claim_agrees"] = bool(done.get("success")) == bool(rec.result.get("passed"))
    pts = [tuple(rec.start)] + [(x, y, z) for _, x, y, z in rec.trace["track"]] + \
          ([tuple(rec.final_position)] if rec.final_position else [])
    rec.trace["distance_travelled"] = round(sum(
        ((p[0] - q[0]) ** 2 + (p[1] - q[1]) ** 2 + (p[2] - q[2]) ** 2) ** 0.5 for p, q in zip(pts, pts[1:])), 1)
    rec.frame_diffs = [frame_diff_json(before, f) for f in frames]
    elog.emit("trial_end", passed=(rec.result or {}).get("passed"), score=(rec.result or {}).get("score"),
              checks=(rec.result or {}).get("checks"), error=rec.error, seconds=rec.seconds, stats=rec.final_stats,
              health=rec.final_health, time=rec.final_time, position=rec.final_position, diff=rec.diff)
    rec.plot = {"volume": plot.volume.to_dict(), "floor_y": plot.floor_y, "flat": plot.flat,
                "dimension": getattr(plot, "dimension", "minecraft:overworld")}
    rec.trace["loops"] = loops.measure(rec.trace.get("subgoals") or [], rec.result, task.grader, rec.seconds,
                                       rec.trace.get("track") or [], rec.plot)
    rec.trace["construction"] = construction_features(before, after, d, frames)
    rec.trace["progress"] = [{"label": f.label, "placed": len(diff(before, f.snapshot).placed),
                              "broken": len(diff(before, f.snapshot).broken),
                              "items": sum(i["count"] for i in f.inventory)} for f in frames]
    if render:
        try:
            render_trial(rec, plot, before, after, d, frames, out_dir)
        except Exception as e:  # noqa: BLE001 — never fail a trial over observability, but say so loudly
            rec.trace["render_error"] = f"{type(e).__name__}: {e}"
            print(f"!!! RENDER FAILED for {task.id} trial {trial}: {type(e).__name__}: {e}", file=sys.stderr)
            traceback.print_exc()
    (out_dir / f"trial_{trial}.json").write_text(json.dumps(rec.to_dict(), indent=1, default=str))
    return rec


def frame_diff_json(before: Snapshot, f: Frame) -> dict:
    dd = diff(before, f.snapshot)
    return {"label": f.label, "t": f.t, "position": list(f.position) if f.position else None,
            "set": [[*p, b] for p, b in {**dd.placed, **{p: nb for p, (_, nb) in dd.replaced.items()}}.items()],
            "removed": [list(p) for p in dd.broken],
            "stats": dict(f.stats or {}),                        # the server's counters at this frame (deaths, damage...)
            "food": f.food,
            **({"equipment": f.equipment} if f.equipment is not None else {}),
            "inventory": [{"name": i.get("name"), "count": i.get("count")} for i in (f.inventory or [])]}


def frames_from_record(before: Snapshot, frame_diffs: list) -> list[Frame]:
    out = []
    for fd in frame_diffs:
        snap = dict(before)
        for x, y, z, b in fd.get("set", []):
            snap[(x, y, z)] = b
        for x, y, z in fd.get("removed", []):
            snap.pop((x, y, z), None)
        out.append(Frame(fd["label"], snap, tuple(fd["position"]) if fd.get("position") else None, [], fd.get("t")))
    return out


def rerender(paths: list[Path], log=print) -> int:
    """(Re)generate frames, timelapses and HTML reports from saved trial records."""
    from .world.volume import Volume, snapshot_from_json
    n = 0
    for root in paths:
        for f in sorted(Path(root).rglob("trial_*.json")):
            if f.name.endswith((".strategy.json", "_goal.json", "_agent.json")):
                continue
            d = json.loads(f.read_text())
            rec = TrialRecord(**{k: v for k, v in d.items() if k in TrialRecord.__dataclass_fields__})
            before, after = snapshot_from_json(d.get("before", [])), snapshot_from_json(d.get("after", []))
            pl = d.get("plot") or {}
            if not pl:
                log(f"  (skip {f}: no plot info saved)")
                continue
            vol = Volume.from_dict(pl["volume"])
            plot = Plot(rec.trial, (vol.min[0], pl["floor_y"], vol.min[2]), vol, pl.get("flat", True))
            try:
                render_trial(rec, plot, before, after, diff(before, after), frames_from_record(before, rec.frame_diffs), f.parent)
                d["report"] = rec.report
                f.write_text(json.dumps(d, indent=1))
                n += 1
                log(f"  rendered {f.parent.name}/{f.name}")
            except Exception as e:  # noqa: BLE001
                log(f"  !!! render failed for {f}: {type(e).__name__}: {e}")
    return n


def construction_features(before: Snapshot, after: Snapshot, d, frames: list[Frame]) -> dict:
    """What got built, in numbers: for cross-agent comparison of constructions."""
    from collections import Counter
    from .graders.structural import main_cluster
    now = built(d)
    ever: set = set(now)
    for f in frames:
        ever |= set(built(diff(before, f.snapshot)))
    cluster = main_cluster(now)
    feat = {"blocks": len(now), "materials": dict(Counter(now.values()).most_common(8)),
            "waste": len(ever - set(now)), "broken": len(d.broken), "stray_blocks": len(now) - len(cluster)}
    if cluster:
        xs = [p[0] for p in cluster]; ys = [p[1] for p in cluster]; zs = [p[2] for p in cluster]
        feat["size"] = [max(xs) - min(xs) + 1, max(ys) - min(ys) + 1, max(zs) - min(zs) + 1]
    return feat


def summarize(records: list[TrialRecord]) -> dict:
    by_task: dict[str, list[TrialRecord]] = {}
    for r in records:
        by_task.setdefault(r.task, []).append(r)
    rows = []
    for tid, rs in by_task.items():
        graded = [r for r in rs if r.result]
        passed = sum(1 for r in graded if r.result["passed"])
        rows.append({
            "task": tid, "mode": rs[0].mode, "split": (rs[0].bench or {}).get("split", "public"),
            "model": rs[0].trace.get("model", "-"), "trials": len(rs),
            "pass_rate": round(passed / len(graded), 2) if graded else None,
            "mean_score": round(sum(r.result["score"] for r in graded) / len(graded), 2) if graded else None,
            "mean_seconds": round(sum(r.seconds for r in rs) / len(rs), 1),
            "mean_turns": round(sum(r.trace.get("turns", 0) for r in rs) / len(rs), 1),
            "mean_cost_usd": round(sum(r.trace.get("cost_usd", 0) for r in rs) / len(rs), 4),
            "errors": sum(1 for r in rs if r.error),
            "caveats": sorted({c for r in rs for cv in r.caveats for c in cv.get("missing_capabilities", [])}
                              | {cv["tag"] for r in rs for cv in r.caveats if cv.get("tag")}),
            # how the time went (loops.py), beside the score and never in it
            "loops": round(sum((r.trace.get("loops") or {}).get("loops", 0) for r in rs) / len(rs), 1),
            "quiet_s": _mean_of([(r.trace.get("loops") or {}).get("quiet_s") for r in rs]),
        })
    return {"rows": rows}


def _mean_of(xs: list) -> float | None:
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 1) if xs else None


def print_summary(summary: dict) -> None:
    cols = ["task", "mode", "model", "split", "trials", "pass_rate", "mean_score", "mean_seconds", "mean_turns",
            "mean_cost_usd", "errors", "loops", "quiet_s"]
    print("\n" + " | ".join(f"{c:>14}" for c in cols))
    for row in summary["rows"]:
        print(" | ".join(f"{str(row.get(c, '-')):>14}" for c in cols) + (f"  (caveat: {row['caveats']})" if row.get("caveats") else ""))


# ------------------------------------------------------------------ a run

@dataclass
class Env:
    arena: Arena
    observer: Observer
    stand_in: StandIn

    def close(self) -> None:
        for fn in (self.observer.close, self.stand_in.close, self.arena.rcon.close):
            with contextlib.suppress(Exception):
                fn()


def open_env(s: Settings, kind: str) -> Env:
    if kind == "survival":
        from .survival import ServerControl, WorldArena
        c = s.survival
        ctl = ServerControl(c)
        arena: Arena = WorldArena(c, ctl.wait_ready(timeout=60), ctl)
    else:
        c = s.arena
        arena = Arena(c, Rcon(c.host, c.rcon_port, c.rcon_password).connect())
        arena.init_world()
    obs = Observer(c.host, c.port, c.observer_username, s.arena.version, arena)
    return Env(arena, obs, StandIn(c.host, c.port, c.bot_username, s.arena.version, arena))


def run(tasks: list[Task], agent: Agent, s: Settings, run_dir: Path, *, trials: int | None = None,
        resume: bool = False, render: bool = True, open_report: bool = False, split: str = "public") -> list[TrialRecord]:
    """Each task's trials in turn. In the varied and heldout splits each trial is its own instance of a task with
    params (variants.py): trial i draws instance i."""
    records: list[TrialRecord] = []
    envs: dict[str, Env] = {}
    dead = None
    try:
        for task in tasks:
            if dead:
                break
            kind = task.world.type
            if kind not in envs:
                envs[kind] = open_env(s, kind)
            env = envs[kind]
            n = trials or task.trials
            out_dir = run_dir / task.id
            out_dir.mkdir(parents=True, exist_ok=True)
            for i in range(n):
                existing = out_dir / f"trial_{i}.json"
                if resume and existing.exists():
                    rec = TrialRecord(**{k: v for k, v in json.loads(existing.read_text()).items()
                                         if k in TrialRecord.__dataclass_fields__})
                    print(f"\n=== {task.id} trial {i + 1}/{n} [{agent.name}] === (resumed: already done)")
                    records.append(rec)
                    continue
                inst = task.instance(split, i)
                shown = f" [{split}]" if inst.params and split != "public" else ""
                print(f"\n=== {task.id} trial {i + 1}/{n} [{agent.name}]{shown} ===")
                try:
                    rec = run_trial(s, env.arena, env.observer, env.stand_in, inst, i, agent, out_dir, render=render)
                except ObserverDead as e:
                    # the observer is every grade's eyes: nothing more can run here. Stop cleanly, with what is done
                    # on record; --resume carries on from this trial in a fresh process
                    dead = f"{task.id} trial {i + 1}: {e}"
                    print(f"\n!!! {dead}. Stopping this run: resume it to carry on from here.")
                    break
                verdict = "PASS" if rec.result and rec.result["passed"] else "FAIL" if rec.result else "ungraded"
                print(f"--> {verdict} score={rec.result['score'] if rec.result else '-'} "
                      f"{rec.seconds}s diff={rec.diff} checks={rec.result['checks'] if rec.result else ''}"
                      f" stats={rec.final_stats} health={rec.final_health} time={rec.final_time}"
                      + (f" ERROR={rec.error}" if rec.error else ""))
                records.append(rec)
    finally:
        for env in envs.values():
            env.close()
    summary = summarize(records)
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=1))
    print_summary(summary)
    if render:
        rows = []
        for r in records:
            rel = Path(r.report).relative_to(run_dir) if r.report else None
            thumb = f"{r.task}/trial_{r.trial}_frames/final_se.png"
            rows.append({"task": r.task, "trial": r.trial, "result": r.result, "turns": r.trace.get("turns", 0),
                         "seconds": r.seconds, "cost": r.trace.get("cost_usd", 0.0),
                         "report": str(rel) if rel else "#", "thumb": thumb if (run_dir / thumb).exists() else None})
        index = run_index(run_dir, rows, summary)
        print(f"report: {index}")
        if open_report:
            import webbrowser
            webbrowser.open(index.as_uri())
    print(f"\nrun saved to {run_dir}")
    return records


def select_tasks(ids: list[str] | None, all_: bool, tags: list[str] | None) -> list[Task]:
    """The tasks named, or all of them; with `tags`, those carrying any of them. The tag `variants` selects the tasks
    with params without being one of their tags (tags reach the goal file; which tasks vary is the bench's business)."""
    tasks = list(load_all().values()) if all_ or (tags and not ids) else [load(t) for t in (ids or [])]
    if tags:
        tasks = [t for t in tasks if any(tag in t.tags or (tag == "variants" and t.params) for tag in tags)]
    return tasks


def default_run_dir(agent: Agent, split: str = "public") -> Path:
    tail = "" if split == "public" else f"-{split}"
    return ROOT / "runs" / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{agent.name}{tail}"


__all__ = ["TrialRecord", "Observer", "StandIn", "run_trial", "run", "build_goal", "config"]
