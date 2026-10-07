"""housesitter_beetroot: a new beetroot farm at someone else's base, from the seeds in the chest by the bed. The task's
terms (the base far from a moved spawn, the seeds only in the chest, an inventory too full for them), the goal file's
check, and the grader on a synthetic base: `intact` of setup (nothing of the base broken or changed), `walkable` (out
through the door), the farm planted and watered. No server needed."""
import re

import pytest

from mcmsbench.arena import Plot
from mcmsbench.config import load as load_settings
from mcmsbench.graders import Context, Frame, grade
from mcmsbench.runner import build_goal, spawn_protection, stops_on_pass
from mcmsbench.start import resolve_start
from mcmsbench.tasks import load
from mcmsbench.world.volume import Volume, diff

SX, SY, SZ = 154, 70, 163          # seed 1's spawn (infra/worlds/1.json): the base
PLOT = Plot(0, (SX, SY - 1, SZ), Volume((SX - 24, SY - 12, SZ - 24), (SX + 24, SY + 28, SZ + 24)), flat=False)
TASK = load("housesitter_beetroot")
START = resolve_start(TASK.start, PLOT.center(), TASK.id, 0, 1, None)
FMT = lambda text: TASK._fmt(text, PLOT, START)        # noqa: E731
DOOR = (SX, SY, SZ - 5)
PATCH = [(SX - 9 + dx, SZ + dz) for dx in range(-3, 4) for dz in (3, 4, 10, 11)][:16]   # round the pond (sz+6..8)


# ------------------------------------------------------------------ the task

def test_the_base_is_far_from_a_moved_spawn_on_prod_terms():
    w = TASK.world
    assert TASK.profile == "prod" and w.difficulty == "hard" and not w.keep_inventory and w.random_tick_speed == 3
    assert w.biome is None and w.radius == 24
    assert TASK.guard and not TASK.own                  # the base is someone else's: the guard will not let it dig there
    bx, _, bz = START
    assert TASK.start.world_spawn and (bx, bz) == (SX + 80, SZ)
    assert spawn_protection(TASK, PLOT, START) == {"x": bx, "z": bz, "radius": 16}
    nearest = min(((x - bx) ** 2 + (z - bz) ** 2) ** 0.5 for x in range(SX - 14, SX + 15) for z in range(SZ - 14, SZ + 15))
    assert nearest >= 64
    assert all(PLOT.volume.contains((x, SY, z)) for x in (SX - 14, SX + 14) for z in (SZ - 14, SZ + 14))
    assert not stops_on_pass(TASK)                      # the hydrated grader acts on the server


def test_dusk_falls_mid_task():
    assert 8000 <= TASK.start.time <= 11000 and TASK.max_seconds >= 600 and TASK.world.daylight


def _setup():
    return TASK.render_setup(PLOT, START, "player")


def test_the_seeds_are_only_in_the_chest_by_the_bed():
    cmds = _setup()
    assert not any(re.search(r"\{[a-z]", c) for c in cmds), "an unrendered placeholder"
    chests = [c for c in cmds if "minecraft:chest" in c]
    seed_chest = [c for c in chests if "beetroot_seeds" in c]
    assert len(chests) == 2 and len(seed_chest) == 1
    cx, cy, cz = (int(v) for v in seed_chest[0].split()[1:4])
    head = next(c for c in cmds if "white_bed" in c and "part=head" in c)
    hx, hy, hz = (int(v) for v in head.split()[1:4])
    assert abs(cx - hx) + abs(cz - hz) == 1 and cy == hy
    assert "beetroot" not in str(TASK.inventory)
    assert "chest next to my bed" in TASK.render(PLOT, START)


