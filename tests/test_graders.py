"""Grader tests on synthetic snapshots — no server needed.
Every grader must pass a correct build AND fail a deliberately broken one."""
import pytest

from mcmsbench.graders import Context, grade
from mcmsbench.world.volume import Volume, diff

FLOOR = -61
PLOT = Volume((0, FLOOR + 1, 0), (31, FLOOR + 24, 31))
Y0 = FLOOR + 1


def ctx(after, before=None, pos=None, inv=None):
    before = before or {}
    return Context(before, after, diff(before, after), PLOT, FLOOR, pos, inv or [])


def platform(x0, z0, w, d, y=Y0, mat="cobblestone"):
    return {(x0 + x, y, z0 + z): mat for x in range(w) for z in range(d)}


def walls(x0, z0, w, d, h, y0=Y0, mat="oak_planks"):
    return {(x0 + x, y0 + y, z0 + z): mat for y in range(h) for x in range(w) for z in range(d)
            if x in (0, w - 1) or z in (0, d - 1)}


# ------------------------------------------------------------------ exact

def test_exact_platform_passes_and_scores_partial():
    spec = {"kind": "exact", "shape": "platform", "size": [5, 5], "block": "cobblestone", "at": [12, 0, 12]}
    good = platform(12, 12, 5, 5)
    r = grade(spec, ctx(good))
    assert r.passed and r.score == 1.0
    broken = dict(good); del broken[(14, Y0, 14)]
    r = grade(spec, ctx(broken))
    assert not r.passed and abs(r.score - 24 / 25) < 1e-9


def test_exact_wrong_material_fails():
    spec = {"kind": "exact", "shape": "platform", "size": [2, 2], "block": "cobblestone", "at": [0, 0, 0]}
    assert not grade(spec, ctx(platform(0, 0, 2, 2, mat="dirt"))).passed


def test_exact_extra_blocks_fail():
    spec = {"kind": "exact", "shape": "platform", "size": [2, 2], "block": "cobblestone", "at": [0, 0, 0]}
    after = platform(0, 0, 2, 2); after[(10, Y0, 10)] = "cobblestone"
    r = grade(spec, ctx(after))
    assert not r.passed and r.checks["all_expected_present"] and not r.checks["no_extra_blocks"]


def test_exact_hollow_box():
    spec = {"kind": "exact", "shape": "hollow_box", "size": [5, 5], "height": 3, "block": "cobblestone", "at": [12, 0, 12]}
    good = walls(12, 12, 5, 5, 3, mat="cobblestone")
    assert len(good) == 48
    assert grade(spec, ctx(good)).passed
    filled = dict(good); filled[(14, Y0, 14)] = "cobblestone"   # interior block = extra
    assert not grade(spec, ctx(filled)).passed


# --------------------------------------------------------------- position

def test_position():
    spec = {"kind": "position", "target": [24, 0, 24], "tolerance": 1.0}
    assert grade(spec, ctx({}, pos=(24, Y0, 24))).passed
    assert grade(spec, ctx({}, pos=(25, Y0, 24))).passed
    r = grade(spec, ctx({}, pos=(30, Y0, 24)))
    assert not r.passed and r.score < 1.0
    assert not grade(spec, ctx({}, pos=None)).passed


# -------------------------------------------------------------- inventory

def test_inventory():
    spec = {"kind": "inventory", "item": "oak_log", "count": 10}
    assert grade(spec, ctx({}, inv=[{"name": "oak_log", "count": 6}, {"name": "oak_log", "count": 5}])).passed
    r = grade(spec, ctx({}, inv=[{"name": "oak_log", "count": 5}]))
    assert not r.passed and r.score == 0.5


# -------------------------------------------------------------- structure

HOUSE = {"kind": "structure", "min_footprint": 25, "min_interior": 8}


def house(door_gap=True, roof=True, hole=None):
    w = walls(10, 10, 7, 7, 3)
    if door_gap:  # 1x2 doorway in the middle of the south wall
        del w[(13, Y0, 10)]; del w[(13, Y0 + 1, 10)]
    if roof:
        w.update(platform(10, 10, 7, 7, y=Y0 + 3, mat="oak_planks"))
    if hole:
        w.pop(hole, None)
    return w


def test_house_with_doorway_passes():
    r = grade(HOUSE, ctx(house()))
    assert r.passed, r.to_dict()
    assert r.detail["interior_cells"] == 5 * 5 * 3


def test_house_with_door_block_passes():
    h = house(door_gap=False)
    h[(13, Y0, 10)] = "oak_door"; h[(13, Y0 + 1, 10)] = "oak_door"
    assert grade(HOUSE, ctx(h)).passed


def test_house_no_roof_fails():
    r = grade(HOUSE, ctx(house(roof=False)))
    assert not r.passed and not r.checks["has_roof"] and not r.checks["enclosed"]


def test_house_hole_in_wall_fails():
    r = grade(HOUSE, ctx(house(hole=(16, Y0 + 2, 13))))  # east wall, above ground level: a leak not an entrance
    assert not r.passed and not r.checks["enclosed"]


def test_house_no_entrance_fails():
    r = grade(HOUSE, ctx(house(door_gap=False)))
    assert not r.passed and r.checks["enclosed"] and not r.checks["has_entrance"]


def test_house_too_small_fails():
    small = walls(10, 10, 3, 3, 3); del small[(11, Y0, 10)]
    small.update(platform(10, 10, 3, 3, y=Y0 + 3, mat="oak_planks"))
    r = grade(HOUSE, ctx(small))
    assert not r.passed and not r.checks["footprint"]


