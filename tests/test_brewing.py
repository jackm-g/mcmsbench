"""brewing: three potions of one kind, asked for by name, at a brewery with no brewing stand. The task's terms (the flat
arena), the materials (the same in every instance, enough for any of the seven potions and no more glass or rods than
it takes), the prompt and the grader agreeing on the potion and its form, the goal file's agent checks, and the
grader on a synthetic hut with a stand-in server for the potion chest's NBT. No server needed."""
import re

import pytest

from mcmsbench.arena import Arena
from mcmsbench.config import ArenaConfig, load as load_settings
from mcmsbench.graders import Context, Frame, grade
from mcmsbench.runner import build_goal, stops_on_pass
from mcmsbench.tasks import load
from mcmsbench.world.volume import diff

from test_variants import KEY

PLOT = Arena(ArenaConfig(), lambda cmd: "").plot(0)
START = PLOT.center()
SX, SY, SZ = START
TASK = load("brewing")
FMT = lambda text: TASK._fmt(text, PLOT, START)        # noqa: E731
CHEST = (SX + 2, SY, SZ - 6)
# what each potion is brewed from after the awkward potion, as raw materials the world holds (verified by brewing each
# chain on the flat arena, 2026-10-09): the ingredient's sources, then the modifier's
NEEDS = {"long_fire_resistance": {"slime_ball", "blaze_rod", "redstone_ore"},
         "long_night_vision": {"carrot", "gold_ingot", "redstone_ore"},
         "strong_swiftness": {"sugar_cane", "glowstone"},
         "long_water_breathing": {"pufferfish", "redstone_ore"},
         "strong_strength": {"blaze_rod", "glowstone"},
         "strong_healing": {"melon", "gold_ingot", "glowstone"},
         "long_leaping": {"rabbit_foot", "redstone_ore"}}


def _all():
    return [TASK.instance("varied", i) for i in range(24)] + [TASK.instance("heldout", i, KEY) for i in range(24)] + [TASK]


def _items(cmd: str) -> dict[str, int]:
    return {item: int(n) for item, n in re.findall(r'id:\\?"minecraft:([a-z_]+)\\?",count:(\d+)', cmd)}


# ------------------------------------------------------------------ the task

def test_the_flat_arena_and_its_terms():
    assert TASK.world.type == "flat" and TASK.world.difficulty == "peaceful"
    assert TASK.max_seconds <= 600 and not TASK.inventory
    assert not stops_on_pass(TASK)                     # the functional checks run commands on the server
    assert set(TASK.params["potion"]["choices"][i]["id"] for i in range(7)) == set(NEEDS)


def test_every_instance_has_the_same_materials_and_only_the_request_differs():
    public = TASK.render_setup(PLOT, START, "player")
    assert not any(re.search(r"\{[a-z]{2}([+-]\d+)?\}|\$\{", c) for c in public), "an unrendered placeholder"
    for inst in _all():
        assert inst.render_setup(PLOT, START, "player") == public


def test_the_world_holds_every_potions_materials_and_no_spare_glass_or_rods():
    cmds = TASK.render_setup(PLOT, START, "player")
    held: dict[str, int] = {}
    for c in cmds:
        for item, n in _items(c).items():
            held[item] = held.get(item, 0) + n
    placed = {c.split()[-1].split("[")[0].removeprefix("minecraft:") for c in cmds if c.startswith(("fill", "setblock"))}
    world = set(held) | placed
    for potion, needs in NEEDS.items():
        assert needs <= world, (potion, needs - world)
    assert {"nether_wart", "soul_sand", "water", "stone", "gunpowder", "iron_pickaxe"} <= world
    # three bottles from three glass, and two rods: the stand, and two powder (fuel, and magma cream or Strength)
    assert held["glass"] == 3 and held["blaze_rod"] == 2 and held["gold_ingot"] == 1
    assert "glass_bottle" not in held and "brewing_stand" not in world and "blaze_powder" not in held
    # the decoys: a fermented spider eye corrupts, a ghast tear and a membrane brew nothing asked for
    assert {"fermented_spider_eye", "ghast_tear", "phantom_membrane", "spider_eye"} <= set(held)


