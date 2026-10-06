"""Graders: predicates over (before, after, diff, final bot state).

Each grader returns a Result with pass/fail, partial credit in [0, 1], and
per-check detail. Structural graders (enclosure, roof, entrance) are what let
open-ended tasks like "build a small house" be graded without a schematic.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Callable

from ..world.volume import BlockDiff, Snapshot, Volume, XYZ
from . import structural


@dataclass
class Frame:
    """World + bot state after one agent step (or 'final')."""
    label: str
    snapshot: Snapshot
    position: XYZ | None = None
    inventory: list[dict] = field(default_factory=list)
    t: float | None = None                      # seconds since the trial started
    stats: dict = field(default_factory=dict)   # scoreboard counters (server truth)
    food: int | None = None                     # the hunger bar (server truth), 0..20
    equipment: dict | None = None               # {slot: {name, count}}: what the player wears and holds (server truth),
                                                # read when the grader asks for it; None: not read at this frame
    containers: dict | None = None              # {pos: [items]}: the bot's chests and barrels (server truth); only the
                                                # final frame has them (a chest's past contents are not recorded)


@dataclass
class Context:
    before: Snapshot
    after: Snapshot
    diff: BlockDiff
    plot_volume: Volume
    floor_y: int | None                 # None on terrain: ground comes from the snapshot
    bot_position: XYZ | None = None
    bot_inventory: list[dict] = field(default_factory=list)
    frames: list[Frame] = field(default_factory=list)   # per-step history, for milestones
    stats: dict = field(default_factory=dict)           # final scoreboard counters
    health: float | None = None                         # final server-side health
    world_time: int | None = None                       # final time of day (ticks)
    center: XYZ | None = None                           # plot centre / world spawn (absolute)
    after_states: dict = field(default_factory=dict)    # {pos: properties} for redstone/rail-like blocks at the end
    rcon: object | None = None                          # live server handle for functional graders (world still intact)
    fmt: object | None = None                           # task template formatter: fmt("{ax} {ay} {az}") -> str
    track: list = field(default_factory=list)           # [(t, x, y, z), ...] bot positions sampled every ~2 s (RCON)
    seconds: float | None = None                        # how long the trial took (the final frame's time)
    refusals: list = field(default_factory=list)        # live-guard refusals during the trial (task.guard)
    respawn: dict | None = None                         # {pos, bed}: the server's respawn point at the end
    setup: Snapshot = field(default_factory=dict)       # blocks the task's setup placed (`before` is taken after it)
    start: XYZ | None = None                            # where the bot started the task (absolute)
    broke: list = field(default_factory=list)           # [(t, x, y, z, block, by)]: every block the bot broke, t seconds
                                                        # since the start, by 'dig' | 'path' (the pathfinder on a walk)
    food: int | None = None                             # final server-side hunger bar, 0..20
    herd: dict | None = None                            # the herd watcher's tally: {baby_kills, baby_lost, born, grown}
    equipment: dict | None = None                       # final {slot: {name, count}}: armour worn, hands
    containers: dict | None = None                      # final {pos: [items]}: the chests and barrels the bot placed
    day: int | None = None                              # the day the trial ended in (1 = the one it started in, each dawn
                                                        # the bench saw starts the next); None: no day clock ran

    def at_frame(self, f: Frame) -> "Context":
        """A context describing the world as of one frame."""
        from ..world.volume import diff as _diff
        return Context(self.before, f.snapshot, _diff(self.before, f.snapshot), self.plot_volume, self.floor_y,
                       f.position, f.inventory, [], f.stats, self.health, self.world_time, self.center,
                       self.after_states, self.rcon, self.fmt, self.track, self.seconds, self.refusals, self.respawn,
                       self.setup, self.start, self.broke, f.food, self.herd, f.equipment, f.containers, self.day)

    def at_position(self, pos: XYZ) -> "Context":
        """The final world with the bot at a sampled position (for position checks against the track)."""
        c = Context(**{k: getattr(self, k) for k in self.__dataclass_fields__})
        c.bot_position = tuple(pos)
        c.frames = []
        return c


@dataclass
class Result:
    passed: bool
    score: float                       # partial credit, 0..1
    checks: dict[str, bool] = field(default_factory=dict)
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"passed": self.passed, "score": round(self.score, 3), "checks": self.checks, "detail": self.detail}


Grader = Callable[[Context, dict], Result]
REGISTRY: dict[str, Grader] = {}


def grader(name: str):
    def deco(fn: Grader) -> Grader:
        REGISTRY[name] = fn
        return fn
    return deco


def built_for(ctx: Context, spec: dict) -> Snapshot:
    """built(ctx.diff), and with `with_setup: true` also the blocks the task's setup placed that still stand: for a
    check about a structure the task provides (the shed to put the chest in, the hut to seal the villager in, the
    chest to shelter). Without it, what setup placed is the world as the bot found it, not its work."""
    placed = built(ctx.diff)
    if spec.get("with_setup") and ctx.setup:
        placed = {**{p: b for p, b in ctx.setup.items() if ctx.after.get(p) == b}, **placed}
    return placed


# Thin cover a wall goes down over (the cell is replaced, not placed), and the natural blocks that are only ever there
# because something put them: dirt over a snow layer is a wall, not the ground growing (a dirt hut on the tundra
# graded as open: its bottom course, laid over snow, was read as terrain)
COVER = structural.GROUND_COVER | {"short_grass", "tall_grass", "grass", "fern", "large_fern", "dead_bush", "tall_dry_grass",
                                   "bush", "firefly_bush"}
LAID = {"dirt", "coarse_dirt", "rooted_dirt", "stone", "deepslate", "cobbled_deepslate", "andesite", "diorite", "granite",
        "tuff", "sand", "red_sand", "gravel", "sandstone", "red_sandstone", "snow_block", "packed_ice", "netherrack", "mud",
        "moss_block", "clay"}


def built(d: BlockDiff) -> Snapshot:
    """Blocks the bot added: newly placed, plus placed over something passable (grass, snow,
    water...) or in the place of terrain it dug out first (stone -> torch), which the diff records
    as `replaced`. A replacement that is itself natural (grass spreading, water flowing, a stone
    fill by the task's setup) is not a build, unless it is a building block laid over thin cover
    (dirt over a snow layer)."""
    from ..tables import is_natural
    out = dict(d.placed)
    for p, (old, new) in d.replaced.items():
        if not is_natural(new) or (old in COVER and new in LAID):   # a torch over leaf litter, planks over grass, dirt over snow
            out[p] = new
    return out


def _matches(name: str, pat) -> bool:
    import fnmatch
    pats = pat if isinstance(pat, list) else [pat]
    return any(fnmatch.fnmatch(name, p) for p in pats)


def grade(spec: dict, ctx: Context) -> Result:
    kind = spec["kind"]
    if kind not in REGISTRY:
        raise KeyError(f"unknown grader {kind!r}; known: {sorted(REGISTRY)}")
    return REGISTRY[kind](ctx, {k: v for k, v in spec.items() if k != "kind"})


# ----------------------------------------------------------------- exact

def _expected_blocks(spec: dict, ctx: Context) -> Snapshot:
    """Expand a shape spec into {pos: block}. Positions are relative to the plot's
    min corner at floor level, i.e. (0, 0, 0) is the first air block above the floor."""
    ox, oy, oz = ctx.plot_volume.min
    shape = spec["shape"]
    mat = spec["block"]
    dx, dy, dz = spec.get("at", [0, 0, 0])
    out: Snapshot = {}
    if shape == "blocks":
        for x, y, z in spec["positions"]:
            out[(ox + dx + x, oy + dy + y, oz + dz + z)] = mat
    elif shape == "platform":
        w, d = spec["size"]
        for x in range(w):
            for z in range(d):
                out[(ox + dx + x, oy + dy, oz + dz + z)] = mat
    elif shape == "hollow_box":  # walls only, open top and bottom
        w, d = spec["size"]
        h = spec["height"]
        for y in range(h):
            for x in range(w):
                for z in range(d):
                    if x in (0, w - 1) or z in (0, d - 1):
                        out[(ox + dx + x, oy + dy + y, oz + dz + z)] = mat
    else:
        raise ValueError(f"unknown shape {shape!r}")
    return out


@grader("exact")
def exact(ctx: Context, spec: dict) -> Result:
    """Every expected block is present in `after`; nothing else was placed in the plot."""
    expected = _expected_blocks(spec, ctx)
    present = {p for p, b in expected.items() if ctx.after.get(p) == b}
    extra = {p for p in built(ctx.diff) if p not in expected and ctx.plot_volume.contains(p)}
    allow_extra = spec.get("allow_extra", 0)
    score = len(present) / max(1, len(expected))
    checks = {"all_expected_present": len(present) == len(expected), "no_extra_blocks": len(extra) <= allow_extra}
    return Result(all(checks.values()), score * (1.0 if checks["no_extra_blocks"] else 0.8), checks,
                  {"expected": len(expected), "present": len(present), "missing": len(expected) - len(present),
                   "extra": sorted(extra)[:20]})


# --------------------------------------------------------------- position

@grader("position")
def position(ctx: Context, spec: dict) -> Result:
    """Bot ends within `tolerance` blocks (xz, and optional y) of a plot-relative target."""
    if ctx.bot_position is None:
        return Result(False, 0.0, {"has_position": False})
    if "target_abs" in spec:                       # "spawn" (plot centre) or [x, y, z]
        t = spec["target_abs"]
        tx, ty, tz = ctx.center if t == "spawn" else tuple(t)
    elif "target_rel" in spec:                     # [dx, dz] from the plot centre / spawn (ground level unknown), or [dx, dy, dz]
        rel = spec["target_rel"]
        dx, dy, dz = (rel[0], 0, rel[1]) if len(rel) == 2 else rel
        cx, cy, cz = ctx.center
        tx, ty, tz = cx + dx, cy + dy, cz + dz
    else:
        ox, oy, oz = ctx.plot_volume.min
        tx, ty, tz = spec["target"]
        tx, ty, tz = ox + tx, oy + ty, oz + tz
    bx, by, bz = ctx.bot_position
    dxz = ((bx - tx) ** 2 + (bz - tz) ** 2) ** 0.5
    tol = spec.get("tolerance", 1.0)
    ok_xz = dxz <= tol
    ok_y = abs(by - ty) <= spec.get("y_tolerance", 1.0)
    score = max(0.0, 1.0 - dxz / max(tol * 4, 1.0))
    return Result(ok_xz and ok_y, 1.0 if ok_xz and ok_y else score, {"xz_within": ok_xz, "y_within": ok_y},
                  {"target": [tx, ty, tz], "bot": list(ctx.bot_position), "distance_xz": round(dxz, 2)})


@grader("stayed")
def stayed(ctx: Context, spec: dict) -> Result:
    """The bot never went more than `radius` blocks (xz) from `target_rel` [dx, dz] off the plot centre: every
    sampled track position and the final one. For a task that is done where the bot stands, where the failure is
    the walk somewhere else."""
    dx, dz = spec.get("target_rel", [0, 0])[0], spec.get("target_rel", [0, 0])[-1]
    cx, _, cz = ctx.center if ctx.center is not None else (0, 0, 0)
    tx, tz = cx + dx, cz + dz
    pts = [(x, z) for _, x, _, z in ctx.track] + ([(ctx.bot_position[0], ctx.bot_position[2])] if ctx.bot_position else [])
    if not pts:
        return Result(False, 0.0, {"has_track": False})
    far = max(((x - tx) ** 2 + (z - tz) ** 2) ** 0.5 for x, z in pts)
    r = float(spec.get("radius", 32))
    return Result(far <= r, 1.0 if far <= r else max(0.0, 1.0 - (far - r) / max(r, 1.0)), {"stayed": far <= r},
                  {"target": [tx, tz], "farthest": round(far, 1), "radius": r})


# -------------------------------------------------------------- inventory

@grader("inventory")
def inventory(ctx: Context, spec: dict) -> Result:
    """Bot ends with at least `count` of `item` (glob ok: '*_log', or a list of names), or with at most
    `max` of it (`{item: gold_ingot, max: 40}`: it gave some away)."""
    have = sum(i["count"] for i in ctx.bot_inventory if _matches(i["name"], spec["item"]))
    if "max" in spec and "count" not in spec:
        ok = have <= spec["max"]
        return Result(ok, 1.0 if ok else 0.0, {"few_enough": ok}, {"have": have, "max": spec["max"]})
    need = spec["count"]
    return Result(have >= need, min(1.0, have / need), {"has_enough": have >= need}, {"have": have, "need": need})


# -------------------------------------------------------------- structure

@grader("structure")
def structure(ctx: Context, spec: dict) -> Result:
    """Open-ended build: enclosed interior, roof, entrance, footprint (`min_side`: the shorter side of the bbox in xz,
    so a 4x9 is not a 6x6; `min_height`: the bbox's height; `within`: its centre this near the plot's), containment."""
    req = {
        "enclosed": spec.get("enclosed", True),
        "roof": spec.get("roof", True),
        "entrance": spec.get("entrance", True),
        "min_footprint": spec.get("min_footprint", 0),
        "min_interior": spec.get("min_interior", 1),
        "contained": spec.get("contained", True),
        "materials": spec.get("materials"),
        "min_placed": spec.get("min_placed", 0),
    }
    placed = built(ctx.diff)
    a = structural.analyze(ctx.after, placed, ctx.plot_volume, ctx.floor_y)
    checks: dict[str, bool] = {}
    if req["min_placed"]:
        checks["min_placed"] = len(placed) >= req["min_placed"]
    if req["enclosed"]:
        checks["enclosed"] = a.enclosed and a.interior_cells >= req["min_interior"]
    if req["roof"]:
        checks["has_roof"] = a.has_roof
    if req["entrance"]:
        checks["has_entrance"] = a.has_entrance
    if req["min_footprint"]:
        checks["footprint"] = a.footprint >= req["min_footprint"]
    if spec.get("min_side"):
        b = a.bbox or {}
        lo, hi = b.get("min"), b.get("max")
        checks["min_side"] = bool(lo and hi) and min(hi[0] - lo[0], hi[2] - lo[2]) + 1 >= spec["min_side"]
    if spec.get("max_side"):        # with min_side: an exact size (7x7, not 7x9)
        b = a.bbox or {}
        lo, hi = b.get("min"), b.get("max")
        checks["max_side"] = bool(lo and hi) and max(hi[0] - lo[0], hi[2] - lo[2]) + 1 <= spec["max_side"]
    if spec.get("min_height"):      # the bbox's height: a tower 5 tall, not a hut 3 tall
        b = a.bbox or {}
        lo, hi = b.get("min"), b.get("max")
        checks["min_height"] = bool(lo and hi) and hi[1] - lo[1] + 1 >= spec["min_height"]
    if spec.get("within") and ctx.center is not None:   # the structure stands at the site (the plot's centre)
        b = a.bbox or {}
        lo, hi = b.get("min"), b.get("max")
        checks["at_site"] = bool(lo and hi) and math.hypot((lo[0] + hi[0]) / 2 - ctx.center[0], (lo[2] + hi[2]) / 2 - ctx.center[2]) <= spec["within"]
    if req["contained"]:
        checks["contained_in_plot"] = all(ctx.plot_volume.contains(p) for p in placed)
    if req["materials"]:
        used = {b for b in placed.values()}
        checks["materials"] = any(m in used for m in req["materials"])
    score = sum(checks.values()) / max(1, len(checks))
    return Result(all(checks.values()), score, checks, a.to_dict())


@grader("stories")
def stories(ctx: Context, spec: dict) -> Result:
    """Multi-story build: at least `count` indoor floor levels of `min_area` standable cells each,
    and (with `connected`) a way to walk or climb from each story to the next: stairs, slabs,
    blocks to jump up, or a ladder. Exterior staircases count, unless `indoor: true` (the way up is
    inside the shell). `stairs: true`: the way up is a staircase of stair blocks, no ladder and no
    jumping onto full blocks."""
    need = spec.get("count", 2)
    _, box = structural.shell_box(built(ctx.diff), ctx.floor_y)
    if box is None:
        return Result(False, 0.0, {"stories": False}, {"stories": 0})
    found = structural.find_stories(ctx.after, box, ctx.floor_y, spec.get("min_area", 9), spec.get("min_rise", 3))
    checks = {"stories": len(found) >= need}
    score = min(1.0, len(found) / need)
    linked = None
    if spec.get("connected", True):
        region = box if spec.get("indoor") else box.expand(3)
        linked = [structural.can_walk(ctx.after, lo["cells"], hi["cells"], region, ctx.floor_y,
                                      stairs_only=bool(spec.get("stairs")), states=ctx.after_states)
                  for lo, hi in zip(found, found[1:])]
        checks["connected"] = checks["stories"] and all(linked)
        score = (score + (sum(linked) / max(1, need - 1) if linked else 0.0)) / 2
    return Result(all(checks.values()), min(1.0, score), checks,
                  {"stories": [{"y": s["y"], "area": len(s["cells"])} for s in found], "linked": linked})


# Every wood a tree or a fungus gives, and what is made of it, for "built of wood": planks, logs, stripped logs, wood,
# stems and hyphae, wooden stairs and slabs, bamboo's block and mosaic
WOODS = ("oak", "spruce", "birch", "jungle", "acacia", "dark_oak", "mangrove", "cherry", "pale_oak", "bamboo", "crimson",
         "warped")
WOOD_FORMS = ("_planks", "_log", "_wood", "_stem", "_hyphae", "_stairs", "_slab", "_block", "_mosaic", "_mosaic_stairs",
              "_mosaic_slab")
MATERIAL_FAMILIES = {"wood": lambda n: any(n.removeprefix("stripped_") == w + f for w in WOODS for f in WOOD_FORMS)}
# In a wall but not its material: windows, doors, and a fence or bars across an opening
WALL_FITTINGS = ("*glass*", "*_door", "*_trapdoor", "*_fence", "*_fence_gate", "iron_bars")


def _of_material(name: str, mat) -> bool:
    """`mat`: a family name (MATERIAL_FAMILIES: 'wood'), a glob, or a list of either."""
    return any(MATERIAL_FAMILIES[m](name) if m in MATERIAL_FAMILIES else _matches(name, m)
               for m in (mat if isinstance(mat, list) else [mat]))


@grader("story_walls")
def story_walls(ctx: Context, spec: dict) -> Result:
    """What each story's walls are made of. `materials: {1: [cobblestone, mossy_cobblestone], 2: wood}` (story 1 =
    ground; a family name, a glob, or a list): at least `min_fraction` of the solid blocks in that story's wall rows
    (the shell's perimeter, from the story's standing level up to the next story's floor, or three rows under the
    roof) are of it; windows and doors are fittings, not counted either way. `sealed: [2]`: those stories' walls have
    no opening at all, not even a door (the only way off that story is inside the house). `min_area` as stories."""
    _, box = structural.shell_box(built(ctx.diff), ctx.floor_y)
    mats = {int(k): v for k, v in (spec.get("materials") or {}).items()}
    sealed = [int(n) for n in spec.get("sealed") or []]
    need = max(list(mats) + sealed + [1])
    found = structural.find_stories(ctx.after, box, ctx.floor_y, spec.get("min_area", 9)) if box else []
    if len(found) < need:
        checks = {f"story_{n}": False for n in sorted(set(mats) | set(sealed))}
        return Result(False, 0.0, checks, {"stories": len(found), "need": need})
    frac = spec.get("min_fraction", 0.9)
    checks: dict[str, bool] = {}
    detail: dict[str, dict] = {}
    parts: list[float] = []
    for n, mat in sorted(mats.items()):
        cells = structural.ring(box, structural.wall_rows(found, box, n - 1))
        solid = [ctx.after[p] for p in cells if structural.is_solid(ctx.after, p, ctx.floor_y)
                 and not _matches(ctx.after[p], list(WALL_FITTINGS))]
        good = sum(1 for b in solid if _of_material(b, mat))
        f = good / len(solid) if solid else 0.0
        other: dict[str, int] = {}
        for b in solid:
            if not _of_material(b, mat):
                other[b] = other.get(b, 0) + 1
        checks[f"story_{n}_material"] = bool(solid) and f >= frac
        detail[f"story_{n}"] = {"wall_blocks": len(solid), "of_material": good, "fraction": round(f, 3),
                                "other": dict(sorted(other.items(), key=lambda kv: -kv[1])[:6])}
        parts.append(min(1.0, f / frac) if frac else 1.0)
    for n in sealed:
        cells = structural.ring(box, structural.wall_rows(found, box, n - 1))
        holes = [p for p in cells if not structural.is_solid(ctx.after, p, ctx.floor_y) or ctx.after.get(p, "").endswith(("_door", "_fence_gate"))]
        checks[f"story_{n}_sealed"] = not holes
        detail.setdefault(f"story_{n}", {})["openings"] = [list(p) for p in holes[:8]]
        parts.append(1.0 if not holes else max(0.0, 1.0 - len(holes) / 4))
    return Result(all(checks.values()), sum(parts) / max(1, len(parts)), checks, detail)


def _upstairs(p, box, level: int) -> bool:
    """A feet position (floats ok) within the shell's walls, at or above `level`."""
    x, y, z = (math.floor(v) for v in p)
    return box.min[0] < x < box.max[0] and box.min[2] < z < box.max[2] and level <= y <= box.max[1]


