"""Structural analysis of a built structure: is it enclosed, roofed, enterable?

Terrain-agnostic: solidity comes from the `after` snapshot, so natural dirt is
as much a wall as a plank and a house dug into a hillside grades correctly.
`floor_y` is optional (flat plots / synthetic tests without ground blocks).

Enclosure = flood fill from outside the bbox cannot reach any interior cell.
Doors count as walls. An entrance is a gap of <= 2 cells on the perimeter that a
player could walk into from standable ground outside; it is not counted as a leak.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, asdict

from ..world.volume import Snapshot, Volume, XYZ

# thin ground cover (no collision box in 26.1): walked through like air, but a wall standing on it stands on the ground
GROUND_COVER = {"snow", "leaf_litter", "wildflowers", "pink_petals", "short_dry_grass", "short_grass"}
PASSABLE = {"air", "cave_air", "void_air", "water", "short_grass", "tall_grass", "grass", "fern", "large_fern",
            "torch", "wall_torch", "rail", "dandelion", "poppy", "dead_bush", "seagrass", "tall_dry_grass", "bush",
            "firefly_bush"} | GROUND_COVER
NEIGHBORS = [(1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)]
XZ = [(1, 0), (-1, 0), (0, 1), (0, -1)]


@dataclass
class Analysis:
    placed_blocks: int
    bbox: dict | None
    footprint: int          # area of bbox in xz
    interior_cells: int     # enclosed air cells inside the bbox
    enclosed: bool
    has_roof: bool
    has_entrance: bool
    entrance_cells: list[list[int]]
    leak_cells: int

    def to_dict(self) -> dict:
        return asdict(self)


# Furnishings and fittings: not part of the shell. They never extend the structure's bbox
# (a pressure plate on the doorstep is not a wall) and a player can stand in their cell.
DECOR_SUFFIXES = ("_carpet", "_pressure_plate", "_button", "_sign", "torch", "lantern")
DECOR = {"ladder", "lever", "flower_pot", "vine", "scaffolding"}
CLIMBABLE = {"ladder", "vine", "scaffolding"}
TWO_CELL_SUFFIXES = ("_door", "_bed")   # one item, two block cells


def is_decor(name: str) -> bool:
    return name in DECOR or name.endswith(DECOR_SUFFIXES)


# A field is not a building: tilled soil and what grows on it are never the shell (farmstead: a 9x9 wheat field by the
# pond outgrew the 5x5 house beside it, and the house was graded as the field: no roof, no walls, no bed inside)
FIELD = {"farmland", "wheat", "carrots", "potatoes", "beetroots", "melon_stem", "pumpkin_stem", "attached_melon_stem",
         "attached_pumpkin_stem", "torchflower_crop", "pitcher_crop"}
# ...and nor is a tree: a sapling the bot planted (or collect replanted) grows into new logs and leaves, which the
# diff calls placed (farmstead, random ticks at 40: a regrown birch canopy outgrew the house). A wall of logs is a
# wall; leaves never are.
GROWTH_SUFFIXES = ("_leaves", "_sapling", "_propagule")


def in_shell(name: str) -> bool:
    """A placed block that can be part of a structure's shell: not a fitting, a crop or a tree's growth."""
    return not is_decor(name) and name not in FIELD and not name.endswith(GROWTH_SUFFIXES)


def is_solid(after: Snapshot, p: XYZ, floor_y: int | None = None) -> bool:
    if floor_y is not None and p[1] <= floor_y:
        return True
    b = after.get(p)
    return b is not None and b not in PASSABLE and not is_decor(b)


def _outside_flood(after: Snapshot, box: Volume, floor_y: int | None, extra_walls: set[XYZ]) -> set[XYZ]:
    """Cells reachable from outside the box, restricted to box.expand(1)."""
    shell = box.expand(1)
    seen: set[XYZ] = set()
    starts = [p for p in shell if not box.contains(p) and not is_solid(after, p, floor_y)]
    q = deque(starts)
    seen.update(starts)
    while q:
        x, y, z = q.popleft()
        for dx, dy, dz in NEIGHBORS:
            n = (x + dx, y + dy, z + dz)
            if n in seen or not shell.contains(n) or n in extra_walls or is_solid(after, n, floor_y):
                continue
            seen.add(n)
            q.append(n)
    return seen


def _entrance_cells(after: Snapshot, box: Volume, reach: set[XYZ], floor_y: int | None) -> set[XYZ]:
    """Perimeter gaps a player could step into: the cell outside the gap must be
    reachable and have standable ground (solid below, or one air cell above solid)."""
    def boundary(p: XYZ) -> bool:
        return p[0] in (box.min[0], box.max[0]) or p[2] in (box.min[2], box.max[2])

    def standable(q: XYZ) -> bool:
        below = (q[0], q[1] - 1, q[2])
        if is_solid(after, below, floor_y):
            return True
        below2 = (q[0], q[1] - 2, q[2])
        return below in reach and is_solid(after, below2, floor_y)

    gap: set[XYZ] = set()
    for p in reach:
        if not box.contains(p) or not boundary(p):
            continue
        for dx, dz in XZ:
            q = (p[0] + dx, p[1], p[2] + dz)
            if box.contains(q) or q not in reach:
                continue
            if standable(q):
                gap.add(p)
                break
    return gap


def main_cluster(placed: Snapshot) -> Snapshot:
    """The largest 26-connected group of placed blocks: the structure, minus stray
    scaffolding, a crafting table left nearby, etc."""
    if not placed:
        return {}
    seen: set[XYZ] = set()
    best: list[XYZ] = []
    for start in placed:
        if start in seen:
            continue
        comp = [start]
        seen.add(start)
        q = deque([start])
        while q:
            x, y, z = q.popleft()
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for dz in (-1, 0, 1):
                        n = (x + dx, y + dy, z + dz)
                        if n in placed and n not in seen:
                            seen.add(n)
                            comp.append(n)
                            q.append(n)
        if len(comp) > len(best):
            best = comp
    return {p: placed[p] for p in best}


def trim_strays(placed: Snapshot) -> Snapshot:
    """The cluster without stray blocks stuck to its outside: an outermost side (x, z or top) holding one or two
    blocks, against a real wall one layer in, is a scaffold left behind, a step or a chimney pot, not the shell.
    Kept, it widens the box by a layer of air that reads as a wall full of holes (anthropic-0926 survival_house: one
    dirt block left from roofing touched the east wall, and a sound 5x5 house graded unenclosed and roofless)."""
    placed = dict(placed)
    for _ in range(8):
        if not placed:
            break
        trimmed = False
        for axis, top in ((0, False), (0, True), (2, False), (2, True), (1, True)):
            vals = [p[axis] for p in placed]
            edge = max(vals) if top else min(vals)
            inner = edge - 1 if top else edge + 1
            outer = [p for p in placed if p[axis] == edge]
            n_in = sum(1 for p in placed if p[axis] == inner)
            if len(outer) <= 2 and n_in >= max(6, 3 * len(outer)):
                for p in outer:
                    del placed[p]
                trimmed = True
                break
        if not trimmed:
            break
    return placed


def shell_box(placed: Snapshot, floor_y: int | None = None) -> tuple[Snapshot, Volume | None]:
    """The structure proper (largest cluster of placed non-decor blocks, strays trimmed) and its bbox."""
    placed = trim_strays(main_cluster({p: b for p, b in placed.items() if in_shell(b)}))
    if not placed:
        return {}, None
    xs = [p[0] for p in placed]; ys = [p[1] for p in placed]; zs = [p[2] for p in placed]
    lo_y = max(floor_y + 1, min(ys)) if floor_y is not None else min(ys)
    return placed, Volume((min(xs), lo_y, min(zs)), (max(xs), max(ys), max(zs)))


def analyze(after: Snapshot, placed: Snapshot, plot: Volume, floor_y: int | None = None) -> Analysis:
    placed, box = shell_box(placed, floor_y)
    if not placed:
        return Analysis(0, None, 0, 0, False, False, False, [], 0)
    footprint = (box.max[0] - box.min[0] + 1) * (box.max[2] - box.min[2] + 1)
    interior_candidates = {p for p in box if not is_solid(after, p, floor_y)
                           and box.min[0] < p[0] < box.max[0] and box.min[2] < p[2] < box.max[2]}
    if not interior_candidates:
        return Analysis(len(placed), box.to_dict(), footprint, 0, False, False, False, [], 0)

    reach = _outside_flood(after, box, floor_y, set())
    leaks = interior_candidates & reach
    entrance: list[XYZ] = []
    if leaks:
        gap = _entrance_cells(after, box, reach, floor_y)
        if 0 < len(gap) <= 2:
            entrance = sorted(gap)
            reach = _outside_flood(after, box, floor_y, set(entrance))
            leaks = interior_candidates & reach
    enclosed = not leaks
    interior = interior_candidates - reach
    cols: dict[tuple[int, int], int] = {}
    for x, y, z in interior:
        cols[(x, z)] = max(cols.get((x, z), y), y)
    has_roof = bool(cols) and all(
        any(is_solid(after, (x, yy, z), floor_y) for yy in range(top + 1, box.max[1] + 2))
        for (x, z), top in cols.items())
    has_door = any(b.endswith("_door") for b in placed.values())
    return Analysis(len(placed), box.to_dict(), footprint, len(interior), enclosed, has_roof,
                    has_door or bool(entrance), [list(p) for p in entrance], len(leaks))


# ------------------------------------------------------------------ stories

def _covered(after: Snapshot, p: XYZ, box: Volume, floor_y: int | None) -> bool:
    return any(is_solid(after, (p[0], yy, p[2]), floor_y) for yy in range(p[1] + 2, box.max[1] + 2))


def inside(after: Snapshot, p: XYZ, box: Volume, floor_y: int | None = None) -> bool:
    """Strictly within the shell's walls and under cover."""
    return (box.min[0] < p[0] < box.max[0] and box.min[2] < p[2] < box.max[2]
            and box.min[1] <= p[1] <= box.max[1] and _covered(after, p, box, floor_y))


