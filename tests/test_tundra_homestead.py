"""tundra_homestead: the carved world's setup (fills split under the server's limit, the biome paints within it), and the
grader on a synthetic tundra — a dirt house laid over the snow layer, its door, the bed inside holding the respawn
point. No server needed."""
import pytest

from mcmsbench.arena import Plot
from mcmsbench.graders import Context, built, grade
from mcmsbench.tasks import FILL_LIMIT, load, split_fill
from mcmsbench.world.volume import Volume, diff

SX, SY, SZ = 154, 70, 163          # seed 1's spawn (infra/worlds/1.json)
PLOT = Plot(0, (SX, SY - 1, SZ), Volume((SX - 64, SY - 12, SZ - 64), (SX + 64, SY + 28, SZ + 64)), flat=False)
TASK = load("tundra_homestead")


def _box(cmd: str) -> tuple[list[int], list[int]]:
    v = [int(t) for t in cmd.split()[1:7]]
    return [min(v[0], v[3]), min(v[1], v[4]), min(v[2], v[5])], [max(v[0], v[3]), max(v[1], v[4]), max(v[2], v[5])]


def _cells(cmds):
    out = []
    for c in cmds:
        lo, hi = _box(c)
        out += [(x, y, z) for x in range(lo[0], hi[0] + 1) for y in range(lo[1], hi[1] + 1) for z in range(lo[2], hi[2] + 1)]
    return out


# ------------------------------------------------------------------ split_fill

def test_split_fill_covers_the_same_cells_under_the_limit():
    big = "fill 0 0 0 127 40 127 minecraft:air"
    parts = split_fill(big)
    assert len(parts) > 1
    assert all(len(_cells([p])) <= FILL_LIMIT for p in parts)
    assert sorted(_cells(parts)) == sorted(_cells([big]))
    assert all(p.endswith(" minecraft:air") for p in parts)


def test_split_fill_cuts_a_layer_too_big_on_its_own_and_keeps_the_mode():
    wide = "fill 0 5 0 299 5 199 minecraft:snow replace minecraft:air"      # 60000 in one layer
    parts = split_fill(wide)
    assert all(len(_cells([p])) <= FILL_LIMIT for p in parts)
    assert sorted(_cells(parts)) == sorted(_cells([wide]))
    assert all(p.endswith(" minecraft:snow replace minecraft:air") for p in parts)


def test_split_fill_leaves_small_fills_and_other_commands_alone():
    assert split_fill("fill 0 0 0 9 9 9 minecraft:stone") == ["fill 0 0 0 9 9 9 minecraft:stone"]
    assert split_fill("summon minecraft:sheep 1 2 3") == ["summon minecraft:sheep 1 2 3"]
    with pytest.raises(ValueError):
        split_fill("fill 0 0 0 99 99 99 minecraft:stone hollow")


# ------------------------------------------------------------------ the world

def test_setup_renders_within_the_servers_limits():
    cmds = TASK.render_setup(PLOT, (SX, SY, SZ))
    fills = [c for c in cmds if c.startswith("fill ")]
    assert fills and all(len(_cells([c])) <= FILL_LIMIT for c in fills)
    q = lambda v: v - v % 4                       # fillbiome counts its box on the 4-block biome grid
    for c in (c for c in cmds if c.startswith("fillbiome ")):
        lo, hi = _box(c)
        assert (q(hi[0]) - q(lo[0]) + 1) * (q(hi[1]) - q(lo[1]) + 1) * (q(hi[2]) - q(lo[2]) + 1) <= FILL_LIMIT, c
    assert not any("{" in c.replace("{Color", "") for c in cmds), "an unrendered placeholder"
    # the land fills reach the border on every side and nothing goes beyond it
    for c in fills:
        lo, hi = _box(c)
        assert SX - 64 <= lo[0] and hi[0] <= SX + 63 and SZ - 64 <= lo[2] and hi[2] <= SZ + 63, c
    assert cmds[-1] == "time set 10600"
    assert TASK.world.border == 128 and TASK.world.radius == 64 and TASK.world.daylight
    assert TASK.spawn_first is False and TASK.world.passive_mobs is False


def test_three_sheep_in_a_gated_pen_among_the_trees():
    cmds = TASK.render_setup(PLOT, (SX, SY, SZ))
    sheep = [c for c in cmds if c.startswith("summon minecraft:sheep")]
    assert len(sheep) == 3                                    # a bed is three wool
    for s in sheep:
        x, y, z = (int(v) for v in s.split()[2:5])
        assert 8 < x - SX < 16 and -52 < z - SZ < -46        # inside the fence ring
    assert any(c.startswith(f"setblock {SX + 12} {SY} {SZ - 46} minecraft:spruce_fence_gate") for c in cmds)
    trees = [c for c in cmds if c.startswith("place feature minecraft:spruce")]
    assert len(trees) >= 12
    near = min(((int(c.split()[3]) - SX) ** 2 + (int(c.split()[5]) - SZ) ** 2) ** 0.5 for c in trees)
    assert near > 40                                          # no wood near the spawn: the walk north is the task


