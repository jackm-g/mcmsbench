"""Graders for things that have to *work*: schematics, rail lines, redstone.

- schematic: layered text pattern vs the world (wildcards, partial credit)
- rail_path: a connected, powered track from A to B (structural partial credit)
- functional: a tiny RCON script — do / wait / assert — run against the live world
  at grade time: flip the lever, summon the cart, see what happens. Templates:
  plot vars ({ax}, {ax+3}...), {find:<block>} (nearest such block to the anchor)
  and {state:<block>|k=v,...} (its blockstate string with overrides).
"""
from __future__ import annotations

import re
import time
from collections import deque

from ..world.volume import Snapshot, XYZ

RAILS = {"rail", "powered_rail", "detector_rail", "activator_rail"}


def _grader(name):
    from . import grader
    return grader(name)


# ------------------------------------------------------------------ schematic

def expand_schematic(spec: dict, origin: XYZ) -> dict[XYZ, str]:
    """layers: bottom-to-top list of rows (z, increasing) of chars (x, increasing).
    legend maps chars to block names; '.' = must be air; '?' = don't care."""
    legend = dict(spec.get("legend", {}))
    out: dict[XYZ, str] = {}
    ox, oy, oz = origin
    for dy, layer in enumerate(spec["layers"]):
        for dz, row in enumerate(layer):
            for dx, ch in enumerate(row):
                if ch == "?" or ch == " ":
                    continue
                out[(ox + dx, oy + dy, oz + dz)] = "air" if ch == "." else legend[ch]
    return out


@_grader("schematic")
def schematic(ctx, spec: dict) -> object:
    from . import Result
    ox, oy, oz = ctx.plot_volume.min
    dx, dy, dz = spec.get("at", [0, 0, 0])
    expected = expand_schematic(spec, (ox + dx, oy + dy, oz + dz))
    matched = 0
    missing: list[list] = []
    for p, want in expected.items():
        have = ctx.after.get(p, "air")
        pats = want.split("|")
        ok = any(have == w or (w.endswith("*") and have.startswith(w[:-1])) for w in pats)
        matched += ok
        if not ok and len(missing) < 25:
            missing.append([*p, want, have])
    total = max(1, len(expected))
    frac = matched / total
    need = spec.get("min_match", 1.0)
    return Result(frac >= need, frac, {"matches": frac >= need}, {"matched": matched, "total": total, "mismatches": missing})


# ------------------------------------------------------------------ rail path

def _rail_neighbors(p: XYZ, rails: set[XYZ]):
    x, y, z = p
    for ddx, ddz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        for ddy in (0, 1, -1):
            q = (x + ddx, y + ddy, z + ddz)
            if q in rails:
                yield q


def rail_route(after: Snapshot, start: XYZ, end: XYZ, snap_radius: int = 2) -> dict:
    rails = {p for p, b in after.items() if b in RAILS}

    def nearest(t: XYZ):
        cands = [p for p in rails if max(abs(p[0] - t[0]), abs(p[1] - t[1]), abs(p[2] - t[2])) <= snap_radius]
        return min(cands, key=lambda p: (p[0] - t[0]) ** 2 + (p[1] - t[1]) ** 2 + (p[2] - t[2]) ** 2) if cands else None

    a, b = nearest(start), nearest(end)
    if not a:
        return {"rails": len(rails), "start_rail": None, "end_rail": b, "connected": False, "reach": 0.0, "path": []}
    prev = {a: None}
    q = deque([a])
    while q:
        p = q.popleft()
        for n in _rail_neighbors(p, rails):
            if n not in prev:
                prev[n] = p
                q.append(n)
    dist = lambda p, t: ((p[0] - t[0]) ** 2 + (p[2] - t[2]) ** 2) ** 0.5
    total = max(1.0, dist(a, end))
    closest = min(prev, key=lambda p: dist(p, end))
    reach = max(0.0, min(1.0, 1 - dist(closest, end) / total))
    path = []
    if b and b in prev:
        p = b
        while p is not None:
            path.append(p); p = prev[p]
        path.reverse()
    return {"rails": len(rails), "start_rail": a, "end_rail": b, "connected": bool(b and b in prev), "reach": round(reach, 2),
            "path": path}


