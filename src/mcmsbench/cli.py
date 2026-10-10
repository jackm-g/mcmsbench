"""mcmsbench: the command line.

  mcmsbench run --agent NAME --task ID [--task ID ...] | --all [--tag T]   run trials, write runs/<stamp>-<agent>/
                [--split public|varied|heldout]                         which instances of tasks with params (variants.py)
  mcmsbench agents                                                        the manifests on MCMSBENCH_AGENT_PATH, and whether each is installed
  mcmsbench tasks [--tag T]                                               the tasks
  mcmsbench goal --task ID [--trial N] [--split S [--reveal]]             the goal file a task makes (no server needed)
  mcmsbench heldout init | rotate | id                                    the held-out split's secret key (in .env), its pack
  mcmsbench check-agent NAME                                              protocol conformance (runs the agent twice)
  mcmsbench compare DIRS [--out FILE] [--summarize] [--open]              one HTML page across runs
  mcmsbench render DIRS                                                   re-render frames and reports from records
  mcmsbench world prepare --seed N | mcmsbench world list                 survival worlds
  mcmsbench probe slice|top --x A [B] --z C [D] --y LO HI [--dimension D]  an arena's blocks as text (read-only, RCON)
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from pathlib import Path

from . import config


def _agent(a) -> "Agent":  # noqa: F821
    from .protocol import Agent, load_manifest
    return Agent(load_manifest(a.agent), provider=a.provider, model=a.model, extra_args=a.agent_arg)


def _agent_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--provider", help="the agent's {provider} (its manifest says what it means: a backend, an API)")
    p.add_argument("--model", help="the agent's {model}")
    p.add_argument("--agent-arg", action="append", default=[], help="an extra argument passed to the agent (repeatable)")


def cmd_run(argv: list[str]) -> int:
    from .runner import default_run_dir, run, select_tasks
    p = argparse.ArgumentParser(prog="mcmsbench run")
    p.add_argument("--agent", required=True, help="a manifest NAME on MCMSBENCH_AGENT_PATH, or a path to a manifest")
    _agent_flags(p)
    p.add_argument("--task", action="append", help="task id (repeatable)")
    p.add_argument("--all", action="store_true")
    p.add_argument("--tag", action="append", help="only tasks carrying this tag (repeatable)")
    p.add_argument("--trials", type=int, help="override each task's `trials`")
    p.add_argument("--run-dir", help="write results here instead of runs/<stamp>-<agent>")
    p.add_argument("--resume", action="store_true", help="skip trials that already have a trial_N.json in --run-dir")
    p.add_argument("--no-render", action="store_true", help="skip frame images and HTML reports")
    p.add_argument("--open", action="store_true", help="open the run's HTML index when done")
    p.add_argument("--slot", help="run on the arenas of [slots.NAME] in mcmsbench.toml (alongside another run)")
    p.add_argument("--split", default="public", choices=["public", "varied", "heldout"],
                   help="public: tasks as written (default); varied: instances drawn by a public seed; heldout: "
                        "instances drawn by the secret key (tasks with params; trial i is instance i)")
    a = p.parse_args(argv)
    s = config.load(slot=a.slot)
    if a.split == "heldout":
        from .variants import heldout_key, key_id
        if not heldout_key():
            p.error("the heldout split needs a key: `mcmsbench heldout init`")
        print(f"held-out key {key_id(heldout_key())}")
    tasks = select_tasks(a.task, a.all, a.tag)
    if not tasks:
        p.error("give --task ID or --all (and check --tag)")
    agent = _agent(a)
    agent.check_installed()
    print(f"agent: {agent.name} ({agent.model}) in {agent.dir}")
    run_dir = Path(a.run_dir).resolve() if a.run_dir else default_run_dir(agent, a.split)
    run(tasks, agent, s, run_dir, trials=a.trials, resume=a.resume, render=not a.no_render, open_report=a.open,
        split=a.split)
    return 0


def cmd_agents(argv: list[str]) -> int:
    from .protocol import Agent, list_manifests
    ms = list_manifests()
    if not ms:
        print("no agent manifests: set MCMSBENCH_AGENT_PATH to the directories holding them (PROTOCOL.md §1)")
    for m in ms:
        ag = Agent(m)
        try:
            ag.check_installed()
            status = "installed"
        except SystemExit as e:
            status = f"not installed: {e}"
        caps = f" [{', '.join(m.capabilities)}]" if m.capabilities else ""
        print(f"{m.name:<18} {status}{caps}\n{'':<18} {m.description}" if m.description else f"{m.name:<18} {status}{caps}")
    return 0


def cmd_tasks(argv: list[str]) -> int:
    from .tasks import load_all
    p = argparse.ArgumentParser(prog="mcmsbench tasks")
    p.add_argument("--tag", action="append")
    a = p.parse_args(argv)
    for t in load_all().values():
        if a.tag and not any(tag in t.tags or (tag == "variants" and t.params) for tag in a.tag):
            continue
        req = f" requires={t.requires}" if t.requires else ""
        par = f" params={','.join(t.params)}" if t.params else ""
        print(f"{t.id:<26} {t.world.type:<9} {t.max_seconds or '-':>5}s  {' '.join(t.tags)}{req}{par}")
    return 0


def offline_plot(task, s: config.Settings, trial: int = 0):
    """The plot and start a trial would have, worked out without a server: the flat arena's layout, or the survival
    world's manifest spawn, its terrain taken as level ground."""
    from .arena import Arena
    from .start import resolve_start
    if task.world.type == "survival":
        from .survival import WorldArena
        arena = WorldArena(s.survival, None, None)
        arena.configure(task.seed_for(trial), task.world.radius, task.world.daylight, task.world.difficulty,
                        border=task.world.border)
        plot = arena.plot(trial)
        start = resolve_start(task.start, plot.center(), task.id, trial, task.seed_for(trial), None)   # ground as level
    else:
        w = task.world
        plot = Arena(s.arena, None).plot(trial, w.dimension, w.size, w.height)
        start = resolve_start(task.start, plot.center(), task.id, trial, 0, None)
    return plot, start


