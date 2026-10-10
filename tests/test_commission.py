"""commission: one late-game item, named and nothing more, made from what is round a base, with its key ingredient
taken partway through. The task's terms, the world (the same in every instance, every target's tree sourced twice: once
before the disruption and once after), the disruption (its trigger, what it takes and spends, Sam's line after it),
the prompt and grader agreeing on the target, and the grader on a synthetic base. No server needed."""
import re

import pytest

from mcmsbench.arena import Arena
from mcmsbench.config import ArenaConfig, load as load_settings
from mcmsbench.graders import Context, Frame, grade
from mcmsbench.runner import build_goal
from mcmsbench.tasks import Say, load
from mcmsbench.world.volume import diff

from test_variants import KEY

TASK = load("commission")
PLOT = Arena(ArenaConfig(), lambda cmd: "").plot(0, size=TASK.world.size)
START = PLOT.center()
SX, SY, SZ = START
FMT = lambda text: TASK._fmt(text, PLOT, START)        # noqa: E731
CHEST = (SX + 3, SY, SZ - 7)
STOREROOM = (SX - 3, SY, SZ - 11)
TARGETS = [c["id"] for c in TASK.params["target"]["choices"]]


def _all():
    return [TASK.instance("varied", i) for i in range(24)] + [TASK.instance("heldout", i, KEY) for i in range(24)] + [TASK]


def _cells(cmds) -> dict:
    """What the setup's fills and setblocks place, cell by cell (the last word wins), properties dropped."""
    out = {}
    for c in cmds:
        w = c.split()
        if w[0] == "fill":
            x0, y0, z0, x1, y1, z1 = (int(v) for v in w[1:7])
            block = w[7].split("[")[0].split("{")[0].removeprefix("minecraft:")
            for x in range(min(x0, x1), max(x0, x1) + 1):
                for y in range(min(y0, y1), max(y0, y1) + 1):
                    for z in range(min(z0, z1), max(z0, z1) + 1):
                        out[(x, y, z)] = block
        elif w[0] == "setblock":
            out[tuple(int(v) for v in w[1:4])] = w[4].split("[")[0].split("{")[0].removeprefix("minecraft:")
    return out


def _held(cmds, at) -> dict[str, int]:
    c = next(c for c in cmds if c.startswith(f"setblock {at[0]} {at[1]} {at[2]} "))
    return {item: int(n) for item, n in re.findall(r'id:\\?"minecraft:([a-z_]+)\\?",count:(\d+)', c)}


SETUP = TASK.render_setup(PLOT, START, "player")
CELLS = _cells(SETUP)
COUNT = lambda block: sum(1 for b in CELLS.values() if b == block)       # noqa: E731
STORE, TOOLS = _held(SETUP, STOREROOM), _held(SETUP, (SX + 3, SY, SZ - 11))
BARREL = _held(SETUP, (SX + 2, SY, SZ + 19))


# ------------------------------------------------------------------ the task

def test_the_flat_arena_with_sam_about():
    assert TASK.world.type == "flat" and TASK.world.size == 48 and TASK.max_seconds <= 600 and not TASK.inventory
    assert [p["name"] for p in TASK.players] == ["Sam"]
    [(name, at, mode)] = TASK.render_players(PLOT, START)
    assert mode == "adventure" and CELLS.get(at) is None                 # on open ground
    assert set(TARGETS) == {"enchanting_table", "ender_chest", "respawn_anchor", "spyglass", "end_crystal",
                            "netherite_sword", "recovery_compass"}


def test_every_instance_has_the_same_world():
    assert not any(re.search(r"\{[a-z]{2}([+-]\d+)?\}|\$\{", c) for c in SETUP), "an unrendered placeholder"
    for inst in _all():
        assert inst.render_setup(PLOT, START, "player") == SETUP


def test_the_world_is_in_the_plot_and_below_it_only_the_floor_the_reset_restores():
    lo, hi = PLOT.volume.min, PLOT.volume.max
    for (x, y, z) in CELLS:
        assert lo[0] <= x <= hi[0] and lo[2] <= z <= hi[2] and PLOT.floor_y <= y <= hi[1], (x, y, z)


