"""obsidian_expedition: a pre-built house made the base (the bot's bed in it, its spawn set there), the gear chest packed
from, a ruined portal 220 blocks east mined for obsidian, and the obsidian brought back to the storage chest. The task's
terms (deaths drop everything, the world spawn moved off the base), the house and the portal as setup builds them, the
two events that grade the order (a base before the trip), the grader against a fake server, and the goal file. No server
needed."""
import re

from mcmsbench.arena import Plot
from mcmsbench.config import load as load_settings
from mcmsbench.graders import Context, Frame, grade
from mcmsbench.graders.structural import can_walk
from mcmsbench.runner import build_goal, stops_on_pass
from mcmsbench.tasks import load
from mcmsbench.world.volume import Volume

SX, SY, SZ = 154, 70, 163          # seed 1's spawn (infra/worlds/1.json): the base
PLOT = Plot(0, (SX, SY - 1, SZ), Volume((SX - 48, SY - 12, SZ - 48), (SX + 48, SY + 28, SZ + 48)), flat=False)
TASK = load("obsidian_expedition")
START = (SX, SY, SZ)
FMT = lambda text: TASK._fmt(text, PLOT, START).replace("{bot}", "player")     # noqa: E731
STORAGE = (SX - 2, SY, SZ - 2)
GEAR = (SX + 2, SY, SZ + 2)
BED = [(SX, SY, SZ - 2), (SX - 1, SY, SZ - 2)]          # head and foot along the north wall


def _setup():
    return TASK.render_setup(PLOT, START, "player")


def _name(token: str) -> str:
    return re.sub(r"[\[{].*$", "", token).removeprefix("minecraft:")


def _house() -> dict:
    """The yard and the house as setup leaves them (the absolute fills and setblocks; the portal's are relative)."""
    w: dict = {}
    for c in _setup():
        p = c.split()
        if p[0] == "setblock":
            w[tuple(int(v) for v in p[1:4])] = _name(p[4])
        elif p[0] == "fill":
            v = [int(t) for t in p[1:7]]
            hollow = "hollow" in p[8:]
            for x in range(min(v[0], v[3]), max(v[0], v[3]) + 1):
                for y in range(min(v[1], v[4]), max(v[1], v[4]) + 1):
                    for z in range(min(v[2], v[5]), max(v[2], v[5]) + 1):
                        edge = x in (v[0], v[3]) or y in (v[1], v[4]) or z in (v[2], v[5])
                        b = _name(p[7]) if (not hollow or edge) else "air"
                        if b == "air":
                            w.pop((x, y, z), None)
                        else:
                            w[(x, y, z)] = b
    return w


HOUSE = _house()


# ------------------------------------------------------------------ the task

def test_the_terms():
    w = TASK.world
    assert TASK.profile is None and not TASK.spawn_first      # the bed is the bot's to place, not the harness's
    assert w.type == "survival" and w.difficulty == "normal" and w.daylight and not w.keep_inventory
    assert 12000 - TASK.start.time <= 9600                     # dusk within about 8 minutes
    assert TASK.inventory == {"white_bed": 1} and not TASK.scripted
    assert stops_on_pass(TASK)                                 # nothing acts on the server: graded live
    assert "east" in TASK.prompt and "220" in TASK.prompt and "pickaxe" not in TASK.prompt


def test_setup_renders_inside_the_border_and_the_loaded_area():
    x0, z0, x1, z1 = TASK.render_load_area(PLOT, START)
    for c in _setup():
        assert not re.search(r"\{(sx|sy|sz|bx|by|bz|bot)\b", c), c
        for m in re.finditer(r"positioned (-?\d+) -?\d+ (-?\d+)", c):
            x, z = int(m.group(1)), int(m.group(2))
            assert x0 <= x <= x1 and z0 <= z <= z1 and abs(x - SX) < 300, c
    spawn = next(c for c in _setup() if c.endswith("run setworldspawn ~ ~ ~"))
    x = int(re.search(r"positioned (-?\d+) ", spawn).group(1))
    assert SX - x >= 90                                        # a death without the bed lands far from the base