def cmd_goal(argv: list[str]) -> int:
    from .runner import build_goal, stops_on_pass
    from .tasks import load
    p = argparse.ArgumentParser(prog="mcmsbench goal", description="Print the goal file a task makes, as an agent "
                                "would get it (positions as the default arenas lay the plot out).")
    p.add_argument("--task", required=True)
    p.add_argument("--trial", type=int, default=0)
    p.add_argument("--split", default="public", choices=["public", "varied", "heldout"])
    p.add_argument("--reveal", action="store_true", help="show a held-out instance (it is no longer unseen after)")
    a = p.parse_args(argv)
    if a.split == "heldout" and not a.reveal:
        p.error("a held-out instance shown is one seen: add --reveal (and rotate the key before relying on it again)")
    s = config.load()
    task = load(a.task, a.split, a.trial)
    plot, start = offline_plot(task, s, a.trial)
    goal = build_goal(task, plot, start, s, a.trial, seed=task.seed_for(a.trial) if task.world.type == "survival" else None,
                      fast_nights=bool(task.fast_nights and task.world.daylight), grader_stops=stops_on_pass(task),
                      max_seconds=float(task.max_seconds or s.trial.max_seconds),
                      max_cost=float(task.max_cost_usd or s.trial.max_cost_usd))
    print(json.dumps(goal, indent=1, default=str))
    return 0


