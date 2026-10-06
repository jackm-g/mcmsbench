"""Scoreboard stats plumbing, start-spec resolution, and the stat/survived/world_time graders."""
import pytest

from mcmsbench.graders import Context, Frame, grade
from mcmsbench.start import SITUATIONS, StartSpec, resolve_start, situation_commands
from mcmsbench.stats import Stats, criterion_for, objective_name
from mcmsbench.world.volume import Volume, diff


def test_criteria_and_objective_names():
    assert criterion_for("deaths") == ("deathCount", 1)
    assert criterion_for("mined:stone") == ("minecraft.mined:minecraft.stone", 1)
    assert criterion_for("killed:zombie") == ("minecraft.killed:minecraft.zombie", 1)
    assert criterion_for("walked")[1] == 100
    with pytest.raises(ValueError):
        criterion_for("nonsense")
    assert objective_name("deaths") == "b_deaths"
    long = objective_name("crafted:diamond_pickaxe")
    assert len(long) <= 16 and long == objective_name("crafted:diamond_pickaxe")
    assert objective_name("mined:stone") != objective_name("mined:sand")


class FakeRcon:
    def __init__(self, scores):
        self.scores = scores; self.cmds = []
    def __call__(self, cmd):
        self.cmds.append(cmd)
        if cmd.startswith("scoreboard players get"):
            _, _, _, player, obj = cmd.split()
            v = self.scores.get(obj)
            return f"{player} has {v} [{obj}]" if v is not None else f"Can't get value of {obj} for {player}; none is set"
        return "ok"


def test_stats_setup_reset_read():
    r = FakeRcon({"b_deaths": 2, "b_walked": 12345})
    st = Stats(r, "player", ["deaths", "walked", "mined:stone", "deaths"])
    st.setup()
    assert "scoreboard objectives add b_deaths deathCount" in r.cmds
    assert "scoreboard players set player b_deaths 0" in r.cmds
    assert st.read() == {"deaths": 2, "walked": 123.45, "mined:stone": 0}


def test_resolve_start_is_deterministic_and_avoids_water():
    spec = StartSpec(random_radius=20)
    def surface(x, z):
        return (70, "water") if x < 100 else (64, "grass_block")
    a = resolve_start(spec, (100, 65, 100), "t", 0, 1, surface)
    b = resolve_start(spec, (100, 65, 100), "t", 0, 1, surface)
    c = resolve_start(spec, (100, 65, 100), "t", 1, 1, surface)
    assert a == b and a != c and a[0] >= 100 and a[1] == 65
    assert resolve_start(StartSpec(offset=[5, -3]), (0, 10, 0), "t", 0, 0) == (5, 10, -3)


PLOT = Volume((0, 0, 0), (31, 31, 31))


def ctx(stats=None, health=20.0, t=1000, frames=()):
    return Context({}, {}, diff({}, {}), PLOT, None, None, [], list(frames), stats or {}, health, t)


def test_stat_grader():
    c = ctx({"deaths": 0, "mined:stone": 7, "walked": 55.0})
    assert grade({"kind": "stat", "name": "deaths", "op": "<=", "value": 0}, c).passed
    assert not grade({"kind": "stat", "name": "deaths", "op": ">=", "value": 1}, c).passed
    r = grade({"kind": "stat", "name": "mined:stone", "min": 10}, c)
    assert not r.passed and abs(r.score - 0.7) < 1e-9
    assert grade({"kind": "stat", "name": "walked", "max": 100}, c).passed
    untracked = grade({"kind": "stat", "name": "kills", "min": 1}, c)
    assert not untracked.passed and untracked.checks == {"tracked": False}


def test_survived_and_world_time():
    assert grade({"kind": "survived"}, ctx({"deaths": 0}, health=13.0)).passed
    assert not grade({"kind": "survived"}, ctx({"deaths": 1}, health=20.0)).passed
    assert not grade({"kind": "survived"}, ctx({"deaths": 0}, health=0.0)).passed
    assert not grade({"kind": "survived", "min_health": 15}, ctx({"deaths": 0}, health=13.0)).passed
    assert grade({"kind": "world_time", "between": [0, 6000]}, ctx(t=1000)).passed
    assert not grade({"kind": "world_time", "between": [0, 6000]}, ctx(t=14000)).passed
    assert grade({"kind": "world_time", "between": [23000, 6000]}, ctx(t=23500)).passed   # wraps midnight


