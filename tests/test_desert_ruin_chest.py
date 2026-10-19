"""desert_ruin_chest: four emeralds out of a trapped chamber. The chamber from the setup commands on a synthetic plot
(the mound, the room, the doorway, the TNT and plate, the chests, the creeper or its marker) in every instance; the
grader on it looted cleanly, looted with the plate taken up, blown, a chest broken, and never entered. No server."""
import re

import pytest

from mcmsbench.arena import Arena
from mcmsbench.config import ArenaConfig, load as load_settings
from mcmsbench.graders import Context, Frame, grade
from mcmsbench.runner import build_goal
from mcmsbench.tasks import load
from mcmsbench.world.volume import diff

from test_variants import KEY

TASK = load("desert_ruin_chest")
PLOT = Arena(ArenaConfig(), lambda cmd: "").plot(0)
SX, SY, SZ = PLOT.center()
START = (SX, SY, SZ + 10)
FMT = lambda text: TASK._fmt(text, PLOT, START)        # noqa: E731
CORNERS = {"ne": (SX + 3, SY + 2, SZ - 3), "nw": (SX - 3, SY + 2, SZ - 3), "se": (SX + 3, SY + 2, SZ + 3), "sw": (SX - 3, SY + 2, SZ + 3)}


def _all():
    return [TASK.instance("varied", i) for i in range(12)] + [TASK.instance("heldout", i, KEY) for i in range(12)] + [TASK]


def _apply(cmds) -> dict:
    w: dict = {}
    for c in cmds:
        parts = c.split()
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


def _chests(cmds) -> dict[tuple, dict[str, int]]:
    out = {}
    for c in cmds:
        if "minecraft:chest" in c:
            x, y, z = (int(v) for v in c.split()[1:4])
            out[(x, y, z)] = {i: int(n) for i, n in re.findall(r'id:\\?"minecraft:([a-z_]+)\\?",count:(\d+)', c)}
    return out


# ------------------------------------------------------------------ the task

def test_the_plot_is_easy_so_the_creeper_stays_and_the_bot_starts_south_of_the_mound():
    assert TASK.world.type == "flat" and TASK.world.difficulty == "easy"
    assert TASK.start.offset == [0, 10] and TASK.max_seconds <= 600
    assert "custom:open_chest" in TASK.stats


@pytest.mark.parametrize("inst", _all(), ids=lambda t: f"{t.split}-{t.param_values['loot']}-{t.param_values['creeper']}")
def test_every_instance_has_the_emeralds_in_the_named_corner_and_the_trap_in_the_volume(inst):
    cmds = inst.render_setup(PLOT, START, "player")
    assert not any(re.search(r"\{[a-z]{2}([+-]\d+)?\}|\$\{", c) for c in cmds), "an unrendered placeholder"
    for c in cmds:
        if c.startswith(("fill ", "setblock ")):
            v = [int(n) for n in c.split()[1:7 if c.startswith("fill") else 4]]
            for p in (v[:3], v[3:] or v[:3]):
                assert PLOT.volume.contains(tuple(p)), c
    room = _apply(cmds)
    assert sum(b == "tnt" for b in room.values()) == 9 and room[(SX, SY + 2, SZ)] == "stone_pressure_plate"
    assert room[(SX, SY + 1, SZ)] == "tnt"                                  # the plate sits on the middle one
    assert all(room.get((SX, y, SZ + 4)) is None for y in (SY + 2, SY + 3))  # the doorway
    assert room.get((SX - 2, SY + 2, SZ - 2)) is None                        # the creeper's corner is air
    chests = _chests(cmds)
    loot = inst.param_values["loot"]
    assert set(chests) == set(CORNERS.values())
    assert chests[CORNERS[loot]]["emerald"] == 4 and sum("emerald" in c for c in chests.values()) == 1
    summon = next(c for c in cmds if c.startswith("summon"))
    assert ("minecraft:creeper" in summon) == bool(inst.param_values["creeper"])
    assert cmds[-1] == f"tp player {SX} {SY} {SZ + 10}"


