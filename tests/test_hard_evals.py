"""The hard-eval plumbing: nether plots, pseudo-stats, liquid paths, milestone deadlines, the new
facade methods, and the six task files — no server needed."""
import pytest

from mcmsbench.arena import NETHER, OVERWORLD, Arena, Plot
from mcmsbench.config import ArenaConfig
from mcmsbench.graders import Context, grade
from mcmsbench.graders.mechanical import liquid_route
from mcmsbench.start import StartSpec, resolve_start, situation_commands
from mcmsbench.stats import Stats, criterion_for, is_pseudo, pseudo_query
from mcmsbench.tasks import Task, WorldSpec, load
from mcmsbench.world.volume import Volume, diff

PLOT = Volume((0, -60, 0), (31, -37, 31))


class FakeRcon:
    def __init__(self, answers=None):
        self.answers = answers or {}
        self.cmds = []

    def __call__(self, cmd):
        self.cmds.append(cmd)
        for k, v in self.answers.items():
            if k in cmd:
                return v
        return "Successfully filled 1 block(s)"


def ctx(after, before=None, pos=None, inv=None, frames=(), stats=None, seconds=None):
    before = before or {}
    return Context(before, after, diff(before, after), PLOT, -61, pos, inv or [], list(frames), stats or {}, 20.0, 1000,
                   (16, -60, 16), seconds=seconds)


# ------------------------------------------------------------- arena: nether rooms, dimensions

def test_nether_plot_is_a_room_and_commands_run_in_the_nether():
    rc = FakeRcon()
    a = Arena(ArenaConfig(), rc)
    p = a.plot(1, "the_nether", size=48, height=24)
    assert p.dimension == NETHER and p.floor_y == 96 and p.volume.min == (256, 97, 0) and p.volume.max == (303, 120, 47)
    assert "Nether" in p.describe()
    a.reset(p)
    fills = [c for c in rc.cmds if "fill" in c]
    assert all(c.startswith("execute in minecraft:the_nether run ") for c in fills)
    assert any("255 95 -1 304" in c and "netherrack" in c for c in fills)      # the shell, one block outside
    assert any("256 97 0 303 104 47 minecraft:air" in c for c in fills)          # hollowed out in slabs
    assert any("256 96 0 303 96 47 minecraft:netherrack" in c for c in fills)    # the floor
    assert any("kill @e[type=!player,type=!item" in c for c in rc.cmds)
    for c in fills:                                                              # under the per-command block cap
        nums = [int(v) for v in c.split("run fill ")[1].split()[:6]]
        assert (nums[3] - nums[0] + 1) * (nums[4] - nums[1] + 1) * (nums[5] - nums[2] + 1) <= 32768
    rc.cmds.clear()
    a.prepare_player("player", p, {"bow": 1})
    assert "execute in minecraft:the_nether run tp player 279 97 23" in rc.cmds
    assert "execute in minecraft:the_nether run spawnpoint player 279 97 23" in rc.cmds
    assert "advancement revoke player everything" in rc.cmds
    rc.cmds.clear()
    over = a.plot(0)
    assert over.dimension == OVERWORLD and over.floor_y == -61
    a.prepare_player("player", over)
    assert "tp player 15 -60 15" in rc.cmds                                     # plain tp in the overworld
    a.teleport("observer", 1, 2, 3, NETHER)
    assert rc.cmds[-1] == "execute in minecraft:the_nether run tp observer 1 2 3"
    rc.answers["Dimension"] = 'player has the following entity data: "minecraft:the_nether"'
    assert a.server_dimension("player") == "minecraft:the_nether"


