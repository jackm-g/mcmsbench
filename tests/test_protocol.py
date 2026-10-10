"""The agent protocol, against a fake agent (tests/fake_agent.py): no server, no model."""
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

import mcmsbench.protocol as P
from mcmsbench.protocol import Agent, Manifest, TrialContext, load_manifest, render_args

FAKE = Path(__file__).with_name("fake_agent.py")


def manifest(tmp_path, **kw) -> Manifest:
    text = "\n".join([f'command = ["{sys.executable}", "{FAKE}"]', 'prefix = "@fake"', f'cwd = "{tmp_path}"']
                     + [f"{k} = {json.dumps(v)}" for k, v in kw.items()])
    p = tmp_path / "fake.toml"
    p.write_text(text)
    return load_manifest(str(p))


def context(tmp_path, **kw) -> tuple[TrialContext, dict]:
    seen = {"calls": [], "frames": [], "events": [], "progress": [], "skips": 0}
    defaults = dict(
        hand_off=lambda: seen["calls"].append("hand_off"),
        take_back=lambda: seen["calls"].append("take_back"),
        progress=seen["progress"].append,
        after_step=seen["frames"].append,
        on_event=seen["events"].append,
    )
    defaults.update(kw)
    goal = {"protocol": 1, "task_id": "t", "prompt": "do the thing", "check": None}
    ctx = TrialContext("t", goal, "127.0.0.1", 25565, "player", "26.1", 30, 0.5, tmp_path / "run" / "trial_0", **defaults)
    return ctx, seen


def test_manifest_keys_args_and_env(tmp_path, monkeypatch):
    assert render_args(["--fixed", ["--model", "{model}"], ["--backend", "{provider}"]], {"provider": "x", "model": ""}) \
        == ["--fixed", "--backend", "x"]
    with pytest.raises(ValueError):
        render_args(["{model}"], {})                         # a placeholder outside a group must have a value
    m = manifest(tmp_path, capabilities=["guard"])
    assert m.line_prefix == "@fake " and m.directory() == tmp_path.resolve()
    (tmp_path / "bad.toml").write_text('command = ["x"]\nwhat = 1\n')
    with pytest.raises(ValueError, match="unknown keys"):
        load_manifest(str(tmp_path / "bad.toml"))
    (tmp_path / "cap.toml").write_text('command = ["x"]\ncapabilities = ["flying"]\n')
    with pytest.raises(ValueError, match="unknown capabilities"):
        load_manifest(str(tmp_path / "cap.toml"))
    monkeypatch.setenv("FAKE_HOME", str(tmp_path))
    assert P.expand_env("${FAKE_HOME}/a") == f"{tmp_path}/a" and P.expand_env("${NOPE_NOT_SET:-../x}") == "../x"


def test_manifests_are_found_on_the_agent_path(tmp_path, monkeypatch):
    one, two = tmp_path / "one", tmp_path / "two"
    for d in (one, two):
        d.mkdir()
    (one / "alpha.toml").write_text('command = ["a"]\ncwd = ".."\nprovider = "p1"\nlabel = "alpha ({provider})"\n')
    (one / "beta.toml").write_text('command = ["first"]\ngoal_format = "text"\n')
    (two / "beta.toml").write_text('command = ["second"]\n')
    single = tmp_path / "gamma.toml"
    single.write_text('command = ["g"]\n')
    monkeypatch.setenv("MCMSBENCH_AGENT_PATH", os.pathsep.join([str(one), str(two), str(single), str(tmp_path / "nope")]))
    assert {"alpha", "beta", "gamma"} <= {m.name for m in P.list_manifests()}
    beta = load_manifest("beta")
    assert beta.command == ["first"] and beta.goal_format == "text"          # the first of a name wins
    alpha = Agent(load_manifest("alpha"), provider="heuristic")
    assert alpha.model == "alpha (heuristic)" and alpha.dir == tmp_path      # cwd is relative to the manifest
    assert load_manifest("gamma").path == single
    with pytest.raises(SystemExit, match="MCMSBENCH_AGENT_PATH"):
        load_manifest("missing")


def test_a_full_run(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "ok")
    skips = []
    ctx, seen = context(tmp_path, night_skip=lambda: skips.append(1))
    tr = Agent(manifest(tmp_path), provider="b1").run(ctx)
    assert seen["calls"] == ["hand_off", "take_back"]
    assert seen["frames"] == ["step_01", "step_02"]                 # subgoal_completed and subgoal_failed
    # the steps as the agent reported them, kept in the trace for the loop measure (loops.py)
    assert [(s["event"], s["subgoal"], s["text"]) for s in tr["subgoals"]] == \
        [("subgoal_completed", "s1", "gathered"), ("subgoal_failed", "s2", "could not build")]
    assert all(isinstance(s["t"], float) for s in tr["subgoals"])
    assert skips == [1]
    assert tr["guard_refusals"] == [{"what": "dig", "at": [1, 2, 3], "why": "not yours"}]
    assert tr["agent"] == "fake" and tr["model"] == "fake-1" and tr["exit_code"] == 0 and tr["stop"] == "done"
    assert tr["goal"]["prompt"] == "do the thing" and tr["argv"]["goal_kind"] == "json"
    assert tr["argv"]["username"] == "player"
    assert any("starting up" in m for m in seen["progress"])
    assert [e["event"] for e in seen["events"]][:2] == ["subgoal_started", "subgoal_completed"]
    assert json.loads((tmp_path / "run" / "trial_0_goal.json").read_text())["task_id"] == "t"