# ------------------------------------------------------------------ the grader

SITE = (SX + 4, SZ - 10)           # the scripted reference's corner


def _ground():
    g = {}
    for x in range(SX - 10, SX + 20):
        for z in range(SZ - 20, SZ + 5):
            g[(x, SY - 1, z)] = "grass_block"
            g[(x, SY, z)] = "snow"               # the tundra's snow layer
    return g


def _house(after, x0, z0, w=6, d=6, door=True, mat="dirt"):
    for x in range(x0, x0 + w):
        for z in range(z0, z0 + d):
            if x in (x0, x0 + w - 1) or z in (z0, z0 + d - 1):
                for y in (SY, SY + 1):
                    after[(x, y, z)] = mat
            else:
                after.pop((x, SY, z), None)      # the floor trodden clear of snow
            after[(x, SY + 2, z)] = "spruce_planks"
    for y in (SY, SY + 1):                       # the doorway, mid north wall
        if door:
            after[(x0 + 2, y, z0)] = "spruce_door"
        else:
            after.pop((x0 + 2, y, z0), None)
    return after


def _ctx(after, respawn, before=None):
    before = before if before is not None else _ground()
    return Context(before, after, diff(before, after), PLOT.volume, None, (SITE[0] + 2, SY, SITE[1] + 2), [], [],
                   {"deaths": 0}, 20.0, 1000, (SX, SY, SZ), respawn=respawn)


def _bed(after, x, z):
    after[(x, SY, z)] = "white_bed"
    after[(x, SY, z + 1)] = "white_bed"
    return {"pos": [x, SY, z], "bed": True}


def test_dirt_laid_over_the_snow_layer_is_a_build_and_ice_is_not():
    before = {(0, 70, 0): "snow", (1, 70, 0): "short_grass", (2, 62, 0): "water", (3, 69, 0): "grass_block"}
    after = {(0, 70, 0): "dirt", (1, 70, 0): "cobblestone", (2, 62, 0): "ice", (3, 69, 0): "dirt"}
    assert built(diff(before, after)) == {(0, 70, 0): "dirt", (1, 70, 0): "cobblestone"}   # freezing, a sheep's meal: nature


def test_a_dirt_house_on_snow_with_door_and_bed_spawn_passes():
    after = _house(_ground(), *SITE)
    spawn = _bed(after, SITE[0] + 2, SITE[1] + 2)
    r = grade(TASK.grader, _ctx(after, spawn))
    assert r.passed, r.detail["steps"]
    assert r.checks["house"] and r.checks["door"] and r.checks["bed_inside"] and r.checks["home_spawn"]


def test_no_door_fails_but_the_house_still_counts():
    after = _house(_ground(), *SITE, door=False)
    r = grade(TASK.grader, _ctx(after, _bed(after, SITE[0] + 2, SITE[1] + 2)))
    assert r.checks["house"] and not r.checks["door"] and not r.passed


def test_a_door_set_down_in_the_snow_is_not_the_houses():
    after = _house(_ground(), *SITE, door=False)
    after[(SITE[0] + 2, SY, SITE[1])] = "dirt"; after[(SITE[0] + 2, SY + 1, SITE[1])] = "dirt"   # walled up
    after[(SX - 8, SY, SZ)] = "spruce_door"; after[(SX - 8, SY + 1, SZ)] = "spruce_door"
    r = grade(TASK.grader, _ctx(after, _bed(after, SITE[0] + 2, SITE[1] + 2)))
    assert not r.checks["door"] and not r.passed


def test_the_spawn_bed_must_be_the_one_inside():
    after = _house(_ground(), *SITE)
    _bed(after, SITE[0] + 2, SITE[1] + 2)
    outside = _bed(after, SX - 6, SZ - 2)                     # a second bed out in the open holds the spawn
    r = grade(TASK.grader, _ctx(after, outside))
    assert r.checks["bed_inside"] and not r.checks["home_spawn"] and not r.passed
    gone = {"pos": [SITE[0] + 2, SY, SITE[1] + 2], "bed": False}
    assert not grade(TASK.grader, _ctx(after, gone)).passed
    assert not grade(TASK.grader, _ctx(after, None)).passed


def test_a_long_narrow_hut_is_not_a_6x6():
    after = _house(_ground(), SITE[0], SITE[1] - 4, w=4, d=10)
    r = grade(TASK.grader, _ctx(after, _bed(after, SITE[0] + 1, SITE[1])))
    assert not r.checks["house"] and not r.passed
    assert r.detail["steps"]["house"]["checks"]["min_side"] is False