def cmd_check_agent(argv: list[str]) -> int:
    """Two short runs of the agent against the flat arena (or --host/--port), the bench side only: no reset, no
    grading. 1) a 20 s goal: it must exit by itself near the clock, with goal_end and a well-formed trace. 2) a 120 s
    goal stopped at 8 s: it must exit within the SIGTERM grace and still write its trace. 3) for an agent declaring
    `messages`: a directive from a tester who has a stick, answered on stdin; it must send an `ask` naming what it
    needs, be given the reply, and send a `report` (PROTOCOL.md §5.1)."""
    from .arena import Arena
    from .protocol import KILL_AFTER, Agent, TrialContext, load_manifest
    from .runner import build_goal
    from .tasks import Task
    p = argparse.ArgumentParser(prog="mcmsbench check-agent", description=cmd_check_agent.__doc__)
    p.add_argument("agent")
    _agent_flags(p)
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.add_argument("--username", default="conformance")
    p.add_argument("--out", default=None, help="directory for the agent's files (default: runs/check-<agent>)")
    a = p.parse_args(argv)
    s = config.load()
    agent = Agent(load_manifest(a.agent), provider=a.provider, model=a.model, extra_args=a.agent_arg)
    agent.check_installed()
    out = Path(a.out or config.ROOT / "runs" / f"check-{agent.name}").resolve()
    out.mkdir(parents=True, exist_ok=True)
    plot = Arena(s.arena, None).plot(0)
    task = Task(id="protocol_check", prompt="Stand where you are and do nothing until the clock runs out.",
                grader=None, tags=["protocol"])
    results: list[tuple[str, bool, str]] = []

    def one(label: str, max_seconds: float, stop_at: float | None) -> tuple[dict, list[dict], float]:
        goal = build_goal(task, plot, plot.center(), s, 0, seed=None, fast_nights=False, grader_stops=False,
                          max_seconds=max_seconds, max_cost=0.05)
        events: list[dict] = []
        stop = threading.Event()
        ctx = TrialContext(task.id, goal, a.host or s.arena.host, a.port or s.arena.port, a.username, s.arena.version,
                           max_seconds, 0.05, out / label, hand_off=lambda: None, take_back=lambda: None,
                           progress=lambda m: print(f"  {m}"), on_event=events.append, stop=stop)
        if stop_at is not None:
            threading.Timer(stop_at, stop.set).start()
        t = time.time()
        trace = agent.run(ctx)
        return trace, events, time.time() - t

    def judge(name: str, ok: bool, why: str) -> None:
        results.append((name, ok, why))

    print(f"[1/2] {agent.name}: a 20 s goal, to the clock")
    tr, evs, took = one("clock", 20, None)
    judge("trace written", tr.get("stop") != "error" or "without a trace" not in str(tr.get("error", "")),
          str(tr.get("error") or tr.get("stop")))
    judge("goal_end sent", any(e.get("event") == "goal_end" for e in evs), f"{len(evs)} events")
    judge("required trace keys", all(k in tr for k in ("turns", "cost_usd", "done", "stop")),
          ", ".join(k for k in ("turns", "cost_usd", "done", "stop") if k not in tr) or "all there")
    judge("exited near the clock", took < 20 + 30, f"{took:.0f} s")
    judge("exit code 0", tr.get("exit_code") == 0, str(tr.get("exit_code")))
    print(f"[2/2] {agent.name}: a 120 s goal, stopped at 8 s")
    tr, evs, took = one("sigterm", 120, 8.0)
    judge("stopped on SIGTERM", took < 8 + KILL_AFTER, f"{took:.0f} s")
    judge("trace written on SIGTERM", "without a trace" not in str(tr.get("error", "")), str(tr.get("stop")))
    if "messages" in agent.capabilities:
        from .protocol import Inbox
        from .responders import Responder, Responders, needs_of
        print(f"[3/3] {agent.name}: a directive from a tester, answered on stdin")
        mtask = Task(id="protocol_messages", prompt="Could you ask me for a stick? I'll give you one. Then tell me it's done.",
                     grader=None, tags=["protocol"], requester="Tester")
        goal = build_goal(mtask, plot, plot.center(), s, 0, seed=None, fast_nights=False, grader_stops=False,
                          max_seconds=60, max_cost=0.05)
        inbox = Inbox()
        answers = Responders([Responder("Tester", {"stick": 1}, {"inventory": True}, gives="here's a stick")], "Tester",
                             lambda cmd: "(conformance: nothing handed over in the world)", inbox, a.username)
        ctx = TrialContext(mtask.id, goal, a.host or s.arena.host, a.port or s.arena.port, a.username, s.arena.version,
                           60, 0.05, out / "messages", hand_off=lambda: None, take_back=lambda: None,
                           progress=lambda m: print(f"  {m}"), inbox=inbox,
                           on_event=lambda ev: answers.answer(ev) if ev.get("event") == "ask" else None)
        tr = agent.run(ctx)
        msgs = tr.get("messages") or []
        asks = [m for m in msgs if m.get("dir") == "out" and m.get("event") == "ask"]
        reports = [m for m in msgs if m.get("dir") == "out" and m.get("event") == "report"]
        judge("ask with a need", bool(asks) and bool(needs_of(asks[0])),
              f"{len(asks)} asks" + (f", need {needs_of(asks[0])}" if asks else ""))
        judge("reply delivered on stdin", any(m.get("dir") == "in" and m.get("delivered") for m in msgs),
              f"{sum(1 for m in msgs if m.get('dir') == 'in')} replies")
        judge("report with a status", any(str(m.get("status")) in ("done", "blocked", "failed", "in_progress")
                                          for m in reports), f"{len(reports)} reports")
    print()
    for name, ok, why in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name:<28} {why}")
    return 0 if all(ok for _, ok, _ in results) else 1