def test_the_manifest_args_reach_the_agent(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "ok")
    ctx, _ = context(tmp_path)
    tr = Agent(manifest(tmp_path, args=[["--backend", "{provider}"]]), provider="mock", extra_args=["--more"]).run(ctx)
    assert tr["argv"]["backend"] == "mock" and tr["argv"]["extra"] == ["--more"]


def test_sigterm_stops_the_agent_and_its_trace_is_read_from_out(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "hang")
    stop = threading.Event()
    ctx, seen = context(tmp_path, stop=stop)
    threading.Timer(1.0, stop.set).start()
    t = time.time()
    tr = Agent(manifest(tmp_path)).run(ctx)
    assert time.time() - t < 8
    assert tr["stop"] == "terminated" and tr["done"] == {"success": False, "summary": "stopped"}
    assert seen["calls"] == ["hand_off", "take_back"]


def test_no_trace_is_an_error(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "no_trace")
    ctx, _ = context(tmp_path)
    tr = Agent(manifest(tmp_path)).run(ctx)
    assert tr["stop"] == "error" and "without a trace" in tr["error"] and "something went wrong" in tr["error"]
    assert tr["exit_code"] == 3


def test_a_loose_trace_is_normalized(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "bare")
    ctx, _ = context(tmp_path)
    tr = Agent(manifest(tmp_path)).run(ctx)
    assert tr["turns"] == 4 and tr["cost_usd"] == 0.5 and tr["steps"] == []
    assert tr["done"]["success"] is True and tr["done_flag"] is True


def test_a_passing_grade_stops_the_agent(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "hang")
    monkeypatch.setattr(P, "GOAL_CHECK_EVERY", 0.3)
    monkeypatch.setattr(P, "CONFIRM_SETTLE", 0.1)
    grades = []
    ctx, _ = context(tmp_path, goal_met=lambda: grades.append(1) or True)
    t = time.time()
    tr = Agent(manifest(tmp_path)).run(ctx)
    assert time.time() - t < 8
    assert tr["stopped_by"] == "grader" and tr["stop"] == "grader_passed" and len(grades) >= 2   # the pass, then the confirm


def test_a_lost_task_stops_the_agent(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "hang")
    monkeypatch.setattr(P, "LOST_CHECK_EVERY", 0.3)
    reads = iter([None, None] + ["the player died"] * 100)
    ctx, seen = context(tmp_path, lost=lambda: next(reads))
    t = time.time()
    tr = Agent(manifest(tmp_path)).run(ctx)
    assert time.time() - t < 8
    assert tr["stopped_by"] == "lost" and tr["stop"] == "task_lost"
    assert any("the player died" in m and "can no longer pass" in m for m in seen["progress"])


def test_a_pass_that_does_not_hold_lets_the_agent_play_on(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "hang")
    monkeypatch.setattr(P, "GOAL_CHECK_EVERY", 0.3)
    monkeypatch.setattr(P, "CONFIRM_SETTLE", 0.05)
    answers = iter([True, False] + [False] * 100)
    stop = threading.Event()
    ctx, seen = context(tmp_path, goal_met=lambda: next(answers), stop=stop)
    threading.Timer(1.5, stop.set).start()
    tr = Agent(manifest(tmp_path)).run(ctx)
    assert "stopped_by" not in tr and tr["stop"] == "terminated"
    assert any("not on a settled world" in m for m in seen["progress"])


def test_the_runner_takes_frames_on_a_timer(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "hang")
    monkeypatch.setattr(P, "FRAME_EVERY", 0.3)
    stop = threading.Event()
    ctx, seen = context(tmp_path, stop=stop)
    threading.Timer(3.2, stop.set).start()          # the loop looks at the clock about once a second
    tr = Agent(manifest(tmp_path)).run(ctx)
    assert tr["timer_frames"] >= 2 and all(f.startswith("t") for f in seen["frames"])


def test_a_text_goal_file_for_an_older_agent(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "ok")
    ctx, _ = context(tmp_path)
    tr = Agent(manifest(tmp_path, goal_format="text")).run(ctx)
    assert tr["goal"] == {"prompt": "do the thing"} and tr["argv"]["goal_kind"] == "text"