def _outside(p, box) -> bool:
    x, _, z = (math.floor(v) for v in p)
    return not (box.min[0] <= x <= box.max[0] and box.min[2] <= z <= box.max[2])


@grader("exit_route")
def exit_route(ctx: Context, spec: dict) -> Result:
    """Left the top story on foot, the way a player would: once the top story's envelope was whole (its wall rows all
    standing, the roof on: the first frame in which every one of those cells the final world holds solid was solid),
    the bot was up there (that frame, a later one, or a track sample since: inside the walls at that story's level),
    and it ends outside the
    shell on the ground, below that story — and never once opened that envelope after it was whole: no frame shows one
    of its cells open, and the bot's own record of what it broke (dug, or broken by the pathfinder on a walk) holds none
    of them. A house left by digging through its upper wall, or by a gap left in it, fails. `story` (default: the top
    one), `min_area` as stories."""
    _, box = structural.shell_box(built(ctx.diff), ctx.floor_y)
    found = structural.find_stories(ctx.after, box, ctx.floor_y, spec.get("min_area", 9)) if box else []
    n = spec.get("story", len(found))
    if len(found) < 2 or not 2 <= n <= len(found):
        return Result(False, 0.0, {"upper_story": False}, {"stories": len(found)})
    level = found[n - 1]["y"]
    walls = structural.ring(box, structural.wall_rows(found, box, n - 1))
    above = [p for p in box if p[1] > max(r for r in structural.wall_rows(found, box, n - 1))
             and structural.is_solid(ctx.after, p, ctx.floor_y)]
    envelope = set(walls) | set(above)
    final_open = [p for p in walls if not structural.is_solid(ctx.after, p, ctx.floor_y) or ctx.after.get(p, "").endswith("_door")]
    shut = {p for p in envelope if structural.is_solid(ctx.after, p, ctx.floor_y)}
    frames = list(ctx.frames) + [Frame("final", ctx.after, ctx.bot_position, ctx.bot_inventory, t=ctx.seconds)]

    def whole(f: Frame) -> bool:
        return all(structural.is_solid(f.snapshot, p, ctx.floor_y) for p in shut)

    done = next((i for i, f in enumerate(frames) if whole(f)), None)
    checks = {"upper_walls_whole": not final_open and done is not None}
    detail: dict = {"story_y": level, "openings": [list(p) for p in final_open[:8]], "whole_at": None}
    if done is None:
        checks.update(went_upstairs=False, ended_outside=False, never_breached=False)
        return Result(False, 0.0, checks, detail)
    t_done = frames[done].t or 0.0
    detail["whole_at"] = {"frame": frames[done].label, "t": t_done}
    later = frames[done + 1:]
    breached = [f.label for f in later if not whole(f)]
    broke = [b for b in ctx.broke if b[0] > t_done and tuple(b[1:4]) in envelope]
    checks["never_breached"] = not breached and not broke
    detail["breached_in"] = breached[:6]
    detail["broke"] = [list(b) for b in broke[:8]]
    # standing up there in the frame it became whole counts: from then on the way out is the stairs or a breach
    ups = [(f.t or 0.0, f.position) for f in frames[done:] if f.position] + \
          [(t, (x, y, z)) for t, x, y, z in ctx.track if t >= t_done]
    ups = sorted((t, p) for t, p in ups if _upstairs(p, box, level))
    checks["went_upstairs"] = bool(ups)
    detail["upstairs_at"] = ups[0][0] if ups else None
    pos = ctx.bot_position
    out = pos is not None and _outside(pos, box) and math.floor(pos[1]) < level - 1
    checks["ended_outside"] = out
    detail["final_position"] = list(pos) if pos else None
    # out the top on the way down: a sample outside the shell at the top story's height after it was upstairs
    if ups:
        hi = [(t, p) for t, x, y, z in ctx.track for p in [(x, y, z)] if t > ups[0][0] and _outside(p, box) and math.floor(y) >= level - 1]
        checks["left_on_the_ground"] = not hi
        detail["out_up_high"] = [[t, *p] for t, p in hi[:4]]
    else:
        checks["left_on_the_ground"] = False
    return Result(all(checks.values()), sum(checks.values()) / len(checks), checks, detail)


