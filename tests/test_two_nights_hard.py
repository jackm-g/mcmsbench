"""two_nights_hard: home far from a moved spawn, the dawn clock (counting dawns through skipped and natural nights), the protocol's dawn frames and
its stop at the task's last dawn, the server reads it adds (armour worn, chests), and the grader on a synthetic tundra
house: built by the first dawn, a door that can be walked through, its own blocks left standing. No server needed."""
import sys
import threading
import time
from pathlib import Path

import pytest

import mcmsbench.protocol as P
from mcmsbench.arena import Plot, parse_equipment
from mcmsbench.config import load as load_settings
from mcmsbench.graders import Context, Frame, grade
from mcmsbench.protocol import Agent, load_manifest
from mcmsbench.runner import DayClock, build_goal, needs_containers, needs_day_clock, spawn_protection, stops_on_pass
from mcmsbench.start import resolve_start
from mcmsbench.tasks import load
from mcmsbench.world.volume import Volume, diff

SX, SY, SZ = 154, 70, 163          # seed 1's spawn (infra/worlds/1.json)
PLOT = Plot(0, (SX, SY - 1, SZ), Volume((SX - 32, SY - 12, SZ - 32), (SX + 32, SY + 28, SZ + 32)), flat=False)
TASK = load("two_nights_hard")
SITE = (SX + 20, SZ - 10)          # inside the plot round the old spawn
DAWN_1, DAWN_2 = 610.0, 1250.0     # seconds since the start
IRON = {"chest": {"name": "iron_chestplate", "count": 1}, "legs": {"name": "iron_leggings", "count": 1}}


# ------------------------------------------------------------------ the task

def test_the_task_runs_on_prod_terms_to_the_second_dawn():
    assert TASK.profile == "prod" and TASK.world.difficulty == "hard" and not TASK.world.keep_inventory
    assert TASK.world.spawn_protection == 16 and TASK.world.daylight and TASK.world.radius == 32
    assert TASK.end_at_dawn == 2 and TASK.spawn_first is False and TASK.inventory == {}
    assert needs_day_clock(TASK) and needs_containers(TASK.grader)
    assert not stops_on_pass(TASK)                       # a pass on day 1 is not the end of it
    # two days and nights fit in the clock even when the nights run their full ten minutes
    assert TASK.max_seconds >= 2 * 1200 + 120


def _start():
    return resolve_start(TASK.start, PLOT.center(), TASK.id, 0, 1, None)


def test_home_is_128_blocks_or_more_from_the_spawn_the_bot_starts_on():
    bx, _, bz = start = _start()
    assert TASK.start.world_spawn and (bx, bz) == (SX + 160, SZ)          # east: the only land 160 out (the probe)
    (x0, _, z0), (x1, _, z1) = PLOT.volume.min, PLOT.volume.max
    nearest = min(((x - bx) ** 2 + (z - bz) ** 2) ** 0.5 for x in range(x0, x1 + 1) for z in range(z0, z1 + 1))
    assert nearest >= 128
    # the server's spawn protection is round the start, not round the plot
    assert spawn_protection(TASK, PLOT, start) == {"x": bx, "z": bz, "radius": 16}
    assert spawn_protection(TASK, PLOT) == {"x": SX, "z": SZ, "radius": 16}     # no start: the plot's centre, as before
    # the border, round the site, has room past the start; the repaint and the livestock cull reach the border
    radius = TASK.world.border / 2
    assert radius - (bx - SX) >= 48 and TASK.world.biome_radius >= radius
    assert "128 blocks" in TASK.render(PLOT, start) and f"({SX}, {SZ}), 160 blocks west" in TASK.render(PLOT, start)


def test_the_goal_file_puts_spawn_protection_on_the_start():
    goal = build_goal(TASK, PLOT, _start(), load_settings(), 0, seed=1, fast_nights=True, grader_stops=False,
                      max_seconds=2700, max_cost=6)
    assert goal["profile"]["spawnProtection"] == {"x": SX + 160, "z": SZ, "radius": 16}
    assert goal["profile"]["border"]["x"] == SX and goal["plot"]["center"] == [SX, SY, SZ]


