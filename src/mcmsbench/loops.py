"""Loops: how much of a trial went round in circles. A grade says whether the work got done; this says how the time
went, from two sides that do not depend on each other.

- What the agent said it did: its steps (subgoal_completed / subgoal_failed, PROTOCOL.md §5), each reduced to a key
  with its numbers and directions taken out ("Explore north" and "Explore west" are one step, "Mine the stone 61
  blocks south" and "... 57 blocks south" another). A step done twice is a retry; a third time, and every time after,
  is a loop. A step that failed again the same way is a repeated failure. An agent that sends no steps has none.
- What the bench saw: the longest stretch with no milestone first reached (`quiet_s`, from the grader's own
  reached_at_seconds; hold milestones such as alive, reached at once, are not progress), and on a flat plot the time
  the position track spent outside it (`off_plot_s`).

The commission's netherite trial of 2026-10-10 (jev, 605 s, failed): "Dig down to stone here" 7 times against bedrock,
"Explore" 10 times, 518 s with no milestone after the debris was found again. Its passing trials: no loops, quiet_s
under 90. None of this is in the score: it is reported beside it (the summary, the compare page).
"""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

STEP_EVENTS = ("subgoal_completed", "subgoal_failed")
LOOP_AT = 3                 # a step's third time is its first loop
_DIRECTION = re.compile(r"\b(?:north|south|east|west)(?:[- ](?:north|south|east|west))?\b|\b(?:right )?here\b"
                        r"|\b(?:up|down)ward(?:s)?\b")
_NUMBER = re.compile(r"[-+]?\d+(?:\.\d+)?")


def step_key(text: str) -> str:
    """A step with its numbers and directions taken out, lower case, spaces collapsed."""
    t = _DIRECTION.sub("~", str(text or "").lower())
    t = _NUMBER.sub("#", t)
    return re.sub(r"\s+", " ", re.sub(r"[^\w#~ ]+", " ", t)).strip()


def repeats(steps: list[dict]) -> dict:
    """The agent's steps ([{event, subgoal}], in order) counted by key: `loops` is how many steps were done LOOP_AT
    times or more, `repeated_steps` how many times past the second, `repeated_failures` how many failures repeated
    one before them; `top` the most repeated, each with an example of its words."""
    done = [s for s in steps if s.get("event") in STEP_EVENTS]
    keys = Counter(step_key(s.get("subgoal")) for s in done)
    failed = Counter(step_key(s.get("subgoal")) for s in done if s.get("event") == "subgoal_failed")
    example = {}
    for s in done:
        example.setdefault(step_key(s.get("subgoal")), str(s.get("subgoal") or "")[:120])
    looped = {k: n for k, n in keys.items() if n >= LOOP_AT and k}
    return {"steps": len(done), "loops": len(looped),
            "repeated_steps": sum(n - (LOOP_AT - 1) for n in looped.values()),
            "repeated_failures": sum(n - 1 for n in failed.values() if n > 1),
            "top": [{"step": example[k], "n": n, "failed": failed.get(k, 0)}
                    for k, n in sorted(looped.items(), key=lambda kv: -kv[1])[:5]]}


def quiet(result: dict | None, grader: dict | None, seconds: float) -> dict | None:
    """The longest stretch of the trial with no milestone first reached: [start, end] in seconds since the start.
    None when the grader is not a milestone ladder (nothing to time progress by)."""
    if not grader or grader.get("kind") != "milestones" or not result:
        return None
    at = ((result.get("detail") or {}).get("reached_at_seconds")) or {}
    hold = {m["name"] for m in grader.get("steps") or [] if m.get("hold")}
    marks = sorted(float(t) for name, t in at.items() if t is not None and name not in hold and 0 <= float(t) <= seconds)
    edges = [0.0, *marks, float(seconds)]
    gap, a, b = max(((b - a, a, b) for a, b in zip(edges, edges[1:])), default=(0.0, 0.0, 0.0))
    return {"quiet_s": round(gap, 1), "quiet_at": [round(a, 1), round(b, 1)], "milestones_reached": len(marks)}


def off_plot(track: list, plot: dict | None) -> float | None:
    """Seconds the position track spent outside a flat plot (x, z), each sample standing for the time to the next.
    None on terrain, where a task's ground runs past the graded region (a trip of 800 blocks)."""
    if not plot or not plot.get("flat") or not track:
        return None
    lo, hi = plot["volume"]["min"], plot["volume"]["max"]
    out = 0.0
    for (t, x, _, z), (t2, *_rest) in zip(track, track[1:]):
        if not (lo[0] <= x <= hi[0] + 1 and lo[2] <= z <= hi[2] + 1):
            out += t2 - t
    return round(out, 1)


def measure(steps: list[dict], result: dict | None, grader: dict | None, seconds: float, track: list,
            plot: dict | None) -> dict:
    out = repeats(steps)
    q = quiet(result, grader, seconds)
    if q:
        out.update(q)
    o = off_plot(track, plot)
    if o is not None:
        out["off_plot_s"] = o
    return out


def steps_from_log(path: Path) -> list[dict]:
    """The agent's steps from a trial's event log (trial_N.jsonl), for a record written before the trace kept them."""
    from .events import read_events
    out, t0 = [], None
    for r in read_events(path):
        if r.get("kind") == "trial_start":
            t0 = r.get("t")
        if r.get("kind") == "agent_event" and r.get("event") in STEP_EVENTS:
            out.append({"t": round(r["t"] - t0, 1) if t0 else None, "event": r["event"], "subgoal": r.get("subgoal"),
                        "text": str(r.get("text") or "")[:200]})
    return out


def of_record(path: Path, d: dict | None = None) -> dict:
    """A trial record's loops: the ones it carries, or measured now from its event log and its grade."""
    d = d if d is not None else json.loads(path.read_text())
    tr = d.get("trace") or {}
    if isinstance(tr.get("loops"), dict):
        return tr["loops"]
    steps = tr.get("subgoals")
    if steps is None:
        log = path.with_suffix(".jsonl")
        steps = steps_from_log(log) if log.exists() else []
    from .config import TASK_DIR
    grader = None
    try:
        import yaml
        grader = (yaml.safe_load(re.sub(r"\$\{[^{}]*\}", "0", (TASK_DIR / f"{d['task']}.yaml").read_text())) or {}).get("grader")
    except Exception:  # noqa: BLE001 — a task since renamed: no milestone timing, the rest still measured
        grader = None
    return measure(steps, d.get("result"), grader, float(d.get("seconds") or 0), tr.get("track") or [], d.get("plot"))
