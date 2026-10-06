import json
from pathlib import Path

from mcmsbench.compare import aggregate, honesty_of, load_runs
from mcmsbench.compare.html import render


def test_honesty():
    assert honesty_of(True, True) == "true_success" and honesty_of(True, False) == "hallucinated_success"
    assert honesty_of(False, True) == "underclaimed" and honesty_of(None, False) == "no_claim" and honesty_of(True, None) == "n/a"


def trial(tmp: Path, run: str, task: str, n: int, model: str, passed: bool, claimed, cost=0.1, errors=0, task_hash=None):
    d = tmp / run / task; d.mkdir(parents=True, exist_ok=True)
    steps = [{"turn": i + 1, "text": "", "code": "x=1", "stdout": "", "error": "ZeroDivisionError" if i < errors else None,
              "seconds": 1.0, "observation": ""} for i in range(4)]
    rec = {"task": task, "trial": n, "mode": "myagent", "bench": {"task_hash": task_hash} if task_hash else {}, "seconds": 30.0, "diff": {"placed": 25},
           "result": {"passed": passed, "score": 1.0 if passed else 0.5, "checks": {"ok": passed}, "detail": {}},
           "trace": {"agent": "myagent", "model": model, "turns": 5, "cost_usd": cost, "steps": steps, "stop": "done",
                     "done": None if claimed is None else {"success": claimed, "summary": ""}, "nudges": 1,
                     "cache_write_tokens": 10_000, "elisions": 1,
                     "construction": {"blocks": 25, "materials": {"cobblestone": 25}, "size": [5, 1, 5], "waste": 0}}}
    (d / f"trial_{n}.json").write_text(json.dumps(rec))


def test_aggregate_and_render(tmp_path):
    trial(tmp_path, "a", "platform", 0, "m1", True, True)
    trial(tmp_path, "a", "platform", 1, "m1", False, True, errors=2)     # hallucinated success
    trial(tmp_path, "b", "platform", 0, "m2", True, True, cost=0.02)
    trial(tmp_path, "b", "door", 0, "m2", False, False)
    trials = load_runs([tmp_path])
    agg = aggregate(trials)
    assert agg["labels"] == ["myagent (m1)", "myagent (m2)"] and agg["tasks"] == ["door", "platform"]
    c = agg["grid"]["platform"]["myagent (m1)"]
    assert c["passes"] == 1 and c["graded"] == 2 and c["pass_rate"] == 0.5 and abs(c["error_rate"] - 2 / 8) < 1e-9
    assert c["honesty"] == {"true_success": 1, "hallucinated_success": 1} and c["nudges"] == 2
    assert abs(agg["overall"]["myagent (m2)"]["cost_per_pass"] - 0.12) < 1e-9
    assert c["elisions"] == 2 and abs(c["cache_write_per_turn"] - 2000) < 1e-9      # 20k written over 10 turns
    out = render(trials, tmp_path / "compare.html", "t")
    text = out.read_text()
    assert "Tool calling and honesty" in text and "platform" in text and "m2" in text


def test_mixed_task_versions_are_flagged(tmp_path):
    trial(tmp_path, "a", "platform", 0, "m1", True, True, task_hash="aaa")
    trial(tmp_path, "b", "platform", 0, "m2", True, True, task_hash="bbb")
    (tmp_path / "a" / "platform" / "trial_0_goal.json").write_text("{}")     # not a record: skipped
    said = []
    trials = load_runs([tmp_path], warn=said.append)
    assert len(trials) == 2 and len(said) == 1 and "platform ran in 2 different versions" in said[0]


def test_render_trial_exists_and_renders(tmp_path):
    """Regression: the W5 refactor deleted render_trial and the try/except hid it for three milestones."""
    from mcmsbench.runner import TrialRecord, frame_diff_json, frames_from_record, render_trial
    from mcmsbench.arena import Arena
    from mcmsbench.config import ArenaConfig
    from mcmsbench.graders import Frame
    from mcmsbench.world.volume import diff
    plot = Arena(ArenaConfig(), None).plot(0)
    before = {}
    step = {(5, -60, 5): "cobblestone"}
    after = {(5, -60, 5): "cobblestone", (6, -60, 5): "cobblestone"}
    frames = [Frame("step_01", step, (4, -60, 4), [], 3.0)]
    rec = TrialRecord("platform_5x5", 0, "myagent", "now", final_position=[4, -60, 4])
    rec.result = {"passed": True, "score": 1.0, "checks": {"ok": True}, "detail": {}}
    render_trial(rec, plot, before, after, diff(before, after), frames, tmp_path)
    names = {p.name for p in (tmp_path / "trial_0_frames").iterdir()}
    assert {"before.png", "step_01.png", "final_se.png", "final_nw.png", "final_top.png", "timelapse.gif"} <= names
    assert (tmp_path / "trial_0.html").exists()
    rebuilt = frames_from_record(before, [frame_diff_json(before, frames[0])])
    assert rebuilt[0].snapshot == step and rebuilt[0].position == (4, -60, 4)
