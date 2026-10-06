"""Where and when a trial starts: the bot's initial situation as a task parameter."""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Callable

XYZ = tuple[int, int, int]
NOT_LAND = {"water", "lava", "kelp", "seagrass", "tall_seagrass", "bubble_column"}
WATER = {"water", "kelp", "seagrass", "tall_seagrass", "bubble_column"}
SITUATIONS = ("surface", "cave", "underwater", "enclosed", "pillar", "water_pit", "cavern")


@dataclass
class StartSpec:
    offset: list[int] = field(default_factory=lambda: [0, 0])   # dx, dz from the plot's centre / world spawn
    random_radius: int = 0                                      # if > 0: seeded random position within this radius
    distance: int = 0                                           # if > 0: this far (xz) from the centre, at `bearing`
    bearing: str | int = "random"                               # degrees clockwise from north (+z is south), or "random" (seeded)
    time: str | int | None = None                               # "day" | "noon" | "night" | "midnight" | ticks
    weather: str | None = None                                  # clear | rain | thunder
    gamemode: str = "survival"
    situation: str = "surface"                                  # surface | cave (a sealed pocket `depth` below) |
    depth: int = 15                                             #   underwater (on the floor of water >= 3 deep) |
                                                                #   enclosed (a stone shell around you) |
    height: int = 30                                            #   pillar (on top of a stone column `height` tall,
    width: int = 3                                              #     `width` x `width`; a fall from it is lethal) |
    lip: int = 2                                                #   water_pit (sunk in water `depth` deep, in a `width`
                                                                #     square pit whose bank stands `lip` over the water,
    wall: str = "dirt"                                          #     cut in `wall`; cap "ice" freezes the water's top
    cap: str = ""                                               #     layer over, but for the hole the bot fell through) |
                                                                #   cavern (a torch-lit cave about `width` across, `depth`
                                                                #     below, irregular walls and a domed roof)
    food: int | None = None                                     # hunger bar (0..20) to start at; saturation ends at 0


def resolve_start(spec: StartSpec, center: XYZ, task_id: str, trial: int, seed: int,
                  surface: Callable[[int, int], tuple[int | None, str | None]] | None = None,
                  floor: Callable[[int, int], tuple[int | None, str | None]] | None = None) -> XYZ:
    """Pick the start position. Deterministic per (task, trial, seed). On terrain,
    `surface(x, z) -> (top_y, block)` resolves the ground and water columns are skipped —
    except for an underwater start, which wants water and uses `floor(x, z)` (highest
    solid block) to stand the bot on the bottom. A cave start is `depth` below the surface;
    a pillar start is `height` above it; a water_pit start is on the floor of the pit's water, `lip` + `depth`
    below the standing level."""
    if spec.situation not in SITUATIONS:
        raise ValueError(f"unknown start situation {spec.situation!r}; one of {SITUATIONS}")
    cx, cy, cz = center
    x, z = cx + spec.offset[0], cz + spec.offset[1]
    rng = random.Random(f"{task_id}:{trial}:{seed}")
    if spec.distance > 0:
        deg = rng.uniform(0, 360) if spec.bearing == "random" else float(spec.bearing)
        # compass bearing: 0 = north (-z), 90 = east (+x)
        x += round(spec.distance * math.sin(math.radians(deg)))
        z -= round(spec.distance * math.cos(math.radians(deg)))
    if surface is None:
        # flat plot: the ground is uniform, so situations are pure offsets from the standing level
        if spec.random_radius > 0:
            x, z = x + rng.randint(-spec.random_radius, spec.random_radius), z + rng.randint(-spec.random_radius, spec.random_radius)
        if spec.situation == "pillar":
            return x, cy + spec.height, z
        if spec.situation in ("cave", "cavern"):
            return x, cy - spec.depth, z
        if spec.situation == "water_pit":
            return x, cy - spec.lip - spec.depth, z
        return x, cy, z
    # candidate columns: the point itself, then random ones in widening rings (a lake or an
    # ocean under the chosen bearing must not silently turn into a mid-air start)
    base_r = spec.random_radius or 12
    for ring in (1, 2, 3):
        r = base_r * ring
        candidates = [(x, z)] if ring == 1 and not spec.random_radius else []
        candidates += [(x + rng.randint(-r, r), z + rng.randint(-r, r)) for _ in range(64)]
        for px, pz in candidates:
            top_y, block = surface(px, pz)
            if top_y is None:
                continue
            if spec.situation == "underwater":
                if block not in WATER or floor is None:
                    continue
                fy, _ = floor(px, pz)
                if fy is not None and top_y - fy >= 3:
                    return px, fy + 1, pz
                continue
            if block in NOT_LAND:
                continue
            if spec.situation in ("cave", "cavern"):
                return px, top_y - spec.depth, pz
            if spec.situation == "pillar":
                return px, top_y + spec.height + 1, pz
            if spec.situation == "water_pit":
                return px, top_y + 1 - spec.lip - spec.depth, pz
            return px, top_y + 1, pz
    want = "water at least 3 deep" if spec.situation == "underwater" else "dry land"
    raise RuntimeError(f"no {want} within {base_r * 3} blocks of ({x}, {z}) for a {spec.situation} start "
                       f"(bearing {spec.bearing}); pick a bearing or offset that lands on it")