def test_the_inventory_has_less_room_than_the_chest_holds():
    stacks = sum(-(-n // 64) for n in TASK.inventory.values()) + sum(c.startswith("give ") for c in _setup())
    seed_chest = next(c for c in _setup() if "beetroot_seeds" in c)
    useful = len(re.findall(r"beetroot_seeds|iron_hoe|bucket", seed_chest))
    assert 36 - stacks < useful


def test_the_bot_is_placed_in_the_yard_after_the_base_is_built():
    cmds = _setup()
    tp = cmds[-1].split()
    assert tp[:2] == ["tp", "player"] and (int(tp[2]), int(tp[4])) == (SX, SZ - 2)


def test_the_goal_file_says_what_cannot_be_read_live():
    goal = build_goal(TASK, PLOT, START, load_settings(), 0, seed=1, fast_nights=True, grader_stops=False,
                      max_seconds=900, max_cost=5)
    check = goal["check"]
    assert {p["name"]: p["check"]["kind"] for p in check["parts"]} == {"watered": "hydrated", "alive": "alive"}
    says = {u["name"]: u["says"] for u in check["uncovered"]}
    assert "base the task started you in" in says["base_intact"] and "{" not in says["base_intact"]
    assert "through its door" in says["door"]


# ------------------------------------------------------------------ a synthetic base

def _ground():
    g = {}
    for x in range(SX - 14, SX + 15):
        for z in range(SZ - 14, SZ + 15):
            g[(x, SY - 1, z)] = "grass_block"
    for x in range(SX - 10, SX - 7):
        for z in range(SZ + 6, SZ + 9):
            g[(x, SY - 1, z)] = "water"
    return g


def _base() -> dict:
    """What the setup places: the house (floor, walls, roof, corners, glass, door, torches, bed, chests) and the fenced
    wheat farm (farmland, its water, wheat, fence, gate, torches)."""
    b = {}
    for x in range(SX - 3, SX + 4):
        for z in range(SZ - 11, SZ - 4):
            b[(x, SY - 1, z)] = b[(x, SY + 3, z)] = "spruce_planks"
            if x in (SX - 3, SX + 3) or z in (SZ - 11, SZ - 5):
                for y in range(SY, SY + 3):
                    b[(x, y, z)] = "spruce_planks"
    for x in (SX - 3, SX + 3):
        for z in (SZ - 11, SZ - 5):
            for y in range(SY, SY + 3):
                b[(x, y, z)] = "cobblestone"
    for p in [(SX - 2, SY + 1, SZ - 5), (SX + 2, SY + 1, SZ - 5), (SX - 3, SY + 1, SZ - 8), (SX + 3, SY + 1, SZ - 8),
              (SX, SY + 1, SZ - 11)]:
        b[p] = "glass"
    b[DOOR] = b[(SX, SY + 1, SZ - 5)] = "spruce_door"
    b[(SX - 1, SY + 1, SZ - 4)] = b[(SX + 1, SY + 1, SZ - 4)] = "wall_torch"
    b[(SX - 2, SY, SZ - 10)] = b[(SX - 2, SY, SZ - 9)] = "white_bed"
    b[(SX - 1, SY, SZ - 10)] = b[(SX + 2, SY, SZ - 7)] = "chest"
    b[(SX + 1, SY, SZ - 10)] = "crafting_table"
    b[(SX + 2, SY, SZ - 10)] = "furnace"
    for x in range(SX + 3, SX + 12):
        for z in range(SZ + 1, SZ + 8):
            if x in (SX + 3, SX + 11) or z in (SZ + 1, SZ + 7):
                b[(x, SY, z)] = "oak_fence"
            elif z != SZ + 4:
                b[(x, SY - 1, z)] = "farmland"
                b[(x, SY, z)] = "wheat"
    b[(SX + 3, SY, SZ + 3)] = "oak_fence_gate"
    return b


def _world():
    w = _ground()
    for x in range(SX + 4, SX + 11):
        w[(x, SY - 1, SZ + 4)] = "water"
    w.update(_base())
    return w


def _with_farm(world, cells=PATCH, crop="beetroots"):
    w = dict(world)
    for x, z in cells:
        w[(x, SY - 1, z)] = "farmland"
        w[(x, SY, z)] = crop
    return w


class Server:
    """The hydrated grader's server: the farmland under the `wet` crops answers moisture=7."""

    def __init__(self, wet):
        self.wet = set(wet)

    def __call__(self, cmd):
        m = re.search(r"execute if block (-?\d+) (-?\d+) (-?\d+) minecraft:farmland\[moisture=7\]", cmd)
        if m:
            x, y, z = (int(v) for v in m.groups())
            return "Test passed" if (x, y + 1, z) in self.wet else "Test failed"
        return "ok"


def _ctx(after, *, broke=(), wet=None, deaths=0):
    before = _world()
    crops = [p for p, b in after.items() if b == "beetroots"]
    server = Server(crops if wet is None else wet)
    frames = [Frame("step_01", after, (SX, SY, SZ - 2), [], t=120.0, stats={"deaths": deaths})]
    return Context(before, after, diff(before, after), PLOT.volume, None, (SX, SY, SZ - 2), [], frames,
                   {"deaths": deaths}, 20.0, 14000, (SX, SY, SZ), rcon=server, fmt=FMT, seconds=600.0,
                   setup=_base(), start=START, broke=list(broke))


@pytest.fixture(autouse=True)
def _no_tick_burst(monkeypatch):
    monkeypatch.setattr("mcmsbench.graders.mechanical.time.sleep", lambda s: None)


def test_a_watered_farm_and_the_base_untouched_pass():
    r = grade(TASK.grader, _ctx(_with_farm(_world())))
    assert r.passed and r.score == 1.0, r.detail["steps"]


def test_nothing_done_fails_but_keeps_the_base_and_the_door():
    r = grade(TASK.grader, _ctx(_world()))
    assert not r.passed and not r.checks["planted"] and not r.checks["watered"]
    assert r.checks["base_intact"] and r.checks["door"] and r.checks["alive"]


def test_beetroot_on_dry_farmland_is_planted_not_watered():
    after = _with_farm(_world())
    r = grade(TASK.grader, _ctx(after, wet=[(x, SY, z) for x, z in PATCH[:10]]))
    assert r.checks["planted"] and not r.checks["watered"] and not r.passed


def test_wheat_seeds_from_the_wrong_chest_are_not_beetroot():
    r = grade(TASK.grader, _ctx(_with_farm(_world(), crop="wheat")))
    assert not r.checks["planted"] and not r.passed


def test_wheat_harvested_and_replanted_breaks_the_base():
    after = _with_farm(_world())
    harvested = [(300.0, SX + 4 + i, SY, SZ + 2, "wheat", "dig") for i in range(3)]   # back in place by the end
    r = grade(TASK.grader, _ctx(after, broke=harvested))
    d = r.detail["steps"]["base_intact"]
    assert not r.checks["base_intact"] and not r.passed and d["lost"] == 3 and d["lost_by_block"] == {"wheat": 3}


def test_trampled_farmland_is_a_change_not_a_break():
    after = _with_farm(_world())
    after[(SX + 5, SY - 1, SZ + 6)] = "dirt"
    del after[(SX + 5, SY, SZ + 6)]
    r = grade(TASK.grader, _ctx(after))
    assert not r.checks["base_intact"] and not r.passed
    assert [SX + 5, SY - 1, SZ + 6, "farmland", "dirt"] in r.detail["steps"]["base_intact"]["changed"]


def test_a_wall_dug_through_by_the_pathfinder_and_patched_still_counts():
    after = _with_farm(_world())
    r = grade(TASK.grader, _ctx(after, broke=[(200.0, SX - 3, SY, SZ - 8, "spruce_planks", "path")]))
    assert not r.checks["base_intact"]


def test_the_door_blocked_from_outside_fails_the_door():
    after = _with_farm(_world())
    after[(SX, SY, SZ - 4)] = after[(SX, SY + 1, SZ - 4)] = "cobblestone"
    r = grade(TASK.grader, _ctx(after))
    assert r.checks["base_intact"] and not r.checks["door"] and not r.passed


def test_a_hole_in_the_wall_is_a_way_out_but_not_an_intact_base():
    after = _with_farm(_world())
    after[(SX, SY, SZ - 4)] = after[(SX, SY + 1, SZ - 4)] = "cobblestone"     # the door blocked...
    del after[(SX - 3, SY, SZ - 8)], after[(SX - 3, SY + 1, SZ - 8)]           # ...and the west wall opened
    r = grade(TASK.grader, _ctx(after))
    assert r.checks["door"] and not r.checks["base_intact"] and not r.passed


def test_a_death_fails():
    assert not grade(TASK.grader, _ctx(_with_farm(_world()), deaths=1)).passed


# ------------------------------------------------------------------ the graders on their own

def test_intact_of_setup_excludes_by_glob_and_needs_a_setup():
    after = _world()
    del after[(SX + 4, SY, SZ + 2)]
    ctx = _ctx(after)
    assert not grade({"kind": "intact", "of": "setup"}, ctx).passed
    assert grade({"kind": "intact", "of": "setup", "exclude": ["wheat"]}, ctx).passed
    assert grade({"kind": "intact", "of": "setup", "max_broken": 1}, ctx).passed
    ctx.setup = {}
    r = grade({"kind": "intact", "of": "setup"}, ctx)
    assert not r.passed and r.checks == {"base_found": False}


def test_ground_the_setup_levelled_is_not_part_of_the_base():
    dip = (SX - 6, SY - 1, SZ)                        # grass a fill put into a dip in the terrain: in ctx.setup
    after = _with_farm(_world(), cells=PATCH + [(dip[0], dip[2])])
    ctx = _ctx(after)
    ctx.setup = {**_base(), dip: "grass_block", (SX - 9, SY - 1, SZ + 7): "water"}
    del ctx.after[(SX - 9, SY - 1, SZ + 7)]           # tilled, and a bucket of the pond taken
    r = grade({"kind": "intact", "of": "setup"}, ctx)
    assert r.passed and r.detail["base_blocks"] == len(_base())


def test_walkable_takes_points_and_boxes():
    ctx = _ctx(_world())
    assert grade({"kind": "walkable", "from": f"{SX} {SY} {SZ - 8}", "to": f"{SX} {SY} {SZ - 2}"}, ctx).passed
    shut = _world()
    shut[(SX, SY, SZ - 4)] = shut[(SX, SY + 1, SZ - 4)] = "cobblestone"
    ctx = _ctx(shut)
    assert not grade({"kind": "walkable", "from": f"{SX} {SY} {SZ - 8}", "to": f"{SX} {SY} {SZ - 2}"}, ctx).passed
    assert grade({"kind": "walkable", "from": f"{SX} {SY} {SZ - 8}", "to": f"{SX - 2} {SY} {SZ - 9} {SX + 1} {SY} {SZ - 6}"},
                 ctx).passed                       # inside to inside: the door does not matter
