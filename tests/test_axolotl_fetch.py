"""axolotl_fetch: a lush cave about 200 blocks east through carved tunnels, an axolotl caught in a bucket and put in the
chest by the start. The task's terms (the kit, no X-ray, the furniture and ore, the guards and axolotls), the route
itself (its commands played onto solid stone: walkable from the start to the chamber and every dead end), the goal
file's check, and the grader against a fake server: the `container` grader reading the chest, `target_start` for the
chamber. Also `tunnel_commands` on its own: legs, stairs, the sealed shell, the laid floor. No server needed."""
import re

import pytest

from mcmsbench.arena import Plot
from mcmsbench.config import load as load_settings
from mcmsbench.graders import Context, Frame, grade
from mcmsbench.graders.structural import can_walk
from mcmsbench.runner import build_goal, stops_on_pass
from mcmsbench.start import resolve_start, situation_commands, tunnel_commands
from mcmsbench.tasks import load
from mcmsbench.world.volume import Volume

SX, SY, SZ = 154, 70, 163          # seed 1's spawn (infra/worlds/1.json)
PLOT = Plot(0, (SX, SY - 1, SZ), Volume((SX - 48, SY - 12, SZ - 48), (SX + 48, SY + 28, SZ + 48)), flat=False)
TASK = load("axolotl_fetch")
START = resolve_start(TASK.start, PLOT.center(), TASK.id, 0, 1, None)     # flat-plot resolution: depth below spawn
BX, BY, BZ = START
FMT = lambda text: TASK._fmt(text, PLOT, START)        # noqa: E731
CHEST = (BX, BY, BZ - 3)
CHAMBER = (BX + 195, BY - 6, BZ + 12)                  # a moss floor cell in the chamber, by the pool


# ------------------------------------------------------------------ a world of solid stone, and commands played on it

class Ground(dict):
    """A snapshot where every cell not set is stone: the ground the commands cut into."""

    def get(self, key, default=None):
        return super().get(key, "stone")


def _name(token: str) -> str:
    return re.sub(r"[\[{].*$", "", token).removeprefix("minecraft:")


def play(cmds, world: Ground | None = None) -> Ground:
    """The fills and setblocks among `cmds` applied in order (replace by name or #minecraft:air, hollow)."""
    w = world if world is not None else Ground()
    for c in cmds:
        p = c.split()
        if p[0] == "setblock":
            w[tuple(int(v) for v in p[1:4])] = _name(p[4])
        elif p[0] == "fill":
            x0, y0, z0, x1, y1, z1 = (int(v) for v in p[1:7])
            block, mode = _name(p[7]), p[8:]
            for x in range(min(x0, x1), max(x0, x1) + 1):
                for y in range(min(y0, y1), max(y0, y1) + 1):
                    for z in range(min(z0, z1), max(z0, z1) + 1):
                        here = w.get((x, y, z))
                        if mode and mode[0] == "replace":
                            want = mode[1]
                            if not (here in ("air", "cave_air") if want == "#minecraft:air" else here == _name(want)):
                                continue
                        if mode and mode[0] == "hollow":
                            edge = x in (x0, x1) or y in (y0, y1) or z in (z0, z1)
                            w[(x, y, z)] = block if edge else "air"
                        else:
                            w[(x, y, z)] = block
    return w


def _route_world() -> Ground:
    tunnels, _ = TASK.render_tunnels(PLOT, START)
    return play(situation_commands(TASK.start, START) + tunnels + TASK.render_setup(PLOT, START, "player"))


WORLD = _route_world()
REGION = Volume((BX - 12, BY - 12, BZ - 60), (BX + 212, BY + 4, BZ + 24))


def _summons(kind):
    return [tuple(int(v) for v in c.split()[2:5]) for c in TASK.render_setup(PLOT, START, "player")
            if c.startswith(f"summon minecraft:{kind} ")]


# ------------------------------------------------------------------ the task