def test_every_target_can_be_made_before_and_after_its_ingredient_is_taken():
    # the tools, the workshop and the fuel
    assert {"diamond_pickaxe", "iron_pickaxe", "iron_shovel", "water_bucket"} <= set(TOOLS) and TOOLS["coal"] >= 2
    assert COUNT("crafting_table") == COUNT("furnace") == 1 and COUNT("oak_log") >= 1
    # the first sources, and the second once Sam has the first (what he took from the bot is gone too)
    lava_near = sum(1 for (x, y, z), b in CELLS.items() if b == "lava" and x < SX + 15)
    lava_far = COUNT("lava") - lava_near
    portal_obsidian = COUNT("obsidian")
    crying_near = sum(1 for (x, y, z), b in CELLS.items() if b == "crying_obsidian" and z < SZ)
    debris_near = sum(1 for (x, y, z), b in CELLS.items() if b == "ancient_debris" and x > SX)
    clusters = sorted(p for p, b in CELLS.items() if b == "amethyst_cluster")
    # enchanting table: 2 diamonds (storeroom, then the outcrop's ore), a book, 4 obsidian
    assert STORE["diamond"] == 2 and COUNT("diamond_ore") >= 2
    assert COUNT("sugar_cane") >= 3 and STORE["leather"] == 1 and lava_near >= 4
    # ender chest: 8 obsidian (the near pool, then the far one and the old portal's plain obsidian), an eye
    assert lava_near >= 8 and lava_far + portal_obsidian >= 8 and STORE["ender_pearl"] == 1 and STORE["blaze_rod"] == 1
    # respawn anchor: 6 crying obsidian (the west portal, then the south one), 12 glowstone dust (2-4 a block)
    assert crying_near >= 6 and COUNT("crying_obsidian") - crying_near >= 6 and COUNT("glowstone") * 2 >= 12
    # spyglass: 2 copper (raw, smelted), a shard (the near cluster, then the far one)
    assert STORE["raw_copper"] == 2 and len(clusters) == 2
    # end crystal: 7 glass (the beach), an eye, a ghast tear (the storeroom, then the barrel)
    assert COUNT("sand") >= 7 and STORE["ghast_tear"] == 1 and BARREL["ghast_tear"] == 1
    # netherite sword: 4 debris (the near pile, then the far one), 4 gold each time, the sword and template, 2 iron
    assert debris_near == 4 and COUNT("ancient_debris") - debris_near >= 4 and STORE["gold_ingot"] >= 8
    assert STORE["diamond_sword"] == 1 and STORE["netherite_upgrade_smithing_template"] == 1 and STORE["iron_ingot"] >= 2
    # recovery compass: 8 echo shards (the storeroom, then the barrel), a compass (4 iron, redstone)
    assert STORE["echo_shard"] == 8 and BARREL["echo_shard"] == 8 and STORE["iron_ingot"] >= 4 and COUNT("redstone_ore") >= 1
    # none is there made
    assert not set(TARGETS) & (set(STORE) | set(TOOLS) | set(BARREL) | set(CELLS.values()))


@pytest.mark.parametrize("inst", _all(), ids=lambda t: f"{t.split}-{t.param_values['target']['id']}-{t.param_values['after']}")
def test_the_disruption_takes_the_ingredient_and_spends_its_first_source(inst):
    v = inst.param_values["target"]
    events = inst.render_events(PLOT, START, "player")
    clock, gated = events[:2], [e for e in events if len(e) == 3]
    assert clock[0][0] == clock[1][0] == 0 and "#t0" in clock[1][1]
    [(at, first, when)] = gated
    assert at == inst.param_values["after"] >= 45
    for k in (v["k1"], v["k2"], v["k3"]):
        assert any(f'id:"minecraft:{k}"' in w for w in when)               # carried: any form of it
    assert any("matches 3000.." in w for w in when) and when[-1] == "execute if score #k bench_cm matches 1.."
    after = [cmd for t, cmd, *_ in events[events.index(gated[0]):] if t == at]
    runs = [c for c in after if isinstance(c, str)]
    assert runs[:3] == [f"clear player minecraft:{k}" for k in (v["k1"], v["k2"], v["k3"])]
    # what is spent held the ingredient's first source at the start
    for gone in runs[3:]:
        if gone.startswith("item replace block"):
            x, y, z, slot = re.match(r"item replace block (-?\d+) (-?\d+) (-?\d+) container\.(\d+)", gone).groups()
            assert (int(x), int(y), int(z)) == STOREROOM
            assert f'Slot:{slot}b,id:"minecraft:{v["k1"]}"' in next(c for c in SETUP if c.startswith(f"setblock {x} {y} {z} "))
        elif gone.startswith("setblock"):                                  # the near cluster, taken
            assert CELLS[tuple(int(n) for n in gone.split()[1:4])] == "amethyst_cluster" and gone.endswith("minecraft:air")
        else:                                                              # a fill over the first source, replacing it
            box = [int(n) for n in gone.split()[1:7]]
            source = gone.split()[-1].removeprefix("minecraft:")
            inside = [b for (x, y, z), b in CELLS.items()
                      if box[0] <= x <= box[3] and box[1] <= y <= box[4] and box[2] <= z <= box[5]]
            assert " replace " in gone and (source in inside or (source, "lava") == ("obsidian", inside[0]))
            assert source in ("lava", "obsidian", "crying_obsidian", "ancient_debris")
    # Sam says what he took after he took it
    assert isinstance(after[-1], Say) and after[-1].player == "Sam" and after[-1].text == v["say"]