@_grader("rail_path")
def rail_path(ctx, spec: dict) -> object:
    """Connected rail track from `from` to `to` (plot-relative, or absolute with _abs),
    with powered rails at least every `powered_every` blocks along it, each actually
    powered (block state powered=true)."""
    from . import Result
    ox, oy, oz = ctx.plot_volume.min
    rel = lambda v: (ox + v[0], oy + v[1], oz + v[2])
    start = tuple(spec["from_abs"]) if "from_abs" in spec else rel(spec["from"])
    end = tuple(spec["to_abs"]) if "to_abs" in spec else rel(spec["to"])
    r = rail_route(ctx.after, start, end)
    checks = {"connected": r["connected"]}
    every = spec.get("powered_every", 0)
    detail = dict(r); detail["path"] = len(r["path"])
    if every and r["connected"]:
        path = r["path"]
        powered_idx = [i for i, p in enumerate(path) if ctx.after.get(p) == "powered_rail"]
        live = [i for i in powered_idx if str(ctx.after_states.get(path[i], {}).get("powered", "")).lower() == "true"]
        gaps_ok = bool(live) and all(b - a <= every for a, b in zip([0] + live, live + [len(path) - 1]))
        checks["powered_rails_spaced"] = gaps_ok
        detail.update({"powered_rails": len(powered_idx), "powered_live": len(live)})
    score = 0.6 * r["reach"] + 0.4 * (sum(checks.values()) / len(checks))
    return Result(all(checks.values()), round(score, 3), checks, detail)


# ------------------------------------------------------------------ liquid path

LIQUIDS = {"lava": {"lava", "flowing_lava"}, "water": {"water", "flowing_water", "bubble_column"}}


def liquid_route(after: Snapshot, liquid: str, start: XYZ, end: XYZ, snap_radius: int = 1) -> dict:
    """Connected liquid (source or flowing) from near `start` to near `end`, face-adjacent
    steps. `reach` = how far down toward `end` the connected body gets, by height."""
    names = LIQUIDS.get(liquid, {liquid})
    cells = {p for p, b in after.items() if b in names}

    def nearest(t: XYZ):
        cands = [p for p in cells if max(abs(p[0] - t[0]), abs(p[1] - t[1]), abs(p[2] - t[2])) <= snap_radius]
        return min(cands, key=lambda p: sum((a - b) ** 2 for a, b in zip(p, t))) if cands else None

    a = nearest(start)
    if not a:
        return {"cells": len(cells), "start": None, "end": None, "connected": False, "reach": 0.0, "body": 0}
    seen = {a}
    q = deque([a])
    while q:
        x, y, z = q.popleft()
        for dx, dy, dz in ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)):
            n = (x + dx, y + dy, z + dz)
            if n in cells and n not in seen:
                seen.add(n); q.append(n)
    b = next((p for p in seen if max(abs(p[0] - end[0]), abs(p[1] - end[1]), abs(p[2] - end[2])) <= snap_radius), None)
    drop = max(1, start[1] - end[1])
    lowest = min(p[1] for p in seen)
    reach = max(0.0, min(1.0, (start[1] - lowest) / drop)) if not b else 1.0
    return {"cells": len(cells), "start": a, "end": b, "connected": b is not None, "reach": round(reach, 2), "body": len(seen)}


@_grader("liquid_path")
def liquid_path(ctx, spec: dict) -> object:
    """A lava (or water) fall: connected liquid from `from` to `to` (plot-relative, or `from_abs` /
    `to_abs`), e.g. a source poured on a ledge that reaches the ground below. Partial credit
    follows how far down the connected body reaches."""
    from . import Result
    ox, oy, oz = ctx.plot_volume.min
    rel = lambda v: (ox + v[0], oy + v[1], oz + v[2])
    start = tuple(spec["from_abs"]) if "from_abs" in spec else rel(spec["from"])
    end = tuple(spec["to_abs"]) if "to_abs" in spec else rel(spec["to"])
    r = liquid_route(ctx.after, spec.get("liquid", "lava"), start, end, spec.get("snap", 1))
    checks = {"starts_at_a": r["start"] is not None, "reaches_b": r["connected"]}
    return Result(all(checks.values()), round(0.2 * checks["starts_at_a"] + 0.8 * r["reach"], 3), checks, r)


# ------------------------------------------------------------------ functional

_ARITH = re.compile(r"\{([a-z]{2})([+-]\d+)\}")