def test_task_setup_runs_in_the_plot_dimension():
    t = Task(id="t", prompt="x", setup=["execute as {bot} at @s run summon minecraft:ghast ~14 ~8 ~"],
             world=WorldSpec(dimension="the_nether"))
    assert t.world.dimension_id == "minecraft:the_nether"
    nether = Plot(0, (0, 96, 0), Volume((0, 97, 0), (31, 120, 31)), dimension=NETHER)
    assert t.render_setup(nether, (5, 97, 5), "player") == [
        "execute in minecraft:the_nether run execute as player at @s run summon minecraft:ghast ~14 ~8 ~"]
    over = Plot(0, (0, -61, 0), PLOT)
    assert t.render_setup(over, None, "player")[0].startswith("execute as player")


def test_flat_pillar_and_cave_starts():
    top = resolve_start(StartSpec(situation="pillar", height=30), (15, -60, 15), "t", 0, 0)
    assert top == (15, -30, 15)
    assert situation_commands(StartSpec(situation="pillar", height=30, width=3), top) == ["fill 14 -63 14 16 -31 16 minecraft:stone"]
    assert resolve_start(StartSpec(situation="cave", depth=10), (15, -60, 15), "t", 0, 0) == (15, -70, 15)


# ------------------------------------------------------------- pseudo-stats

def test_pseudo_stats_read_through_execute_if():
    assert is_pseudo("dimension:the_nether") and is_pseudo("advancement:story/enter_the_nether") and not is_pseudo("mined:stone")
    assert criterion_for("dimension:the_nether") == ("dimension:the_nether", 1)
    assert pseudo_query("advancement:story/enter_the_nether", "player") == \
        "execute if entity @a[name=player,advancements={minecraft:story/enter_the_nether=true}]"
    assert pseudo_query("dimension:the_nether", "player") == "execute in minecraft:the_nether if entity @a[name=player,distance=0..]"
    rc = FakeRcon({"the_nether if entity": "Test passed", "advancements=": "Test failed", "b_deaths": "player has 0 [b_deaths]"})
    st = Stats(rc, "player", ["deaths", "dimension:the_nether", "advancement:story/enter_the_nether"])
    st.setup()
    assert not any("objectives add" in c and "dimension" in c for c in rc.cmds)   # no scoreboard for pseudo-stats
    assert "advancement revoke player only minecraft:story/enter_the_nether" in rc.cmds
    assert st.read() == {"deaths": 0, "dimension:the_nether": 1, "advancement:story/enter_the_nether": 0}
    r = grade({"kind": "stat", "name": "dimension:the_nether", "min": 1}, ctx({}, stats=st.read()))
    assert r.passed


# ------------------------------------------------------------- graders

def test_liquid_path_lava_fall():
    # a ledge top at y=-56 (A), lava spreading east, falling at x=13 to the ground (y=-60), spreading to B at x=14
    lava = {(x, -56, 16): "lava" for x in (10, 11, 12)}
    lava.update({(13, y, 16): "lava" for y in range(-60, -55)})
    lava.update({(x, -60, 16): "lava" for x in (14, 15, 16)})
    r = liquid_route(lava, "lava", (10, -56, 16), (14, -60, 16))
    assert r["connected"] and r["reach"] == 1.0 and r["body"] == len(lava)
    spec = {"kind": "liquid_path", "liquid": "lava", "from": [10, 4, 16], "to": [14, 0, 16]}
    assert grade(spec, ctx(lava)).passed
    short = {p: b for p, b in lava.items() if p[1] > -59}            # the fall stops two blocks up
    r = grade(spec, ctx(short))
    assert not r.passed and r.checks["starts_at_a"] and not r.checks["reaches_b"] and 0.4 < r.score < 0.9
    wrong_side = {(x, -56, 16): "lava" for x in (7, 8, 9)}
    wrong_side.update({(13, -60, 16): "lava"})                       # disconnected puddle at the bottom
    r = grade(spec, ctx(wrong_side))
    assert not r.passed and r.checks["starts_at_a"] and not r.checks["reaches_b"]
    assert not grade(spec, ctx({})).passed
    assert grade({**spec, "to": [10, 4, 16]}, ctx({(10, -56, 16): "lava"})).passed      # source at A only