def find_stories(after: Snapshot, box: Volume, floor_y: int | None = None, min_area: int = 9,
                 min_rise: int = 3) -> list[dict]:
    """Indoor floor levels, bottom-up. A floor cell is somewhere a player can stand inside the
    shell: solid below, two cells of room, a ceiling or roof somewhere above. A story is a level
    with at least `min_area` such cells, at least `min_rise` above the story below it (so the
    top of a bed or a chest is not a storey)."""
    levels: dict[int, set[XYZ]] = {}
    for p in box:
        if is_solid(after, p, floor_y) or is_solid(after, (p[0], p[1] + 1, p[2]), floor_y):
            continue
        if is_solid(after, (p[0], p[1] - 1, p[2]), floor_y) and inside(after, p, box, floor_y):
            levels.setdefault(p[1], set()).add(p)
    out: list[dict] = []
    for y in sorted(levels):
        if len(levels[y]) >= min_area and (not out or y - out[-1]["y"] >= min_rise):
            out.append({"y": y, "cells": levels[y]})
    return out


def wall_rows(stories: list[dict], box: Volume, i: int) -> range:
    """The y rows of story i's walls (0-based): from its standing level up to below the next story's floor; the top
    story's up to three rows, stopping under the roof's top layer (a gable or a ridge is not the wall)."""
    lo = stories[i]["y"]
    if i + 1 < len(stories):
        return range(lo, stories[i + 1]["y"] - 1)
    return range(lo, min(box.max[1] - 1, lo + 2) + 1)