def test_the_goal_file_says_what_the_trap_check_needs():
    goal = build_goal(TASK, PLOT, START, load_settings(), 0, seed=None, fast_nights=False, grader_stops=True,
                      max_seconds=420, max_cost=5)
    parts = {u["name"]: u for u in goal["check"]["uncovered"]}
    assert "TNT" in parts["no_blast"]["says"] and parts["no_blast"]["check"] == {"kind": "intact", "of": "setup", "exclude": ["stone_pressure_plate"]}
    assert {p["name"]: p["check"] for p in goal["check"]["parts"]}["emeralds"] == {"kind": "inventory", "item": "emerald", "count": 4}


# ------------------------------------------------------------------ the grader on the synthetic chamber

ROOM = _apply(TASK.render_setup(PLOT, START, "player"))
TNT = [p for p, b in ROOM.items() if b == "tnt"]
CHESTS = list(CORNERS.values())


def _ctx(after, *, pos=(SX, SY, SZ + 10), track=(), inv=(), deaths=0, stats=None, broke=()):
    stats = {"deaths": deaths, "damage_taken": 0, "custom:open_chest": 2, "killed:creeper": 0, **(stats or {})}
    inventory = [{"name": n, "count": c} for n, c in inv]
    frames = [Frame("step_01", after, pos, inventory, t=200.0, stats=stats)]
    return Context(ROOM, after, diff(ROOM, after), PLOT.volume, PLOT.floor_y, pos, inventory, frames, stats, 20.0,
                   6000, (SX, SY, SZ), fmt=FMT, seconds=300.0, setup=dict(ROOM), start=START, broke=list(broke),
                   track=[(t, *p) for t, p in enumerate(track)])


INSIDE = [(SX, SY + 2, SZ + 6), (SX, SY + 2, SZ + 2), (SX + 2, SY + 2, SZ - 2), (SX, SY + 2, SZ + 6)]


def test_the_emeralds_carried_out_with_the_trap_whole_passes():
    r = grade(TASK.grader, _ctx(dict(ROOM), track=INSIDE, inv=[("emerald", 4), ("bone", 4)]))
    assert r.passed and r.score == 1.0, r.detail["steps"]


def test_the_plate_taken_up_first_is_disarming_not_springing():
    after = dict(ROOM)
    del after[(SX, SY + 2, SZ)]
    r = grade(TASK.grader, _ctx(after, track=INSIDE, inv=[("emerald", 4)], broke=[(40.0, SX, SY + 2, SZ, "stone_pressure_plate", "dig")]))
    assert r.passed and r.checks["no_blast"]


def test_the_trap_sprung_fails_whatever_was_carried_out():
    after = dict(ROOM)
    for p in TNT + CHESTS + [(SX, SY + 2, SZ)]:
        after.pop(p, None)
    for x in range(SX - 2, SX + 3):                   # the floor round the blast too
        for z in range(SZ - 2, SZ + 3):
            after.pop((x, SY + 1, z), None)
    r = grade(TASK.grader, _ctx(after, track=INSIDE, inv=[("emerald", 4)]))
    assert not r.passed and not r.checks["no_blast"] and r.checks["emeralds"]
    assert r.detail["steps"]["no_blast"]["lost_by_block"]["tnt"] == 9


def test_a_chest_broken_to_get_at_it_costs_the_trap_check():
    after = dict(ROOM)
    del after[CORNERS["ne"]]
    r = grade(TASK.grader, _ctx(after, track=INSIDE, inv=[("emerald", 4)], broke=[(50.0, *CORNERS["ne"], "chest", "dig")]))
    assert not r.passed and not r.checks["no_blast"]


def test_one_tnt_mined_for_keeps_is_not_leaving_it():
    after = dict(ROOM)
    del after[TNT[0]]
    assert not grade(TASK.grader, _ctx(after, track=INSIDE, inv=[("emerald", 4), ("tnt", 1)])).passed


def test_never_going_in_or_coming_out_short_fails():
    r = grade(TASK.grader, _ctx(dict(ROOM), stats={"custom:open_chest": 0}))
    assert not r.passed and not r.checks["in_the_room"] and not r.checks["emeralds"] and r.checks["no_blast"]
    r = grade(TASK.grader, _ctx(dict(ROOM), track=INSIDE, inv=[("emerald", 3)]))
    assert not r.passed and r.checks["in_the_room"] and not r.checks["emeralds"]


def test_a_death_fails():
    assert not grade(TASK.grader, _ctx(dict(ROOM), track=INSIDE, inv=[("emerald", 4)], deaths=1)).passed