def test_milestones_can_use_stats_per_frame():
    f1 = Frame("step_01", {}, None, [], stats={"deaths": 0, "mined:stone": 0})
    f2 = Frame("step_02", {}, None, [], stats={"deaths": 0, "mined:stone": 3})
    spec = {"kind": "milestones", "required": "stone",
            "steps": [{"name": "stone", "check": {"kind": "stat", "name": "mined:stone", "min": 3}}]}
    r = grade(spec, ctx({"deaths": 0, "mined:stone": 3}, frames=[f1, f2]))
    assert r.passed and r.detail["reached_at_step"]["stone"] == 2


def test_milestones_required_list():
    spec = {"kind": "milestones", "required": ["alive", "morning"],
            "steps": [{"name": "alive", "check": {"kind": "survived"}},
                      {"name": "morning", "check": {"kind": "world_time", "between": [23000, 6000]}}]}
    assert grade(spec, ctx({"deaths": 0}, health=20.0, t=300)).passed
    r = grade(spec, ctx({"deaths": 0}, health=20.0, t=13000))
    assert not r.passed and r.checks == {"alive": True, "morning": False}


def test_resolve_start_distance_and_bearing():
    flat = lambda x, z: (64, "grass_block")  # noqa: E731
    east = resolve_start(StartSpec(distance=300, bearing=90), (100, 65, 100), "t", 0, 1, flat)
    north = resolve_start(StartSpec(distance=300, bearing=0), (100, 65, 100), "t", 0, 1, flat)
    assert east == (400, 65, 100) and north == (100, 65, -200)
    a = resolve_start(StartSpec(distance=600, random_radius=24), (0, 65, 0), "t", 0, 1, flat)
    b = resolve_start(StartSpec(distance=600, random_radius=24), (0, 65, 0), "t", 0, 1, flat)
    c = resolve_start(StartSpec(distance=600, random_radius=24), (0, 65, 0), "t", 1, 1, flat)
    assert a == b and a != c
    for p in (a, c):
        assert 600 - 40 <= (p[0] ** 2 + p[2] ** 2) ** 0.5 <= 600 + 40


def test_task_dist_placeholder_border_and_exec_timeout(tmp_path):
    from mcmsbench.arena import Plot
    from mcmsbench.tasks import Task
    y = tmp_path / "trip.yaml"
    y.write_text("prompt: 'base at {sx},{sz}; you are {dist} blocks away'\n"
                 "world: {type: survival, border: 1500}\nexec_timeout: 300\nstart: {distance: 600}\n")
    t = Task.from_yaml(y)
    plot = Plot(0, (0, 64, 0), Volume((-48, 65, -48), (48, 100, 48)), flat=False)
    assert t.render(plot, start=(300, 70, -400)) == "base at 0,0; you are 500 blocks away"
    assert t.world.border == 1500 and t.start.distance == 600 and t.exec_timeout == 300