@grader("furnishings")
def furnishings(ctx: Context, spec: dict) -> Result:
    """Things the bot placed, by kind. items: [{name, item (glob or list), count, inside: bool,
    story: n (1 = ground; implies inside), next_to: glob (touching such a block, e.g. a pressure
    plate beside a door), in_shell: bool (within the structure's bounding box: a door hung in its
    wall, not one set down in a field), clear: bool (a door that can be walked through: open two
    high on both sides, not walled in)}]. Doors and beds span two cells and count once."""
    placed = built_for(ctx, spec)
    _, box = structural.shell_box(placed, ctx.floor_y)
    levels = [s["y"] for s in structural.find_stories(ctx.after, box, ctx.floor_y, spec.get("min_area", 9))] if box else []
    checks: dict[str, bool] = {}
    detail: dict[str, dict] = {}
    score = 0.0
    for it in spec["items"]:
        cells = [p for p, b in placed.items() if _matches(b, it["item"])]
        if it.get("inside") or "story" in it:
            cells = [p for p in cells if box and structural.inside(ctx.after, p, box, ctx.floor_y)]
        if it.get("in_shell"):
            cells = [p for p in cells if box and box.contains(p)]
        if "story" in it:   # the story an item is on = the highest floor level at or below it
            cells = [p for p in cells if sum(1 for y in levels if y <= p[1]) == it["story"]]
        if "next_to" in it:
            cells = [p for p in cells if any(_matches(ctx.after.get((p[0] + dx, p[1], p[2] + dz), "air"), it["next_to"])
                                             for dx, dz in structural.XZ)]
        if it.get("clear"):         # a way through it: open, two high, on both sides (not blocked in from outside)
            cells = [p for p in cells if _clear_through(ctx, _lower_half(placed, p))]
        two_cell = bool(cells) and all(placed[p].endswith(structural.TWO_CELL_SUFFIXES) for p in cells)
        have = len(cells) // 2 if two_cell else len(cells)
        need = it.get("count", 1)
        name = it.get("name") or str(it["item"])
        checks[name] = have >= need
        detail[name] = {"have": have, "need": need}
        score += min(1.0, have / need)
    return Result(all(checks.values()), score / max(1, len(spec["items"])), checks, detail)


