"""farmstead: the carved world (livestock, meadow, ore, water), the arithmetic that makes the finish line a farmer's
(the slaughter ceiling below the store asked for), the food / food_stock / herd graders and live checks, and the herd
watcher's books (a baby grown is not a death; a baby the bot kills is not a mob's) against a simulated server. No
server needed."""
import json
from pathlib import Path
import re

from mcmsbench.arena import Plot
from mcmsbench.goalspec import from_grader
from mcmsbench.tables import FOOD_POINTS, food_points
from mcmsbench.graders import Context, Frame, grade
from mcmsbench.tasks import FILL_LIMIT, load
from mcmsbench.world.volume import Volume, diff

SX, SY, SZ = 154, 70, 163          # seed 1's spawn (infra/worlds/1.json)
PLOT = Plot(0, (SX, SY - 1, SZ), Volume((SX - 64, SY - 12, SZ - 64), (SX + 64, SY + 28, SZ + 64)), flat=False)
TASK = load("farmstead")
FOODS = str(Path(__file__).resolve().parents[1] / "node_modules/minecraft-data/minecraft-data/data/pc/26.1/foods.json")


def _setup():
    return TASK.render_setup(PLOT, (SX, SY, SZ))


def _volume(cmd: str) -> int:
    v = [int(t) for t in cmd.split()[1:7]]
    return (abs(v[3] - v[0]) + 1) * (abs(v[4] - v[1]) + 1) * (abs(v[5] - v[2]) + 1)


# ------------------------------------------------------------------ the world

def test_setup_stays_in_the_border_and_under_the_fill_limit():
    cmds = _setup()
    assert not [c for c in cmds if "{" in c and "summon" not in c]          # every placeholder filled
    fills = [c for c in cmds if c.startswith("fill ")]
    assert fills and all(_volume(c) <= FILL_LIMIT for c in fills)
    for c in fills:
        v = [int(t) for t in c.split()[1:7]]
        assert SX - 64 <= min(v[0], v[3]) and max(v[0], v[3]) <= SX + 63, c
        assert SZ - 64 <= min(v[2], v[5]) and max(v[2], v[5]) <= SZ + 63, c
    assert cmds[-1] == "time set 0"


def test_two_cows_three_chickens_and_nothing_else_summoned():
    summons = [c.split()[1] for c in _setup() if c.startswith("summon ")]
    assert sorted(summons) == ["minecraft:chicken"] * 3 + ["minecraft:cow"] * 2
    assert all("PersistenceRequired:1b" in c for c in _setup() if c.startswith("summon "))
    assert TASK.world.passive_mobs is False and TASK.herd_watch["types"] == ["cow", "chicken"]


def test_meadow_grass_trees_water_and_iron_on_a_face():
    cmds = _setup()
    assert any(c.endswith("minecraft:short_grass replace minecraft:air") for c in cmds)
    trees = [c for c in cmds if c.startswith("place feature")]
    assert len(trees) >= 12 and all("minecraft:birch" in c for c in trees)     # birch: no apples to forage
    assert any(c.startswith("fill") and c.endswith("minecraft:water") for c in cmds)
    # the outcrop's south face (z = sz-18) is open to the spawn and carries iron
    assert f"fill {SX - 1} {SY} {SZ - 18} {SX + 1} {SY} {SZ - 18} minecraft:iron_ore" in cmds
    assert TASK.world.difficulty == "normal" and TASK.world.random_tick_speed and TASK.world.random_tick_speed > 3
    assert TASK.start.food == 6 and TASK.inventory == {"red_bed": 1} and TASK.spawn_first is False and TASK.fast_nights


# ------------------------------------------------------------------ the numbers

def test_food_points_agree_with_minecraft_data():
    data = {f["name"]: f["foodPoints"] for f in json.load(open(FOODS))}
    for name, pts in FOOD_POINTS.items():
        assert data[name] == pts, name
    for poison in ("rotten_flesh", "spider_eye", "poisonous_potato", "pufferfish"):
        assert poison not in FOOD_POINTS


def _stock_needed() -> int:
    return next(m["check"]["min_points"] for m in TASK.grader["steps"] if m["check"]["kind"] == "food_stock")


def test_slaughtering_the_whole_herd_cannot_make_the_store():
    """2 cows drop at most 3 beef each, a chicken one chicken: every animal cooked is 66 points, of which filling the
    bar from the start's food takes 20 - food. What is left must be short of the store."""
    cows = sum(1 for c in _setup() if c.startswith("summon minecraft:cow"))
    chickens = sum(1 for c in _setup() if c.startswith("summon minecraft:chicken"))
    ceiling = cows * 3 * FOOD_POINTS["cooked_beef"] + chickens * FOOD_POINTS["cooked_chicken"]
    assert ceiling - (20 - TASK.start.food) < _stock_needed()