def test_situation_starts():
    def surface(x, z):
        return (60, "water") if x < 0 else (70, "grass_block")
    def floor(x, z):
        return (52, "sand") if x < -10 else (58, "sand")     # deep water only west of -10
    cave = resolve_start(StartSpec(situation="cave", depth=14, distance=100, bearing=90), (0, 71, 0), "t", 0, 1, surface, floor)
    assert cave == (100, 56, 0)
    assert situation_commands(StartSpec(situation="cave"), cave) == [
        "fill 98 55 -2 102 59 2 minecraft:stone", "fill 99 56 -1 101 57 1 minecraft:air"]
    uw = resolve_start(StartSpec(situation="underwater", offset=[-40, 0], random_radius=30), (0, 71, 0), "t", 0, 1, surface, floor)
    assert uw[0] < -10 and uw[1] == 53           # on the floor of water >= 3 deep, never the shallows
    assert situation_commands(StartSpec(situation="underwater"), uw) == []
    box = resolve_start(StartSpec(situation="enclosed", offset=[20, 0]), (0, 71, 0), "t", 0, 1, surface, floor)
    assert box == (20, 71, 0)
    assert situation_commands(StartSpec(situation="enclosed"), box) == ["fill 19 70 -1 21 73 1 minecraft:stone hollow"]
    top = resolve_start(StartSpec(situation="pillar", height=30, offset=[20, 0]), (0, 71, 0), "t", 0, 1, surface, floor)
    assert top == (20, 101, 0)                   # 30 above the ground's standing level (70 + 1 + 30)
    assert situation_commands(StartSpec(situation="pillar", height=30, width=3), top) == [
        "fill 19 68 -1 21 100 1 minecraft:stone"]  # solid 3x3 column from under the ground to the block under the feet
    assert situation_commands(StartSpec(situation="pillar", height=10, width=1), (5, 81, 5)) == [
        "fill 5 68 5 5 80 5 minecraft:stone"]
    pit = StartSpec(situation="water_pit", depth=3, lip=2, width=3, offset=[20, 0])
    sunk = resolve_start(pit, (0, 71, 0), "t", 0, 1, surface, floor)
    assert sunk == (20, 66, 0)                   # on the floor of 3 of water whose top is 2 under the ground (70)
    assert situation_commands(pit, sunk) == [
        "fill 18 65 -2 22 70 2 minecraft:dirt",   # the pit's block, its rim the ground's top
        "fill 19 66 -1 21 68 1 minecraft:water",  # 3 deep
        "fill 19 69 -1 21 70 1 minecraft:air",    # the bank 2 over the water, open above
        "fill 18 71 -2 22 74 2 minecraft:air"]    # nothing over the pit (no tree to hide the sky)
    frozen = StartSpec(situation="water_pit", depth=4, lip=0, width=7, cap="ice")
    under = resolve_start(frozen, (0, 71, 0), "t", 0, 1)
    assert under == (0, 67, 0) and situation_commands(frozen, under)[2:4] == [
        "fill -3 70 -3 3 70 3 minecraft:ice", "setblock 2 70 0 minecraft:water"]   # frozen over, the hole it fell through
    shaft = StartSpec(situation="water_pit", depth=3, lip=10, width=1, wall="stone")
    assert situation_commands(shaft, (0, 50, 0))[0] == "fill -1 49 -1 1 62 1 minecraft:stone"
    level = StartSpec(situation="water_pit", depth=2, lip=0, width=1)
    assert situation_commands(level, resolve_start(level, (0, 71, 0), "t", 0, 1)) == [
        "fill -1 68 -1 1 70 1 minecraft:dirt", "fill 0 69 0 0 70 0 minecraft:water", "fill -1 71 -1 1 74 1 minecraft:air"]
    with pytest.raises(ValueError):
        resolve_start(StartSpec(situation="nether"), (0, 71, 0), "t", 0, 1, surface)
    with pytest.raises(RuntimeError):
        resolve_start(StartSpec(situation="underwater", offset=[50, 0]), (0, 71, 0), "t", 0, 1, surface, floor)
    with pytest.raises(RuntimeError):   # a lake under the bearing: never a mid-air "cave"
        resolve_start(StartSpec(situation="cave", offset=[-500, 0], random_radius=16), (0, 71, 0), "t", 0, 1, surface, floor)
    # ...but a wider ring finds the shore
    near = resolve_start(StartSpec(situation="cave", offset=[-20, 0], random_radius=16), (0, 71, 0), "t", 0, 1, surface, floor)
    assert near[0] >= 0 and near[1] == 70 - 15


def test_position_target_rel():
    c = Context({}, {}, diff({}, {}), PLOT, None, (73, 80, -49), [], [], {}, 20.0, 1000, center=(3, 64, 1))
    r = grade({"kind": "position", "target_rel": [70, -50], "tolerance": 4, "y_tolerance": 400}, c)
    assert r.passed and r.detail["target"] == [73, 64, -49]
    r = grade({"kind": "position", "target_rel": [70, -50], "tolerance": 4, "y_tolerance": 400}, 
              Context({}, {}, diff({}, {}), PLOT, None, (60, 80, -49), [], [], {}, 20.0, 1000, center=(3, 64, 1)))
    assert not r.passed


def test_escape_height_task_is_the_vague_version():
    from mcmsbench.tasks import load
    t = load("escape_height")
    assert t.start.situation == "pillar" and t.start.height == 30 and t.start.width == 3
    assert "x=" not in t.prompt and "{" not in t.prompt      # no coordinates: the bot infers where "down" is
    assert t.grader["required"] == "down" and t.inventory["cobblestone"] >= 30


def test_all_task_yamls_load():
    from mcmsbench.protocol import CAPABILITIES
    from mcmsbench.tasks import load_all
    tasks = load_all()
    assert len(tasks) >= 60
    for tid, t in tasks.items():
        assert t.start.situation in SITUATIONS, tid
        assert set(t.requires) <= set(CAPABILITIES), tid