def test_placed_and_inventory_max():
    frame = {(12 + dx, -60 + dy, 12): "obsidian" for dx in (0, 3) for dy in (1, 2, 3)}
    frame.update({(13, -60, 12): "obsidian", (14, -60, 12): "obsidian", (13, -56, 12): "obsidian", (14, -56, 12): "obsidian"})
    assert grade({"kind": "placed", "block": "obsidian", "count": 10}, ctx(frame)).passed
    assert not grade({"kind": "placed", "block": "nether_portal", "count": 6}, ctx(frame)).passed
    lit = dict(frame); lit.update({(13 + dx, -59 + dy, 12): "nether_portal" for dx in (0, 1) for dy in (0, 1, 2)})
    assert grade({"kind": "placed", "block": "nether_portal", "count": 6}, ctx(lit)).passed
    # carrying obsidian does not count as placing it
    assert not grade({"kind": "placed", "block": "obsidian", "count": 1}, ctx({}, inv=[{"name": "obsidian", "count": 12}])).passed
    assert grade({"kind": "inventory", "item": "gold_ingot", "max": 47}, ctx({}, inv=[{"name": "gold_ingot", "count": 40}])).passed
    assert not grade({"kind": "inventory", "item": "gold_ingot", "max": 47}, ctx({}, inv=[{"name": "gold_ingot", "count": 48}])).passed


def test_milestone_deadline():
    from mcmsbench.graders import Frame
    spec = {"kind": "milestones", "required": "down",
            "steps": [{"name": "down", "weight": 4, "by": 90, "check": {"kind": "position", "target_abs": "spawn", "tolerance": 12, "y_tolerance": 3}},
                      {"name": "alive", "weight": 2, "hold": True, "check": {"kind": "survived"}}]}
    up, down = (16, -30, 16), (18, -60, 16)
    fast = [Frame("step_01", {}, up, t=10.0), Frame("step_02", {}, down, t=40.0)]
    r = grade(spec, ctx({}, pos=down, frames=fast, stats={"deaths": 0}, seconds=60.0))
    assert r.passed and r.score == 1.0 and r.detail["reached_at_seconds"]["down"] == 40.0
    slow = [Frame("step_01", {}, up, t=10.0), Frame("step_02", {}, up, t=100.0)]
    r = grade(spec, ctx({}, pos=down, frames=slow, stats={"deaths": 0}, seconds=140.0))
    assert not r.passed and not r.checks["down"] and r.checks["alive"]          # got down, but at 140 s
    # a hold milestone reached at step 1 is unaffected by `by` on another step
    assert r.detail["holds_at_end"]["down"]


# ------------------------------------------------------------- facade surface

# ------------------------------------------------------------- the six tasks

def test_hard_tasks_are_wired_up():
    lava = load("lava_fall")
    assert lava.inventory["lava_bucket"] == 1 and lava.grader["required"] == "reaches_ground"
    assert any(s["check"]["kind"] == "liquid_path" for s in lava.grader["steps"])
    drop = load("water_drop")
    assert drop.start.situation == "pillar" and drop.start.height >= 24 and "cobblestone" not in drop.inventory
    assert next(s for s in drop.grader["steps"] if s["name"] == "down")["by"] <= 120
    dia = load("find_diamonds")
    assert dia.xray is False and dia.world.type == "survival" and dia.start.situation == "cave"
    assert "deepslate_diamond_ore" in dia.setup[0]
    portal = load("nether_portal")
    assert portal.inventory["obsidian"] >= 10 and "dimension:the_nether" in portal.stats
    piglin = load("piglin_barter")
    assert piglin.world.dimension == "the_nether" and piglin.world.difficulty != "peaceful"
    assert piglin.inventory["golden_helmet"] == 1 and "helmet" not in piglin.prompt.lower()
    ghast = load("ghast_fight")
    assert ghast.world.dimension == "the_nether" and ghast.world.size == 48 and ghast.inventory["arrow"] >= 16
    assert "killed:ghast" in ghast.stats