def _lower_half(placed: Snapshot, p: XYZ) -> XYZ:
    """The bottom cell of a two-cell block (a door): `p`, or the cell under it when that is the same block."""
    under = (p[0], p[1] - 1, p[2])
    return under if placed.get(under) == placed.get(p) else p


def _clear_through(ctx: Context, p: XYZ) -> bool:
    """Open on both sides of `p` along x or along z, at its height and the one above: a doorway a player walks through."""
    x, y, z = p
    return any(all(not structural.is_solid(ctx.after, (x + s * dx, y + h, z + s * dz), ctx.floor_y)
                   for s in (1, -1) for h in (0, 1))
               for dx, dz in ((1, 0), (0, 1)))


# ------------------------------------------------------------- survival bits

@grader("have_or_placed")
def have_or_placed(ctx: Context, spec: dict) -> Result:
    """`item` is in the inventory or a matching block was placed in the world (e.g. crafting_table)."""
    have = sum(i["count"] for i in ctx.bot_inventory if _matches(i["name"], spec["item"]))
    placed = sum(1 for b in built(ctx.diff).values() if _matches(b, spec["item"]))
    ok = have + placed >= spec.get("count", 1)
    return Result(ok, 1.0 if ok else 0.0, {"have_or_placed": ok}, {"have": have, "placed": placed})