def _resolve(ctx, text: str) -> str:
    """Template: plot vars via ctx.fmt, {ax+3} arithmetic, {find:block}, {state:block|k=v}."""
    def find_near(name: str):
        anchor = None
        if ctx.fmt:
            try:
                anchor = tuple(int(v) for v in ctx.fmt("{ax} {ay} {az}").split())
            except Exception:  # noqa: BLE001
                anchor = None
        cands = [p for p, b in ctx.after.items() if b == name or (name.endswith("*") and b.startswith(name[:-1]))]
        if not cands:
            return None
        if anchor is None:
            return cands[0]
        return min(cands, key=lambda p: sum((a - b) ** 2 for a, b in zip(p, anchor)))

    def repl_find(m):
        p = find_near(m.group(1))
        return f"{p[0]} {p[1]} {p[2]}" if p else "0 -9999 0"

    def repl_state(m):
        name, overrides = m.group(1), m.group(2)
        p = find_near(name)
        if not p:
            return f"minecraft:{name}"
        props = dict(ctx.after_states.get(p, {}))
        for kv in (overrides or "").lstrip("|").split(","):
            if "=" in kv:
                k, v = kv.split("=", 1); props[k] = v
        st = ",".join(f"{k}={str(v).lower()}" for k, v in sorted(props.items()))
        return f"minecraft:{ctx.after[p]}" + (f"[{st}]" if st else "")

    text = re.sub(r"\{find:([a-z_*]+)\}", repl_find, text)
    text = re.sub(r"\{state:([a-z_*]+)(\|[^}]*)?\}", repl_state, text)
    if ctx.fmt:
        # arithmetic on plot vars: {ax+3} -> value
        base = {}
        for var in set(_ARITH.findall(text)) | set((v, "") for v in re.findall(r"\{([a-z]{2})\}", text)):
            name = var[0]
            if name not in base:
                try:
                    base[name] = int(ctx.fmt("{" + name + "}"))
                except Exception:  # noqa: BLE001
                    pass
        text = _ARITH.sub(lambda m: str(base[m.group(1)] + int(m.group(2))) if m.group(1) in base else m.group(0), text)
        text = ctx.fmt(text)
    return text


def _poke(ctx, at: str) -> str:
    """A block update at `at`: a barrier set and cleared in an air cell beside it. A lever flipped with setblock updates
    only its own neighbours, not those of the block it is fixed to, so a door powered through that block never hears
    of it; a player's flip does both (anthropic-0926 redstone_door: two working doors graded broken)."""
    try:
        x, y, z = (int(v) for v in at.split())
    except ValueError:
        return f"bad position {at!r}"
    for dx, dy, dz in ((0, 0, -1), (0, 0, 1), (-1, 0, 0), (1, 0, 0), (0, -1, 0), (0, 1, 0)):
        c = f"{x + dx} {y + dy} {z + dz}"
        if "passed" in ctx.rcon(f"execute if block {c} minecraft:air").lower():
            ctx.rcon(f"setblock {c} minecraft:barrier")
            ctx.rcon(f"setblock {c} minecraft:air")
            return f"updated via {c}"
    return "no air beside it to update through"


@_grader("functional")
def functional(ctx, spec: dict) -> object:
    """Run `steps` against the live world: {do: cmd} runs it; {wait: seconds} sleeps;
    {assert: cmd} passes if the output says 'passed' (execute if ...), and with `within: seconds` it is tried every
    half second until it does or the time is up (a cart that arrives at 11 s, then rolls back, still arrived);
    {assert_not: cmd} the opposite; {poke: "x y z"} makes the block there re-read its neighbours (see _poke).
    Score = fraction of assertions passed."""
    from . import Result
    if ctx.rcon is None:
        return Result(False, 0.0, {"live_world": False}, {"reason": "no server handle at grade time"})
    log = []
    passed = 0
    total = 0
    for step in spec["steps"]:
        if "do" in step:
            cmd = _resolve(ctx, step["do"]); out = ctx.rcon(cmd); log.append({"do": cmd, "out": out[:120]})
        elif "wait" in step:
            time.sleep(float(step["wait"]))
        elif "poke" in step:
            at = _resolve(ctx, step["poke"]); log.append({"poke": at, "out": _poke(ctx, at)})
        elif "assert" in step or "assert_not" in step:
            neg = "assert_not" in step
            cmd = _resolve(ctx, step.get("assert") or step["assert_not"]); out = ctx.rcon(cmd)
            ok = ("passed" in out.lower()) != neg
            until = time.time() + float(step.get("within", 0))
            while not ok and time.time() < until:
                time.sleep(0.5)
                out = ctx.rcon(cmd)
                ok = ("passed" in out.lower()) != neg
            total += 1; passed += ok
            log.append({"assert_not" if neg else "assert": cmd, "out": out[:120], "ok": ok})
    for cmd in spec.get("cleanup", []):
        ctx.rcon(_resolve(ctx, cmd))
    return Result(total > 0 and passed == total, passed / total if total else 0.0,
                  {f"assert_{i + 1}": e["ok"] for i, e in enumerate(e for e in log if "ok" in e)}, {"log": log})