def test_food_points_counts_only_food():
    inv = [{"name": "bread", "count": 4}, {"name": "cooked_beef", "count": 2}, {"name": "wheat_seeds", "count": 30},
           {"name": "rotten_flesh", "count": 9}]
    assert food_points(inv) == 4 * 5 + 2 * 8


# ------------------------------------------------------------------ graders

class Server:
    """Answers `execute if entity` counts for a herd: {type: n}."""

    def __init__(self, counts):
        self.counts, self.sent = counts, []

    def __call__(self, cmd):
        self.sent.append(cmd)
        m = re.search(r"type=minecraft:(\w+)", cmd)
        n = self.counts.get(m.group(1), 0) if m else 0
        return f"Test passed. Count: {n}" if n else "Test failed"


def _ground():
    return {(x, SY - 1, z): "grass_block" for x in range(SX - 8, SX + 16) for z in range(SZ - 8, SZ + 12)}


def _farm(after, n=14):
    """A field by the pond: farmland under wheat."""
    cells = [(SX + 7 + i % 7, SZ - 5 + i // 7) for i in range(n)]
    for x, z in cells:
        after[(x, SY - 1, z)] = "farmland"
        after[(x, SY, z)] = "wheat"
    return after


def _house(after, x0=SX - 3, z0=SZ - 3, w=5):
    for x in range(x0, x0 + w):
        for z in range(z0, z0 + w):
            if x in (x0, x0 + w - 1) or z in (z0, z0 + w - 1):
                for y in (SY, SY + 1):
                    after[(x, y, z)] = "birch_planks"
            after[(x, SY + 2, z)] = "birch_planks"
    for y in (SY, SY + 1):
        after[(x0 + 2, y, z0)] = "birch_door"
    after[(x0 + 2, SY, z0 + 2)] = "red_bed"
    after[(x0 + 2, SY, z0 + 3)] = "red_bed"
    return after, {"pos": [x0 + 2, SY, z0 + 2], "bed": True}


class Hydrating:
    """The hydrated grader's server: random ticks set, then every crop's farmland asked about (all wet here)."""

    def __init__(self, herd):
        self.herd = Server(herd)

    def __call__(self, cmd):
        if cmd.startswith("gamerule"):
            return "ok"
        if "farmland[moisture=7]" in cmd:
            return "Test passed"
        return self.herd(cmd)


def _ctx(after, inv, food, herd_counts, herd, respawn=None, stats=None):
    before = _ground()
    stats = {"deaths": 0, "mined:wheat": 30, "crafted:bread": 12, "custom:animals_bred": 2, **(stats or {})}
    return Context(before, after, diff(before, after), PLOT.volume, None, (SX, SY, SZ), inv,
                   [Frame("step_01", after, (SX, SY, SZ), inv, t=60, stats=stats, food=food)], stats, 20.0, 3000,
                   (SX, SY, SZ), rcon=Hydrating(herd_counts), respawn=respawn, food=food, herd=herd,
                   fmt=lambda t: t)


def test_food_stock_and_food_graders():
    ctx = _ctx(_ground(), [{"name": "bread", "count": 12}], 20, {}, None)
    assert grade({"kind": "food_stock", "min_points": 60}, ctx).passed
    assert not grade({"kind": "food_stock", "min_points": 61}, ctx).passed
    assert grade({"kind": "food", "min": 20}, ctx).passed
    ctx.food = 17
    r = grade({"kind": "food", "min": 20}, ctx)
    assert not r.passed and r.detail == {"food": 17, "need": 20}


def test_herd_grader_counts_each_kind_and_reads_the_watcher():
    spec = {"kind": "herd", "types": ["cow", "chicken"], "min_each": 2, "max_baby_kills": 0}
    ok = _ctx(_ground(), [], 20, {"cow": 3, "chicken": 2}, {"baby_kills": 0})
    r = grade(spec, ok)
    assert r.passed and r.detail["counts"] == {"cow": 3, "chicken": 2}
    assert all("execute if entity @e[type=minecraft:" in c for c in ok.rcon.herd.sent)
    assert not grade(spec, _ctx(_ground(), [], 20, {"cow": 1, "chicken": 4}, {"baby_kills": 0})).passed
    killed_a_calf = grade(spec, _ctx(_ground(), [], 20, {"cow": 3, "chicken": 3}, {"baby_kills": 1}))
    assert not killed_a_calf.passed and killed_a_calf.checks["babies_spared"] is False
    assert not grade(spec, _ctx(_ground(), [], 20, {"cow": 3, "chicken": 3}, None)).passed   # no watcher: unproven


def test_the_farmers_trial_passes_and_the_butchers_does_not(monkeypatch):
    monkeypatch.setattr("mcmsbench.graders.mechanical.time.sleep", lambda s: None)    # the hydrated grader's tick burst
    after, spawn = _house(_farm(_ground(), n=56))      # the field bigger than the house: still the house is the base
    farmer = _ctx(after, [{"name": "bread", "count": 13}, {"name": "wheat_seeds", "count": 9}], 20,
                  {"cow": 3, "chicken": 4}, {"baby_kills": 0}, respawn=spawn)
    r = grade(TASK.grader, farmer)
    assert r.passed, r.detail["steps"]
    assert r.checks["base"] and r.checks["door"] and r.checks["bed_inside"] and r.checks["home_spawn"], r.detail["steps"]
    # a sapling grown into a tree in the grove (random ticks at 40): new logs and a canopy bigger than the house
    for dx in range(-3, 4):
        for dz in range(-3, 4):
            for y in (SY + 4, SY + 5, SY + 6):
                after[(SX - 20 + dx, y, SZ + 5 + dz)] = "birch_leaves"
    for y in range(SY, SY + 6):
        after[(SX - 20, y, SZ + 5)] = "birch_log"
    r = grade(TASK.grader, _ctx(after, farmer.bot_inventory, 20, {"cow": 3, "chicken": 4}, {"baby_kills": 0}, respawn=spawn))
    assert r.checks["base"] and r.checks["home_spawn"], r.detail["steps"]["base"]
    # every animal eaten: the bar full, no herd, no field, the store short
    butcher = _ctx(_ground(), [{"name": "cooked_beef", "count": 3}], 20, {}, {"baby_kills": 0},
                   stats={"killed:cow": 2, "killed:chicken": 3, "mined:wheat": 0, "crafted:bread": 0,
                          "custom:animals_bred": 0})
    r = grade(TASK.grader, butcher)
    assert not r.passed
    assert r.checks["fed"] and not r.checks["herd"] and not r.checks["farm"] and not r.checks["stocked"]


# ------------------------------------------------------------------ the goal file's check

def test_the_task_check_is_the_finish_line_only():
    check = TASK.check
    assert [p["name"] for p in check["parts"]] == ["fed", "stocked"]
    # read off the grader instead, the herd is uncovered: an agent cannot count the livestock the way the server does
    comp = from_grader(TASK.grader, plot=PLOT)
    assert {u["name"] for u in comp["uncovered"]} == {"herd"}
    assert {p["check"]["kind"] for p in comp["parts"]} >= {"food", "food_stock", "hydrated", "alive"}


# ------------------------------------------------------------------ the herd watcher

PRED = re.compile(r"execute as (@e\[[^\]]*\]) (if|unless) predicate .* run tag @s add (\w+)$")


class World:
    """A server's herd as the watcher sees it: entities with a type, babyhood, tags and Age; the bot's kill counters."""

    def __init__(self):
        self.mobs: list[dict] = []
        self.killed: dict[str, int] = {}
        self.sent: list[str] = []

    def add(self, kind, baby=False):
        self.mobs.append({"type": kind, "baby": baby, "tags": set(), "age": -24000 if baby else 0})
        return self.mobs[-1]

    def kill(self, mob, by_bot=True):
        self.mobs.remove(mob)
        if by_bot:
            self.killed[mob["type"]] = self.killed.get(mob["type"], 0) + 1

    def match(self, sel):
        out = list(self.mobs)
        for f in sel[3:-1].split(","):
            k, v = f.split("=", 1)
            if k == "type":
                out = [m for m in out if m["type"] == v.removeprefix("minecraft:")]
            elif k == "tag" and v.startswith("!"):
                out = [m for m in out if v[1:] not in m["tags"]]
            elif k == "tag":
                out = [m for m in out if v in m["tags"]]
        return out

    def __call__(self, cmd):
        self.sent.append(cmd)
        if m := PRED.match(cmd):
            for mob in self.match(m.group(1)):
                if mob["baby"] == (m.group(2) == "if"):
                    mob["tags"].add(m.group(3))
            return ""
        if m := re.match(r"kill (@e\[[^\]]*\])$", cmd):
            for mob in self.match(m.group(1)):
                self.mobs.remove(mob)                   # by command, in the void: no kill of the bot's, no drops in reach
            return ""
        if m := re.match(r"execute if entity (@e\[[^\]]*\])$", cmd):
            n = len(self.match(m.group(1)))
            return f"Test passed. Count: {n}" if n else "Test failed"
        if m := re.match(r"tag (@e\[[^\]]*\]) (add|remove) (\w+)$", cmd):
            for mob in self.match(m.group(1)):
                (mob["tags"].add if m.group(2) == "add" else mob["tags"].discard)(m.group(3))
            return ""
        if m := re.match(r"scoreboard players add (@e\[[^\]]*\]) bv_age (\d+)$", cmd):
            for mob in self.match(m.group(1)):
                mob["age"] += int(m.group(2))
                mob["baby"] = mob["age"] < 0
            return ""
        if m := re.match(r"scoreboard players get (\w+) b_killed_(\w+)$", cmd):
            return f"{m.group(1)} has {self.killed.get(m.group(2), 0)} [x]"
        return ""


def _watch(world, grow=1.0, strays=False):
    from mcmsbench.runner import HerdWatcher
    return HerdWatcher("h", 0, "pw", "player", ["cow", "chicken"], grow=grow, strays=strays, rcon=world, thread=False)


def test_watcher_counts_a_calf_the_bot_kills_and_not_one_that_grows_up():
    w = World()
    a, b = w.add("cow"), w.add("cow")
    hw = _watch(w)
    calf = w.add("cow", baby=True)                  # bred
    hw.poll_once()
    assert hw.tally["born"] == 1 and hw._babies["cow"] == 1
    calf["baby"] = False                            # grown up between polls
    hw.poll_once()
    assert hw.tally["grown"] == 1 and hw.tally["baby_kills"] == 0 and hw.tally["baby_lost"] == 0
    second = w.add("cow", baby=True)
    hw.poll_once()
    w.kill(second)                                  # the bot kills the calf
    hw.poll_once()
    assert hw.tally["baby_kills"] == 1
    w.kill(a)                                       # an adult for dinner: not a baby kill
    hw.poll_once()
    out = hw.stop()
    assert out["baby_kills"] == 1 and out["adults_died"] == 1 and out["by_type"]["cow"]["baby_kills"] == 1
    assert b in w.mobs


def test_watcher_puts_a_mobs_kill_down_as_lost_and_an_adult_kill_beside_it_as_the_bots():
    w = World()
    hen = w.add("chicken")
    w.add("chicken")
    hw = _watch(w)
    chick = w.add("chicken", baby=True)
    hw.poll_once()
    w.kill(chick, by_bot=False)                     # a zombie's, at night
    hw.poll_once()
    assert hw.tally["baby_lost"] == 1 and hw.tally["baby_kills"] == 0
    chick2 = w.add("chicken", baby=True)
    hw.poll_once()
    w.kill(hen)                                     # the bot kills an adult while something else takes the chick
    w.kill(chick2, by_bot=False)
    hw.poll_once()
    assert hw.tally["baby_kills"] == 0 and hw.tally["baby_lost"] == 2


def test_watcher_removes_livestock_the_world_spawns_but_keeps_the_herds_young():
    w = World()
    cow = w.add("cow")
    w.add("cow")
    hw = _watch(w, strays=True)
    calf = w.add("cow", baby=True)              # bred
    hw.poll_once()
    w.add("chicken")                            # natural spawning on Normal, or a jockey's mount
    w.add("cow")
    hw.poll_once()
    assert hw.tally["strays_removed"] == 2 and [m["type"] for m in w.mobs] == ["cow", "cow", "cow"]
    assert hw.tally["adults_died"] == 0         # a stray is never counted as a herd animal that died
    calf["baby"] = False                        # grown up: still the herd's
    hw.poll_once()
    assert calf in w.mobs and cow in w.mobs and hw.tally["grown"] == 1 and hw.tally["strays_removed"] == 2
    assert TASK.herd_watch.get("strays") is True


def test_watcher_ages_babies_faster():
    w = World()
    w.add("cow")
    hw = _watch(w, grow=4)
    calf = w.add("cow", baby=True)
    hw._last -= 10                                  # ten seconds since the last poll
    hw.poll_once()
    assert any(c.startswith("scoreboard players add @e[type=minecraft:cow,tag=bv_baby] bv_age ") for c in w.sent)
    assert calf["age"] > -24000 + 3 * 10 * 20 - 50   # three extra seconds of growth for each one that passed
    assert any("store result entity @s Age int 1" in c for c in w.sent)