def test_the_kit_and_the_terms():
    assert TASK.inventory == {"iron_pickaxe": 1, "iron_sword": 1, "cooked_porkchop": 10, "torch": 32}
    cmds = TASK.render_setup(PLOT, START, "player")
    assert "item replace entity player weapon.offhand with minecraft:shield" in cmds
    assert "item replace entity player armor.chest with minecraft:iron_chestplate" in cmds
    assert TASK.xray is False and TASK.world.type == "survival" and TASK.world.difficulty == "normal"
    assert TASK.start.situation == "cavern" and TASK.start.random_radius > 0
    assert TASK.world.border >= 2 * 240            # the chamber and its dead ends inside the border round spawn
    assert not TASK.scripted and TASK.max_seconds >= 1800
    assert stops_on_pass(TASK)                     # the chest is only read: the trial stops once the axolotl is in it
    assert "east" in TASK.prompt and "200" in TASK.prompt and "chest" in TASK.prompt


def test_setup_renders_with_nothing_left_unfilled():
    tunnels, box = TASK.render_tunnels(PLOT, START)
    for c in tunnels + TASK.render_setup(PLOT, START, "player"):
        assert not re.search(r"\{(bx|by|bz|sx|sy|sz|ax|ay|az|bot)\b", c), c
    x0, z0, x1, z1 = box
    assert x0 <= BX - 1 and x1 >= BX + 192 and z0 <= BZ - 53 and z1 >= BZ + 13


def test_the_table_chest_and_furnace_stand_where_the_grader_looks():
    stored = next(m for m in TASK.grader["steps"] if m["name"] == "stored")
    assert tuple(int(v) for v in FMT(stored["check"]["at"]).split()) == CHEST
    assert WORLD.get(CHEST) == "chest"
    assert WORLD.get((BX - 1, BY, BZ - 3)) == "crafting_table" and WORLD.get((BX + 1, BY, BZ - 3)) == "furnace"


def test_the_route_is_walkable_from_the_start_to_the_chamber_and_every_dead_end():
    assert can_walk(WORLD, {START}, {CHAMBER}, REGION)
    for end in ([34, 0, 0], [50, -2, -52], [74, -2, -12], [120, -3, 12]):
        cell = (BX + end[0], BY + end[1], BZ + end[2])
        assert can_walk(WORLD, {START}, {cell}, REGION), end


def test_the_chamber_is_reached_only_by_the_route():
    sealed = Ground(WORLD)
    for y in range(BY - 6, BY - 3):                # the tunnel's mouth walled up
        for z in range(BZ + 11, BZ + 14):
            sealed[(BX + 191, y, z)] = "stone"
    assert not can_walk(sealed, {START}, {CHAMBER}, REGION)


def test_the_ore_shows_a_face_to_the_route():
    ores = [(p, b) for p, b in WORLD.items() if b.endswith("_ore")]
    assert sorted(b for _, b in ores).count("iron_ore") == 7 and sorted(b for _, b in ores).count("coal_ore") == 3
    for (x, y, z), b in ores:
        faces = [(x + 1, y, z), (x - 1, y, z), (x, y, z + 1), (x, y, z - 1)]
        assert any(WORLD.get(f) == "air" for f in faces), (b, (x - BX, y - BY, z - BZ))
    near = [p for p, b in ores if p[0] - BX <= 30]
    assert len(near) == 7                          # 3 coal and 4 iron in the first tunnel: enough for a bucket


def test_four_axolotls_in_the_pool():
    axolotls = _summons("axolotl")
    assert len(axolotls) == 4
    assert all(WORLD.get(p) == "water" for p in axolotls)
    assert all('Tags:["fetch"]' in c and "PersistenceRequired:1b" in c
               for c in TASK.render_setup(PLOT, START, "player") if "summon minecraft:axolotl" in c)
    pool = [p for p, b in WORLD.items() if b == "water"]
    assert all(WORLD.get((x, y - 1, z)) in ("water", "clay") for x, y, z in pool)   # clay-lined


def test_the_guards_stand_on_the_route_between_the_start_and_the_chamber():
    zombies, skeletons = _summons("zombie"), _summons("skeleton")
    assert len(zombies) == 1 and len(skeletons) == 2
    for p in zombies + skeletons:
        x, y, z = p
        assert WORLD.get(p) == "air" and WORLD.get((x, y + 1, z)) == "air" and WORLD.get((x, y - 1, z)) != "air"
        assert can_walk(WORLD, {START}, {p}, REGION)
        assert 0 < x - BX < 192
    bows = [c for c in TASK.render_setup(PLOT, START, "player") if "summon minecraft:skeleton" in c]
    assert all("minecraft:bow" in c and 'Tags:["fetch_guard"]' in c for c in bows)


