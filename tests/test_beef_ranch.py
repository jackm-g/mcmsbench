"""beef_ranch: two loose cows lured into an empty pen with wheat from the farm, bred up, culled and cooked. The task's
terms (peaceful, the day frozen, the kit, the coal), the base played onto a synthetic world (the pen's ring whole but
for its one gate, shut; the farm watered), why breeding is forced, the grader against a fake server that counts cows
by their positions (the `herd` grader `within` the pen, `block_state` on the gate), and the goal file. No server
needed."""
import re

import pytest

from mcmsbench.arena import Plot
from mcmsbench.config import load as load_settings
from mcmsbench.graders import Context, Frame, grade
from mcmsbench.runner import build_goal, stops_on_pass
from mcmsbench.tasks import load
from mcmsbench.world.volume import Volume, diff

SX, SY, SZ = 154, 70, 163          # seed 1's spawn (infra/worlds/1.json)
PLOT = Plot(0, (SX, SY - 1, SZ), Volume((SX - 40, SY - 12, SZ - 40), (SX + 40, SY + 28, SZ + 40)), flat=False)
TASK = load("beef_ranch")
START = (SX, SY, SZ)
FMT = lambda text: TASK._fmt(text, PLOT, START)        # noqa: E731
GATE = (SX - 8, SY, SZ + 2)
INSIDE = [(x, z) for x in range(SX - 11, SX - 4) for z in range(SZ + 3, SZ + 10)]    # the pen's 7x7


def _setup():
    return TASK.render_setup(PLOT, START, "player")


def _name(token: str) -> str:
    return re.sub(r"[\[{].*$", "", token).removeprefix("minecraft:")


def _world() -> dict:
    """The base as setup leaves it, on a grass plane: the land fills (the whole border) left out, the rest played."""
    w = {(x, SY - 1, z): "grass_block" for x in range(SX - 20, SX + 30) for z in range(SZ - 20, SZ + 30)}
    for c in _setup():
        p = c.split()
        if p[0] == "setblock":
            w[tuple(int(v) for v in p[1:4])] = _name(p[4])
        elif p[0] == "fill":
            v = [int(t) for t in p[1:7]]
            if (abs(v[3] - v[0]) + 1) * (abs(v[5] - v[2]) + 1) > 2000:
                continue                                  # the land: what the plane above already is
            for x in range(min(v[0], v[3]), max(v[0], v[3]) + 1):
                for y in range(min(v[1], v[4]), max(v[1], v[4]) + 1):
                    for z in range(min(v[2], v[5]), max(v[2], v[5]) + 1):
                        w[(x, y, z)] = _name(p[7])
    return w


BASE = _world()


# ------------------------------------------------------------------ the task

def test_peaceful_frozen_day_and_the_kit():
    w = TASK.world
    assert w.type == "survival" and w.difficulty == "peaceful" and not w.daylight and not w.passive_mobs
    assert w.random_tick_speed and w.random_tick_speed > 3
    assert TASK.inventory == {"stone_sword": 1}
    assert TASK.herd_watch == {"types": ["cow"], "grow": 4, "strays": True}
    assert not TASK.scripted and TASK.max_seconds >= 1800
    assert not stops_on_pass(TASK)                 # the hydrated grader acts on the server


def test_setup_renders_and_stays_in_the_border():
    cmds = _setup()
    assert not [c for c in cmds if re.search(r"\{(sx|sy|sz|bx|by|bz|bot)\b", c)]
    for c in cmds:
        if c.startswith("fill "):
            v = [int(t) for t in c.split()[1:7]]
            assert SX - 48 <= min(v[0], v[3]) and max(v[0], v[3]) <= SX + 47, c
            assert SZ - 48 <= min(v[2], v[5]) and max(v[2], v[5]) <= SZ + 47, c
    chest = next(c for c in cmds if "minecraft:chest" in c)
    assert 'id:"minecraft:coal",count:8' in chest
    assert BASE[(SX + 3, SY, SZ - 2)] == "furnace" and BASE[(SX + 2, SY, SZ - 2)] == "crafting_table"


def test_two_cows_loose_and_far_from_the_pen():
    cows = [tuple(int(v) for v in c.split()[2:5]) for c in _setup() if c.startswith("summon minecraft:cow ")]
    assert len(cows) == 2 and not [c for c in _setup() if c.startswith("summon ") and "minecraft:cow" not in c]
    for x, _, z in cows:
        assert (x, z) not in INSIDE
        assert ((x - GATE[0]) ** 2 + (z - GATE[2]) ** 2) ** 0.5 > 20
    assert all("PersistenceRequired:1b" in c for c in _setup() if c.startswith("summon "))


