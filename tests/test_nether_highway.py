"""nether_highway: a delivery to a base about 800 blocks west, through the Nether. The task's terms, the portals at
home, on the Nether side and at the far base (shape, and that each pair links), the far base as setup builds it, the
chunks setup needs loaded in each dimension, the grader against a fake server, and Nether chunks loaded for setup
(`area_loaded` in a dimension). No server needed."""
import math
import re

from mcmsbench.arena import Plot
from mcmsbench.config import load as load_settings
from mcmsbench.graders import Context, Frame, grade
from mcmsbench.runner import area_loaded, build_goal, stops_on_pass
from mcmsbench.tasks import FILL_LIMIT, load
from mcmsbench.world.volume import Volume

SX, SY, SZ = -1, 63, 5             # seed 2's spawn (infra/worlds/2.json)
NX, NZ = SX // 8, SZ // 8
PLOT = Plot(0, (SX, SY - 1, SZ), Volume((SX - 48, SY - 12, SZ - 48), (SX + 48, SY + 28, SZ + 48)), flat=False)
TASK = load("nether_highway")
START = (SX, SY, SZ)
FMT = lambda text: TASK._fmt(text, PLOT, START).replace("{bot}", "player")     # noqa: E731
SETUP = TASK.render_setup(PLOT, START, "player")
NETHER = "execute in minecraft:the_nether run "
CHEST = (SX - 800, SY + 14, SZ - 7)
OW, NE = "minecraft:overworld", "minecraft:the_nether"


def _fills(prefix: str, block: str) -> list[list[int]]:
    out = []
    for c in SETUP:
        if c.startswith(NETHER) != (prefix == NETHER):
            continue
        p = c.removeprefix(NETHER).split()
        if p[0] == "fill" and p[7].startswith(f"minecraft:{block}"):
            out.append([int(v) for v in p[1:7]])
    return out


def _portal(prefix: str) -> tuple[list[int], list[int]]:
    [frame], [inside] = _fills(prefix, "obsidian"), _fills(prefix, "nether_portal")
    return frame, inside


# ------------------------------------------------------------------ the task

def test_the_terms():
    w = TASK.world
    assert w.type == "survival" and w.seeds == [2] and w.difficulty == "normal" and w.border == 2000
    assert TASK.inventory["iron_ingot"] == 24 and TASK.inventory["obsidian"] >= 10 and "flint_and_steel" in TASK.inventory
    prompt = TASK.render(PLOT, START)
    assert "x -808, z 0" in prompt and "Nether" in prompt and "800" in prompt
    assert stops_on_pass(TASK) and not TASK.scripted


def test_setup_fits_the_fill_limit():
    for c in SETUP:
        p = c.removeprefix(NETHER).split()
        if p[0] == "fill":
            v = [int(t) for t in p[1:7]]
            assert (abs(v[3] - v[0]) + 1) * (abs(v[4] - v[1]) + 1) * (abs(v[5] - v[2]) + 1) <= FILL_LIMIT, c


def test_each_portal_is_a_frame_round_a_lit_two_by_three():
    for prefix in ("", NETHER):
        frames = [f for f in _fills(prefix, "obsidian")]
        insides = _fills(prefix, "nether_portal")
        for (fx0, fy0, fz0, fx1, fy1, fz1), (ix0, iy0, iz0, ix1, iy1, iz1) in zip(frames, insides):
            flat = "x" if fz0 == fz1 else "z"
            assert (fz0 == fz1) or (fx0 == fx1)
            w0, w1, i0, i1 = (fx0, fx1, ix0, ix1) if flat == "x" else (fz0, fz1, iz0, iz1)
            assert w1 - w0 == 3 and fy1 - fy0 == 4                      # 4 wide, 5 high
            assert (i0, i1) == (w0 + 1, w1 - 1) and (iy0, iy1) == (fy0 + 1, fy1 - 1)
    assert len(_fills("", "obsidian")) == 2 and len(_fills(NETHER, "obsidian")) == 1


def test_the_home_portal_links_to_the_nether_pad():
    (_, _, _, _, _, _), (ix0, iy0, iz0, ix1, _, _) = _portal_at_home()
    nether_frame, nether_inside = _portal(NETHER)
    # the game looks for a portal within 16 (Nether) of the eighth-scaled point
    hx, hz = (ix0 + ix1 + 1) / 2 / 8, (iz0 + 0.5) / 8
    nx, nz = (nether_inside[0] + nether_inside[3] + 1) / 2, nether_inside[2] + 0.5
    assert math.hypot(hx - nx, hz - nz) < 16
    assert f"{NETHER}fill {NX - 3} 52 {NZ - 2} {NX + 4} 52 {NZ + 3} minecraft:netherrack" in SETUP