def ring(box: Volume, rows) -> list[XYZ]:
    """The perimeter cells of the box (its outermost x and z columns) at the given y rows."""
    return [(x, y, z) for y in rows for x in range(box.min[0], box.max[0] + 1) for z in range(box.min[2], box.max[2] + 1)
            if x in (box.min[0], box.max[0]) or z in (box.min[2], box.max[2])]


def _walkable(after: Snapshot, p: XYZ, floor_y: int | None) -> bool:
    """A player can occupy this cell: passable, or a door/gate they can open."""
    if not is_solid(after, p, floor_y):
        return True
    return after.get(p, "").endswith(("_door", "_fence_gate"))


FACING = {"north": (0, -1), "south": (0, 1), "east": (1, 0), "west": (-1, 0)}


def stair_walkable(props: dict | None, dx: int, dz: int) -> bool:
    """Can a stair be walked up (no jump) stepping onto it along (dx, dz)? A stair climbs toward its facing: from its
    low front or its sides, yes; into its tall back, or onto an upside-down one (half=top), it is a full block's
    step. Unknown state (no `after_states`): yes."""
    if not props:
        return True
    if str(props.get("half", "bottom")) == "top":
        return False
    return FACING.get(str(props.get("facing"))) != (-dx, -dz)


def can_walk(after: Snapshot, starts: set[XYZ], goals: set[XYZ], region: Volume, floor_y: int | None = None,
             stairs_only: bool = False, states: dict | None = None) -> bool:
    """Could a player get from any start cell to any goal cell on foot? Feet positions; moves are
    a level step, a one-block step up (needs jump headroom unless it is onto stairs or a slab), a
    drop of up to 3, and straight up or down a ladder/vine/scaffolding column. `stairs_only`: a staircase a player
    walks up: every climb is onto a stair block, from its front or side and right way up (stair_walkable, by
    `states` {pos: props} when known), with the headroom the step lifts the head into (the cell two above the feet
    before it: a stairwell's opening runs back over the step below); no ladders, no jumping onto full blocks."""
    states = states or {}

    def climb(p: XYZ) -> bool:
        return not stairs_only and after.get(p) in CLIMBABLE

    def stand(p: XYZ) -> bool:
        if not (_walkable(after, p, floor_y) and _walkable(after, (p[0], p[1] + 1, p[2]), floor_y)):
            return False
        below = (p[0], p[1] - 1, p[2])
        return is_solid(after, below, floor_y) or climb(p) or climb(below)

    seen = {p for p in starts if stand(p)}
    q = deque(seen)
    while q:
        p = q.popleft()
        if p in goals:
            return True
        x, y, z = p
        nxt: list[XYZ] = []
        for dy in (1, -1):
            n = (x, y + dy, z)
            if climb(p) or climb(n):
                nxt.append(n)
        for dx, dz in XZ:
            nxt.append((x + dx, y, z + dz))
            step = after.get((x + dx, y, z + dz), "")
            head = _walkable(after, (x, y + 2, z), floor_y)   # a jump's headroom; a stair's too, its half-step lifts the head into it
            if stairs_only:
                up = step.endswith("_stairs") and head and stair_walkable(states.get((x + dx, y, z + dz)), dx, dz)
            else:
                up = step.endswith(("_stairs", "_slab")) or head
            if up:
                nxt.append((x + dx, y + 1, z + dz))
            for drop in (1, 2, 3):   # walk off an edge: the whole column we fall through must be clear
                if not all(_walkable(after, (x + dx, yy, z + dz), floor_y) for yy in range(y - drop + 1, y + 2)):
                    break
                nxt.append((x + dx, y - drop, z + dz))
        for n in nxt:
            if n not in seen and region.contains(n) and stand(n):
                seen.add(n)
                q.append(n)
    return False


