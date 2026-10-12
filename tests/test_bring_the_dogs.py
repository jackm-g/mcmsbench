"""bring_the_dogs: two tamed wolves walked from the kennel to a hut 120 blocks east, sat inside, the door shut. The
task's terms (the border past the hut in every instance, the lead there when the prompt says so, the owner set from
the bot's UUID), and the grader on a stand-in server that answers its RCON checks from where the dogs are and how
the door stands. No server needed."""
import re

import pytest

from mcmsbench.arena import Plot
from mcmsbench.config import load as load_settings
from mcmsbench.graders import Context, Frame, grade
from mcmsbench.runner import build_goal, stops_on_pass
from mcmsbench.tasks import load
from mcmsbench.world.volume import Volume, diff

from test_variants import KEY

SX, SY, SZ = 154, 70, 163          # seed 1's spawn (infra/worlds/1.json)
PLOT = Plot(0, (SX, SY - 1, SZ), Volume((SX - 24, SY - 12, SZ - 24), (SX + 24, SY + 28, SZ + 24)), flat=False)
START = (SX, SY, SZ)
TASK = load("bring_the_dogs")
FMT = lambda text: TASK._fmt(text, PLOT, START)        # noqa: E731
HUT = (SX + 120, SY, SZ)                               # the marker, in the public instance (the hut's inside round it)


def _all():
    return [TASK.instance("varied", i) for i in range(12)] + [TASK.instance("heldout", i, KEY) for i in range(12)] + [TASK]


# ------------------------------------------------------------------ the task

def test_the_trip_is_by_day_on_seed_1_and_the_grader_reads_the_live_world():
    w = TASK.world
    assert w.type == "survival" and w.seeds == [1] and w.daylight and w.difficulty == "normal"
    assert TASK.start.time == 1000 and TASK.max_seconds <= 600
    assert not stops_on_pass(TASK)                                # functional checks read the server at the end
    assert "killed:wolf" in TASK.stats


@pytest.mark.parametrize("inst", _all(), ids=lambda t: f"{t.split}-{t.param_values['distance']}-{t.param_values['lead']['item']}")
def test_every_instance_reaches_its_hut_and_says_about_the_lead(inst):
    d, lead = inst.param_values["distance"], inst.param_values["lead"]["item"]
    assert inst.world.border >= d + 40
    cmds = inst.render_setup(PLOT, START, "player")
    assert not any(re.search(r"\{[a-z]{2}([+-]\d+)?\}|\$\{", c) for c in cmds), "an unrendered placeholder"
    marker = next(c for c in cmds if "summon minecraft:marker" in c)
    assert f"positioned {SX + d} 0 {SZ}" in marker and "positioned over" in marker
    assert not any(c.startswith("kill @e[tag=outpost") for c in cmds)       # the marker is kept for the grader
    x0, z0, x1, z1 = inst.render_load_area(PLOT, START)
    assert x0 <= SX - 8 and x1 >= SX + d + 4
    assert sum("summon minecraft:wolf" in c for c in cmds) == 2
    assert sum("Owner set from entity @s UUID" in c and c.startswith("execute as player run") for c in cmds) == 2
    lead_cmd = next(c for c in cmds if "container.2 with" in c)
    assert lead_cmd.endswith(f"minecraft:{lead}")
    prompt = " ".join(inst.render(PLOT, START).split())
    assert ("lead" in prompt) == (lead == "lead") and f"about {d} blocks east" in prompt
    reached = next(m["check"] for m in inst.grader["steps"] if m["name"] == "reached")
    assert reached["target_rel"] == [d, 0]
    both = next(m["check"] for m in inst.grader["steps"] if m["name"] == "both_inside")
    assert inst._fmt(both["steps"][0]["do"], PLOT, START) == f"forceload add {SX + d - 8} {SZ - 8} {SX + d + 8} {SZ + 8}"


def test_the_goal_file_says_what_each_check_needs():
    goal = build_goal(TASK, PLOT, START, load_settings(), 0, seed=1, fast_nights=True, grader_stops=False,
                      max_seconds=600, max_cost=5)
    says = {u["name"]: u["says"] for u in goal["check"]["uncovered"]}
    assert "both dogs" in says["both_inside"] and not any("{" in s for s in says.values())


# ------------------------------------------------------------------ a stand-in server