@grader("placed")
def placed(ctx: Context, spec: dict) -> Result:
    """At least `count` blocks matching `block` were placed (or appeared: a lit portal's
    nether_portal blocks count). Unlike have_or_placed, what the bot carries does not count."""
    n = sum(1 for b in built(ctx.diff).values() if _matches(b, spec["block"]))
    need = spec.get("count", 1)
    return Result(n >= need, min(1.0, n / need), {"placed_enough": n >= need}, {"placed": n, "need": need})


@grader("blocks")
def blocks(ctx: Context, spec: dict) -> Result:
    """At least `min` blocks matching `block` exist in the plot at the end (placed or not): farmland,
    wheat, torches..."""
    n = sum(1 for p, b in ctx.after.items() if ctx.plot_volume.contains(p) and _matches(b, spec["block"]))
    need = spec.get("min", 1)
    return Result(n >= need, min(1.0, n / need), {"enough": n >= need}, {"count": n, "need": need})


@grader("guard")
def guard(ctx: Context, spec: dict) -> Result:
    """The live guard refused at most `max_refusals` calls (task.guard must be on)."""
    n = len(ctx.refusals)
    ok = n <= spec.get("max_refusals", 0)
    return Result(ok, 1.0 if ok else 0.0, {"few_refusals": ok}, {"refusals": n, "first": ctx.refusals[:3]})


@grader("entity_inside")
def entity_inside(ctx: Context, spec: dict) -> Result:
    """A mob (`entity`: villager...) is alive, and — with `inside: true` — stands inside the built shell
    (walls around, cover above; `enclosed: true` wants that shell closed but allows a doorway,
    `sealed: true` allows only a door block), and — with
    `near: {block, within, count}` — has that many placed blocks (torches) within `within` blocks of it.
    Position comes from the live server (RCON)."""
    import re
    if ctx.rcon is None:
        return Result(False, 0.0, {"live_world": False}, {"reason": "no server handle"})
    out = ctx.rcon(f"data get entity @e[type=minecraft:{spec['entity']},limit=1] Pos")
    m = re.search(r"\[(-?[\d.]+)d, (-?[\d.]+)d, (-?[\d.]+)d\]", out)
    checks = {"alive": m is not None}
    detail: dict = {"raw": out[:80]}
    if m:
        import math
        pos = tuple(math.floor(float(v)) for v in m.groups())
        detail["position"] = list(pos)
        placed = built_for(ctx, spec)
        if spec.get("inside"):
            _, box = structural.shell_box(placed, ctx.floor_y)
            checks["inside"] = box is not None and structural.inside(ctx.after, pos, box, ctx.floor_y)
        if spec.get("enclosed") or spec.get("sealed"):
            a = structural.analyze(ctx.after, placed, ctx.plot_volume, ctx.floor_y)
            if spec.get("enclosed"):
                checks["enclosed"] = a.enclosed
            if spec.get("sealed"):        # enclosed with no open doorway either: a door block is fine, a gap is not
                checks["sealed"] = a.enclosed and not a.entrance_cells
        if "near" in spec:
            n = spec["near"]
            cnt = sum(1 for p, b in placed.items() if _matches(b, n["block"]) and max(abs(p[i] - pos[i]) for i in range(3)) <= n.get("within", 4))
            checks["near_" + str(n["block"])] = cnt >= n.get("count", 1)
            detail["near"] = cnt
    else:
        for k in ("inside", "enclosed", "sealed"):
            if spec.get(k):
                checks[k] = False
        if "near" in spec:
            checks["near_" + str(spec["near"]["block"])] = False
    return Result(all(checks.values()), sum(checks.values()) / len(checks), checks, detail)