def test_solid_cube_is_not_a_house():
    solid = {(10 + x, Y0 + y, 10 + z): "oak_planks" for x in range(5) for y in range(3) for z in range(5)}
    r = grade(HOUSE, ctx(solid))
    assert not r.passed and not r.checks["enclosed"]


def test_build_outside_plot_fails_containment():
    h = house(); h[(40, Y0, 40)] = "oak_planks"
    r = grade(HOUSE, ctx(h))
    assert not r.checks["contained_in_plot"]


def test_inventory_glob_and_list():
    inv = [{"name": "oak_log", "count": 6}, {"name": "birch_log", "count": 5}, {"name": "stick", "count": 4}]
    assert grade({"kind": "inventory", "item": "*_log", "count": 10}, ctx({}, inv=inv)).passed
    assert grade({"kind": "inventory", "item": ["oak_log", "birch_log"], "count": 11}, ctx({}, inv=inv)).passed
    assert not grade({"kind": "inventory", "item": "*_log", "count": 12}, ctx({}, inv=inv)).passed


def test_milestones_state_milestones_need_hold():
    """A milestone that held at step 1 but not at the end counts only when it is neither
    required nor marked hold: alive-at-start must not score a trial that died later."""
    from mcmsbench.graders import Context, Frame, grade
    from mcmsbench.world.volume import Volume, diff
    vol = Volume((0, 0, 0), (31, 31, 31))
    alive_frame = Frame("step_01", {}, (0, 0, 0), [], t=1.0, stats={"deaths": 0})
    dead_end = Context({}, {}, diff({}, {}), vol, None, (0, 0, 0), [], [alive_frame], {"deaths": 2}, 20.0, 1000)
    spec = {"kind": "milestones", "required": "arrived", "steps": [
        {"name": "arrived", "weight": 1, "check": {"kind": "position", "target": [0, 0, 0], "tolerance": 1}},
        {"name": "alive", "weight": 1, "check": {"kind": "survived"}},
        {"name": "alive_held", "weight": 1, "hold": True, "check": {"kind": "survived"}}]}
    r = grade(spec, dead_end)
    assert r.passed and r.checks == {"arrived": True, "alive": True, "alive_held": False}
    assert abs(r.score - 2 / 3) < 1e-9
    spec["required"] = ["arrived", "alive"]
    r = grade(spec, dead_end)
    assert not r.passed and r.checks["alive"] is False and abs(r.score - 1 / 3) < 1e-9


def test_milestones_use_the_position_track_and_must():
    from mcmsbench.graders import Context, grade
    from mcmsbench.world.volume import Volume, diff
    vol = Volume((0, 0, 0), (31, 31, 31))
    spec = {"kind": "milestones", "required": "home", "steps": [
        {"name": "visit_a", "weight": 1, "must": True, "check": {"kind": "position", "target_rel": [70, -50], "tolerance": 4, "y_tolerance": 400}},
        {"name": "home", "weight": 1, "check": {"kind": "position", "target_abs": "spawn", "tolerance": 3}}]}
    home = Context({}, {}, diff({}, {}), vol, None, (10, 64, 10), [], [], {}, 20.0, 1000, center=(10, 64, 10))
    r = grade(spec, home)
    assert not r.passed and r.checks == {"visit_a": False, "home": True}      # never seen at A
    home.track = [(2.0, 15, 64, 12), (4.0, 81, 70, -38), (6.0, 30, 66, 0)]     # A passed mid-step
    r = grade(spec, home)
    assert r.passed and r.checks["visit_a"] and r.detail["reached_at_step"]["visit_a"] == 0
    assert r.detail["reached_at_seconds"]["visit_a"] == 4.0


def test_stayed_counts_every_sampled_position():
    from mcmsbench.graders import Context, grade
    from mcmsbench.world.volume import Volume, diff
    empty = {}
    vol = Volume((0, 0, 0), (10, 10, 10))
    base = dict(before=empty, after=empty, diff=diff(empty, empty), plot_volume=vol, floor_y=None, center=(100, 70, 0))
    spec = {"kind": "stayed", "target_rel": [40, 0], "radius": 40}
    home = Context(**base, bot_position=(141, 70, 2), track=[(0, 140, 70, 0), (2, 150, 70, 20)])
    assert grade(spec, home).passed
    walked = Context(**base, bot_position=(141, 70, 2), track=[(0, 140, 70, 0), (60, 210, 70, 50), (120, 141, 70, 2)])
    r = grade(spec, walked)
    assert not r.passed and r.detail["farthest"] > 80           # the walk to the old hut and back


def test_milestones_keep_each_checks_detail():
    """Why a milestone failed, not only that it did (anthropic-0926: redstone_door's functional log and
    survival_house's shell analysis were dropped, which hid two grader bugs)."""
    after = {(5, FLOOR + 1, 5): "torch"}
    spec = {"kind": "milestones", "required": ["lit"], "steps": [
        {"name": "lit", "check": {"kind": "placed", "block": "*torch", "count": 4}},
        {"name": "logs", "check": {"kind": "inventory", "item": "*_log", "count": 2}}]}
    r = grade(spec, ctx(after, inv=[{"name": "oak_log", "count": 3}]))
    assert not r.passed
    steps = r.detail["steps"]
    assert steps["lit"]["placed"] == 1 and steps["lit"]["need"] == 4 and steps["lit"]["checks"] == {"placed_enough": False}
    assert steps["logs"]["checks"] == {"has_enough": True}