def _portal_at_home():
    frames = _fills("", "obsidian")
    insides = _fills("", "nether_portal")
    i = next(i for i, f in enumerate(frames) if abs(f[0] - SX) < 20)
    return frames[i], insides[i]


def test_the_base_portal_is_the_overworld_side_of_the_exit_mark():
    frames, insides = _fills("", "obsidian"), _fills("", "nether_portal")
    i = next(i for i, f in enumerate(frames) if f[0] < -700)
    ix, iz = insides[i][0] + 0.5, (insides[i][2] + insides[i][5] + 1) / 2
    ex, ez = (int(v) for v in FMT("{nx-100} {nz}").split())
    assert (ex, ez) == (-101, 0)
    assert math.hypot(ix / 8 - ex, iz / 8 - ez) < 1.5          # the mark is the base portal, an eighth of the way
    # (a Nether portal within the grader's 16 of the mark links to it: the game looks 128 round its eighth-scaled point)


def test_the_far_base_is_inside_its_loaded_area_and_the_nether_pad_inside_its_own():
    x0, z0, x1, z1 = TASK.render_load_area(PLOT, START)
    n0, m0, n1, m1 = TASK.render_load_nether(PLOT, START)
    for c in SETUP:
        nether = c.startswith(NETHER)
        p = c.removeprefix(NETHER).split()
        if p[0] not in ("fill", "setblock"):
            continue
        xs, zs = ([int(p[1]), int(p[4])], [int(p[3]), int(p[6])]) if p[0] == "fill" else ([int(p[1])], [int(p[3])])
        if nether:
            assert all(n0 <= x <= n1 for x in xs) and all(m0 <= z <= m1 for z in zs), c
        elif min(xs) < -700:
            assert all(x0 <= x <= x1 for x in xs) and all(z0 <= z <= z1 for z in zs), c
        else:
            lo, hi = PLOT.volume.min, PLOT.volume.max
            assert all(lo[0] <= x <= hi[0] for x in xs) and all(lo[2] <= z <= hi[2] for z in zs), c
    # the base is within the overworld border (2000 across, round the spawn)
    assert x0 > SX - 1000


def test_the_chest_is_by_the_bed_inside_the_hut_and_the_door_faces_the_portal():
    blocks = {}
    for c in SETUP:
        p = c.split()
        if p[0] == "setblock" and int(p[1]) < -700:
            blocks[tuple(int(v) for v in p[1:4])] = re.sub(r"[\[{].*$", "", p[4]).removeprefix("minecraft:")
    assert blocks[CHEST] == "chest"
    beds = [pos for pos, b in blocks.items() if b == "white_bed"]
    assert any(abs(pos[0] - CHEST[0]) + abs(pos[2] - CHEST[2]) == 1 for pos in beds)
    door = next(pos for pos, b in blocks.items() if b == "oak_door")
    frames = _fills("", "obsidian")
    base_portal_x = next(f[0] for f in frames if f[0] < -700)
    assert base_portal_x < door[0]                             # the door is on the portal's side of the hut


# ------------------------------------------------------------------ the grader against a fake server

def _snbt(held: dict) -> str:
    items = ", ".join(f'{{Slot: {i}b, count: {n}, id: "minecraft:{k}"}}' for i, (k, n) in enumerate(held.items()))
    return f"{CHEST[0]} {CHEST[1]} {CHEST[2]} has the following block data: [{items}]"


class Server:
    def __init__(self, held):
        self.held = held

    def __call__(self, cmd):
        if cmd.startswith("forceload query"):
            return "Chunk at [0, 0] in minecraft:overworld is not marked for force loading"
        if cmd == f"data get block {CHEST[0]} {CHEST[1]} {CHEST[2]} Items":
            return "The target block is not a block entity" if self.held is None else _snbt(self.held)
        return "ok"


# home, through the portal onto the pad, west along the lowland, up onto the shelf, out by the base's portal
TRIP = [(2.0, SX - 4, SY, SZ + 2), (15.0, 0.5, 53, 1.5), (40.0, -40.5, 38, 2.5), (70.0, -79.5, 39, 3.5),
        (90.0, -100.5, 55, 2.5), (92.0, -101.5, 55, 3.5), (96.0, -811.5, 77, 0.5), (110.0, -806.5, 77, 0.5)]