@grader("dug")
def dug(ctx: Context, spec: dict) -> Result:
    """At least `count` blocks matching `block` were broken (natural blocks only)."""
    n = sum(1 for b in ctx.diff.broken.values() if _matches(b, spec["block"]))
    need = spec.get("count", 1)
    return Result(n >= need, min(1.0, n / need), {"dug_enough": n >= need}, {"dug": n, "need": need})


@grader("level_site")
def level_site(ctx: Context, spec: dict) -> Result:
    """The site was prepared: no gaps under the walls (footing) and the interior ground is
    level within `tolerance` (natural, filled, or a placed floor)."""
    placed = built(ctx.diff)
    lv = structural.site_levelness(ctx.after, placed, ctx.floor_y)
    tol = spec.get("tolerance", 0)
    checks = {"footing": bool(placed) and lv["footing_ok"],
              "interior_level": lv["spread"] is not None and lv["spread"] <= tol}
    return Result(all(checks.values()), sum(checks.values()) / 2, checks, lv)


LIVE_KINDS = {"functional", "entity_inside", "respawn", "hydrated", "herd", "day"}   # graded against the server's final state: evaluated once, at the end
TRAJECTORY_KINDS = {"exit_route", "intact"}   # read the whole trial (frames, track, what broke): evaluated once, on the full context


@grader("milestones")
def milestones(ctx: Context, spec: dict) -> Result:
    """A ladder of checks evaluated against every per-step frame (and the final state).
    A milestone counts once it holds at any point — except `required` ones (a name or a
    list of names; they decide pass/fail and must hold at the end) and those marked
    `hold: true`, which only count if they still hold at the end. Use `hold` for state
    milestones such as alive/unharmed, which every trial "reaches" at step 1. `must: true`
    marks a milestone that has to have been reached at some point for the trial to pass
    (a waypoint on a patrol). `position` checks also look at the sampled position track,
    so a place visited in the middle of a step still counts. `by: 60` only counts a milestone
    first reached within that many seconds of the start (a required one with `by` fails late).
    `at_dawn: 1` is the same deadline at the frame the bench took at that dawn (`dawn_1`, the start of day 2); a trial
    that ended before that dawn has no such deadline. A check read once at the end (LIVE_KINDS, TRAJECTORY_KINDS)
    cannot take `at_dawn`: it has no time of its own. score = weighted fraction of milestones counted."""
    steps = spec["steps"]
    required = spec.get("required")
    required = [required] if isinstance(required, str) else list(required or [])
    for m in steps:
        if "at_dawn" in m and m["check"].get("kind") in LIVE_KINDS | TRAJECTORY_KINDS:
            raise ValueError(f"milestone {m['name']!r}: a {m['check']['kind']} check is read at the end, so it takes no at_dawn")
    frames = list(ctx.frames)
    dawn_t = {int(f.label[5:]): f.t for f in frames if f.label.startswith("dawn_") and f.label[5:].isdigit()}
    final = Frame("final", ctx.after, ctx.bot_position, ctx.bot_inventory, t=ctx.seconds, stats=ctx.stats, food=ctx.food,
                  equipment=ctx.equipment, containers=ctx.containers)
    reached: dict[str, int | None] = {}
    reached_t: dict[str, float | None] = {}
    final_ok: dict[str, bool] = {}
    final_detail: dict[str, dict] = {}      # each check's own detail at the end: why a milestone failed, not only that it did
    for m in steps:
        name, check = m["name"], m["check"]
        if check.get("kind") in TRAJECTORY_KINDS:
            r = grade(check, ctx)
            reached[name] = len(frames) + 1 if r.passed else None
            reached_t[name] = ctx.seconds if r.passed else None
            final_ok[name] = r.passed
            final_detail[name] = _compact({"checks": r.checks, **(r.detail or {})})
            continue
        hit = None
        hit_t = None
        # a check that acts on the live server (flip the lever, ask where the villager is) sees the
        # final world whatever frame it is asked about — and may not be repeatable — so it runs once
        history = [] if check.get("kind") in LIVE_KINDS else frames
        live_r = None
        for idx, f in enumerate(history + [final]):
            r = grade(check, ctx.at_frame(f))
            if f is final:
                live_r = r
            if r.passed:
                hit = idx + 1
                hit_t = f.t
                break
        if hit is None and check.get("kind") == "position" and ctx.track:
            for t, x, y, z in ctx.track:
                if grade(check, ctx.at_position((x, y, z))).passed:
                    hit, hit_t = 0, t          # step 0: seen on the track, between steps
                    break
        reached[name] = hit
        reached_t[name] = hit_t
        if check.get("kind") in LIVE_KINDS:
            final_ok[name] = hit is not None
            end_r = live_r
        else:
            end_r = grade(check, ctx.at_frame(final))
            final_ok[name] = end_r.passed
        if end_r is not None:
            final_detail[name] = _compact({"checks": end_r.checks, **(end_r.detail or {})})
    def deadline(m: dict) -> float | None:
        out = m.get("by")
        dawn = dawn_t.get(int(m["at_dawn"])) if "at_dawn" in m else None
        if dawn is not None:
            out = dawn if out is None else min(out, dawn)
        return out

    def in_time(m: dict) -> bool:
        d = deadline(m)
        if d is None or reached[m["name"]] is None:
            return True
        t = reached_t[m["name"]]
        return t is not None and t <= d
    def counted(m: dict) -> bool:
        name = m["name"]
        if not in_time(m):
            return False
        if name in required or m.get("hold"):
            return final_ok[name]
        return reached[name] is not None
    total = sum(m.get("weight", 1) for m in steps)
    got = sum(m.get("weight", 1) for m in steps if counted(m))
    by_name = {m["name"]: m for m in steps}
    passed = all(final_ok[r] and in_time(by_name[r]) for r in required) if required else all(v is not None for v in reached.values())
    passed = passed and all(reached[m["name"]] is not None and in_time(m) for m in steps if m.get("must"))
    checks = {m["name"]: counted(m) for m in steps}
    return Result(passed, got / total if total else 0.0, checks,
                  {"reached_at_step": reached, "reached_at_seconds": reached_t, "holds_at_end": final_ok,
                   "frames": len(frames), "dawns": dawn_t, "steps": final_detail})