def test_the_hut_garden_pond_and_outcrop_are_in_the_plot():
    for c in TASK.render_setup(PLOT, START, "player"):
        if c.startswith(("fill ", "setblock ")):
            v = [int(n) for n in c.split()[1:7 if c.startswith("fill") else 4]]
            for p in (v[:3], v[3:] or v[:3]):
                x, y, z = p
                assert PLOT.volume.min[0] <= x <= PLOT.volume.max[0] and PLOT.volume.min[2] <= z <= PLOT.volume.max[2], c
                assert PLOT.floor_y <= y <= PLOT.volume.max[1], c       # the floor layer is the reset's to restore


@pytest.mark.parametrize("inst", _all(), ids=lambda t: f"{t.split}-{t.param_values['potion']['id']}-{t.param_values['form']['item']}")
def test_the_prompt_and_the_grader_ask_for_the_same_potion(inst):
    v = inst.param_values
    prompt = " ".join(inst.render(PLOT, START).split())
    assert f"three {v['form']['words']} of {v['potion']['words']}" in prompt
    steps = {m["name"]: m for m in inst.grader["steps"]}
    filt = steps["delivered"]["check"]["steps"][1]["do"]
    assert f'id:"minecraft:{v["form"]["item"]}"' in filt and f'potion:"minecraft:{v["potion"]["id"]}"' in filt
    assert 'id:"minecraft:' not in steps["right_potion"]["check"]["steps"][1]["do"]      # either form
    assert steps["delivered"]["agent_check"]["items"] == {v["form"]["item"]: 3}
    assert steps["delivered"]["agent_check"]["potion"] == v["potion"]["id"]


def test_the_goal_file_says_where_and_what():
    goal = build_goal(TASK, PLOT, START, load_settings(), 0, seed=None, fast_nights=False, grader_stops=False,
                      max_seconds=600, max_cost=5)
    says = {u["name"]: u["says"] for u in goal["check"]["uncovered"]}
    assert "long 8-minute" in says["delivered"] and not any("{" in s for s in says.values())
    checks = {u["name"]: u.get("check") for u in goal["check"]["uncovered"]}
    assert checks["delivered"] == {"kind": "container", "at": list(CHEST), "items": {"potion": 3},
                                   "potion": "long_fire_resistance"}
    assert checks["hut_intact"]["exclude"] == ["crafting_table", "glowstone", "soul_sand", "nether_wart"]


# ------------------------------------------------------------------ a synthetic hut

def _hut() -> dict:
    b = {}
    for x in range(SX - 3, SX + 4):
        for z in range(SZ - 11, SZ - 4):
            b[(x, SY + 3, z)] = "spruce_planks"
            if x in (SX - 3, SX + 3) or z in (SZ - 11, SZ - 5):
                for y in range(SY, SY + 3):
                    b[(x, y, z)] = "spruce_planks"
    b[(SX, SY, SZ - 5)] = b[(SX, SY + 1, SZ - 5)] = "spruce_door"
    for p in [(SX - 2, SY + 1, SZ - 5), (SX + 2, SY + 1, SZ - 5), (SX - 3, SY + 1, SZ - 8), (SX + 3, SY + 1, SZ - 8)]:
        b[p] = "glass"
    b[(SX - 2, SY, SZ - 10)] = b[(SX + 2, SY, SZ - 10)] = b[CHEST] = "chest"
    b[(SX - 2, SY, SZ - 7)] = "barrel"
    b[(SX + 2, SY, SZ - 8)] = "crafting_table"
    for x in range(SX - 9, SX - 6):
        b[(x, SY, SZ - 9)] = "nether_wart"
    b[(SX + 8, SY + 1, SZ - 3)] = "glowstone"
    return b