def test_the_portal_is_fourteen_obsidian_from_one_marker():
    cmds = _setup()
    marker = next(i for i, c in enumerate(cmds) if "summon minecraft:marker" in c)
    kill = cmds.index("kill @e[tag=portal]")
    assert f"positioned {SX + 220} 0 {SZ} positioned over motion_blocking_no_leaves" in cmds[marker]
    rel = [c for c in cmds if c.startswith("execute at @e[tag=portal")]
    assert all(marker < cmds.index(c) < kill for c in rel)
    cells: dict = {}
    for c in rel:
        m = re.search(r"run (fill|setblock) (.*) minecraft:(\w+)", c)
        nums = [int(t.lstrip("~") or 0) for t in m.group(2).split()]
        lo, hi = (nums[:3], nums[3:]) if m.group(1) == "fill" else (nums, nums)
        for x in range(lo[0], hi[0] + 1):
            for y in range(lo[1], hi[1] + 1):
                for z in range(lo[2], hi[2] + 1):
                    cells[(x, y, z)] = m.group(3)
    obsidian = [p for p, b in cells.items() if b == "obsidian"]
    assert len(obsidian) == 14
    stored = next(m for m in TASK.grader["steps"] if m["name"] == "stored")["check"]["items"]["obsidian"]
    assert 8 <= stored < len(obsidian)
    assert all(cells[(x, y, 0)] == "air" for x in (0, 1) for y in (1, 2, 3))     # a frame, open in the middle


def test_the_house_has_its_chests_a_way_out_and_room_for_a_bed():
    assert HOUSE[STORAGE] == "chest" and HOUSE[GEAR] == "chest" and HOUSE[(SX + 2, SY, SZ - 2)] == "crafting_table"
    assert all(p not in HOUSE and (p[0], SY - 1, p[2]) in HOUSE for p in BED)    # free floor along the north wall
    assert HOUSE[(SX, SY, SZ + 3)] == "spruce_door"
    region = Volume((SX - 8, SY - 1, SZ - 8), (SX + 8, SY + 4, SZ + 8))
    assert can_walk(HOUSE, {(SX, SY, SZ)}, {(SX, SY, SZ + 6)}, region)
    stored = next(m for m in TASK.grader["steps"] if m["name"] == "stored")["check"]
    assert tuple(int(v) for v in FMT(stored["at"]).split()) == STORAGE


def test_the_gear_chest_holds_three_pickaxes_and_only_one_mines_obsidian():
    chest = next(c for c in _setup() if c.startswith(f"setblock {GEAR[0]} {GEAR[1]} {GEAR[2]} minecraft:chest"))
    held = {m.group(1): int(m.group(2)) for m in re.finditer(r'id:"minecraft:(\w+)",count:(\d+)', chest)}
    assert {"diamond_pickaxe", "iron_pickaxe", "stone_pickaxe", "iron_sword", "cooked_beef", "torch"} <= set(held)
    assert not [k for k in held if k.startswith(("netherite_", "golden_")) and k.endswith("_pickaxe")]
    assert "diamond_pickaxe" in TASK.stats[2] and "iron_pickaxe" in TASK.stats[3]     # which one did the work


def test_the_events_grade_a_base_before_the_trip():
    [(a0, run0, when0), (a1, run1, when1)] = TASK.render_events(PLOT, START, "player")
    assert when0 == "execute if data entity player respawn" and run0 == "tag player add based"
    assert run1 == "tag player add set_out"
    # graded from the record of what fired, the same command the event ran: not a tag asked of an offline player
    [step] = [m for m in TASK.grader["steps"] if m["name"] == "based_first"]
    assert step["check"]["kind"] == "event" and FMT(step["check"]["run"]) == run1
    m = re.search(r"x=(-?\d+),y=(-?\d+),z=(-?\d+),dx=(\d+),dy=(\d+),dz=(\d+)", when1)
    x, _, z, dx, _, dz = (int(v) for v in m.groups())
    assert x <= SX + 220 <= x + dx and z <= SZ <= z + dz and x - SX > 150


# ------------------------------------------------------------------ the grader against a fake server

def _snbt(at, held: dict) -> str:
    items = ", ".join(f'{{Slot: {i}b, count: {n}, id: "minecraft:{k}"}}' for i, (k, n) in enumerate(held.items()))
    return f"{at[0]} {at[1]} {at[2]} has the following block data: [{items}]"