def _compact(v, depth: int = 0):
    """A check's detail cut to a size a trial record can keep: long lists and deep nesting shortened."""
    if depth > 3:
        return str(v)[:120]
    if isinstance(v, dict):
        return {str(k): _compact(x, depth + 1) for k, x in list(v.items())[:30]}
    if isinstance(v, (list, tuple)):
        out = [_compact(x, depth + 1) for x in list(v)[:12]]
        return out + [f"... {len(v) - 12} more"] if len(v) > 12 else out
    if isinstance(v, str):
        return v[:400]
    return v if isinstance(v, (int, float, bool)) or v is None else str(v)[:120]


# ---------------------------------------------------------------- server stats

_OPS = {"<=": lambda a, b: a <= b, "<": lambda a, b: a < b, ">=": lambda a, b: a >= b, ">": lambda a, b: a > b,
        "==": lambda a, b: a == b}


@grader("stat")
def stat(ctx: Context, spec: dict) -> Result:
    """Compare a scoreboard counter (server truth): {name: deaths, op: "<=", value: 0} or
    {name: "mined:stone", min: 10}. Names: deaths, kills, damage_taken, damage_dealt, walked,
    sleep, mined:<block>, crafted:<item>, killed:<mob>, custom:<stat>."""
    name = spec["name"]
    have = ctx.stats.get(name)
    if have is None:
        return Result(False, 0.0, {"tracked": False}, {"name": name, "hint": "add it to the task's `stats` list"})
    if "min" in spec:
        ok = have >= spec["min"]
        score = min(1.0, have / spec["min"]) if spec["min"] else 1.0
    elif "max" in spec:
        ok = have <= spec["max"]
        score = 1.0 if ok else 0.0
    else:
        ok = _OPS[spec.get("op", ">=")](have, spec["value"])
        score = 1.0 if ok else 0.0
    return Result(ok, score, {name: ok}, {"name": name, "value": have})


@grader("survived")
def survived(ctx: Context, spec: dict) -> Result:
    """No deaths this trial (scoreboard) and alive at the end (server health)."""
    deaths = ctx.stats.get("deaths")
    checks = {"no_deaths": deaths is not None and deaths <= spec.get("max_deaths", 0),
              "alive": ctx.health is not None and ctx.health > 0}
    if "min_health" in spec:
        checks["min_health"] = ctx.health is not None and ctx.health >= spec["min_health"]
    return Result(all(checks.values()), sum(checks.values()) / len(checks), checks,
                  {"deaths": deaths, "health": ctx.health, "damage_taken": ctx.stats.get("damage_taken")})


@grader("respawn")
def respawn(ctx: Context, spec: dict) -> Result:
    """The respawn point at the end is set on a bed (server truth: the point, and a bed block at it), and
    optionally at least `min_from_spawn` blocks (xz) from the world spawn / plot centre. A bed the bot slept in or
    set_spawn on counts; a bed that is only placed does not, and neither does a point whose bed is gone.
    `max_from_spawn` bounds it the other way (the bed is near the work, not wherever the sheep were). `inside: true`
    wants the bed within the walls and under the roof of what the bot built (structural.inside): the bed of its base."""
    r = ctx.respawn or {}
    pos = r.get("pos")
    checks = {"bed_spawn": bool(pos) and bool(r.get("bed"))}
    detail = {"respawn": pos, "bed": r.get("bed")}
    if spec.get("inside"):
        _, box = structural.shell_box(built_for(ctx, spec), ctx.floor_y)
        checks["inside"] = bool(pos) and box is not None and structural.inside(ctx.after, tuple(pos), box, ctx.floor_y)
        detail["shell"] = box.to_dict() if box is not None else None
    if ("min_from_spawn" in spec or "max_from_spawn" in spec) and ctx.center is not None:
        d = math.hypot(pos[0] - ctx.center[0], pos[2] - ctx.center[2]) if pos else None
        detail["from_spawn"] = round(d, 1) if d is not None else None
        if "min_from_spawn" in spec:
            checks["away_from_spawn"] = d is not None and d >= spec["min_from_spawn"]
        if "max_from_spawn" in spec:
            checks["near_spawn"] = d is not None and d <= spec["max_from_spawn"]
    return Result(all(checks.values()), sum(checks.values()) / len(checks), checks, detail)


@grader("food")
def food(ctx: Context, spec: dict) -> Result:
    """The hunger bar (server truth) is at least `min` (20 = full)."""
    need = int(spec.get("min", 20))
    have = ctx.food
    ok = have is not None and have >= need
    return Result(ok, min(1.0, (have or 0) / need) if need else 1.0, {"fed": ok}, {"food": have, "need": need})


@grader("food_stock")
def food_stock(ctx: Context, spec: dict) -> Result:
    """The food carried is worth at least `min_points` hunger points eaten (food.FOOD_POINTS: bread 5, cooked beef 8):
    a store, not a meal. `in_containers: true` also counts what lies in the chests and barrels the bot placed, read at
    the end (a frame before it has no record of them, so only what was carried counts there)."""
    from ..tables import FOOD_POINTS, food_points
    need = int(spec["min_points"])
    carried = food_points(ctx.bot_inventory)
    stored = 0
    detail: dict = {}
    if spec.get("in_containers") and ctx.containers:
        stored = sum(food_points(items) for items in ctx.containers.values())
        detail["stored"] = {str(list(p)): {i["name"]: i["count"] for i in items if i.get("name") in FOOD_POINTS}
                            for p, items in ctx.containers.items() if food_points(items)}
    have = carried + stored
    items = {i["name"]: i["count"] for i in ctx.bot_inventory if i.get("name") in FOOD_POINTS}
    return Result(have >= need, min(1.0, have / need) if need else 1.0, {"stocked": have >= need},
                  {"points": have, "carried": carried, "in_containers": stored, "need": need, "items": items, **detail})


