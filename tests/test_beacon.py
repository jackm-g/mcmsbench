"""beacon: a working beacon at a base, from the Wither's star, obsidian made from the lava pool, glass smelted from the
pond's sand, and a base crafted out of a vault of mixed metal. The task's terms (a running clock, which a beacon needs
to light), every instance's vault (exactly nine blocks and one payment item over), the star in one place only, and
the grader on a synthetic base with a stand-in server for the beacon's own Levels and power. No server needed."""
import re

import pytest

from mcmsbench.arena import Plot
from mcmsbench.config import load as load_settings
from mcmsbench.graders import Context, Frame, grade
from mcmsbench.runner import build_goal, stops_on_pass
from mcmsbench.tasks import load
from mcmsbench.world.volume import Volume, diff

from test_variants import KEY

SX, SY, SZ = 154, 70, 163          # seed 1's spawn (infra/worlds/1.json): the base
PLOT = Plot(0, (SX, SY - 1, SZ), Volume((SX - 24, SY - 12, SZ - 24), (SX + 24, SY + 28, SZ + 24)), flat=False)
START = (SX, SY, SZ)
TASK = load("beacon")
FMT = lambda text: TASK._fmt(text, PLOT, START)        # noqa: E731
BEACON = (SX + 6, SY + 1, SZ + 4)                      # where the synthetic beacon stands, in the yard
PAYMENT = {"iron_ingot", "gold_ingot", "emerald", "diamond", "netherite_ingot"}
NINE = {"iron_ingot", "gold_ingot", "emerald", "diamond", "raw_iron", "raw_gold"}   # nine make a block (raw: smelted)
BASE = {"iron_block", "gold_block", "emerald_block", "diamond_block", "netherite_block"}


def _all():
    return [TASK.instance("varied", i) for i in range(24)] + [TASK.instance("heldout", i, KEY) for i in range(24)] + [TASK]


def _chest(cmds, what: str) -> dict[str, int]:
    c = next(c for c in cmds if "minecraft:chest" in c and what in c)
    return {item: int(n) for item, n in re.findall(r'id:\\?"minecraft:([a-z_]+)\\?",count:(\d+)', c)}


# ------------------------------------------------------------------ the task

def test_the_clock_runs_so_a_beacon_can_light():
    w = TASK.world
    assert w.type == "survival" and w.seeds == [1] and w.daylight and w.radius == 24
    assert TASK.start.time == 0 and TASK.max_seconds <= 600       # ten minutes of daylight from dawn
    assert TASK.anchor == (24, 12, 24)                            # {find:beacon} measures from the spawn
    assert not stops_on_pass(TASK)                                # the functional checks read the live world