def test_the_herds_are_far_out_and_the_clock_is_reset_last():
    bx, _, bz = _start()
    cmds = TASK.render_setup(PLOT, (bx, SY, bz))
    assert sum(c.startswith("summon minecraft:sheep") for c in cmds) >= 3          # a bed is three wool
    for c in (c for c in cmds if c.startswith("spreadplayers")):
        x, z = (int(v) for v in c.split()[1:3])
        assert ((x - SX) ** 2 + (z - SZ) ** 2) ** 0.5 >= 60, c                       # none within the site's reach
        assert ((x - bx) ** 2 + (z - bz) ** 2) ** 0.5 >= 60, c                       # nor at the start
    assert cmds[-1] == "time set 1000"
    assert not any("{" in c.replace("{Color", "").replace("{Tags", "") for c in cmds), "an unrendered placeholder"


def test_the_goal_file_says_when_the_trial_ends():
    s = load_settings()
    goal = build_goal(TASK, PLOT, (SX, SY, SZ), s, 0, seed=1, fast_nights=True, grader_stops=stops_on_pass(TASK),
                      max_seconds=2700, max_cost=6)
    assert goal["budget"]["ends"] == {"at_dawn": 2} and goal["profile"]["graderStops"] is False
    uncovered = {u["name"] for u in goal["check"]["uncovered"]}
    assert {"armoured", "food", "reached_day3"} <= uncovered
    other = build_goal(load("survive_night"), PLOT, (SX, SY, SZ), s, 0, seed=1, fast_nights=True, grader_stops=True,
                       max_seconds=600, max_cost=5)
    assert "ends" not in other["budget"]                 # additive: only a task that ends at a dawn says so


# ------------------------------------------------------------------ the day clock

def test_a_skipped_night_and_a_natural_one_are_each_a_dawn():
    c = DayClock()
    assert [c.observe(t, at=i) for i, t in enumerate([1000, 6000, 12600, 14000, 1000])] == [False] * 4 + [True]
    assert [c.observe(t) for t in [5000, 12900, 18000, 22950, 23500, 40, 600]] == [False] * 5 + [True, False]
    assert len(c.dawns) == 2 and c.day == 3 and c.dawns[0] == 4


def test_a_night_slept_through_is_a_dawn():
    c = DayClock()                          # a bed from 12542: the clock never reads 13000 before the morning
    assert [c.observe(t) for t in [11658, 12217, 12538, 12658, 56, 400]] == [False] * 4 + [True, False]
    assert c.day == 2


def test_a_jump_back_by_day_and_unread_times_are_not_dawns():
    c = DayClock()
    assert not any(c.observe(t) for t in [5000, 1000, None, 11000, 12500, 23200, 11999 + 1])
    assert c.day == 1


def test_a_poll_reads_the_server_clock():
    times = iter([1000, 14000, 1000])
    c = DayClock(lambda: next(times), t0=time.time())
    assert [c.poll(), c.poll(), c.poll()] == [0, 0, 1]


# ------------------------------------------------------------------ server reads

def test_equipment_is_read_slot_by_slot_past_components():
    out = ('player has the following entity data: {mainhand: {count: 1, id: "minecraft:stone_sword"}, chest: '
           '{components: {"minecraft:damage": 5, "minecraft:trim": {id: "minecraft:x", pattern: "y"}}, count: 1, '
           'id: "minecraft:iron_chestplate"}, legs: {count: 1, id: "minecraft:iron_leggings"}}')
    eq = parse_equipment(out)
    assert eq == {"mainhand": {"name": "stone_sword", "count": 1}, "chest": {"name": "iron_chestplate", "count": 1},
                  "legs": {"name": "iron_leggings", "count": 1}}
    assert parse_equipment("player has the following entity data: {}") == {}


# ------------------------------------------------------------------ the grader

def _ground():
    g = {}
    for x in range(SITE[0] - 8, SITE[0] + 14):
        for z in range(SITE[1] - 8, SITE[1] + 14):
            g[(x, SY - 1, z)] = "grass_block"
            g[(x, SY, z)] = "snow"
    return g


def _house(world, w=6, d=6):
    x0, z0 = SITE
    for x in range(x0, x0 + w):
        for z in range(z0, z0 + d):
            if x in (x0, x0 + w - 1) or z in (z0, z0 + d - 1):
                for y in (SY, SY + 1):
                    world[(x, y, z)] = "cobblestone"
            else:
                world.pop((x, SY, z), None)
            world[(x, SY + 2, z)] = "spruce_planks"
    for y in (SY, SY + 1):
        world[(x0 + 2, y, z0)] = "spruce_door"          # the north wall's door
    world[(x0 + 2, SY, z0 + 2)] = world[(x0 + 2, SY, z0 + 3)] = "white_bed"
    return world