ARMOUR_SLOTS = ("head", "chest", "legs", "feet")


@grader("equipped")
def equipped(ctx: Context, spec: dict) -> Result:
    """What the player wears or holds (server truth): `slots: {chest: [iron_chestplate, diamond_chestplate], legs:
    "*_leggings"}`, a glob or a list per slot (head, chest, legs, feet, offhand, mainhand). Worn, not carried: armour
    in the inventory is not armour on."""
    want = dict(spec.get("slots") or {})
    eq = ctx.equipment
    if eq is None:
        return Result(False, 0.0, {f"{k}_worn": False for k in want}, {"reason": "equipment not read"})
    checks = {f"{k}_worn": bool(eq.get(k)) and _matches(eq[k]["name"], pat) for k, pat in want.items()}
    return Result(all(checks.values()) if checks else False, sum(checks.values()) / max(1, len(checks)), checks,
                  {"worn": {k: v["name"] for k, v in eq.items()}})


@grader("intact")
def intact(ctx: Context, spec: dict) -> Result:
    """The bot's house stood its ground after it was built: the shell of what it had built by the frame labelled `at`
    (`dawn_1`: the house it slept in), and what it placed within that shell, lost at most `max_broken` blocks from then
    on. Lost: broken in the trial's break log after that frame (patched or not), or gone from the final world. The
    house mined into for its stone, walled through instead of using the door, dug out from under its bed."""
    from ..world.volume import diff as _diff
    at = str(spec.get("at", "dawn_1"))
    ref = next((f for f in ctx.frames if f.label == at), None)
    if ref is None:
        return Result(False, 0.0, {"house_found": False}, {"reason": f"no {at} frame: the trial ended before it"})
    own = built(_diff(ctx.before, ref.snapshot))
    _, box = structural.shell_box(own, ctx.floor_y)
    if box is None:
        return Result(False, 0.0, {"house_found": False}, {"reason": f"nothing built by {at}"})
    house = {p: b for p, b in own.items() if box.contains(p)}
    t_ref = ref.t or 0.0
    broke = {tuple(b[1:4]) for b in ctx.broke if b[0] > t_ref and tuple(b[1:4]) in house}
    gone = {p for p in house if ctx.after.get(p) is None}          # air now (a block turned to another is not a loss)
    lost = broke | gone
    allow = int(spec.get("max_broken", 0))
    ok = len(lost) <= allow
    score = 1.0 if ok else max(0.0, 1.0 - (len(lost) - allow) / max(1.0, len(house) / 4))
    return Result(ok, score, {"house_found": True, "intact": ok},
                  {"house_blocks": len(house), "lost": len(lost), "allowed": allow, "since": {"frame": at, "t": t_ref},
                   "broken": [list(p) for p in sorted(broke)[:12]], "gone": [list(p) for p in sorted(gone)[:12]]})


@grader("day")
def day(ctx: Context, spec: dict) -> Result:
    """The trial lasted into day `min` (1 = the day it started in; the bench counts a dawn each time the clock comes
    out of night). For a task that ends at a dawn (`end_at_dawn`): it ran to its end, not to a death or the clock."""
    need = int(spec.get("min", 2))
    if ctx.day is None:
        return Result(False, 0.0, {"reached_day": False}, {"reason": "no day clock ran (the task needs daylight)"})
    ok = ctx.day >= need
    return Result(ok, 1.0 if ok else (ctx.day - 1) / max(1, need - 1), {"reached_day": ok}, {"day": ctx.day, "need": need})


HERD_COUNT = re.compile(r"count: (\d+)", re.I)


def herd_count(rcon, selector: str) -> int | None:
    """How many entities a selector matches, by `execute if entity` (26.x: "Test passed. Count: 3" / "Test failed").
    None when the server's answer is neither."""
    out = rcon(f"execute if entity {selector}")
    m = HERD_COUNT.search(out or "")
    if m:
        return int(m.group(1))
    return 0 if "failed" in (out or "").lower() else None


@grader("herd")
def herd(ctx: Context, spec: dict) -> Result:
    """Livestock kept: at least `min_each` living animals (babies count) of every kind in `types`, counted by the
    server in the plot at the end; with `max_baby_kills`, the herd watcher (evals/run.py HerdWatcher) saw the bot kill
    no more babies than that over the trial. A herd eaten down to one cow cannot breed back."""
    types = [str(t).removeprefix("minecraft:") for t in spec.get("types") or []]
    need = int(spec.get("min_each", 2))
    if ctx.rcon is None:
        return Result(False, 0.0, {"live_world": False}, {"reason": "no server handle at grade time"})
    (x0, _, z0), (x1, _, z1) = ctx.plot_volume.min, ctx.plot_volume.max
    box = f"x={x0},y=-64,z={z0},dx={x1 - x0},dy=384,dz={z1 - z0}"      # the plot's columns, every height
    counts = {t: herd_count(ctx.rcon, f"@e[type=minecraft:{t},{box}]") for t in types}
    checks = {f"{t}_kept": (n or 0) >= need for t, n in counts.items()}
    detail: dict = {"counts": counts, "need_each": need}
    if "max_baby_kills" in spec:
        kills = (ctx.herd or {}).get("baby_kills")
        checks["babies_spared"] = kills is not None and kills <= int(spec["max_baby_kills"])
        detail["herd"] = ctx.herd
    return Result(all(checks.values()) if checks else False, sum(checks.values()) / len(checks) if checks else 0.0,
                  checks, detail)


@grader("world_time")
def world_time(ctx: Context, spec: dict) -> Result:
    """Time of day at the end is within `between: [lo, hi]` ticks (wraps at 24000).
    Daytime is roughly 0..12000; 'morning' = [0, 6000]."""
    t = ctx.world_time
    lo, hi = spec.get("between", [0, 12000])
    ok = t is not None and ((lo <= t <= hi) if lo <= hi else (t >= lo or t <= hi))
    return Result(ok, 1.0 if ok else 0.0, {"time_in_window": ok}, {"time": t, "window": [lo, hi]})


from . import mechanical  # noqa: E402,F401  — registers schematic / rail_path / functional (needs `grader` above)