@pytest.mark.parametrize("inst", _all(), ids=lambda t: f"{t.split}-{t.param_values['vault']['a']}")
def test_every_vault_is_nine_blocks_and_one_payment_over_and_the_furnace_can_do_it(inst):
    cmds = inst.render_setup(PLOT, START, "player")
    assert not any(re.search(r"\{[a-z]{2}([+-]\d+)?\}|\$\{", c) for c in cmds), "an unrendered placeholder"
    vault = _chest(cmds, "copper_block")
    blocks = sum(n for item, n in vault.items() if item in BASE)
    blocks += sum(n // 9 for item, n in vault.items() if item in NINE)
    spare = {item.replace("raw_", "") + ("_ingot" if item.startswith("raw_") else ""): n % 9
             for item, n in vault.items() if item in NINE and n % 9}
    assert blocks == 9 and sum(spare.values()) == 1 and set(spare) <= PAYMENT, vault
    assert vault["copper_block"] == 4                             # no use for a beacon
    tools = _chest(cmds, "diamond_pickaxe")
    smelt = 5 + sum(n for item, n in vault.items() if item.startswith("raw_"))
    assert tools["coal"] * 8 >= smelt and tools.get("water_bucket", 0) + tools.get("bucket", 0) == 1
    assert {"crying_obsidian", "light_blue_stained_glass", "iron_pickaxe"} <= set(tools)
    assert "nether_star" not in str(inst.inventory) and "obsidian" not in inst.inventory


@pytest.mark.parametrize("inst", _all(), ids=lambda t: f"{t.split}-{t.param_values['star']['ender']}")
def test_the_star_is_in_one_place_and_the_prompt_says_which(inst):
    cmds = inst.render_setup(PLOT, START, "player")
    frame = next(c for c in cmds if "item_frame" in c)
    ender = next(c for c in cmds if c.startswith("item replace entity player enderchest"))
    assert ("nether_star" in frame) != ("nether_star" in ender)
    prompt = " ".join(inst.render(PLOT, START).split())
    assert ("ender chest" in prompt) == ("nether_star" in ender)
    assert f"set its power to {inst.param_values['power']['words']}" in prompt
    powered = next(m["check"] for m in inst.grader["steps"] if m["name"] == "powered")
    assert f'minecraft:{inst.param_values["power"]["id"]}' in powered["steps"][0]["assert"]


def test_the_yard_the_pond_and_the_lava_are_in_the_plot():
    for c in TASK.render_setup(PLOT, START, "player"):
        if c.startswith(("fill ", "setblock ")):
            v = [int(n) for n in c.split()[1:7 if c.startswith("fill") else 4]]
            for p in (v[:3], v[3:] or v[:3]):
                assert PLOT.volume.contains(tuple(p)), c


def test_the_goal_file_says_what_each_check_needs():
    goal = build_goal(TASK, PLOT, START, load_settings(), 0, seed=1, fast_nights=True, grader_stops=False,
                      max_seconds=600, max_cost=5)
    says = {u["name"]: u["says"] for u in goal["check"]["uncovered"]}
    assert "open sky" in says["lit"] and "Haste" in says["powered"]
    assert not any("{" in s for s in says.values())
    # what an agent can score: the beacon lit, its power, and the house kept (the furnace and table may move)
    checks = {u["name"]: u.get("check") for u in goal["check"]["uncovered"]}
    assert checks["lit"] == {"kind": "beacon", "min_levels": 1}
    assert checks["powered"] == {"kind": "beacon", "primary": "haste"}
    assert checks["house_intact"] == {"kind": "intact", "of": "setup", "exclude": ["furnace", "crafting_table"]}
    speed = TASK.instance("varied", next(i for i in range(24) if TASK.instance("varied", i).param_values["power"]["id"] == "speed"))
    goal = build_goal(speed, PLOT, START, load_settings(), 0, seed=1, fast_nights=True, grader_stops=False,
                      max_seconds=600, max_cost=5)
    assert {u["name"]: u.get("check") for u in goal["check"]["uncovered"]}["powered"] == {"kind": "beacon", "primary": "speed"}


# ------------------------------------------------------------------ a synthetic base

def _house() -> dict:
    """What the setup places that is not ground: the house, its door, glass, torches, chests and workshop."""
    b = {}
    for x in range(SX - 3, SX + 4):
        for z in range(SZ - 11, SZ - 4):
            b[(x, SY - 1, z)] = b[(x, SY + 3, z)] = "spruce_planks"
            if x in (SX - 3, SX + 3) or z in (SZ - 11, SZ - 5):
                for y in range(SY, SY + 3):
                    b[(x, y, z)] = "spruce_planks"
    b[(SX, SY, SZ - 5)] = b[(SX, SY + 1, SZ - 5)] = "spruce_door"
    for p in [(SX - 2, SY + 1, SZ - 5), (SX + 2, SY + 1, SZ - 5), (SX - 3, SY + 1, SZ - 7), (SX + 3, SY + 1, SZ - 7)]:
        b[p] = "glass"
    for p in [(SX - 1, SY + 1, SZ - 4), (SX + 1, SY + 1, SZ - 4), (SX - 2, SY + 1, SZ - 9), (SX + 2, SY + 1, SZ - 9)]:
        b[p] = "wall_torch"
    b[(SX - 2, SY, SZ - 10)] = b[(SX + 2, SY, SZ - 10)] = "chest"
    b[(SX, SY, SZ - 10)] = "ender_chest"
    b[(SX + 2, SY, SZ - 7)] = "crafting_table"
    b[(SX - 2, SY, SZ - 7)] = "furnace"
    return b


def _setup() -> dict:
    s = _house()
    for x in range(SX + 10, SX + 13):
        for z in (SZ - 3, SZ - 2):
            s[(x, SY - 1, z)] = "lava"
    for x in range(SX - 15, SX - 7):
        for z in range(SZ + 4, SZ + 12):
            s[(x, SY - 1, z)] = "sand"
    return s


def _world(beacon=True, base="iron_block"):
    w = {(x, SY - 1, z): "grass_block" for x in range(SX - 16, SX + 17) for z in range(SZ - 16, SZ + 17)}
    w.update(_setup())
    for x in range(SX + 10, SX + 12):
        w[(x, SY - 1, SZ - 3)] = "air"                # obsidian made and mined
    if beacon:
        for dx in (-1, 0, 1):
            for dz in (-1, 0, 1):
                w[(BEACON[0] + dx, SY, BEACON[2] + dz)] = base
        w[BEACON] = "beacon"
    return w


class Server:
    """The beacon as the server reads it: its Levels (0 unless the base is all metal) and its primary power."""

    def __init__(self, after, effect="haste", sky=True):
        self.after, self.effect, self.sky = after, effect, sky

    def __call__(self, cmd):
        m = re.match(r"execute if block (-?\d+) (-?\d+) (-?\d+) minecraft:beacon unless data block \S+ \S+ \S+ \{Levels:0\}", cmd)
        if m:
            p = tuple(int(v) for v in m.groups())
            base = [self.after.get((p[0] + dx, p[1] - 1, p[2] + dz)) for dx in (-1, 0, 1) for dz in (-1, 0, 1)]
            lit = self.after.get(p) == "beacon" and all(b in BASE for b in base) and self.sky
            return "Test passed" if lit else "Test failed"
        m = re.match(r'execute if data block (-?\d+) (-?\d+) (-?\d+) \{primary_effect:"minecraft:([a-z_]+)"\}', cmd)
        if m:
            p = tuple(int(v) for v in m.groups()[:3])
            return "Test passed" if self.after.get(p) == "beacon" and m.group(4) == self.effect else "Test failed"
        return "ok"


def _ctx(after, *, effect="haste", sky=True, broke=(), deaths=0, stats=None):
    before = _world(beacon=False)
    stats = {"deaths": deaths, "mined:obsidian": 3, "crafted:glass": 5, "crafted:beacon": 1, **(stats or {})}
    frames = [Frame("step_01", after, (SX, SY, SZ), [], t=300.0, stats=stats)]
    return Context(before, after, diff(before, after), PLOT.volume, None, (SX, SY, SZ), [], frames, stats, 20.0, 9000,
                   (SX, SY, SZ), rcon=Server(after, effect, sky), fmt=FMT, seconds=500.0, setup=_setup(), start=START,
                   broke=list(broke))


@pytest.fixture(autouse=True)
def _no_waiting(monkeypatch):
    monkeypatch.setattr("mcmsbench.graders.mechanical.time.sleep", lambda s: None)


def test_a_lit_beacon_set_to_haste_with_the_house_whole_passes():
    r = grade(TASK.grader, _ctx(_world()))
    assert r.passed and r.score == 1.0, r.detail["steps"]


def test_nothing_built_fails_but_keeps_the_house():
    r = grade(TASK.grader, _ctx(_world(beacon=False), stats={"crafted:beacon": 0}))
    assert not r.passed and not r.checks["lit"] and not r.checks["powered"] and r.checks["house_intact"]


def test_a_copper_base_or_no_sky_is_not_lit():
    assert not grade(TASK.grader, _ctx(_world(base="copper_block"))).checks["lit"]
    r = grade(TASK.grader, _ctx(_world(), sky=False))
    assert not r.checks["lit"] and not r.passed


def test_lit_but_left_on_speed_or_unset_fails_powered():
    for effect in ("speed", "none"):
        r = grade(TASK.grader, _ctx(_world(), effect=effect))
        assert r.checks["lit"] and not r.checks["powered"] and not r.passed


def test_a_window_broken_for_glass_or_the_ender_chest_mined_for_obsidian_breaks_the_house():
    after = _world()
    del after[(SX - 3, SY + 1, SZ - 7)]
    r = grade(TASK.grader, _ctx(after, broke=[(60.0, SX - 3, SY + 1, SZ - 7, "glass", "dig")]))
    assert not r.checks["house_intact"] and not r.passed
    after = _world()
    del after[(SX, SY, SZ - 10)]
    r = grade(TASK.grader, _ctx(after, broke=[(90.0, SX, SY, SZ - 10, "ender_chest", "dig")]))
    assert not r.checks["house_intact"] and r.detail["steps"]["house_intact"]["lost_by_block"] == {"ender_chest": 1}


def test_the_furnace_and_table_may_be_moved_and_the_lava_and_sand_used():
    after = _world()
    del after[(SX - 2, SY, SZ - 7)], after[(SX + 2, SY, SZ - 7)]
    after[(SX + 3, SY, SZ + 3)], after[(SX + 4, SY, SZ + 3)] = "furnace", "crafting_table"
    for x in range(SX - 15, SX - 10):
        after[(x, SY - 1, SZ + 4)] = "air"            # five sand dug
    after[(SX + 12, SY - 1, SZ - 2)] = "obsidian"     # the rest of the pool turned
    assert grade(TASK.grader, _ctx(after, broke=[(30.0, SX - 2, SY, SZ - 7, "furnace", "dig")])).passed


def test_a_death_fails():
    assert not grade(TASK.grader, _ctx(_world(), deaths=1)).passed