class Server:
    """Where the dogs are and how the door stands, answering the grader's RCON checks. `dogs` is a list of
    (where, sitting): where is 'hut', 'kennel' or 'dead'."""

    def __init__(self, dogs, door_shut=True):
        self.dogs, self.door_shut, self.scores, self.cmds = dogs, door_shut, {}, []

    def __call__(self, cmd):
        self.cmds.append(cmd)
        alive = [d for d in self.dogs if d[0] != "dead"]
        inside = [d for d in alive if d[0] == "hut"]
        if cmd.startswith(("forceload", "scoreboard objectives add")):
            return "ok"
        m = re.match(r"execute store result score (\w+) bench_dogs .*if entity @e\[type=minecraft:wolf,tag=dogs(,dx=2,dy=2,dz=2)?\]$", cmd)
        if m:
            self.scores[m.group(1)] = len(inside) if m.group(2) else len(alive)
            return f"Test passed, count: {self.scores[m.group(1)]}"
        m = re.match(r"execute if score (\w+) bench_dogs matches (\d+)\.\.$", cmd)
        if m:
            return "Test passed" if self.scores.get(m.group(1), 0) >= int(m.group(2)) else "Test failed"
        m = re.match(r"execute at @e\[tag=outpost,limit=1\] positioned ~-1 ~ ~-1 (if|unless) entity @e\[type=minecraft:wolf,tag=dogs,dx=2,dy=2,dz=2,nbt=\{Sitting:(\d)b\}\]$", cmd)
        if m:
            any_ = any(d[1] == (m.group(2) == "1") for d in inside)
            return "Test passed" if any_ == (m.group(1) == "if") else "Test failed"
        if cmd == "execute at @e[tag=outpost,limit=1] if block ~-2 ~ ~ minecraft:oak_door[open=false]":
            return "Test passed" if self.door_shut else "Test failed"
        raise AssertionError(f"the stand-in server does not know {cmd!r}")


def _ctx(server, *, at=HUT, deaths=0, stats=None):
    world = {(x, SY - 1, z): "grass_block" for x in range(SX - 8, SX + 9) for z in range(SZ - 8, SZ + 9)}
    stats = {"deaths": deaths, "damage_taken": 0, "killed:wolf": 0, **(stats or {})}
    frames = [Frame("step_01", world, at, [], t=400.0, stats=stats)]
    track = ([(t, SX + int(t), SY, SZ) for t in range(0, 120, 2)] if at != START else []) + [(130.0, *at)]
    return Context(world, world, diff(world, world), PLOT.volume, None, at, [], frames, stats, 20.0, 7000,
                   (SX, SY, SZ), rcon=server, fmt=FMT, seconds=500.0, setup={}, start=START, track=track)


@pytest.fixture(autouse=True)
def _no_waiting(monkeypatch):
    monkeypatch.setattr("mcmsbench.graders.mechanical.time.sleep", lambda s: None)


def test_both_dogs_sat_in_the_hut_with_the_door_shut_passes():
    s = Server([("hut", True), ("hut", True)])
    r = grade(TASK.grader, _ctx(s))
    assert r.passed and r.score == 1.0, r.detail["steps"]
    assert any(c.startswith("forceload remove") for c in s.cmds)       # the hut's chunks let go again


def test_one_dog_left_sitting_at_the_kennel_fails():
    r = grade(TASK.grader, _ctx(Server([("hut", True), ("kennel", True)])))
    assert not r.passed and r.checks["one_inside"] and not r.checks["both_inside"] and r.checks["none_lost"]


def test_a_dog_lost_on_the_way_fails_twice():
    r = grade(TASK.grader, _ctx(Server([("hut", True), ("dead", True)])))
    assert not r.passed and not r.checks["both_inside"] and not r.checks["none_lost"]


def test_standing_dogs_or_an_open_door_cost_but_pass():
    r = grade(TASK.grader, _ctx(Server([("hut", False), ("hut", True)])))
    assert r.passed and not r.checks["sat_down"] and r.score < 1.0
    r = grade(TASK.grader, _ctx(Server([("hut", True), ("hut", True)], door_shut=False)))
    assert r.passed and not r.checks["door_shut"] and r.checks["sat_down"]


def test_the_bot_never_going_costs_the_walk():
    r = grade(TASK.grader, _ctx(Server([("kennel", True), ("kennel", True)]), at=START))
    assert not r.passed and not r.checks["reached"] and not r.checks["one_inside"]


def test_a_death_or_a_wolf_killed():
    assert not grade(TASK.grader, _ctx(Server([("hut", True), ("hut", True)]), deaths=1)).passed
    r = grade(TASK.grader, _ctx(Server([("hut", True), ("hut", True)]), stats={"killed:wolf": 1}))
    assert r.passed and not r.checks["no_wolf_killed"]