def cmd_compare(argv: list[str]) -> int:
    from .compare import load_runs
    from .compare.html import render
    p = argparse.ArgumentParser(prog="mcmsbench compare")
    p.add_argument("paths", nargs="+")
    p.add_argument("--out", help="output HTML (default: <first path>/compare.html)")
    p.add_argument("--title")
    p.add_argument("--summarize", action="store_true", help="add a one-paragraph strategy summary per trial "
                                                             "(Claude Haiku; ANTHROPIC_API_KEY; `pip install mcmsbench[summarize]`)")
    p.add_argument("--open", action="store_true")
    a = p.parse_args(argv)
    paths = [Path(x).resolve() for x in a.paths]
    if a.summarize:
        import os
        from .compare.strategy import summarize_runs
        n = summarize_runs(paths, os.environ.get("ANTHROPIC_API_KEY", ""))
        print(f"{n} strategy summaries written")
    trials = load_runs(paths)
    if not trials:
        p.error("no trial records under those paths")
    out = Path(a.out).resolve() if a.out else paths[0] / "compare.html"
    render(trials, out, a.title or f"MCMSBench comparison — {paths[0].name}")
    print(f"compare: {out}  ({len(trials)} trials)")
    if a.open:
        import webbrowser
        webbrowser.open(out.as_uri())
    return 0


def cmd_render(argv: list[str]) -> int:
    from .runner import rerender
    if not argv:
        print("usage: mcmsbench render RUN_DIR [...]")
        return 2
    n = rerender([Path(x).resolve() for x in argv])
    print(f"{n} trials rendered")
    return 0


def cmd_world(argv: list[str]) -> int:
    from . import survival
    p = argparse.ArgumentParser(prog="mcmsbench world")
    sub = p.add_subparsers(dest="cmd", required=True)
    pp = sub.add_parser("prepare", help="generate, validate and snapshot a seeded survival world")
    pp.add_argument("--seed", type=int, required=True)
    pp.add_argument("--slot")
    sub.add_parser("list", help="show prepared worlds")
    a = p.parse_args(argv)
    s = config.load(slot=getattr(a, "slot", None))
    if a.cmd == "prepare":
        survival.prepare(s.survival, a.seed, s.arena.version, s.survival.observer_username)
        return 0
    for m in survival.list_worlds(s.survival):
        print(f"seed {m.seed}: spawn {m.spawn} on {m.top_block}, {m.logs_within_radius} logs within {m.radius}")
    return 0


