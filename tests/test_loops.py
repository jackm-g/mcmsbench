"""Loops (loops.py): steps the agent repeated, the longest stretch with no milestone, time off a flat plot; carried in
the trial record, the run's summary and the compare page, beside the score. And the fence: a world border round a flat
overworld plot for its trial (arena.py), so a plot's edge is the world's. No server needed."""
import json

from mcmsbench import loops
from mcmsbench.arena import Arena
from mcmsbench.config import ArenaConfig
from mcmsbench.runner import TrialRecord, summarize

# the commission's netherite trial of 2026-10-10, as jev reported its steps (abridged): the debris found again, then
# stone and wood it did not need, dug for into bedrock and walked for off the plot until the clock ran out
NETHERITE = [("subgoal_completed", "Mine the ancient debris 10 blocks south"),
             ("subgoal_failed", "Look in the barrel 17 blocks south for a netherite sword"),
             ("subgoal_completed", "Mine the ancient debris 41 blocks north-west"),
             ("subgoal_completed", "Explore north"),
             ("subgoal_failed", "Dig down to stone here"), ("subgoal_failed", "Dig down to stone here"),
             ("subgoal_failed", "Mine the stone 61 blocks south"),
             ("subgoal_failed", "Dig down to stone here"),
             ("subgoal_completed", "Explore north"),
             ("subgoal_failed", "Dig down to stone here"), ("subgoal_failed", "Dig down to stone here"),
             ("subgoal_failed", "Mine the stone 57 blocks south"),
             ("subgoal_completed", "Explore east"), ("subgoal_failed", "Dig down to stone here"),
             ("subgoal_completed", "Explore north-east"), ("subgoal_failed", "Explore west")]
GRADER = {"kind": "milestones", "required": ["delivered"],
          "steps": [{"name": "made", "check": {}}, {"name": "second_source", "check": {}}, {"name": "delivered", "check": {}},
                    {"name": "alive", "hold": True, "check": {}}]}


def _steps(pairs):
    return [{"t": float(i * 10), "event": e, "subgoal": s, "text": ""} for i, (e, s) in enumerate(pairs)]


def _result(**at):
    return {"passed": False, "score": 0.3, "detail": {"reached_at_seconds": {"made": None, "second_source": None,
                                                                            "delivered": None, "alive": 18.0, **at}}}


def test_a_step_is_its_words_without_numbers_or_directions():
    k = loops.step_key
    assert k("Explore north") == k("Explore north-east") == k("Explore west") == "explore ~"
    assert k("Mine the stone 61 blocks south") == k("Mine the stone 57 blocks south-east") == "mine the stone # blocks ~"
    assert k("Dig down to stone here") == k("Dig down to stone right here")
    assert k("Mine 3 obsidian") != k("Mine 3 crying obsidian")
    assert k("Look in the chest 12 blocks north for a diamond") != k("Look in the barrel 17 blocks south for a diamond")


def test_the_netherite_trial_looped_and_its_passing_kind_did_not():
    r = loops.repeats(_steps(NETHERITE))
    tops = {t["step"]: t["n"] for t in r["top"]}
    assert r["loops"] == 2 and tops == {"Dig down to stone here": 6, "Explore north": 5}
    assert r["repeated_steps"] == (6 - 2) + (5 - 2)
    assert r["repeated_failures"] == (6 - 1) + (2 - 1)       # the digs, and the stone it walked for twice
    clean = loops.repeats(_steps([("subgoal_completed", "Look in the chest 7 blocks north-west for a diamond"),
                                  ("subgoal_completed", "Mine the diamond ore 24 blocks south-east"),
                                  ("subgoal_failed", "Mine the obsidian 14 blocks north"),
                                  ("subgoal_completed", "Mine the obsidian 3 blocks east"),
                                  ("subgoal_completed", "Craft a enchanting table")]))
    assert clean["loops"] == clean["repeated_steps"] == clean["repeated_failures"] == 0   # a retry, not a loop


def test_quiet_is_the_longest_stretch_with_no_milestone_and_holds_are_not_progress():
    q = loops.quiet(_result(second_source=82.6), GRADER, 604.9)
    assert q == {"quiet_s": 522.3, "quiet_at": [82.6, 604.9], "milestones_reached": 1}
    q = loops.quiet(_result(second_source=60.0, made=95.0, delivered=97.0), GRADER, 97.0)
    assert q["quiet_s"] == 60.0 and q["milestones_reached"] == 3
    assert loops.quiet(_result(), GRADER, 300.0)["quiet_s"] == 300.0          # nothing reached: all of it
    assert loops.quiet(_result(), {"kind": "structure"}, 300.0) is None       # no ladder, nothing to time by


