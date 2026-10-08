"""island_delivery: the dock chest's cargo taken by boat across about 150 blocks of sea to the chest in an island's
lighthouse. The task's terms, the shore, sea, dock and lighthouse as setup builds them (the basin's rim, a walk from the
beach to the chest, too little wood for a chest boat), the drowned, the event that marks a boat ride over the middle of
the sea, the grader against a fake server, and the pieces that are new with it (`load_area`, the container grader's
`items`, the boat and swim counters). No server needed."""
import re

from mcmsbench.arena import Plot
from mcmsbench.config import load as load_settings
from mcmsbench.graders import Context, Frame, grade
from mcmsbench.graders.structural import can_walk
from mcmsbench.runner import build_goal, stops_on_pass
from mcmsbench.stats import criterion_for
from mcmsbench.tasks import FILL_LIMIT, load
from mcmsbench.world.volume import Volume

SX, SY, SZ = 154, 70, 163          # seed 1's spawn (infra/worlds/1.json)
PLOT = Plot(0, (SX, SY - 1, SZ), Volume((SX - 48, SY - 12, SZ - 48), (SX + 48, SY + 28, SZ + 48)), flat=False)
TASK = load("island_delivery")
START = (SX, SY, SZ)
FMT = lambda text: TASK._fmt(text, PLOT, START).replace("{bot}", "player")     # noqa: E731
CHEST = (SX + 151, SY + 2, SZ + 1)
CARGO = {"torch": 32, "bread": 16, "coal": 16}


def _setup():
    return TASK.render_setup(PLOT, START, "player")


def _name(token: str) -> str:
    return re.sub(r"[\[{].*$", "", token).removeprefix("minecraft:")


def _volume(v):
    return (abs(v[3] - v[0]) + 1) * (abs(v[4] - v[1]) + 1) * (abs(v[5] - v[2]) + 1)


def _played(near=(SX + 150, SZ), reach=12) -> dict:
    """The setup played onto an empty world, only within `reach` (x/z) of `near`: the sea's fills clipped to it."""
    w: dict = {}
    for c in _setup():
        p = c.split()
        if p[0] == "setblock":
            pos = tuple(int(v) for v in p[1:4])
            if abs(pos[0] - near[0]) <= reach and abs(pos[2] - near[1]) <= reach:
                w[pos] = _name(p[4])
        elif p[0] == "fill":
            v = [int(t) for t in p[1:7]]
            lo = [max(min(v[0], v[3]), near[0] - reach), min(v[1], v[4]), max(min(v[2], v[5]), near[1] - reach)]
            hi = [min(max(v[0], v[3]), near[0] + reach), max(v[1], v[4]), min(max(v[2], v[5]), near[1] + reach)]
            hollow = "hollow" in p[8:]
            for x in range(lo[0], hi[0] + 1):
                for y in range(lo[1], hi[1] + 1):
                    for z in range(lo[2], hi[2] + 1):
                        edge = x in (v[0], v[3]) or y in (v[1], v[4]) or z in (v[2], v[5])
                        b = _name(p[7]) if (not hollow or edge) else "air"
                        if b == "air":
                            w.pop((x, y, z), None)
                        else:
                            w[(x, y, z)] = b
    return w


ISLAND = _played()
DOCK = _played(near=(SX + 12, SZ), reach=8)


# ------------------------------------------------------------------ the task

def test_the_terms():
    w = TASK.world
    assert w.type == "survival" and w.difficulty == "normal" and not w.daylight and w.border == 400
    assert TASK.inventory == {"bread": 4} and not TASK.scripted
    assert "east" in TASK.prompt and "150" in TASK.prompt and "boat" in TASK.prompt
    assert not stops_on_pass(TASK)                     # the functional assert: read once, at the end


def test_setup_renders_under_the_limit_inside_the_border_and_the_loaded_area():
    x0, z0, x1, z1 = TASK.render_load_area(PLOT, START)
    assert (x0, z0, x1, z1) == (SX - 30, SZ - 81, SX + 180, SZ + 81)
    for c in _setup():
        assert not re.search(r"\{(sx|sy|sz|bx|by|bz|bot)\b", c), c
        p = c.split()
        if p[0] in ("fill", "setblock", "summon"):
            v = [int(t) for t in (p[1:7] if p[0] == "fill" else p[1:4] if p[0] == "setblock" else p[2:5])]
            xs, zs = v[0::3], v[2::3]
            assert x0 <= min(xs) and max(xs) <= x1 and z0 <= min(zs) and max(zs) <= z1, c
            assert all(abs(x - SX) < 200 for x in xs) and all(abs(z - SZ) < 200 for z in zs)
        if p[0] == "fill":
            assert _volume([int(t) for t in p[1:7]]) <= FILL_LIMIT, c


