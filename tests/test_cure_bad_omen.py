"""cure_bad_omen: an effect cleared with milk. The task's terms (the effect given after the runner's clears, the
gated event that watches for it going, the chest's bucket or iron, the prompt naming both), and the grader on the
bench's record of the event firing or not, with the counters for the bucket, the milk and the cow. No server."""
import re

import pytest

from mcmsbench.arena import Arena
from mcmsbench.config import ArenaConfig, load as load_settings
from mcmsbench.graders import Context, Frame, grade
from mcmsbench.runner import build_goal
from mcmsbench.tasks import load
from mcmsbench.world.volume import diff

from test_variants import KEY

TASK = load("cure_bad_omen")
PLOT = Arena(ArenaConfig(), lambda cmd: "").plot(0)
START = PLOT.center()
SX, SY, SZ = START
FMT = lambda text: TASK._fmt(text, PLOT, START)        # noqa: E731


def _all():
    return [TASK.instance("varied", i) for i in range(12)] + [TASK.instance("heldout", i, KEY) for i in range(12)] + [TASK]


# ------------------------------------------------------------------ the task

def test_the_effect_is_given_in_setup_and_watched_by_a_gated_event():
    cmds = TASK.render_setup(PLOT, START, "player")
    assert cmds[0] == "effect give player minecraft:bad_omen 6000 0 true"
    ev = TASK.render_events(PLOT, START, "player")
    assert len(ev) == 1
    at, run, when = ev[0]
    assert at == 0 and run == "tag player add cured"
    assert when.startswith("execute as @a[name=player] unless entity @s[nbt=")    # fails while nobody is on
    assert TASK.max_seconds <= 300 and not TASK.movements["can_dig"]


@pytest.mark.parametrize("inst", _all(), ids=lambda t: f"{t.split}-{t.param_values['effect']['id']}-{t.param_values['bucket']['item']}")
def test_every_instance_names_its_effect_and_its_bucket(inst):
    cmds = inst.render_setup(PLOT, START, "player")
    assert not any(re.search(r"\{[a-z]{2}([+-]\d+)?\}|\$\{", c) for c in cmds), "an unrendered placeholder"
    eff, bucket = inst.param_values["effect"], inst.param_values["bucket"]
    assert f"minecraft:{eff['id']} 6000" in cmds[0]
    chest = next(c for c in cmds if "minecraft:chest" in c)
    assert f'id:"minecraft:{bucket["item"]}",count:{bucket["n"]}' in chest
    assert any("minecraft:crafting_table" in c for c in cmds) and any("summon minecraft:cow" in c for c in cmds)
    prompt = " ".join(inst.render(PLOT, START).split())
    assert eff["words"] in prompt and bucket["words"] in prompt and "cow" in prompt
    assert f'minecraft:{eff["id"]}' in inst.render_events(PLOT, START, "player")[0][2]


def test_the_goal_file_says_the_bench_watches_for_the_cure():
    goal = build_goal(TASK, PLOT, START, load_settings(), 0, seed=None, fast_nights=False, grader_stops=True,
                      max_seconds=300, max_cost=5)
    parts = {u["name"]: u for u in goal["check"]["uncovered"]}
    assert parts["cleared"]["kind"] == "event" and "effect gone" in parts["cleared"]["says"]


# ------------------------------------------------------------------ the grader on the bench's record

def _ctx(*, cured=True, bucket=1, milk=1, cow_killed=0, deaths=0):
    world = {}
    stats = {"deaths": deaths, "damage_taken": 0, "used:bucket": bucket, "used:milk_bucket": milk, "killed:cow": cow_killed}
    frames = [Frame("step_01", world, START, [], t=100.0, stats=stats)]
    events = [{"at": 0, "t": 80.0, "run": "tag player add cured", "out": "Added tag"}] if cured else []
    return Context(world, world, diff(world, world), PLOT.volume, PLOT.floor_y, START, [], frames, stats, 20.0, 6000,
                   START, fmt=lambda t: FMT(t).replace("{bot}", "player"), seconds=150.0, start=START, events=events)


def test_milk_drunk_and_the_effect_gone_passes():
    r = grade(TASK.grader, _ctx())
    assert r.passed and r.score == 1.0, r.detail["steps"]


def test_the_effect_still_on_fails_even_with_milk_drunk():
    r = grade(TASK.grader, _ctx(cured=False))
    assert not r.passed and not r.checks["cleared"] and r.checks["drank"]


def test_the_cow_killed_costs_and_nothing_done_fails():
    r = grade(TASK.grader, _ctx(cow_killed=1))
    assert r.passed and not r.checks["cow_alive"] and r.score < 1.0
    r = grade(TASK.grader, _ctx(cured=False, bucket=0, milk=0))
    assert not r.passed and not any(r.checks[k] for k in ("milked", "drank", "cleared"))


def test_a_death_fails():
    assert not grade(TASK.grader, _ctx(deaths=1)).passed