# ------------------------------------------------------------- second batch: eight more tasks

def test_built_counts_dug_then_placed_and_setup_fills():
    from mcmsbench.graders import built
    before = {(0, 0, 0): "stone", (1, 0, 0): "leaf_litter", (2, 0, 0): "dirt", (3, 0, 0): "grass_block"}
    after = {(0, 0, 0): "cobblestone", (1, 0, 0): "torch", (2, 0, 0): "grass_block", (3, 0, 0): "stone", (4, 0, 0): "stone"}
    b = built(diff(before, after))
    assert b == {(0, 0, 0): "cobblestone", (1, 0, 0): "torch", (4, 0, 0): "stone"}   # grass spread and a stone fill over dirt are not builds


def test_blocks_guard_and_position_rel_dy():
    after = {(10 + i, -60, 16): "wheat" for i in range(7)}
    assert grade({"kind": "blocks", "block": "wheat", "min": 7}, ctx(after)).passed
    assert not grade({"kind": "blocks", "block": "wheat", "min": 8}, ctx(after)).passed
    c = ctx({}); c.refusals = ["guard: chest looks like someone's build"]
    assert not grade({"kind": "guard", "max_refusals": 0}, c).passed and grade({"kind": "guard", "max_refusals": 1}, c).passed
    assert grade({"kind": "position", "target_rel": [9, 24, 0], "tolerance": 2, "y_tolerance": 2}, ctx({}, pos=(25, -36, 16))).passed
    assert not grade({"kind": "position", "target_rel": [9, 24, 0], "tolerance": 2, "y_tolerance": 2}, ctx({}, pos=(25, -60, 16))).passed


def test_entity_inside_reads_the_live_villager():
    box = {(x, y, z): "cobblestone" for x in range(10, 15) for y in range(-61, -56) for z in range(10, 15)
           if x in (10, 14) or z in (10, 14) or y in (-61, -57)}
    before = {p: "stone" for p in box if p[1] == -61}            # the bottom face replaced natural ground: still built
    after = dict(box); after[(12, -60, 13)] = "torch"
    c = Context(before, after, diff(before, after), PLOT, None, None, [], [], {}, 20.0, 1000, (16, -60, 16),
                rcon=FakeRcon({"villager": "Villager has the following entity data: [12.4d, -60.0d, 12.6d]"}))
    spec = {"kind": "entity_inside", "entity": "villager", "inside": True, "sealed": True, "near": {"block": "*torch", "within": 5}}
    r = grade(spec, c)
    assert r.passed, r.to_dict()
    opened = dict(after); del opened[(10, -60, 12)]; del opened[(10, -59, 12)]     # a doorway: enclosed but not sealed
    r = grade(spec, Context(before, opened, diff(before, opened), PLOT, None, None, [], [], {}, 20.0, 1000, (16, -60, 16), rcon=c.rcon))
    assert not r.passed and r.checks["inside"] and not r.checks["sealed"]
    gone = Context(before, after, diff(before, after), PLOT, None, None, [], [], {}, 20.0, 1000, (16, -60, 16), rcon=FakeRcon({"villager": "No entity was found"}))
    assert not grade(spec, gone).passed and not grade(spec, gone).checks["alive"]


def test_a_structure_the_setup_built_counts_only_with_with_setup():
    """`before` is taken after the task's setup, so a shed the setup built is the world, not the bot's work; a check
    about a structure the task provides says `with_setup: true` (stale_memory: the chest goes inside the shed)."""
    shed = {(x, y, z): "oak_planks" for x in range(10, 15) for y in range(-61, -57) for z in range(10, 15)
            if x in (10, 14) or z in (10, 14) or y in (-61, -58)}
    del shed[(12, -60, 10)], shed[(12, -59, 10)]                 # the doorway
    before = dict(shed)
    after = dict(shed); after[(12, -60, 12)] = "chest"            # the bot's chest, in the setup's shed
    c = Context(before, after, diff(before, after), PLOT, None, None, [], [], {}, 20.0, 1000, (16, -60, 16), setup=dict(shed))
    spec = {"kind": "furnishings", "items": [{"name": "chest_inside", "item": "chest", "count": 1, "inside": True}]}
    assert not grade(spec, c).passed                              # the bot built no shell of its own
    assert grade(dict(spec, with_setup=True), c).passed
    from mcmsbench.graders import Frame
    assert c.at_frame(Frame("step_01", after, None, [])).setup == shed   # the per-step frames keep it too