class Server:
    """The potion chest's NBT as the functional checks query it: a score set from the matching slots, then tested."""

    def __init__(self, chest):
        self.chest, self.score = chest, 0

    def __call__(self, cmd):
        m = re.match(r"execute store result score #potions bench_potions if data block (-?\d+) (-?\d+) (-?\d+) Items\[\{(.*)\}\]$", cmd)
        if m:
            where = tuple(int(v) for v in m.groups()[:3])
            item = re.search(r'^id:"minecraft:([a-z_]+)"', m.group(4))
            potion = re.search(r'potion:"minecraft:([a-z_]+)"', m.group(4)).group(1)
            self.score = sum(1 for i, p in self.chest if p == potion and (item is None or i == item.group(1))) \
                if where == CHEST else 0
            return f"Test passed. Count: {self.score}" if self.score else "Test failed"
        if cmd == "execute if score #potions bench_potions matches 3..":
            return "Test passed" if self.score >= 3 else "Test failed"
        return "ok"


def _ctx(after, chest, *, broke=(), deaths=0, stats=None):
    before = _hut()
    stats = {"deaths": deaths, "crafted:brewing_stand": 1, "custom:interact_with_brewingstand": 4, **(stats or {})}
    frames = [Frame("step_01", after, START, [], t=300.0, stats=stats)]
    return Context(before, after, diff(before, after), PLOT.volume, PLOT.floor_y, START, [], frames, stats, 20.0, 6000,
                   START, rcon=Server(chest), fmt=FMT, seconds=500.0, setup=_hut(), start=START, broke=list(broke))


THREE = [("potion", "long_fire_resistance")] * 3


def test_three_long_fire_resistance_in_the_chest_pass():
    r = grade(TASK.grader, _ctx(_hut(), THREE))
    assert r.passed and r.score == 1.0, r.detail["steps"]


def test_the_wrong_form_the_short_kind_or_two_fail():
    for chest in ([("splash_potion", "long_fire_resistance")] * 3, [("potion", "fire_resistance")] * 3,
                  THREE[:2] + [("potion", "mundane")], [("potion", "long_night_vision")] * 3):
        r = grade(TASK.grader, _ctx(_hut(), chest))
        assert not r.checks["delivered"] and not r.passed
    assert grade(TASK.grader, _ctx(_hut(), [("splash_potion", "long_fire_resistance")] * 3)).checks["right_potion"]


def test_a_splash_instance_wants_splash():
    inst = next(TASK.instance("varied", i) for i in range(24)
                if TASK.instance("varied", i).param_values["form"]["item"] == "splash_potion")
    pid = inst.param_values["potion"]["id"]
    assert grade(inst.grader, _ctx(_hut(), [("splash_potion", pid)] * 3)).passed
    assert not grade(inst.grader, _ctx(_hut(), [("potion", pid)] * 3)).passed


def test_a_window_broken_for_glass_breaks_the_hut_but_the_garden_and_outcrop_are_for_using():
    after = _hut()
    del after[(SX - 3, SY + 1, SZ - 8)]
    r = grade(TASK.grader, _ctx(after, THREE, broke=[(60.0, SX - 3, SY + 1, SZ - 8, "glass", "dig")]))
    assert not r.checks["hut_intact"] and not r.passed
    after = _hut()
    del after[(SX + 8, SY + 1, SZ - 3)], after[(SX - 8, SY, SZ - 9)], after[(SX + 2, SY, SZ - 8)]
    after[(SX + 1, SY, SZ + 2)] = "crafting_table"
    broke = [(40.0, SX + 8, SY + 1, SZ - 3, "glowstone", "dig"), (50.0, SX - 8, SY, SZ - 9, "nether_wart", "dig")]
    assert grade(TASK.grader, _ctx(after, THREE, broke=broke)).passed


def test_a_death_fails():
    assert not grade(TASK.grader, _ctx(_hut(), THREE, deaths=1)).passed
