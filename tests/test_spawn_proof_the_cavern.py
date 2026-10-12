"""spawn_proof_the_cavern: slabs on every surface of a Nether room that is to be a farm. The room from the setup
commands applied to a synthetic plot (the shelf, the alcove under it, the lava basin, the walkway): 262 surfaces in
every instance and a slab budget that leaves a shortfall the planks cover; the `spawnable` grader on that room
covered, half covered, torched instead, and with the alcove missed; the walkway kept. No server needed."""
import re

import pytest

from mcmsbench.arena import Arena
from mcmsbench.config import ArenaConfig, load as load_settings
from mcmsbench.graders import Context, Frame, grade, spawnable_surfaces
from mcmsbench.runner import build_goal
from mcmsbench.tasks import load
from mcmsbench.world.volume import diff

from test_variants import KEY

TASK = load("spawn_proof_the_cavern")
PLOT = Arena(ArenaConfig(), lambda cmd: "").plot(0, "the_nether", 16, 12)
START = PLOT.center()
FMT = lambda text: TASK._fmt(text, PLOT, START)        # noqa: E731
FLOOR = PLOT.floor_y


def _all():
    return [TASK.instance("varied", i) for i in range(16)] + [TASK.instance("heldout", i, KEY) for i in range(16)] + [TASK]


def _bare(c: str) -> str:
    """A setup command without the Nether plot's `execute in ... run` prefix."""
    return c.split(" run ", 1)[1] if c.startswith("execute in ") else c


def _apply(cmds) -> dict:
    """The setup's fills and setblocks on an empty plot: {pos: block}, air removing."""
    w: dict = {}
    for c in cmds:
        parts = _bare(c).split()
        if parts[0] == "fill":
            x0, y0, z0, x1, y1, z1 = (int(v) for v in parts[1:7])
            block = parts[7].removeprefix("minecraft:").split("[")[0].split("{")[0]
            for x in range(min(x0, x1), max(x0, x1) + 1):
                for y in range(min(y0, y1), max(y0, y1) + 1):
                    for z in range(min(z0, z1), max(z0, z1) + 1):
                        if block == "air":
                            w.pop((x, y, z), None)
                        else:
                            w[(x, y, z)] = block
        elif parts[0] == "setblock":
            x, y, z = (int(v) for v in parts[1:4])
            w[(x, y, z)] = parts[4].removeprefix("minecraft:").split("[")[0].split("{")[0]
    return w


def _room(inst=TASK) -> dict:
    return _apply(inst.render_setup(PLOT, START, "player"))


def _chest(cmds) -> dict[str, int]:
    c = next(c for c in cmds if "minecraft:chest" in c)
    out: dict[str, int] = {}
    for item, n in re.findall(r'id:\\?"minecraft:([a-z_]+)\\?",count:(\d+)', c):
        out[item] = out.get(item, 0) + int(n)
    return out


# ------------------------------------------------------------------ the task

def test_the_room_is_a_small_nether_plot_with_nothing_to_dig():
    w = TASK.world
    assert w.type == "flat" and w.dimension == "the_nether" and w.size == 16 and w.height == 12
    assert not TASK.movements["can_dig"] and TASK.inventory == {}
    assert "mined:netherrack" in TASK.stats


@pytest.mark.parametrize("inst", _all(), ids=lambda t: f"{t.split}-{t.param_values['alcove']}-{t.param_values['basin']['x']}-{t.param_values['slabs']}")
def test_every_instance_has_262_surfaces_and_a_budget_the_planks_cover(inst):
    cmds = inst.render_setup(PLOT, START, "player")
    assert not any(re.search(r"\{[a-z]{2}([+-]\d+)?\}|\$\{", c) for c in cmds), "an unrendered placeholder"
    for c in map(_bare, cmds):
        if c.startswith(("fill ", "setblock ")):
            v = [int(n) for n in c.split()[1:7 if c.startswith("fill") else 4]]
            for p in (v[:3], v[3:] or v[:3]):
                assert PLOT.volume.contains(tuple(p)), c
    room = _room(inst)
    open_, tops = spawnable_surfaces(room, PLOT.volume, FLOOR)
    assert len(open_) == 262 and tops == 262 + 1 + 9, (len(open_), tops)   # covered: under the chest, under the lava
    assert room.get((PLOT.volume.min[0] + 7, FLOOR + 1, PLOT.volume.min[2] + 7)) is None   # the bot starts on open floor
    chest = _chest(cmds)
    slabs = inst.param_values["slabs"]
    assert chest["cobblestone_slab"] == slabs and 0 < slabs - 192 <= 64
    assert chest["oak_planks"] * 2 >= 262 - slabs                         # 3 planks make 6 slabs
    assert inst.param_values["basin"]["words"] in inst.render(PLOT, START)