def test_the_pen_is_a_fence_ring_with_one_gate_shut():
    ring = [(x, z) for x in range(SX - 12, SX - 3) for z in (SZ + 2, SZ + 10)] + \
           [(x, z) for x in (SX - 12, SX - 4) for z in range(SZ + 3, SZ + 10)]
    blocks = [BASE[(x, SY, z)] for x, z in ring]
    assert blocks.count("oak_fence_gate") == 1 and blocks.count("oak_fence") == len(ring) - 1
    assert BASE[GATE] == "oak_fence_gate"
    assert "minecraft:oak_fence_gate[facing=north,open=false]" in next(c for c in _setup() if c.startswith(f"setblock {SX - 8} "))
    assert all((x, SY, z) not in BASE for x, z in INSIDE)               # an empty pen, open ground inside
    penned = next(m for m in TASK.grader["steps"] if m["name"] == "penned")["check"]
    v = [int(n) for n in FMT(penned["within"]).split()]
    assert (v[0], v[2], v[3], v[5]) == (SX - 11, SZ + 3, SX - 5, SZ + 9)


def test_the_farm_is_watered_and_fenced():
    wheat = [p for p, b in BASE.items() if b == "wheat"]
    water = [p for p, b in BASE.items() if b == "water"]
    assert len(wheat) == 36 and water
    for x, y, z in wheat:
        assert BASE[(x, y - 1, z)] == "farmland"
        assert min(max(abs(x - wx), abs(z - wz)) for wx, _, wz in water) <= 4
    replanted = next(m for m in TASK.grader["steps"] if m["name"] == "replanted")["check"]
    assert len(wheat) > replanted["min"] > 18                # harvesting the ripe 18 without replanting fails it


def test_breeding_is_forced():
    beef = next(m for m in TASK.grader["steps"] if m["name"] == "beef")["check"]["count"]
    keep = next(m for m in TASK.grader["steps"] if m["name"] == "penned")["check"]["min_each"]
    cows = sum(1 for c in _setup() if c.startswith("summon minecraft:cow "))
    culled_most = max(0, cows - keep)                  # cows that can be killed and still leave the pen's number
    assert culled_most * 3 < beef                      # 3 beef a cow at most, no looting


# ------------------------------------------------------------------ the grader against a fake server

class Server:
    """Cows at positions, counted by `execute if entity @e[type=minecraft:cow,x=,y=,z=,dx=,dy=,dz=]` as the server
    does (the box is x..x+dx+1); the hydrated grader's ticks and farmland all answered wet."""

    def __init__(self, cows):
        self.cows, self.sent = list(cows), []

    def __call__(self, cmd):
        self.sent.append(cmd)
        if cmd.startswith("gamerule"):
            return "ok"
        if "farmland[moisture=7]" in cmd:
            return "Test passed"
        m = re.search(r"type=minecraft:cow,x=(-?\d+),y=(-?\d+),z=(-?\d+),dx=(\d+),dy=(\d+),dz=(\d+)", cmd)
        if m:
            x, y, z, dx, dy, dz = (int(v) for v in m.groups())
            n = sum(1 for cx, cy, cz in self.cows if x <= cx < x + dx + 1 and y <= cy < y + dy + 1 and z <= cz < z + dz + 1)
            return f"Test passed. Count: {n}" if n else "Test failed"
        return "ok"