def test_the_sea_is_held_in_by_a_stone_rim_level_with_the_shore():
    fills = [c.split() for c in TASK.setup if c.startswith("fill") and "minecraft:" in c]
    basin = next(f for f in fills if f[7] == "minecraft:stone" and f[1] == "{sx+10}")
    water = next(f for f in fills if f[7] == "minecraft:water")
    fmt = lambda f: [int(v) for v in FMT(" ".join(f[1:7])).split()]     # noqa: E731
    b, wv = fmt(basin), fmt(water)
    assert b[4] == wv[4] == SY - 1                                       # the rim and the water's surface: shore level
    assert b[0] == wv[0] and b[3] == wv[3] + 1 and b[2] == wv[2] - 1 and b[5] == wv[5] + 1 and b[1] < wv[1]


def test_the_dock_reaches_the_water_and_holds_the_cargo():
    assert all(DOCK.get((x, SY - 1, SZ)) == "oak_planks" for x in range(SX + 10, SX + 17))
    assert DOCK.get((SX + 17, SY - 1, SZ)) == "water"
    assert DOCK.get((SX + 16, SY, SZ - 1)) == "chest" and DOCK.get((SX + 16, SY, SZ + 1)) == "crafting_table"
    chest = next(c for c in _setup() if c.startswith(f"setblock {SX + 16} {SY} {SZ - 1} minecraft:chest"))
    held = {m.group(1): int(m.group(2)) for m in re.finditer(r'id:"minecraft:(\w+)",count:(\d+)', chest)}
    assert held == {**CARGO, "oak_log": 3}
    assert 5 <= held["oak_log"] * 4 < 5 + 8            # a boat's planks, not a chest boat's (a chest is 8 more)


def test_a_walk_from_the_beach_through_the_door_to_the_chest():
    assert ISLAND.get(CHEST) == "chest" and ISLAND.get((SX + 149, SY + 2, SZ - 1)) == "torch"
    assert ISLAND.get((SX + 148, SY + 2, SZ)) == "oak_door" and ISLAND.get((SX + 150, SY + 13, SZ)) == "sea_lantern"
    region = Volume((SX + 140, SY - 2, SZ - 8), (SX + 158, SY + 6, SZ + 8))
    assert can_walk(ISLAND, {(SX + 145, SY + 1, SZ)}, {(SX + 150, SY + 2, SZ + 1)}, region)
    shut = dict(ISLAND)
    shut[(SX + 148, SY + 2, SZ)] = shut[(SX + 148, SY + 3, SZ)] = "stone_bricks"
    assert not can_walk(shut, {(SX + 145, SY + 1, SZ)}, {(SX + 150, SY + 2, SZ + 1)}, region)


def test_four_drowned_in_the_water_between_the_shore_and_the_island():
    drowned = [tuple(int(v) for v in c.split()[2:5]) for c in _setup() if c.startswith("summon minecraft:drowned ")]
    assert len(drowned) == 4 and not [c for c in _setup() if c.startswith("summon ") and "drowned" not in c]
    for x, y, z in drowned:
        assert SX + 20 < x < SX + 140 and SY - 10 <= y <= SY - 1
    assert all('Tags:["deep"]' in c and "PersistenceRequired:1b" in c for c in _setup() if c.startswith("summon "))


def test_the_event_scores_a_boat_ride_over_the_middle_of_the_sea():
    [(at, run, when)] = TASK.render_events(PLOT, START, "player")
    # a score on a fake holder, not a tag on the player: graded after the stand-in logs out, no selector finds it
    assert at == 0 and run == "scoreboard players set #sailed bench_sailed 1"
    assert "scoreboard players reset #sailed bench_sailed" in _setup()
    assert when.startswith("execute as player on vehicle if entity @s[type=#minecraft:boat,")
    m = re.search(r"x=(-?\d+),y=(-?\d+),z=(-?\d+),dx=(\d+),dy=(\d+),dz=(\d+)", when)
    x, y, z, dx, dy, dz = (int(v) for v in m.groups())
    assert SX + 30 < x and x + dx < SX + 140                # past the dock, short of the island
    assert y <= SY - 1 <= y + dy and z <= SZ - 60 and z + dz >= SZ + 60


# ------------------------------------------------------------------ the grader against a fake server

def _snbt(held: dict) -> str:
    items = ", ".join(f'{{Slot: {i}b, count: {n}, id: "minecraft:{k}"}}' for i, (k, n) in enumerate(held.items()))
    return f"{CHEST[0]} {CHEST[1]} {CHEST[2]} has the following block data: [{items}]"


class Server:
    """The lighthouse chest (its contents, or gone) and whether the `sailed` score is set."""

    def __init__(self, held: dict | None, sailed=True):
        self.held, self.sailed, self.sent = held, sailed, []

    def __call__(self, cmd):
        self.sent.append(cmd)
        if cmd.startswith("forceload query"):
            return "Chunk at [0, 0] in minecraft:overworld is not marked for force loading"
        if cmd == "execute if score #sailed bench_sailed matches 1":
            return "Test passed. Count: 1" if self.sailed else "Test failed"
        if cmd == f"data get block {CHEST[0]} {CHEST[1]} {CHEST[2]} Items":
            return "The target block is not a block entity" if self.held is None else _snbt(self.held)
        return "ok"