def cmd_heldout(argv: list[str]) -> int:
    """The held-out split's key: `init` puts a new one in .env when there is none, `rotate` replaces it (new instances;
    results under the old key keep its id), `id` prints the key's public id and the held-out pack's tasks and ids."""
    from .variants import KEY_ENV, PACK_ENV, heldout_dir, heldout_key, key_id, new_key, pack_id
    p = argparse.ArgumentParser(prog="mcmsbench heldout", description=cmd_heldout.__doc__)
    p.add_argument("cmd", choices=["init", "rotate", "id"])
    a = p.parse_args(argv)
    env = config.ROOT / ".env"
    have = heldout_key()
    if a.cmd == "id":
        print(key_id(have) if have else f"no key: {KEY_ENV} is not set (`mcmsbench heldout init`)")
        d = heldout_dir()
        packs = sorted(d.glob("*.yaml")) if d else []
        print(f"pack {d}: " + (", ".join(f"{q.stem} {pack_id(q.read_text())}" for q in packs) or "empty") if d else
              f"no held-out pack ({PACK_ENV}, or heldout/ here): held-out instances come from the task files' public domains")
        return 0 if have else 1
    if a.cmd == "init" and have:
        print(f"a key is already set ({key_id(have)}); `mcmsbench heldout rotate` replaces it")
        return 0
    key = new_key()
    lines = env.read_text().splitlines() if env.exists() else []
    lines = [ln for ln in lines if not ln.startswith(f"{KEY_ENV}=")] + [f"{KEY_ENV}={key}"]
    env.write_text("\n".join(lines) + "\n")
    print(f"{'rotated' if have else 'new'} held-out key {key_id(key)} in {env}"
          + (f" (was {key_id(have)})" if have else "") + ": keep .env out of git and away from agents")
    return 0



def cmd_probe(argv: list[str]) -> int:
    from .probe import legend, loaded, slice_view, top_view
    from .rcon import Rcon
    p = argparse.ArgumentParser(prog="mcmsbench probe", description="an arena's blocks as text, read over RCON (nothing changed)")
    p.add_argument("view", choices=["slice", "top"], help="slice: a side view along one line; top: the ground from above")
    p.add_argument("--x", type=int, nargs="+", required=True, help="one value, or a range A B")
    p.add_argument("--z", type=int, nargs="+", required=True, help="one value, or a range C D")
    p.add_argument("--y", type=int, nargs=2, required=True, metavar=("LO", "HI"))
    p.add_argument("--dimension", default="overworld", help="overworld, the_nether or the_end")
    p.add_argument("--flat", action="store_true", help="the flat arena, not the survival one")
    p.add_argument("--slot", help="the survival arena of [slots.NAME]")
    p.add_argument("--load", action="store_true", help="force-load chunks not loaded (between trials only), let go after")
    a = p.parse_args(argv)
    s = config.load(slot=a.slot)
    host, port, pw = (s.arena.host, s.arena.rcon_port, s.arena.rcon_password) if a.flat else \
        (s.survival.host, s.survival.rcon_port, s.survival.rcon_password)
    rng = lambda v: (v[0], v[1]) if len(v) > 1 else v[0]      # noqa: E731
    x, z = rng(a.x), rng(a.z)
    if a.view == "top" and not (isinstance(x, tuple) and isinstance(z, tuple)):
        p.error("top takes a range for both --x and --z")
    cells = (abs(a.y[1] - a.y[0]) + 1) * (abs(x[1] - x[0]) + 1 if isinstance(x, tuple) else 1) * (abs(z[1] - z[0]) + 1 if isinstance(z, tuple) else 1)
    if cells > 200_000:
        p.error(f"{cells} cells is too many for one probe (200000 at most)")
    with Rcon(host, port, pw) as r:
        xs = x if isinstance(x, tuple) else (x, x)
        zs = z if isinstance(z, tuple) else (z, z)
        with loaded(r.command, a.dimension, xs[0], zs[0], xs[1], zs[1]) if a.load else _nothing():
            view = slice_view if a.view == "slice" else top_view
            print(view(r.command, a.dimension, x=x, z=z, y=(a.y[0], a.y[1])))
    print(legend())
    return 0


def _nothing():
    import contextlib
    return contextlib.nullcontext(0)


COMMANDS = {"run": cmd_run, "agents": cmd_agents, "tasks": cmd_tasks, "goal": cmd_goal, "check-agent": cmd_check_agent,
            "compare": cmd_compare, "render": cmd_render, "world": cmd_world, "probe": cmd_probe, "heldout": cmd_heldout}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    fn = COMMANDS.get(argv[0])
    if fn is None:
        print(f"unknown command {argv[0]!r}\n{__doc__}")
        return 2
    return fn(argv[1:]) or 0


if __name__ == "__main__":
    sys.exit(main())
