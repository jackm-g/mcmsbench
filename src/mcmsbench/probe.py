"""Probe: what the blocks of an arena are, read over RCON without changing them. A side view along one line (x across,
y up) or a top-down map of the ground, in any dimension, as text an agent (or a person) reads at a glance. For a
trial gone wrong: the wall of netherrack 49 blocks short of the goal, the lava pool under the way.

Read-only: `execute ... if block` tests, one cell at a time (about 0.35 ms each on a local arena). With `load`, chunks
not loaded already are force-loaded for the read and let go after (only those): for a probe between trials, when no
player keeps them loaded. Never with a trial running: a chunk kept loaded keeps its mobs ticking."""
from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Callable, Iterator

Rcon = Callable[[str], str]

# one character a cell: what a player sees there
LEGEND = {".": "air", "#": "solid", "L": "lava", "~": "water", "f": "fire", ",": "plant or snow (no collision)",
          "P": "nether portal", "O": "obsidian", "?": "not loaded"}
NOT_LOADED = "not loaded"


def _in(dimension: str) -> str:
    d = dimension if ":" in dimension else f"minecraft:{dimension}"
    return f"execute in {d} "


def cell(rcon: Rcon, dimension: str, x: int, y: int, z: int) -> str:
    """One cell's character (LEGEND). Air first (most of what a probe reads), then the solid test, then what an open
    cell holds."""
    pre = f"{_in(dimension)}if block {x} {y} {z} "

    def test(block: str) -> bool | None:
        out = rcon(pre + block)
        if NOT_LOADED in out:
            return None
        return "passed" in out
    air = test("#minecraft:air")
    if air is None:
        return "?"
    if air:
        return "."
    if not test("#minecraft:replaceable"):
        return "P" if test("minecraft:nether_portal") else "O" if test("minecraft:obsidian") else "#"
    if test("minecraft:lava"):
        return "L"
    if test("minecraft:water"):
        return "~"
    if test("#minecraft:fire"):
        return "f"
    return ","


def _span(a: int, b: int) -> range:
    return range(a, b + 1) if a <= b else range(a, b - 1, -1)


def slice_view(rcon: Rcon, dimension: str, *, x: tuple[int, int] | int, z: tuple[int, int] | int,
               y: tuple[int, int]) -> str:
    """A side view: one of x and z a range (across the page, in the order given), the other one value; y from the top
    of its range down. Each row is labelled with its y; a ruler under it labels every tenth column."""
    along_x = isinstance(x, tuple)
    if along_x == isinstance(z, tuple):
        raise ValueError("give a range for exactly one of x and z")
    cols = list(_span(*x)) if along_x else list(_span(*z))  # type: ignore[misc]
    fixed = z if along_x else x
    lo, hi = min(y), max(y)
    rows = []
    for yy in range(hi, lo - 1, -1):
        line = "".join(cell(rcon, dimension, c, yy, fixed) if along_x else cell(rcon, dimension, fixed, yy, c)  # type: ignore[arg-type]
                       for c in cols)
        rows.append(f"{yy:5d} {line}")
    axis, other = ("x", "z") if along_x else ("z", "x")
    ruler = "".join("|" if i % 10 == 0 else " " for i in range(len(cols)))
    marks = "".join(f"{cols[i]:<10d}"[:10] for i in range(0, len(cols), 10))
    head = f"{dimension}: side view at {other} {fixed}, {axis} from {cols[0]} (left) to {cols[-1]} (right), y {hi} (top) to {lo}"
    return "\n".join([head, *rows, f"{'':5s} {ruler}", f"{axis:>5s} {marks}"])


def top_view(rcon: Rcon, dimension: str, *, x: tuple[int, int], z: tuple[int, int], y: tuple[int, int]) -> str:
    """A map from above: each column's ground, scanned down from the top of the y range to the first cell that is not
    open, as the last two digits of the height a player stands at (one above that cell), or `L` / `~` where the
    first thing under open air is lava or water, `..` where nothing in the range is, `##` where the top of the range
    is already solid (a ceiling, or inside a hill), `??` unloaded. North is up: z grows down the page, x across."""
    lo, hi = min(y), max(y)
    xs = list(range(min(x), max(x) + 1))
    rows = []
    for zz in range(min(z), max(z) + 1):
        out = []
        for xx in xs:
            mark = ".."
            for yy in range(hi, lo - 1, -1):
                ch = cell(rcon, dimension, xx, yy, zz)
                if ch == "?":
                    mark = "??"
                    break
                if ch in ".,f":
                    continue
                if yy == hi:
                    mark = "##"
                elif ch in "L~":
                    mark = ch * 2
                else:
                    mark = f"{(yy + 1) % 100:02d}"
                break
            out.append(mark)
        rows.append(f"{zz:5d} {' '.join(out)}")
    ruler = [" "] * (3 * len(xs) + 4)
    for i, v in enumerate(xs):
        if v % 5 == 0:
            for j, ch in enumerate(str(v)):
                ruler[3 * i + j] = ch
    head = (f"{dimension}: ground from above, x {xs[0]}..{xs[-1]} across, z {min(z)}..{max(z)} down (north up), "
            f"scanned from y {hi} down to {lo}; a number is the y stood at, mod 100")
    return "\n".join([head, *rows, f"{'x':>5s} {''.join(ruler).rstrip()}"])


@contextmanager
def loaded(rcon: Rcon, dimension: str, x0: int, z0: int, x1: int, z1: int, timeout: float = 30.0,
           sleep: Callable[[float], None] = time.sleep) -> Iterator[int]:
    """The chunks over the box force-loaded for the read, those not marked for it already, and let go after. Yields
    how many it marked. A chunk the world never held is generated."""
    pre = _in(dimension)
    chunks = [(cx, cz) for cx in range(min(x0, x1) >> 4, (max(x0, x1) >> 4) + 1)
              for cz in range(min(z0, z1) >> 4, (max(z0, z1) >> 4) + 1)]
    # readable is what counts: `if loaded` (entity-ticking) stayed false on 26.1 for a force-loaded chunk whose blocks
    # read fine, and every probe waited out the timeout
    def readable(cx: int, cz: int) -> bool:
        return NOT_LOADED not in rcon(f"{pre}if block {cx * 16} 64 {cz * 16} #minecraft:air")
    # every chunk not already marked is marked: one a player kept loaded unloads when the player leaves, mid-read (the
    # probe right after a trial read half its map as not loaded). Only ours are let go after
    ours = [(cx, cz) for cx, cz in chunks if "is not marked" in rcon(f"{pre}run forceload query {cx * 16} {cz * 16}")]
    for cx, cz in ours:
        rcon(f"{pre}run forceload add {cx * 16} {cz * 16}")
    deadline = time.time() + timeout
    pending = list(ours)
    while pending and time.time() < deadline:
        pending = [(cx, cz) for cx, cz in pending if not readable(cx, cz)]
        if pending:
            sleep(0.5)
    try:
        yield len(ours)
    finally:
        for cx, cz in ours:
            rcon(f"{pre}run forceload remove {cx * 16} {cz * 16}")


def legend() -> str:
    return "legend: " + ", ".join(f"{k} {v}" for k, v in LEGEND.items())