def site_levelness(after: Snapshot, placed: Snapshot, floor_y: int | None = None) -> dict:
    """Was the site prepared? Two things a real builder does:
    footing  - every perimeter column has solid support directly under its lowest placed
               block (fill counts: it's placed and solid);
    interior - the walkable ground inside (highest solid cell below the roof, natural or a
               placed floor) is level: spread = max - min."""
    placed = main_cluster(placed)
    if not placed:
        return {"columns": 0, "footing_ok": False, "footing_gaps": 0, "spread": None}
    xs = [p[0] for p in placed]; zs = [p[2] for p in placed]
    x0, x1, z0, z1 = min(xs), max(xs), min(zs), max(zs)
    top_y = max(p[1] for p in placed)
    lowest: dict[tuple[int, int], int] = {}
    for (x, y, z) in placed:
        lowest[(x, z)] = min(lowest.get((x, z), y), y)
    min_y = min(lowest.values())
    gaps = 0
    perimeter = [(x, z) for x in range(x0, x1 + 1) for z in range(z0, z1 + 1)
                 if x in (x0, x1) or z in (z0, z1)]
    for x, z in perimeter:
        if (x, z) not in lowest:
            continue
        ly = lowest[(x, z)]
        if is_solid(after, (x, ly - 1, z), floor_y):
            continue
        if after.get((x, ly - 1, z)) in GROUND_COVER and is_solid(after, (x, ly - 2, z), floor_y):
            continue                        # on leaf litter or a snow layer over the ground: not floating
        # a doorway: two or more passable cells under the lintel, then solid ground.
        # a single air cell under a wall is a floating wall, i.e. a footing gap.
        k = ly - 1
        while k > ly - 5 and not is_solid(after, (x, k, z), floor_y):
            k -= 1
        if ly - 1 - k >= 2 and is_solid(after, (x, k, z), floor_y):
            continue
        gaps += 1
    grounds: list[int] = []
    for x in range(x0 + 1, x1):
        for z in range(z0 + 1, z1):
            for y in range(top_y - 1, top_y - 16, -1):
                p = (x, y, z)
                if p in placed and y > min_y:
                    continue  # scaffolding / furniture above floor level is not ground
                if is_solid(after, p, floor_y):
                    grounds.append(y)
                    break
    spread = (max(grounds) - min(grounds)) if grounds else None
    return {"columns": len(perimeter), "footing_ok": gaps == 0, "footing_gaps": gaps, "spread": spread,
            "interior_columns": len(grounds)}