class Server:
    """The storage chest's contents. The player is offline at grade time: nothing about it can be asked."""

    def __init__(self, stored: dict):
        self.stored = stored

    def __call__(self, cmd):
        if cmd.startswith("forceload query"):
            return "Chunk at [9, 10] in minecraft:overworld is marked for force loading"     # the plot's
        if "@a[" in cmd or "@p[" in cmd:
            return "Test failed"                        # logged out before grading (runner: stand_in.logout())
        if cmd == f"data get block {STORAGE[0]} {STORAGE[1]} {STORAGE[2]} Items":
            return _snbt(STORAGE, self.stored)
        return "ok"


def _ctx(*, stored=None, set_out=True, bed=True, bed_at=None, deaths=0, track=None):
    after = dict(HOUSE)
    for p in bed_at or BED:
        after[p] = "white_bed"
    respawn = {"pos": list((bed_at or BED)[0]), "bed": bed}
    stats = {"deaths": deaths, "used:diamond_pickaxe": 9, "used:iron_pickaxe": 0, "mined:obsidian": 9}
    track = [(10.0, SX, SY, SZ), (200.0, SX + 219, SY + 3, SZ + 1), (420.0, SX + 1, SY, SZ)] if track is None else track
    frames = [Frame("step_01", after, (SX, SY, SZ), [], t=400.0, stats=stats)]
    # the events as the runner records them: `set_out` waits for `based`
    events = [{"at": 0, "t": 4.0, "run": "tag player add based", "out": "Added tag 'based' to player"}]
    if set_out:
        events.append({"at": 0, "t": 65.0, "run": "tag player add set_out", "out": "Added tag 'set_out' to player"})
    return Context(dict(HOUSE), after, None, PLOT.volume, None, (SX, SY, SZ), [], frames, stats, 20.0, 14000,
                   (SX, SY, SZ), rcon=Server({"obsidian": 9} if stored is None else stored), fmt=FMT,
                   track=track, seconds=450.0, respawn=respawn, setup=dict(HOUSE), start=START, events=events)


def _grade(**kw):
    from mcmsbench.world.volume import diff
    ctx = _ctx(**kw)
    ctx.diff = diff(ctx.before, ctx.after)
    return grade(TASK.grader, ctx)


def test_a_planned_trip_passes():
    r = _grade()
    assert r.passed and r.score == 1.0, r.detail["steps"]


def test_obsidian_in_the_wrong_chest_or_short_fails():
    assert not _grade(stored={}).passed                          # left in the gear chest: the storage chest is empty
    r = _grade(stored={"obsidian": 7})
    assert not r.passed and not r.checks["stored"]


def test_a_bed_placed_but_no_spawn_set_fails():
    r = _grade(bed=False)
    assert not r.passed and not r.checks["bed_spawn"]


def test_a_bed_outside_the_house_fails():
    r = _grade(bed_at=[(SX + 6, SY, SZ + 6), (SX + 6, SY, SZ + 7)])
    assert not r.passed and not r.checks["bed_spawn"], r.detail["steps"]["bed_spawn"]


def test_spawn_set_only_after_the_trip_passes_without_the_order():
    r = _grade(set_out=False)
    assert r.passed and not r.checks["based_first"] and r.score < 1.0


def test_a_death_on_the_way_costs_score_not_the_pass():
    r = _grade(deaths=1)
    assert r.passed and not r.checks["alive"] and r.score < 1.0


def test_never_reaching_the_portal_shows_on_the_track():
    r = _grade(stored={}, track=[(10.0, SX, SY, SZ), (100.0, SX + 120, SY + 2, SZ)])
    assert not r.checks["reached"] and not r.passed


# ------------------------------------------------------------------ the goal file

def test_the_goal_file_says_where_the_obsidian_goes():
    goal = build_goal(TASK, PLOT, START, load_settings(), 0, seed=1, fast_nights=True, grader_stops=False,
                      max_seconds=1800, max_cost=5)
    check = goal["check"]
    parts = {p["name"]: p["check"] for p in check["parts"]}
    assert parts["bed_spawn"]["kind"] == "spawn" and parts["bed_spawn"]["inside"] is True      # read live
    uncovered = {u["name"]: u for u in check["uncovered"]}
    assert set(uncovered) == {"stored"}
    assert uncovered["stored"]["check"] == {"kind": "container", "at": list(STORAGE), "items": {"obsidian": 8}}
    assert goal["world"]["keep_inventory"] is False