def test_the_goal_file_says_what_cannot_be_read_live():
    goal = build_goal(TASK, PLOT, START, load_settings(), 0, seed=1, fast_nights=True, grader_stops=False,
                      max_seconds=2400, max_cost=5)
    check = goal["check"]
    assert {p["name"]: p["check"]["kind"] for p in check["parts"]} == {"alive": "alive"}
    says = {u["name"]: u["says"] for u in check["uncovered"]}
    assert "axolotl in a bucket" in says["stored"] and "chest" in says["stored"]
    assert goal["options"]["xray"] is False


# ------------------------------------------------------------------ the grader against a fake server

AXOLOTL_IN_CHEST = ('{x} {y} {z} has the following block data: [{{Slot: 0b, components: {{"minecraft:bucket_entity_data": '
                    '{{Age: 0, Health: 14.0f, Variant: 2}}}}, count: 1, id: "minecraft:axolotl_bucket"}}]')
WATER_IN_CHEST = '{x} {y} {z} has the following block data: [{{Slot: 4b, count: 1, id: "minecraft:water_bucket"}}]'


class Server:
    """The container grader's server: the chest at CHEST answers with `chest` (an SNBT reply template), or is gone."""

    def __init__(self, chest: str | None, marked: bool = False):
        self.chest, self.marked, self.calls = chest, marked, []

    def __call__(self, cmd):
        self.calls.append(cmd)
        if cmd.startswith("forceload query"):
            return "Chunk at [0, 0] in minecraft:overworld is " + ("marked" if self.marked else "not marked") + \
                " for force loading"
        m = re.match(r"data get block (-?\d+) (-?\d+) (-?\d+) Items", cmd)
        if m:
            x, y, z = (int(v) for v in m.groups())
            if (x, y, z) != CHEST or self.chest is None:
                return f"The target block is not a block entity"
            return self.chest.format(x=x, y=y, z=z)
        return "ok"


def _ctx(*, chest=AXOLOTL_IN_CHEST, deaths=0, track=None, caught=True, stats=None, at=CHAMBER):
    s = {"deaths": deaths, "mined:iron_ore": 4, "crafted:iron_ingot": 3, "crafted:bucket": 1, **(stats or {})}
    inv = [{"name": "axolotl_bucket", "count": 1, "slot": 0}] if caught else []
    frames = [Frame("step_01", {}, at, inv, t=900.0, stats=s)]
    track = [(10.0, BX + 5, BY, BZ), (800.0, BX + 199, BY - 6, BZ + 11), (1500.0, BX + 1, BY, BZ - 2)] \
        if track is None else track
    return Context({}, {}, None, PLOT.volume, None, (BX + 1, BY, BZ - 2), [], frames, s, 20.0, 6000, (SX, SY, SZ),
                   rcon=Server(chest), fmt=FMT, track=track, seconds=1500.0, start=START)


def test_an_axolotl_in_the_chest_passes():
    r = grade(TASK.grader, _ctx())
    assert r.passed and r.score == 1.0, r.detail["steps"]


def test_a_bucket_of_water_in_the_chest_is_not_an_axolotl():
    r = grade(TASK.grader, _ctx(chest=WATER_IN_CHEST))
    assert not r.passed and not r.checks["stored"] and r.checks["reached"] and r.checks["bucket"]


def test_the_chest_broken_fails():
    assert not grade(TASK.grader, _ctx(chest=None)).passed


def test_an_axolotl_carried_home_but_not_put_away_fails():
    r = grade(TASK.grader, _ctx(chest="{x} {y} {z} has the following block data: []"))
    assert not r.passed and r.checks["caught"] and not r.checks["stored"]


def test_a_death_fails():
    assert not grade(TASK.grader, _ctx(deaths=1)).passed