@pytest.mark.parametrize("inst", _all(), ids=lambda t: f"{t.split}-{t.param_values['target']['id']}")
def test_the_prompt_and_the_grader_ask_for_the_same_item(inst):
    v = inst.param_values["target"]
    assert f"I need {v['words']}." in " ".join(inst.render(PLOT, START).split())
    steps = {m["name"]: m["check"] for m in inst.grader["steps"]}
    # made: the item's crafted counter, but a smithing table's upgrade moves none (three delivered swords graded unmade)
    made = "custom:interact_with_smithing_table" if v["id"] == "netherite_sword" else f"crafted:{v['id']}"
    assert steps["delivered"]["items"] == {v["id"]: 1} and steps["made"]["name"] == v["made"] == made
    assert steps["second_source"]["name"] in inst.stats and made in inst.stats


def test_the_goal_file_says_which_chest_and_what():
    goal = build_goal(TASK, PLOT, START, load_settings(), 0, seed=None, fast_nights=False, grader_stops=True,
                      max_seconds=600, max_cost=5)
    parts = {p["name"]: p["check"] for p in goal["check"].get("parts", [])}
    parts.update({u["name"]: u.get("check") for u in goal["check"].get("uncovered", [])})
    assert parts["delivered"]["at"] == list(CHEST) and parts["delivered"]["items"] == {"enchanting_table": 1}


# ------------------------------------------------------------------ a synthetic base

class Server:
    """The commission chest as the container grader reads it."""

    def __init__(self, items):
        self.items = items

    def __call__(self, cmd):
        if cmd.startswith(f"data get block {CHEST[0]} {CHEST[1]} {CHEST[2]} Items"):
            inner = ", ".join(f'{{count: 1, Slot: {i}b, id: "minecraft:{n}"}}' for i, n in enumerate(self.items))
            return f"{CHEST[0]}, {CHEST[1]}, {CHEST[2]} has the following block data: [{inner}]"
        return "Chunk at ... is marked for force loading" if cmd.startswith("forceload query") else "ok"


def _world():
    """What the setup placed as the bench's snapshot sees it: the plot's air space, from the floor up (the floor layer's
    pond, pools and portal ground are the reset's, not the record's)."""
    return {p: b for p, b in CELLS.items() if p[1] >= SY}


def _ctx(after, items=("enchanting_table",), *, broke=(), deaths=0, stats=None):
    before = _world()
    stats = stats or {"deaths": deaths, "crafted:enchanting_table": 1, "mined:diamond_ore": 2}
    frames = [Frame("step_01", after, START, [], t=300.0, stats=stats)]
    return Context(before, after, diff(before, after), PLOT.volume, PLOT.floor_y, START, [], frames, stats, 20.0, 6000,
                   START, rcon=Server(list(items)), fmt=FMT, seconds=500.0, setup=_world(), start=START, broke=list(broke))


def test_the_table_in_the_chest_with_the_hut_whole_passes():
    r = grade(TASK.grader, _ctx(_world()))
    assert r.passed and r.score == 1.0, r.detail["steps"]


def test_an_empty_chest_or_the_wrong_item_fails():
    for items in ((), ("enchanting_table_but_not",), ("bookshelf",)):
        r = grade(TASK.grader, _ctx(_world(), items))
        assert not r.checks["delivered"] and not r.passed


def test_the_sources_may_be_used_but_the_hut_may_not():
    after = _world()
    for p, b in CELLS.items():
        if b in ("crying_obsidian", "ancient_debris", "amethyst_cluster", "glowstone", "obsidian", "diamond_ore"):
            after.pop(p, None)
    assert grade(TASK.grader, _ctx(after)).passed
    after = _world()
    del after[(SX - 4, SY + 1, SZ - 9)]
    r = grade(TASK.grader, _ctx(after, broke=[(60.0, SX - 4, SY + 1, SZ - 9, "glass", "dig")]))
    assert not r.checks["hut_intact"] and not r.passed


def test_a_sword_upgraded_at_a_smithing_table_is_made_without_a_crafted_count():
    sword = next(TASK.instance("varied", i) for i in range(24)
                 if TASK.instance("varied", i).param_values["target"]["id"] == "netherite_sword")
    stats = {"deaths": 0, "custom:interact_with_smithing_table": 1, "mined:ancient_debris": 8}
    r = grade(sword.grader, _ctx(_world(), ("netherite_sword",), stats=stats))
    assert r.passed and r.checks["made"] and r.score == 1.0, r.detail["steps"]


def test_a_death_fails():
    assert not grade(TASK.grader, _ctx(_world(), deaths=1)).passed