BED = {"pos": [SITE[0] + 2, SY, SITE[1] + 2], "bed": True}
CHEST = (SITE[0] + 4, SY, SITE[1] + 4)


def _frame(label, world, t, **kw):
    return Frame(label, world, (SITE[0] + 3, SY, SITE[1] + 3), kw.pop("inventory", []), t=t, stats={"deaths": 0},
                 food=20, **kw)


def _ctx(after, frames, *, inventory=None, equipment=IRON, containers=None, day=3, respawn=BED, deaths=0, broke=()):
    before = _ground()
    inv = inventory if inventory is not None else [{"name": "iron_pickaxe", "count": 1}, {"name": "cooked_beef", "count": 3}]
    return Context(before, after, diff(before, after), PLOT.volume, None, (SITE[0] + 3, SY, SITE[1] + 3), inv, frames,
                   {"deaths": deaths}, 20.0, 1000, (SX, SY, SZ), respawn=respawn, seconds=DAWN_2 + 20,
                   broke=list(broke), food=20, equipment=equipment, containers=containers, day=day)


def _day_two_world():
    """A house standing at the first dawn, and the world at the second."""
    built = _house(_ground())
    frames = [_frame("t0300", _ground(), 300.0), _frame("dawn_1", dict(built), DAWN_1),
              _frame("dawn_2", dict(built), DAWN_2, equipment=IRON)]
    return dict(built), frames


def test_two_nights_with_everything_in_place_pass():
    after, frames = _day_two_world()
    r = grade(TASK.grader, _ctx(after, frames))
    assert r.passed, r.detail["steps"]
    assert all(r.checks[k] for k in ("shelter", "door", "bed_inside", "home_spawn", "iron_pickaxe", "armoured", "food",
                                     "house_intact", "alive", "reached_day3"))
    assert r.detail["dawns"] == {1: DAWN_1, 2: DAWN_2}


def test_a_house_finished_after_the_first_dawn_is_late():
    after, frames = _day_two_world()
    frames[1] = _frame("dawn_1", _ground(), DAWN_1)                    # nothing up yet at dawn
    frames.insert(2, _frame("t0700", dict(after), 700.0))
    r = grade(TASK.grader, _ctx(after, frames))
    assert not r.checks["shelter"] and not r.checks["bed_inside"] and not r.passed
    assert r.detail["reached_at_seconds"]["shelter"] == 700.0


def test_armour_carried_is_not_armour_worn():
    after, frames = _day_two_world()
    carried = [{"name": "iron_pickaxe", "count": 1}, {"name": "cooked_beef", "count": 3},
               {"name": "iron_chestplate", "count": 1}, {"name": "iron_leggings", "count": 1}]
    r = grade(TASK.grader, _ctx(after, frames, inventory=carried, equipment={}))
    assert not r.checks["armoured"] and not r.passed
    half = {"chest": IRON["chest"], "legs": {"name": "leather_leggings", "count": 1}}
    assert not grade(TASK.grader, _ctx(after, frames, equipment=half)).checks["armoured"]


def test_food_in_its_chest_counts_and_not_without_in_containers():
    after, frames = _day_two_world()
    after[CHEST] = "chest"
    stored = {CHEST: [{"name": "bread", "count": 4}]}                    # 20 points
    r = grade(TASK.grader, _ctx(after, frames, inventory=[{"name": "iron_pickaxe", "count": 1}], containers=stored))
    assert r.checks["food"] and r.passed
    assert not grade({"kind": "food_stock", "min_points": 20}, _ctx(after, frames, inventory=[], containers=stored)).passed
    assert not grade(TASK.grader, _ctx(after, frames, inventory=[{"name": "iron_pickaxe", "count": 1}])).checks["food"]


def test_a_trial_that_ends_before_the_last_dawn_fails():
    after, frames = _day_two_world()
    r = grade(TASK.grader, _ctx(after, frames[:2], day=2))
    assert not r.checks["reached_day3"] and not r.passed
    assert not grade(TASK.grader, _ctx(after, frames, day=None)).passed          # no clock ran: never reached


