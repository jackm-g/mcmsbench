"""multi_room_house: a 9x9 two-story house with two rooms a floor, a door between them, an inside staircase, windows
on both floors, and the walk up and back out. The reference build passes every step; each way of getting it wrong
fails the step that is about it. Synthetic snapshots on the flat arena's plot: no server needed."""
from mcmsbench.arena import Arena
from mcmsbench.config import ArenaConfig
from mcmsbench.config import load as load_settings
from mcmsbench.graders import Context, Frame, grade
from mcmsbench.graders.structural import doors_between, find_stories, rooms, shell_box
from mcmsbench.runner import build_goal
from mcmsbench.tasks import load
from mcmsbench.world.volume import diff

TASK = load("multi_room_house")
PLOT = Arena(ArenaConfig(), lambda cmd: "").plot(0)
FLOOR = PLOT.floor_y
OX, OY, OZ = (PLOT.volume.min[i] + TASK.anchor[i] for i in range(3))
STAIRS = [(1, k, 2 + k) for k in range(4)]          # along the west wall, climbing south, the top one in the floor
WELL = {(1, 3, 2), (1, 3, 3), (1, 3, 4)}            # the opening over the steps below the top one


def _at(x, y, z):
    return OX + x, OY + y, OZ + z


def house(*, ground_door=True, upper_door=True, windows=True, ladder=False, balcony=False, buried_glass=False):
    """The reference build, local (x, y, z) from the anchor: walls y 0-6 (the second floor's slab in row 3), the roof
    at 7. Ground floor: a partition at x=4 with a door at z=4, the front door at (2, 0, 0). Second floor: a partition at
    x=5 with a door at z=4. Windows: two panes on each floor's east and north/south walls."""
    w: dict = {}
    for y in range(7):
        for x in range(9):
            for z in range(9):
                if x in (0, 8) or z in (0, 8):
                    w[(x, y, z)] = "oak_planks"
    for x in range(9):
        for z in range(9):
            w[(x, 7, z)] = "oak_planks"
            if 0 < x < 8 and 0 < z < 8 and (x, 3, z) not in WELL:
                w[(x, 3, z)] = "oak_planks"
    for y in range(3):
        for z in range(1, 8):
            w[(4, y, z)] = "oak_planks"
        for z in range(1, 8):
            w[(5, 4 + y, z)] = "oak_planks"
    for x, y, z in [(4, 0, 4), (4, 1, 4)]:          # ground floor's inside door, or a gap
        if ground_door:
            w[(x, y, z)] = "oak_door"
        else:
            del w[(x, y, z)]
    for x, y, z in [(5, 4, 4), (5, 5, 4)]:          # second floor's inside door, or a gap
        if upper_door:
            w[(x, y, z)] = "oak_door"
        else:
            del w[(x, y, z)]
    w[(2, 0, 0)] = w[(2, 1, 0)] = "oak_door"        # the front door
    if ladder:                                       # a ladder up the west wall instead of steps, through the floor
        for y in range(4):
            w[(1, y, 1)] = "ladder"
        for p in WELL | {(1, 3, 5)}:
            w[p] = "oak_planks"
    else:
        for p in STAIRS:
            w[p] = "oak_stairs"
    if windows:
        for p in [(8, 1, 2), (8, 1, 6), (8, 5, 2), (3, 5, 8)]:
            w[p] = "glass_pane"
    if buried_glass:                                 # glass as wall material, behind the partition: no window
        w[(4, 1, 2)] = "glass"
    if balcony:
        w[(3, 4, 8)] = w[(3, 5, 8)] = "oak_door"
    snap = {_at(*p): b for p, b in w.items()}
    states = {_at(*p): {"facing": "south", "half": "bottom"} for p in STAIRS if not ladder}
    return snap, states


UPSTAIRS = _at(3, 4, 3)
OUTSIDE = _at(2, 0, -3)


def ctx(snap, states, *, track=None, pos=OUTSIDE, at_built=UPSTAIRS):
    frames = [Frame("built", snap, at_built, [], t=600.0)]
    track = [(610.0, *UPSTAIRS), (640.0, *_at(1, 1, 3)), (660.0, *_at(2, 0, 1)), (670.0, *OUTSIDE)] if track is None else track
    return Context({}, snap, diff({}, snap), PLOT.volume, FLOOR, pos, [], frames, {}, 20.0, 6000, PLOT.center(),
                   after_states=states, track=track, seconds=700.0)