def test_off_plot_counts_the_track_outside_a_flat_plot_only():
    plot = {"flat": True, "volume": {"min": [0, -60, 0], "max": [47, -37, 47]}}
    track = [(0.0, 20, -60, 20), (2.0, 20, -60, 60), (4.0, 20, -60, 90), (6.0, 20, -60, 30), (8.0, 20, -60, 30)]
    assert loops.off_plot(track, plot) == 4.0
    assert loops.off_plot(track, {**plot, "flat": False}) is None             # terrain: the ground runs past the plot


def test_a_record_written_before_the_trace_kept_steps_is_measured_from_its_log(tmp_path):
    log = tmp_path / "trial_0.jsonl"
    lines = [{"t": 1000.0, "kind": "trial_start"}] + [
        {"t": 1000.0 + s["t"], "kind": "agent_event", "event": s["event"], "subgoal": s["subgoal"], "text": ""}
        for s in _steps(NETHERITE)]
    log.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    rec = tmp_path / "trial_0.json"
    rec.write_text(json.dumps({"task": "commission", "seconds": 604.9, "trace": {"track": []},
                               "result": _result(second_source=82.6), "plot": None}))
    out = loops.of_record(rec)
    assert out["loops"] == 2 and out["quiet_s"] == 522.3                      # the task file names the hold steps
    rec.write_text(json.dumps({"task": "commission", "seconds": 1, "trace": {"loops": {"loops": 9}}}))
    assert loops.of_record(rec) == {"loops": 9}                               # one it carries is kept


def test_the_summary_carries_loops_beside_the_score():
    rs = []
    for i, (n, q) in enumerate([(4, 522.4), (0, 84.6)]):
        r = TrialRecord("commission", i, "jev", "now", seconds=300.0, result={"passed": n == 0, "score": 1.0 if n == 0 else 0.3})
        r.trace = {"loops": {"loops": n, "quiet_s": q}}
        rs.append(r)
    row = summarize(rs)["rows"][0]
    assert row["loops"] == 2.0 and row["quiet_s"] == 303.5 and row["pass_rate"] == 0.5


# ------------------------------------------------------------------ the fence

def test_a_flat_overworld_plot_is_fenced_for_its_trial_and_unfenced_after():
    sent = []
    arena = Arena(ArenaConfig(), lambda cmd: sent.append(cmd) or "ok")
    plot = arena.plot(3, size=48)
    assert plot.fence == 8 and arena.fence(plot) == "ok"
    (x0, _, z0), (x1, _, z1) = plot.volume.min, plot.volume.max
    assert sent == [f"worldborder center {(x0 + x1 + 1) / 2} {(z0 + z1 + 1) / 2}", "worldborder set 64"]
    assert f"x={x0 - 8}..{x1 + 8}" in plot.describe() and "world border" in plot.describe()
    sent.clear()
    arena.unfence()
    assert sent == [f"worldborder set {Arena.UNFENCED}", "worldborder center 0 0"]


def test_a_nether_room_and_a_fence_of_nought_are_not_fenced():
    sent = []
    arena = Arena(ArenaConfig(), lambda cmd: sent.append(cmd) or "ok")
    nether = arena.plot(0, "the_nether")
    assert nether.fence == 0 and arena.fence(nether) is None and "world border" not in nether.describe()
    open_ = Arena(ArenaConfig(fence=0), lambda cmd: sent.append(cmd) or "ok").plot(0)
    assert open_.fence == 0 and arena.fence(open_) is None and not sent


def test_the_goal_file_says_where_the_border_is():
    from mcmsbench.config import load as load_settings
    from mcmsbench.runner import build_goal
    from mcmsbench.tasks import load
    settings = load_settings()
    task = load("brewing")
    plot = Arena(ArenaConfig(), lambda cmd: "").plot(0)
    goal = build_goal(task, plot, plot.center(), settings, 0, seed=None, fast_nights=False, grader_stops=False,
                      max_seconds=600, max_cost=5)
    (x0, _, z0), (x1, _, z1) = plot.volume.min, plot.volume.max
    assert goal["profile"]["border"] == {"x": (x0 + x1 + 1) // 2, "z": (z0 + z1 + 1) // 2, "radius": 24}   # 32 + 2 x 8
    nether = Arena(ArenaConfig(), lambda cmd: "").plot(0, "the_nether")
    goal = build_goal(load("piglin_barter"), nether, nether.center(), settings, 0, seed=None, fast_nights=False,
                      grader_stops=False, max_seconds=600, max_cost=5)
    assert goal["profile"]["border"] is None
