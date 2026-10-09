"""The `leg` grader (a stretch of the trip in one dimension, from the position track and the dimension record), the
tracker's dimension record, and position checks that skip samples in another dimension. No server needed."""
from mcmsbench.graders import Context, grade
from mcmsbench.world.volume import Volume

VOL = Volume((-48, 50, -48), (48, 90, 48))
OW, NE = "minecraft:overworld", "minecraft:the_nether"


def _ctx(track, dims, fmt=None, pos=None):
    return Context({}, {}, None, VOL, None, pos, [], [], {}, 20.0, 6000, (0, 63, 0), track=track, dims=dims,
                   fmt=fmt or (lambda text: text), seconds=track[-1][0] if track else 0.0)


# overworld at the portal, 100 blocks east through the Nether, out near x 120, then the overworld far away
TRIP = [(0.0, 2, 63, 0), (4.0, 3, 63, 0),
        (6.0, 0.5, 70, 0.5), (30.0, 40, 72, 3), (60.0, 90, 68, 4), (80.0, 118, 66, 6),
        (82.0, 950, 70, 47), (100.0, 958, 70, 44)]
DIMS = [(0.0, OW), (6.0, NE), (82.0, OW)]


def test_a_leg_far_enough_that_left_near_its_mark_passes():
    r = grade({"kind": "leg", "min_travel": 80, "exit_near": "120 5", "tolerance": 16}, _ctx(TRIP, DIMS))
    assert r.passed and r.score == 1.0
    [lg] = r.detail["legs"]
    assert lg["from"] == [0.5, 70, 0.5] and lg["left_at"] == [118, 66, 6] and lg["travel"] > 117 and lg["exit_off"] < 3


def test_too_short_or_out_by_the_wrong_portal_fails():
    r = grade({"kind": "leg", "min_travel": 150}, _ctx(TRIP, DIMS))
    assert not r.passed and not r.checks["travelled"] and 0.7 < r.score < 0.8
    r = grade({"kind": "leg", "min_travel": 80, "exit_near": "40 -60", "tolerance": 16}, _ctx(TRIP, DIMS))
    assert not r.passed and r.checks == {"travelled": True, "exit_near": False} and r.score == 0.5


def test_still_in_the_nether_at_the_end_has_travelled_but_not_left():
    r = grade({"kind": "leg", "min_travel": 80, "exit_near": "120 5"}, _ctx(TRIP[:6], DIMS[:2]))
    assert not r.passed and r.checks["travelled"] and not r.checks["exit_near"]
    assert r.detail["legs"][0]["left_at"] is None


def test_the_best_of_several_legs_counts():
    track = [(0.0, 0, 63, 0), (2.0, 0, 70, 0), (4.0, 5, 70, 0), (6.0, 0, 63, 0), (8.0, 0, 70, 0), (40.0, 100, 70, 0),
             (42.0, 800, 70, 0)]
    dims = [(0.0, OW), (2.0, NE), (6.0, OW), (8.0, NE), (42.0, OW)]
    r = grade({"kind": "leg", "min_travel": 80, "exit_near": "100 0"}, _ctx(track, dims))
    assert r.passed and len(r.detail["legs"]) == 2 and r.detail["legs"][0]["travel"] == 5.0


def test_no_dimension_record_is_said_not_guessed():
    r = grade({"kind": "leg", "min_travel": 10}, _ctx(TRIP, []))
    assert not r.passed and r.checks == {"tracked": False}


def test_exit_near_is_templated():
    r = grade({"kind": "leg", "min_travel": 80, "exit_near": "{nx+120} {nz+5}"},
              _ctx(TRIP, DIMS, fmt=lambda text: text.replace("{nx+120}", "120").replace("{nz+5}", "5")))
    assert r.passed


def test_a_position_check_ignores_samples_in_another_dimension():
    # the Nether sample at (90, 68, 4) has the numbers of an overworld target at (90, 68, 4): it is not there
    steps = {"kind": "milestones", "steps": [{"name": "there", "check": {"kind": "position", "target_abs": [90, 68, 4],
                                                                          "tolerance": 3, "y_tolerance": 3}}]}
    assert not grade(steps, _ctx(TRIP, DIMS, pos=(958, 70, 44))).passed
    assert grade(steps, _ctx(TRIP, [], pos=(958, 70, 44))).passed          # no record: as before
    nether = {"kind": "milestones", "steps": [{"name": "there", "check": {"kind": "position", "target_abs": [90, 68, 4],
                                                                           "tolerance": 3, "y_tolerance": 3,
                                                                           "dimension": "the_nether"}}]}
    assert grade(nether, _ctx(TRIP, DIMS, pos=(958, 70, 44))).passed


def test_the_tracker_keeps_each_change_of_dimension():
    import time

    from mcmsbench.runner import PositionTracker
    where = {"dim": OW, "n": 0}

    class Fake:
        def __call__(self, cmd):
            if cmd.endswith(" Pos"):
                where["n"] += 1
                if where["n"] == 3:
                    where["dim"] = NE
                return "player has the following entity data: [1.0d, 64.0d, 2.0d]"
            return f'player has the following entity data: "{where["dim"]}"'

        def close(self):
            pass
    t = PositionTracker.__new__(PositionTracker)
    t.rcon, t.player, t.t0, t.every = Fake(), "player", time.time(), 0.02
    t.track, t.dims = [], []
    import threading
    t._stop = threading.Event()
    t._thread = threading.Thread(target=t._run, daemon=True)
    t._thread.start()
    time.sleep(0.2)
    t.stop()
    assert [d for _, d in t.dims] == [OW, NE] and len(t.track) >= 4