DIMS = [(2.0, OW), (15.0, NE), (96.0, OW)]


def _ctx(held=None, *, track=TRIP, dims=DIMS, deaths=0, at=(-807, 77, 0)):
    held = {"iron_ingot": 24} if held is None else held
    stats = {"deaths": deaths, "damage_taken": 0, "dimension:the_nether": 0, "dimension:overworld": 1}
    frames = [Frame("t0030", {}, (-40, 38, 2), [], t=40.0, stats={**stats, "dimension:the_nether": 1,
                                                                    "dimension:overworld": 0})]
    return Context({}, {}, None, PLOT.volume, None, at, [], frames, stats, 20.0, 6000, START,
                   rcon=Server(held), fmt=FMT, track=track, dims=dims, seconds=track[-1][0], start=START)


def test_the_whole_trip_passes():
    r = grade(TASK.grader, _ctx())
    assert r.passed and r.score == 1.0, r.detail["steps"]


def test_walking_the_800_blocks_fails_the_nether_leg():
    walk = [(2.0, SX, SY, SZ)] + [(10.0 * i, SX - 100 * i, 70, 0) for i in range(1, 9)] + [(95.0, -806.5, 77, 0.5)]
    r = grade(TASK.grader, _ctx(track=walk, dims=[(2.0, OW)]))
    assert not r.passed and not r.checks["nether_leg"] and r.checks["delivered"] and r.checks["arrived"]


def test_out_by_a_portal_off_the_mark_still_passes_but_loses_the_exit():
    off = TRIP[:4] + [(90.0, -125.5, 60, 20.5), (110.0, -1000.5, 70, 160.5), (300.0, -806.5, 77, 0.5)]
    r = grade(TASK.grader, _ctx(track=off, dims=[(2.0, OW), (15.0, NE), (110.0, OW)]))
    assert r.passed and not r.checks["exit_near"] and r.score < 1.0


def test_turning_back_or_dying_fails():
    back = TRIP[:3] + [(60.0, 0.5, 53, 1.5), (64.0, SX - 4, SY, SZ + 2)]
    r = grade(TASK.grader, _ctx(track=back, dims=[(2.0, OW), (15.0, NE), (64.0, OW)], at=(SX - 4, SY, SZ + 2)))
    assert not r.checks["nether_leg"]                           # 41 blocks out and back
    r = grade(TASK.grader, _ctx({}, track=back, dims=[(2.0, OW), (15.0, NE), (64.0, OW)], at=(SX - 4, SY, SZ + 2)))
    assert not r.passed and not r.checks["delivered"]
    assert not grade(TASK.grader, _ctx(deaths=1)).passed


def test_the_goal_file_says_where_the_iron_goes_and_what_the_leg_is():
    goal = build_goal(TASK, PLOT, START, load_settings(), 0, seed=2, fast_nights=False, grader_stops=True,
                      max_seconds=1800, max_cost=5)
    unc = {u["name"]: u for u in goal["check"]["uncovered"]}
    assert unc["delivered"]["check"] == {"kind": "container", "at": list(CHEST), "items": {"iron_ingot": 24}}
    assert "80 blocks across the Nether" in unc["nether_leg"]["says"]
    assert unc["nether_leg"]["check"] == {"kind": "leg", "dimension": "the_nether", "min_travel": 80.0}


# ------------------------------------------------------------------ Nether chunks loaded for setup

def test_area_loaded_in_the_nether_prefixes_every_command_and_leaves_the_plot_alone():
    sent = []

    def rcon(cmd):
        sent.append(cmd)
        return "Test passed" if "if loaded" in cmd else "ok"
    with area_loaded(rcon, (-4, -2, 3, 3), PLOT, dimension=NE, log=lambda *a: None):
        sent.append("SETUP")
    before, after = sent[:sent.index("SETUP")], sent[sent.index("SETUP") + 1:]
    assert before and all(c.startswith("execute in minecraft:the_nether ") for c in before + after)
    assert any("forceload add" in c for c in before) and all("forceload remove" in c for c in after)
    sent.clear()
    with area_loaded(rcon, (-4, -2, 3, 3), PLOT, log=lambda *a: None):
        pass
    assert sent[-1].startswith("forceload add ") and not any(c.startswith("execute in") for c in sent)