def _penned(n):
    return [(SX - 10 + i % 5 + 0.5, SY, SZ + 4 + i // 5 + 0.5) for i in range(n)]


def _ctx(*, cows=None, beef=6, gate_open=False, baby_kills=0, deaths=0, after=None):
    after = dict(BASE) if after is None else after
    inv = [{"name": "cooked_beef", "count": beef, "slot": 0}, {"name": "wheat", "count": 3, "slot": 1}]
    stats = {"deaths": deaths, "mined:wheat": 18, "custom:animals_bred": 4, "killed:cow": 3, "crafted:cooked_beef": 7}
    frames = [Frame("step_01", after, (SX, SY, SZ), inv, t=1200.0, stats=stats)]
    return Context(dict(BASE), after, diff(BASE, after), PLOT.volume, None, (SX, SY, SZ), inv, frames, stats, 20.0, 1000,
                   (SX, SY, SZ), {GATE: {"facing": "north", "open": gate_open, "in_wall": False, "powered": False}},
                   Server(_penned(5) if cows is None else cows), FMT, seconds=1800.0, setup=dict(BASE), start=START,
                   herd={"baby_kills": baby_kills})


@pytest.fixture(autouse=True)
def _no_tick_burst(monkeypatch):
    monkeypatch.setattr("mcmsbench.graders.mechanical.time.sleep", lambda s: None)


def test_a_ranchers_trial_passes():
    r = grade(TASK.grader, _ctx())
    assert r.passed and r.score == 1.0, r.detail["steps"]


def test_cows_bred_but_left_loose_are_not_penned():
    loose = [(SX + 20.5 + i, SY, SZ + 20.5) for i in range(5)]
    r = grade(TASK.grader, _ctx(cows=loose))
    assert not r.passed and not r.checks["penned"] and r.checks["gate_shut"]


def test_the_gate_left_open_fails():
    r = grade(TASK.grader, _ctx(gate_open=True))
    assert not r.passed and not r.checks["gate_shut"] and r.checks["penned"]


def test_a_calf_killed_fails():
    r = grade(TASK.grader, _ctx(baby_kills=1))
    assert not r.passed and not r.checks["penned"]


def test_five_beef_or_three_cows_fail():
    assert not grade(TASK.grader, _ctx(beef=5)).passed
    assert not grade(TASK.grader, _ctx(cows=_penned(3))).passed


def test_a_fence_broken_to_drive_them_in_costs_the_base_not_the_pass():
    after = dict(BASE)
    del after[(SX - 12, SY, SZ + 6)]                   # a hole in the pen's west side
    r = grade(TASK.grader, _ctx(after=after))
    assert r.passed and not r.checks["base_intact"]


def test_the_farm_harvested_and_left_bare_is_not_replanted():
    after = {p: b for p, b in BASE.items() if not (b == "wheat" and p[2] <= SZ - 5)}    # the ripe rows taken
    r = grade(TASK.grader, _ctx(after=after))
    assert not r.checks["replanted"] and r.checks["base_intact"]


# ------------------------------------------------------------------ the new pieces on their own

def test_herd_within_counts_only_the_box():
    ctx = _ctx(cows=_penned(2) + [(SX + 20.5, SY, SZ + 20.5)] * 3)
    whole = grade({"kind": "herd", "types": ["cow"], "min_each": 4}, ctx)
    pen = grade({"kind": "herd", "types": ["cow"], "min_each": 4, "within": "{sx-11} {sy-1} {sz+3} {sx-5} {sy+2} {sz+9}"}, ctx)
    assert whole.passed and whole.detail["counts"] == {"cow": 5}
    assert not pen.passed and pen.detail["counts"] == {"cow": 2}
    # a cow just outside the ring, against the fence, is not inside
    ctx = _ctx(cows=_penned(4) + [(SX - 12.6, SY, SZ + 6.5), (SX - 3.4, SY, SZ + 6.5)])
    assert grade({"kind": "herd", "types": ["cow"], "min_each": 4, "within": "{sx-11} {sy-1} {sz+3} {sx-5} {sy+2} {sz+9}"},
                 ctx).detail["counts"] == {"cow": 4}


def test_block_state_reads_the_observers_states():
    spec = {"kind": "block_state", "at": "{sx-8} {sy} {sz+2}", "block": "*_fence_gate", "state": {"open": False}}
    assert grade(spec, _ctx()).passed
    assert not grade(spec, _ctx(gate_open=True)).passed
    ctx = _ctx()
    ctx.after_states = {GATE: {"open": "false"}}       # a state read as text
    assert grade(spec, ctx).passed
    gone = dict(BASE)
    del gone[GATE]
    r = grade(spec, _ctx(after=gone))
    assert not r.passed and r.checks == {"block": False, "open=false": False}
    walled = dict(BASE)
    walled[GATE] = "oak_fence"                         # the gate swapped for a fence: shut, but not a gate
    assert not grade(spec, _ctx(after=walled)).passed


def test_the_goal_file_says_what_cannot_be_read_live():
    goal = build_goal(TASK, PLOT, START, load_settings(), 0, seed=1, fast_nights=False, grader_stops=False,
                      max_seconds=2400, max_cost=5)
    check = goal["check"]
    assert {p["name"]: p["check"]["kind"] for p in check["parts"]} == {"beef": "inventory", "alive": "alive"}
    says = {u["name"]: u["says"] for u in check["uncovered"]}
    assert "inside the pen" in says["penned"] and "no calf killed" in says["penned"]
    assert "gate" in says["gate_shut"] and "shut" in says["gate_shut"]
    # what each asks, resolved for the trial, for an agent that can read animals and blocks as it goes
    checks = {u["name"]: u["check"] for u in check["uncovered"]}
    assert checks["penned"] == {"kind": "herd", "types": ["cow"], "min_each": 4,
                                "within": [SX - 11, SY - 1, SZ + 3, SX - 5, SY + 2, SZ + 9]}
    assert checks["gate_shut"] == {"kind": "block_state", "at": [SX - 8, SY, SZ + 2], "block": "*_fence_gate",
                                   "state": {"open": False}}