@_grader("container")
def container(ctx, spec: dict) -> object:
    """The chest (or barrel, or any block that holds items) at `at` ("x y z", templated like setup) holds at least
    `count` (default 1) of `item` (a glob or a list), read from the server at the end. Unlike food_stock's
    in_containers it reads a container the task placed, not one the bot did: a chest a setup stood by the start,
    where the thing fetched has to be put. Its chunk is force-loaded for the read, so a bot that ends far away still
    has the chest read (a chunk already held, the plot's, is left as it was: the read changes nothing, so it can run
    mid-trial and stop the agent on a pass). A chest broken (its items on the floor), or a bucket of water where an
    axolotl should be, fails, and the detail says what the chest holds. `items: {torch: 32, bread: 16}` instead asks
    for each of several (a delivery): one check each, the score their mean share."""
    from ..arena import parse_inventory
    from . import Result, _matches
    if ctx.rcon is None:
        return Result(False, 0.0, {"live_world": False}, {"reason": "no server handle at grade time"})
    at = _resolve(ctx, str(spec["at"]))
    x, y, z = (int(v) for v in at.split())
    need = int(spec.get("count", 1))
    held = "is marked" in ctx.rcon(f"forceload query {x} {z}")     # the plot's own chunks stay loaded after
    if not held:
        ctx.rcon(f"forceload add {x} {z}")
    try:
        out = ctx.rcon(f"data get block {x} {y} {z} Items")
    finally:
        if not held:
            ctx.rcon(f"forceload remove {x} {z}")
    found = "not a block entity" not in out and "rror" not in out
    items = parse_inventory(out) if found else []
    holds: dict[str, int] = {}
    for i in items:
        holds[i["name"]] = holds.get(i["name"], 0) + i["count"]
    if spec.get("items"):
        want = {str(k): int(v) for k, v in spec["items"].items()}
        have_each = {k: sum(i["count"] for i in items if _matches(i["name"], k)) for k in want}
        checks = {"container_found": found, **{f"{k}_delivered": have_each[k] >= n for k, n in want.items()}}
        score = sum(min(1.0, have_each[k] / n) if n else 1.0 for k, n in want.items()) / len(want)
        return Result(all(checks.values()), score, checks,
                      {"at": [x, y, z], "have": have_each, "need": want, "holds": holds,
                       **({} if found else {"reason": out[:120]})})
    have = sum(i["count"] for i in items if _matches(i["name"], spec["item"]))
    return Result(have >= need, min(1.0, have / need) if need else 1.0,
                  {"container_found": found, "holds_item": have >= need},
                  {"at": [x, y, z], "have": have, "need": need, "holds": holds,
                   **({} if found else {"reason": out[:120]})})


@_grader("hydrated")
def hydrated(ctx, spec: dict) -> object:
    """Crops that stay watered: `crop` blocks (default wheat) in the final world whose farmland is still hydrated
    (moisture=7) after a burst of random ticks (`ticks` for `wait` seconds, then `restore`). Ice waters nothing,
    and in a cold biome unlit water freezes within the burst, so this passes only when the water beside the crops
    is kept liquid (block light 10+ on it: torches). Passes with at least `min` crops planted and hydrated."""
    from . import Result
    crop, need = spec.get("crop", "wheat"), int(spec.get("min", 1))
    if ctx.rcon is None:
        return Result(False, 0.0, {"live_world": False}, {"reason": "no server handle at grade time"})
    cells = sorted(p for p, b in ctx.after.items() if b == crop)[:64]
    wet = []
    if cells:
        ctx.rcon(f"gamerule random_tick_speed {int(spec.get('ticks', 400))}")
        try:
            time.sleep(float(spec.get("wait", 12)))
            wet = [p for p in cells
                   if "passed" in ctx.rcon(f"execute if block {p[0]} {p[1] - 1} {p[2]} minecraft:farmland[moisture=7]").lower()]
        finally:
            ctx.rcon(f"gamerule random_tick_speed {int(spec.get('restore', 3))}")
    checks = {"planted": len(cells) >= need, "hydrated": len(wet) >= need}
    return Result(all(checks.values()), min(1.0, len(wet) / need) if need else 1.0, checks,
                  {"crops": len(cells), "hydrated": len(wet), "dry": [list(p) for p in cells if p not in wet][:10]})