def test_live_checks_run_once_in_milestones():
    from mcmsbench.graders import Frame
    calls = []
    class CountingRcon(FakeRcon):
        def __call__(self, cmd):
            calls.append(cmd); return "Test passed"
    c = Context({}, {}, diff({}, {}), PLOT, -61, None, [], [Frame("s1", {}, t=1.0), Frame("s2", {}, t=2.0)], {}, 20.0, 1000, (16, -60, 16),
                rcon=CountingRcon(), fmt=lambda t: t)
    spec = {"kind": "milestones", "required": "flush",
            "steps": [{"name": "flush", "check": {"kind": "functional", "steps": [{"do": "setblock 1 2 3 minecraft:lever[powered=true]"}, {"assert": "execute if entity @e[type=item]"}]}}]}
    r = grade(spec, c)
    assert r.passed and calls.count("setblock 1 2 3 minecraft:lever[powered=true]") == 1     # the lever was flipped once, not per frame


def test_nether_scaled_vars_and_memories():
    t = Task(id="t", prompt="p", setup=["execute in minecraft:the_nether run fill {nx-1} 90 {nz} {nx+2} 94 {nz} minecraft:obsidian"],
             memories=[{"text": "shed at {ax+22} {ay} {az+18}", "at": "{ax+22} {ay} {az+18}"}, {"text": "no place"}], anchor=(6, 0, 6))
    plot = Plot(1, (256, -61, 0), Volume((256, -60, 0), (287, -37, 31)))
    assert t.render_setup(plot, None, "player") == ["execute in minecraft:the_nether run fill 32 90 1 35 94 1 minecraft:obsidian"]
    assert t.render_memories(plot) == [("shed at 284 -60 24", (284, -60, 24)), ("no place", None)]


def test_overworld_reset_clears_high_and_removes_stale_nether_portals():
    rc = FakeRcon()
    a = Arena(ArenaConfig(), rc)
    a.reset(a.plot(0))
    fills = [c for c in rc.cmds if c.startswith("fill")]
    assert max(int(c.split()[4]) for c in fills) >= -37 + 40 - 8
    portal_clears = [c for c in rc.cmds if "replace minecraft:nether_portal" in c]
    assert portal_clears and all(c.startswith("execute in minecraft:the_nether run fill -19 ") for c in portal_clears)
    assert len(portal_clears) == 7


def test_second_batch_tasks_are_wired_up():
    trip = load("nether_round_trip")
    assert "{nx" in trip.setup[3] and trip.grader["required"] == ["back", "home"] and any(s.get("must") for s in trip.grader["steps"])
    farm = load("wheat_farm")
    assert farm.inventory["dispenser"] == 1 and any(s["name"] == "flush" and s["check"]["kind"] == "functional" for s in farm.grader["steps"])
    ravine = load("ravine_crossing")
    assert ravine.start.situation == "pillar" and ravine.movements == {"can_dig": False, "towers": False, "scaffolding": []}
    house = load("house_budget")
    assert house.inventory["oak_planks"] == 100 and house.grader["min_footprint"] == 42
    stale = load("stale_memory")
    assert stale.memories and "{ax+22}" in stale.memories[0]["at"]
    safe = load("make_safe")
    assert safe.guard and any(s["check"]["kind"] == "guard" for s in safe.grader["steps"])
    lava = load("ore_under_lava")
    assert lava.xray is False and "lava" in lava.setup[2] and lava.grader["required"] == ["diamond", "alive"]