def situation_commands(spec: StartSpec, start: XYZ) -> list[str]:
    """RCON commands that build the start situation around the resolved position, run
    before the bot is placed. cave: a sealed 3x2x3 air pocket in solid stone. enclosed:
    a one-block stone shell with the bot in its 1x2 interior. pillar: a solid stone column,
    `width` square, from a little under the ground up to the block under the bot's feet. water_pit: a `width`-square
    pit in a block of `wall` (dirt: a farm pond's banks, the live bot 2026-09-25 took 13 steps to leave one; stone: a
    quarry or a flooded shaft), water `depth` deep with the bot on its floor, the bank `lip` over the water, open sky
    above; no beach, so the way out is a step cut into the bank (lip 0: a level bank, climbed straight out). cap "ice":
    the water's top layer frozen, a one-block hole two blocks east of the bot (where it fell through). cavern: see
    cavern_commands."""
    x, y, z = start
    if spec.situation == "cavern":
        return cavern_commands(start, spec.width)
    if spec.situation == "cave":
        return [f"fill {x - 2} {y - 1} {z - 2} {x + 2} {y + 3} {z + 2} minecraft:stone",
                f"fill {x - 1} {y} {z - 1} {x + 1} {y + 1} {z + 1} minecraft:air"]
    if spec.situation == "enclosed":
        return [f"fill {x - 1} {y - 1} {z - 1} {x + 1} {y + 2} {z + 1} minecraft:stone hollow"]
    if spec.situation == "pillar":
        r = max(0, (spec.width - 1) // 2)
        return [f"fill {x - r} {y - spec.height - 3} {z - r} {x + r} {y - 1} {z + r} minecraft:stone"]
    if spec.situation == "water_pit":
        r = max(0, (spec.width - 1) // 2)
        top = y + spec.depth - 1                    # the water's top cell
        ground = top + spec.lip                     # the bank's top block (stand on it at ground + 1)
        cmds = [f"fill {x - r - 1} {y - 1} {z - r - 1} {x + r + 1} {ground} {z + r + 1} minecraft:{spec.wall}",
                f"fill {x - r} {y} {z - r} {x + r} {top} {z + r} minecraft:water"]
        if spec.cap:
            cmds += [f"fill {x - r} {top} {z - r} {x + r} {top} {z + r} minecraft:{spec.cap}",
                     f"setblock {x + min(2, r)} {top} {z} minecraft:water"]
        if spec.lip > 0:
            cmds.append(f"fill {x - r} {top + 1} {z - r} {x + r} {ground} {z + r} minecraft:air")
        return cmds + [f"fill {x - r - 1} {ground + 1} {z - r - 1} {x + r + 1} {ground + 4} {z + r + 1} minecraft:air"]
    return []


CAVERN_ROOF = 6        # air rows over the floor at the cavern's middle; 3 at its walls


def cavern_commands(start: XYZ, width: int) -> list[str]:
    """A sealed cave about `width` across (x and z) around `start`, the bot's feet on its floor: a block of stone with
    the cave cut out of it, column by column. Its wall wanders in and out (a squarish outline with a few lobes, never
    nearer the middle than 0.69 of the half-width), its roof domes from 3 high at the wall to CAVERN_ROOF in the
    middle, a few stone pillars stand 4-8 blocks out (cover from an archer), the floor is patched with andesite and
    tuff, and floor torches about every 8 blocks keep the light up to where nothing spawns (5+ at the walls). Seeded by the position, so a
    trial on the same spot gets the same cave. The middle (9 blocks out along the axes, 7 along the diagonals) is
    always open, and the pillars stay off the axes and diagonals, so a task can summon there."""
    x, y, z = start
    half = width / 2
    lo = -(width // 2)                               # columns lo .. lo + width - 1 (relative to the start)
    mid = lo + (width - 1) / 2
    rng = random.Random(f"cavern:{x}:{y}:{z}:{width}")
    lobes = [(k, rng.uniform(0.01, 0.035), rng.uniform(0, 2 * math.pi)) for k in (3, 5, 7)]
    cols = [(i, k) for i in range(lo, lo + width) for k in range(lo, lo + width)]
    rough = {c: (rng.uniform(0, 0.05), rng.random() < 0.2) for c in cols}   # a ragged wall line; a roof that drips

    def roof(i: int, k: int) -> int:
        """Air rows over the floor at column (x + i, z + k); 0 = wall."""
        u, v = (i - mid) / half, (k - mid) / half
        d = (abs(u) ** 3 + abs(v) ** 3) ** (1 / 3)   # 1 at the square's edge, rounded at the corners
        t = math.atan2(v, u)
        jag, drip = rough[(i, k)]
        edge = 0.95 - jag - sum(a * (1 + math.sin(n * t + ph)) for n, a, ph in lobes)
        if d >= edge:
            return 0
        return max(3, 3 + int((CAVERN_ROOF - 2.01) * (1 - (d / edge) ** 2)) - drip)

    heights = {c: roof(*c) for c in cols}
    pillars = []
    for q in range(4):                               # one a quadrant, 22.5 deg off the axis +-10
        if rng.random() < 0.25:
            continue
        a = math.radians(q * 90 + 22.5 + rng.uniform(-10, 10))
        r = rng.uniform(4.5, 8)
        pillars.append((round(r * math.cos(a)), round(r * math.sin(a))))
    cmds = [f"fill {x + lo - 1} {y - 1} {z + lo - 1} {x + lo + width} {y + CAVERN_ROOF} {z + lo + width} minecraft:stone"]
    for i in range(lo, lo + width):
        k = lo
        while k < lo + width:                        # runs of one roof height along z: one fill each
            h, k1 = heights[(i, k)], k
            while k1 + 1 < lo + width and heights[(i, k1 + 1)] == h:
                k1 += 1
            if h:
                cmds.append(f"fill {x + i} {y} {z + k} {x + i} {y + h - 1} {z + k1} minecraft:air")
            k = k1 + 1
    for pi, pk in pillars:
        cmds.append(f"fill {x + pi} {y} {z + pk} {x + pi} {y + CAVERN_ROOF - 1} {z + pk} minecraft:stone")
    for block in ("andesite", "tuff", "andesite"):  # floor patches, so it is not one grey sheet
        ci, ck, r = rng.randint(lo + 3, lo + width - 4), rng.randint(lo + 3, lo + width - 4), rng.randint(2, 4)
        cmds.append(f"fill {x + ci - r} {y - 1} {z + ck - r} {x + ci + r} {y - 1} {z + ck + r} minecraft:{block} replace minecraft:stone")
    for gi in range(lo + 3, lo + width, 8):          # a torch every 8 blocks, or the open floor nearest that spot
        for gk in range(lo + 3, lo + width, 8):
            near = sorted((abs(i - gi) + abs(k - gk), i, k) for i in range(gi - 4, gi + 4) for k in range(gk - 4, gk + 4)
                          if heights.get((i, k)) and (i, k) not in pillars)
            if near:
                cmds.append(f"setblock {x + near[0][1]} {y} {z + near[0][2]} minecraft:torch")
    return cmds


def time_ticks(t: str | int) -> str:
    """Argument for /time set."""
    return str(t)  # vanilla accepts day/noon/night/midnight or a tick count