def test_never_reaching_the_chamber_shows_on_the_track():
    r = grade(TASK.grader, _ctx(chest=None, caught=False, track=[(10.0, BX + 60, BY - 2, BZ)],
                                stats={"crafted:bucket": 0}, at=(BX + 60, BY - 2, BZ)))
    assert not r.checks["reached"] and not r.checks["bucket"] and r.checks["iron"] and not r.passed


# ------------------------------------------------------------------ the pieces on their own

def test_container_reads_the_chest_and_leaves_the_plot_loaded():
    spec = {"kind": "container", "at": "{bx} {by} {bz-3}", "item": "axolotl_bucket"}
    ctx = _ctx()
    r = grade(spec, ctx)
    assert r.passed and r.detail["holds"] == {"axolotl_bucket": 1}
    assert any(c.startswith("forceload add") for c in ctx.rcon.calls) and ctx.rcon.calls[-1].startswith("forceload remove")
    ctx.rcon = Server(AXOLOTL_IN_CHEST, marked=True)        # a chunk already held (the plot's): left as it was
    assert grade(spec, ctx).passed and not any(c.startswith("forceload ") and "query" not in c for c in ctx.rcon.calls)
    ctx.rcon = Server(None)
    r = grade(spec, ctx)
    assert not r.passed and r.checks == {"container_found": False, "holds_item": False}
    assert grade({**spec, "item": ["water_bucket", "axolotl_bucket"]}, _ctx(chest=WATER_IN_CHEST)).passed


def test_position_target_start_moves_with_the_start():
    spec = {"kind": "position", "target_start": [200, -6, 12], "tolerance": 9, "y_tolerance": 5}
    ctx = _ctx()
    ctx.bot_position = (BX + 195, BY - 6, BZ + 14)
    assert grade(spec, ctx).passed
    ctx.bot_position = (BX + 150, BY - 3, BZ)
    assert not grade(spec, ctx).passed
    ctx.start = None
    assert not grade(spec, ctx).passed


def test_tunnel_legs_stairs_and_box():
    cmds, box = tunnel_commands([[0, 0, 0], [10, 2, 0], [10, 2, -6]], (100, 50, 200))
    w = play(cmds)
    assert all(w.get((100 + i, 50 + min(i, 2) + h, 200 + dz)) == "air" for i in range(11) for h in range(3)
               for dz in (-1, 0, 1))
    assert w.get((101, 50, 200)) == "stone" and w.get((101, 51, 200)) == "air"   # one up a step
    assert all(w.get((x, 52, 194)) == "air" for x in (109, 110, 111))             # the north leg, 3 across
    assert box == (99, 193, 112, 202)
    assert can_walk(w, {(100, 50, 200)}, {(110, 52, 194)}, Volume((95, 45, 190), (115, 60, 205)))


def test_tunnel_seals_liquids_and_lays_a_floor_but_keeps_a_cave_it_crosses():
    w = Ground()
    w[(105, 50, 202)] = "water"                    # an aquifer beside the leg
    w[(106, 53, 200)] = "lava"                     # lava over it
    w[(108, 52, 198)] = "gravel"                   # a gravel roof
    for y in range(44, 50):                        # a natural cave under the floor, and one off its side
        w[(103, y, 200)] = "air"
    w[(104, 51, 202)] = w[(104, 51, 203)] = "air"     # its mouth in the tunnel's wall
    cmds, _ = tunnel_commands([[0, 0, 0], [10, 0, 0]], (100, 50, 200))
    play(cmds, w)
    assert w.get((105, 50, 202)) == w.get((106, 53, 200)) == w.get((108, 52, 198)) == "stone"
    assert w.get((103, 49, 200)) == "stone" and w.get((103, 48, 200)) == "air"    # floored over, the cave below kept
    assert w.get((104, 51, 202)) == "air"                                          # the side passage stays open


def test_tunnel_refuses_a_diagonal_or_too_steep_leg():
    with pytest.raises(ValueError):
        tunnel_commands([[0, 0, 0], [5, 0, 5]], (0, 0, 0))
    with pytest.raises(ValueError):
        tunnel_commands([[0, 0, 0], [2, 3, 0]], (0, 0, 0))
    with pytest.raises(ValueError):
        tunnel_commands([[0, 0, 0]], (0, 0, 0))