def run(**kw):
    walk = ("track", "pos", "at_built")
    snap, states = house(**{k: v for k, v in kw.items() if k not in walk})
    return grade(TASK.grader, ctx(snap, states, **{k: v for k, v in kw.items() if k in walk}))


# ------------------------------------------------------------------ the reference build

def test_the_reference_house_passes_every_step():
    r = run()
    assert r.passed and r.score == 1.0, r.detail["steps"]


def test_the_house_reads_as_two_stories_of_two_rooms():
    snap, _ = house()
    _, box = shell_box(snap, FLOOR)
    assert (box.max[0] - box.min[0] + 1, box.max[2] - box.min[2] + 1) == (9, 9)
    found = find_stories(snap, box, FLOOR, 12)
    assert [s["y"] - OY for s in found] == [0, 4]
    for s in found:
        groups = rooms(s["cells"], 9)
        assert len(groups) == 2 and all(len(g) >= 9 for g in groups)
        assert len(doors_between(snap, groups, s["y"])) == 1


# ------------------------------------------------------------------ what each step catches

def test_an_open_gap_in_the_partition_is_one_room():
    r = run(ground_door=False)
    assert not r.passed and not r.checks["rooms"]
    assert r.detail["steps"]["rooms"]["checks"] == {"story_1_rooms": False, "story_2_rooms": True, "story_2_doors": True}


def test_a_gap_upstairs_fails_too():
    r = run(upper_door=False)
    assert not r.passed and not r.checks["rooms"]


def test_no_windows_fails():
    r = run(windows=False)
    assert not r.passed and not r.checks["windows"]


def test_glass_inside_a_wall_is_not_a_window():
    snap, states = house(windows=False, buried_glass=True)
    r = grade({"kind": "windows", "per_story": {1: 1}, "min_area": 12}, ctx(snap, states))
    assert not r.passed and r.detail["story_1"]["panes"] == 0


def test_touching_panes_are_one_window():
    snap, states = house(windows=False)
    snap[_at(8, 1, 2)] = snap[_at(8, 1, 3)] = snap[_at(8, 2, 3)] = "glass_pane"
    r = grade({"kind": "windows", "per_story": {1: 2}, "min_area": 12}, ctx(snap, states))
    assert not r.passed and r.detail["story_1"] == {"windows": 1, "panes": 3, "need": 2}


def test_a_ladder_is_not_a_staircase():
    r = run(ladder=True)
    assert not r.passed and not r.checks["staircase"]


def test_a_door_out_of_the_second_floor_fails_the_walk():
    r = run(balcony=True)
    assert not r.passed and not r.checks["walked_out"]


def test_never_going_upstairs_fails_the_walk():
    r = run(at_built=_at(2, 0, 2), track=[(660.0, *_at(2, 0, 1)), (670.0, *OUTSIDE)])
    assert not r.passed and not r.checks["walked_out"]


def test_ending_inside_fails_the_walk():
    r = run(pos=_at(2, 0, 2))
    assert not r.passed and not r.checks["walked_out"]


# ------------------------------------------------------------------ the task and the goal file

def test_the_terms():
    assert TASK.world.type == "flat" and not TASK.scripted
    assert set(TASK.grader["required"]) == {"shell", "staircase", "rooms", "windows", "walked_out"}
    held = TASK.inventory
    assert held["oak_stairs"] >= 4 and held["oak_door"] >= 3 and held["glass_pane"] >= 4
    snap, _ = house()
    assert sum(1 for b in snap.values() if b == "oak_planks") <= held["oak_planks"]
    assert PLOT.volume.contains(_at(0, 0, 0)) and PLOT.volume.contains(_at(8, 7, 8))


def test_the_goal_file_says_what_rooms_and_windows_mean():
    goal = build_goal(TASK, PLOT, (OX, OY, OZ), load_settings(), 0, seed=0, fast_nights=False, grader_stops=False,
                      max_seconds=2400, max_cost=1.0)
    says = {u["name"]: u["says"] for u in goal["check"]["uncovered"]}
    assert "door in the wall between them" in says["rooms"] and "glass windows" in says["windows"]
    assert "exit_route" in {u["kind"] for u in goal["check"]["uncovered"]}