def _ctx(held=CARGO, *, sailed=True, deaths=0, track=None, at=(SX + 150, SY + 2, SZ)):
    stats = {"deaths": deaths, "crafted:oak_boat": 1, "boated": 0, "swum": 0}
    track = [(5.0, SX + 12, SY, SZ), (60.0, SX + 80, SY - 1, SZ + 2), (120.0, SX + 146, SY + 1, SZ)] \
        if track is None else track
    frames = [Frame("step_01", {}, at, [], t=150.0, stats=stats)]
    return Context({}, {}, None, PLOT.volume, None, at, [], frames, stats, 20.0, 6000,
                   (SX, SY, SZ), rcon=Server(held, sailed), fmt=FMT, track=track, seconds=200.0, start=START)


def test_everything_delivered_after_a_sail_passes():
    r = grade(TASK.grader, _ctx())
    assert r.passed and r.score == 1.0, r.detail["steps"]


def test_one_item_short_fails():
    r = grade(TASK.grader, _ctx({**CARGO, "bread": 15}))
    assert not r.passed and not r.checks["delivered"] and r.checks["sailed"]


def test_delivered_by_swimming_fails():
    r = grade(TASK.grader, _ctx(sailed=False))
    assert not r.passed and not r.checks["sailed"] and r.checks["delivered"] and r.checks["landed"]


def test_a_death_fails_and_never_landing_shows_on_the_track():
    assert not grade(TASK.grader, _ctx(deaths=1)).passed
    r = grade(TASK.grader, _ctx(None, sailed=False, track=[(5.0, SX + 12, SY, SZ)], at=(SX + 14, SY, SZ)))
    assert not r.checks["landed"] and not r.checks["delivered"] and r.checks["boat"]


# ------------------------------------------------------------------ the new pieces on their own

def test_container_items_checks_each_and_scores_the_share():
    spec = {"kind": "container", "at": "{sx+151} {sy+2} {sz+1}", "items": CARGO}
    r = grade(spec, _ctx({"torch": 32, "bread": 8, "coal": 16, "dirt": 3}))
    assert not r.passed and r.checks == {"container_found": True, "torch_delivered": True, "bread_delivered": False,
                                         "coal_delivered": True}
    assert abs(r.score - (1 + 0.5 + 1) / 3) < 1e-9
    assert r.detail["have"] == {"torch": 32, "bread": 8, "coal": 16} and r.detail["holds"]["dirt"] == 3
    gone = grade(spec, _ctx(None))
    assert not gone.passed and gone.checks["container_found"] is False and gone.score == 0.0
    assert grade({"kind": "container", "at": "{sx+151} {sy+2} {sz+1}", "item": "torch", "count": 32}, _ctx()).passed


def test_load_area_joins_the_tunnels_box():
    assert TASK.render_load_area(PLOT, START, also=(SX - 100, SZ, SX, SZ + 200)) == (SX - 100, SZ - 81, SX + 180, SZ + 200)
    plain = load("goto_point")
    assert plain.render_load_area(PLOT, START) is None
    assert plain.render_load_area(PLOT, START, also=(1, 2, 3, 4)) == (1, 2, 3, 4)


def test_boat_and_swim_counters_are_in_blocks():
    assert criterion_for("boated") == ("minecraft.custom:minecraft.boat_one_cm", 100)
    assert criterion_for("swum") == ("minecraft.custom:minecraft.swim_one_cm", 100)


def test_the_goal_file_says_what_cannot_be_read_live():
    goal = build_goal(TASK, PLOT, START, load_settings(), 0, seed=1, fast_nights=False, grader_stops=False,
                      max_seconds=1500, max_cost=5)
    check = goal["check"]
    assert {p["name"]: p["check"]["kind"] for p in check["parts"]} == {"alive": "alive"}
    says = {u["name"]: u["says"] for u in check["uncovered"]}
    assert "rode a boat" in says["sailed"] and "32 torches" in says["delivered"]


def test_the_goal_file_says_where_the_cargo_goes_and_what():
    goal = build_goal(TASK, PLOT, START, load_settings(), 0, seed=1, fast_nights=False, grader_stops=False,
                      max_seconds=1500, max_cost=5)
    delivered = next(u for u in goal["check"]["uncovered"] if u["name"] == "delivered")
    assert delivered["check"] == {"kind": "container", "at": list(CHEST), "items": CARGO}
    sailed = next(u for u in goal["check"]["uncovered"] if u["name"] == "sailed")
    assert "check" not in sailed                         # a functional test stays words alone