def test_two_deaths_fail_one_does_not():
    after, frames = _day_two_world()
    assert grade(TASK.grader, _ctx(after, frames, deaths=1)).passed
    assert not grade(TASK.grader, _ctx(after, frames, deaths=2)).passed


def test_a_door_blocked_from_outside_does_not_count():
    after, frames = _day_two_world()
    x0, z0 = SITE
    for f in frames[1:]:
        f.snapshot[(x0 + 2, SY, z0 - 1)] = f.snapshot[(x0 + 2, SY + 1, z0 - 1)] = "dirt"
    after[(x0 + 2, SY, z0 - 1)] = after[(x0 + 2, SY + 1, z0 - 1)] = "dirt"
    r = grade(TASK.grader, _ctx(after, frames))
    assert r.checks["shelter"] and not r.checks["door"]


def test_its_house_mined_after_the_first_dawn_is_not_intact_even_patched():
    after, frames = _day_two_world()
    x0, z0 = SITE
    wall = [(x0, SY + 1, z0 + k) for k in range(1, 5)]                 # four blocks of the west wall, put back
    early = [(400.0, x0 + 5, SY, z0 + 1, "cobblestone", None)]           # broken while building: before the dawn
    r = grade(TASK.grader, _ctx(after, frames, broke=early + [(900.0, *p, "cobblestone", None) for p in wall]))
    assert not r.checks["house_intact"] and r.passed                    # not required: the score pays for it
    d = r.detail["steps"]["house_intact"]
    assert d["lost"] == 4 and d["allowed"] == 3
    ok = grade(TASK.grader, _ctx(after, frames, broke=early + [(900.0, *p, "cobblestone", None) for p in wall[:3]]))
    assert ok.checks["house_intact"] and ok.score > r.score


def test_a_check_read_at_the_end_takes_no_dawn_deadline():
    spec = {"kind": "milestones", "steps": [{"name": "s", "at_dawn": 1, "check": {"kind": "respawn"}}]}
    after, frames = _day_two_world()
    with pytest.raises(ValueError, match="at_dawn"):
        grade(spec, _ctx(after, frames))


# ------------------------------------------------------------------ the protocol: dawn frames and the stop

FAKE = Path(__file__).with_name("fake_agent.py")


def _agent(tmp_path):
    p = tmp_path / "fake.toml"
    p.write_text(f'command = ["{sys.executable}", "{FAKE}"]\nprefix = "@fake"\ncwd = "{tmp_path}"\n')
    return Agent(load_manifest(str(p)))


def _context(tmp_path, **kw):
    seen = {"frames": [], "progress": []}
    goal = {"protocol": 1, "task_id": "t", "prompt": "live through it", "check": None}
    ctx = P.TrialContext("t", goal, "127.0.0.1", 25565, "player", "26.1", 30, 0.5, tmp_path / "run" / "trial_0",
                         hand_off=lambda: None, take_back=lambda: None, progress=seen["progress"].append,
                         after_step=seen["frames"].append, **kw)
    return ctx, seen


def test_the_last_dawn_stops_the_agent_after_its_frame(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "hang")
    monkeypatch.setattr(P, "DAWN_POLL_EVERY", 0.2)
    counts = iter([0, 1, 1, 2] + [2] * 100)
    ctx, seen = _context(tmp_path, dawns=lambda: next(counts), end_at_dawn=2)
    t = time.time()
    tr = _agent(tmp_path).run(ctx)
    assert time.time() - t < 10
    assert tr["stopped_by"] == "dawn" and tr["stop"] == "dawn_reached"
    assert [f for f in seen["frames"] if f.startswith("dawn")] == ["dawn_1", "dawn_2"]


def test_dawns_without_an_end_are_framed_and_the_agent_plays_on(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "hang")
    monkeypatch.setattr(P, "DAWN_POLL_EVERY", 0.2)
    counts = iter([0, 2] + [2] * 100)                 # two dawns between reads: both framed, in order
    stop = threading.Event()
    ctx, seen = _context(tmp_path, dawns=lambda: next(counts), stop=stop)
    threading.Timer(2.5, stop.set).start()
    tr = _agent(tmp_path).run(ctx)
    assert "stopped_by" not in tr and tr["stop"] == "terminated"
    assert [f for f in seen["frames"] if f.startswith("dawn")] == ["dawn_1", "dawn_2"]