def test_the_goal_file_carries_the_spawnable_check_for_an_agent_to_score_itself():
    goal = build_goal(TASK, PLOT, START, load_settings(), 0, seed=None, fast_nights=False, grader_stops=True,
                      max_seconds=600, max_cost=5)
    parts = {u["name"]: u for u in goal["check"]["uncovered"]}
    assert parts["covered"]["check"] == {"kind": "spawnable", "max": 0}
    assert "alcove" in parts["covered"]["says"] and "walkway" in parts["walkway_kept"]["says"]


# ------------------------------------------------------------------ the grader on a synthetic room

ROOM = _room()
X0, Z0 = PLOT.volume.min[0], PLOT.volume.min[2]
SURFACES, _ = spawnable_surfaces(ROOM, PLOT.volume, FLOOR)
ALCOVE = [p for p in SURFACES if p[1] == FLOOR and p[2] <= Z0 + 3]
WALKWAY = [p for p in SURFACES if ROOM.get(p) == "nether_bricks"]
RIM = [p for p in SURFACES if ROOM.get(p) == "netherrack" and p[1] == FLOOR + 1 and p[2] >= Z0 + 4]
SHELF = [p for p in SURFACES if p[1] == FLOOR + 3]


def test_the_synthetic_room_has_the_parts_the_notes_describe():
    assert len(ALCOVE) == 16 and len(WALKWAY) == 22 and len(RIM) == 16 and len(SHELF) == 64
    assert len(SURFACES) - 16 - 22 - 16 - 64 == 144           # the open floor and the crafting table's top


def _covered(skip=(), with_="cobblestone_slab") -> dict:
    after = dict(ROOM)
    for x, y, z in SURFACES:
        if (x, y, z) not in set(skip):
            after[(x, y + 1, z)] = with_
    return after


def _ctx(after, *, deaths=0, stats=None, broke=()):
    stats = {"deaths": deaths, "damage_taken": 0, "mined:netherrack": 0, "crafted:oak_slab": 0, **(stats or {})}
    frames = [Frame("step_01", after, START, [], t=200.0, stats=stats)]
    return Context(ROOM, after, diff(ROOM, after), PLOT.volume, FLOOR, START, [], frames, stats, 20.0, 18000,
                   START, fmt=FMT, seconds=400.0, setup=dict(ROOM), start=START, broke=list(broke))


def test_every_surface_slabbed_passes():
    r = grade(TASK.grader, _ctx(_covered()))
    assert r.passed and r.score == 1.0, r.detail["steps"]


def test_oak_slabs_crafted_for_the_shortfall_are_as_good():
    after = _covered()
    for x, y, z in SURFACES[-60:]:
        after[(x, y + 1, z)] = "oak_slab"
    assert grade(TASK.grader, _ctx(after)).passed


def test_nothing_done_fails_with_the_room_whole():
    r = grade(TASK.grader, _ctx(dict(ROOM)))
    assert not r.passed and not r.checks["covered"] and not r.checks["most"] and r.checks["walkway_kept"]
    assert r.detail["steps"]["covered"]["spawnable"] == 262


def test_the_alcove_missed_is_most_but_not_all():
    r = grade(TASK.grader, _ctx(_covered(skip=ALCOVE)))
    assert not r.passed and r.checks["most"] and not r.checks["covered"]
    assert r.detail["steps"]["covered"]["spawnable"] == 16 and r.detail["steps"]["covered"]["by_block"] == {"floor": 16}


def test_the_walkway_and_the_rim_need_slabs_too():
    for part in (WALKWAY, RIM, SHELF):
        r = grade(TASK.grader, _ctx(_covered(skip=part)))
        assert not r.checks["covered"] and r.detail["steps"]["covered"]["spawnable"] == len(part)


def test_torches_do_not_stop_a_spawn_and_carpet_does():
    assert not grade(TASK.grader, _ctx(_covered(with_="torch"))).checks["covered"]
    assert grade(TASK.grader, _ctx(_covered(with_="red_carpet"))).checks["covered"]


def test_a_walkway_brick_taken_up_breaks_the_walkway():
    after = _covered()
    p = WALKWAY[5]
    del after[p], after[(p[0], p[1] + 1, p[2])]
    r = grade(TASK.grader, _ctx(after, broke=[(30.0, *p, "nether_bricks", "dig")]))
    assert not r.checks["walkway_kept"] and not r.passed
    assert r.detail["steps"]["covered"]["spawnable"] == 1                  # the floor under it is bare now


def test_a_death_or_digging_costs():
    assert not grade(TASK.grader, _ctx(_covered(), deaths=1)).passed
    r = grade(TASK.grader, _ctx(_covered(), stats={"mined:netherrack": 3}))
    assert r.passed and not r.checks["no_digging"] and r.score < 1.0
