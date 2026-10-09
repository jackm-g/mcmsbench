"""Cross-run comparison: load trial records from any set of run directories, derive per-trial metrics (performance,
cost, honesty, construction), aggregate by (task, agent and model), and render one static HTML page.

Agents report what they can: the bench's own reads (score, seconds, construction, distance) are there for every agent;
token counts, nudges and malformed tool calls only when an agent's trace carries them (0 otherwise)."""
from __future__ import annotations

import json
import os

import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Trial:
    task: str
    label: str                 # agent (model): the column
    trial: int
    path: Path                 # trial_N.json
    passed: bool | None
    score: float | None
    seconds: float
    turns: int
    cost: float
    input_tokens: int
    output_tokens: int
    steps: int
    error_steps: int
    timeouts: int
    nudges: int
    malformed: int
    unknown_tools: int
    stop: str | None
    honesty: str               # true_success | honest_failure | hallucinated_success | underclaimed | no_claim | n/a
    desyncs: int
    milestones: dict = field(default_factory=dict)       # name -> seconds (or None)
    checks: dict = field(default_factory=dict)
    construction: dict = field(default_factory=dict)
    distance: float | None = None
    stats: dict = field(default_factory=dict)
    strategy: dict | None = None
    error: str | None = None
    recovery: dict = field(default_factory=dict)         # stalls, cancels, reconnects, dig_retries, deaths
    cache_write: int = 0                                 # prompt tokens written to the cache over the trial
    elisions: int = 0                                    # times the loop rewrote history to shorten old results

    @property
    def dir(self) -> Path:
        return self.path.parent

    def asset(self, name: str) -> Path | None:
        p = self.dir / f"trial_{self.trial}_frames" / name
        return p if p.exists() else None

    @property
    def report(self) -> Path | None:
        p = self.dir / f"trial_{self.trial}.html"
        return p if p.exists() else None


def honesty_of(claimed: bool | None, graded: bool | None) -> str:
    if graded is None:
        return "n/a"
    if claimed is None:
        return "no_claim"
    if claimed and graded:
        return "true_success"
    if claimed and not graded:
        return "hallucinated_success"
    if not claimed and graded:
        return "underclaimed"
    return "honest_failure"


def label_of(trace: dict, mode: str) -> str:
    """The column: the agent, and its model when it has one ("myagent (claude-sonnet-5-5)")."""
    model = trace.get("model") or "-"
    agent = trace.get("agent") or trace.get("solver") or mode
    return agent if model in ("-", None, "", agent) else f"{agent} ({model})"


def load_trial(path: Path) -> Trial:
    d = json.loads(path.read_text())
    tr = d.get("trace") or {}
    res = d.get("result") or None
    steps = tr.get("steps") or []
    errs = [s for s in steps if s.get("error")]
    done = tr.get("done")
    det = (res or {}).get("detail") or {}
    ms = det.get("reached_at_seconds") or {}
    if not ms and det.get("reached_at_step"):
        ms = {k: None for k in det["reached_at_step"]}
    strat_path = path.with_suffix(".strategy.json")
    bench = d.get("bench") or {}
    label = label_of(tr, d.get("mode", "?"))
    if bench.get("split", "public") != "public" and bench.get("params"):
        # a split's instances are other tasks than the public ones: never the same column (nor one key's with another's)
        label += f" [{bench['split']}{' ' + bench['key_id'] if bench.get('key_id') else ''}]"
    return Trial(
        task=d["task"], label=label, trial=d["trial"], path=path,
        passed=res["passed"] if res else None, score=res["score"] if res else None,
        seconds=d.get("seconds", 0.0), turns=tr.get("turns", 0), cost=tr.get("cost_usd", 0.0) or 0.0,
        input_tokens=tr.get("input_tokens", 0), output_tokens=tr.get("output_tokens", 0),
        steps=len(steps), error_steps=len(errs), timeouts=sum("ExecTimeout" in (s["error"] or "") for s in errs),
        nudges=tr.get("nudges", 0), malformed=tr.get("malformed_tool_calls", 0), unknown_tools=tr.get("unknown_tool_calls", 0),
        stop=tr.get("stop"), honesty=honesty_of(done.get("success") if done else None, res["passed"] if res else None),
        desyncs=len(tr.get("inventory_desyncs") or []), milestones=ms, checks=(res or {}).get("checks") or {},
        construction=tr.get("construction") or {"blocks": (d.get("diff") or {}).get("placed")},
        distance=tr.get("distance_travelled"), stats=d.get("final_stats") or {},
        strategy=json.loads(strat_path.read_text()) if strat_path.exists() else None, error=d.get("error"),
        recovery=tr.get("recovery") or {}, cache_write=tr.get("cache_write_tokens", 0) or 0,
        elisions=tr.get("elisions", 0) or 0)


SKIP_SUFFIXES = (".strategy.json", "_goal.json", "_agent.json", ".built.json", "_memory.json")


def load_runs(paths: list[Path], warn=print) -> list[Trial]:
    """Accepts run dirs (containing <task>/trial_N.json) or parents of run dirs. Warns when the same task ran in
    different versions across the runs (its `bench.task_hash` differs): those columns did not face the same task."""
    out: list[Trial] = []
    hashes: dict[str, set] = defaultdict(set)
    for root in paths:
        root = Path(root)
        files = [root] if root.is_file() else sorted(root.rglob("trial_*.json"))
        for f in files:
            if f.name.endswith(SKIP_SUFFIXES):
                continue
            try:
                t = load_trial(f)
            except Exception as e:  # noqa: BLE001
                print(f"(skipping {f}: {e})")
                continue
            out.append(t)
            with_hash = (json.loads(f.read_text()).get("bench") or {}).get("task_hash")
            if with_hash:
                hashes[t.task].add(with_hash)
    for task, hs in sorted(hashes.items()):
        if len(hs) > 1:
            warn(f"warning: {task} ran in {len(hs)} different versions across these runs ({', '.join(sorted(hs))}): "
                 "its columns are not comparable")
    return out


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return statistics.fmean(xs) if xs else None


def aggregate(trials: list[Trial]) -> dict:
    cells: dict[tuple[str, str], list[Trial]] = defaultdict(list)
    for t in trials:
        cells[(t.task, t.label)].append(t)
    labels = sorted({t.label for t in trials})
    tasks = sorted({t.task for t in trials})

    def cell_stats(ts: list[Trial]) -> dict:
        graded = [t for t in ts if t.passed is not None]
        steps = sum(t.steps for t in ts)
        turns = sum(t.turns for t in ts)
        return {
            "n": len(ts), "passes": sum(bool(t.passed) for t in graded), "graded": len(graded),
            "pass_rate": (sum(bool(t.passed) for t in graded) / len(graded)) if graded else None,
            "score": _mean([t.score for t in ts]), "turns": _mean([t.turns for t in ts]),
            "seconds": _mean([t.seconds for t in ts]), "cost": _mean([t.cost for t in ts]),
            "total_cost": sum(t.cost for t in ts), "steps": steps,
            "error_rate": (sum(t.error_steps for t in ts) / steps) if steps else None,
            "timeouts": sum(t.timeouts for t in ts), "nudges": sum(t.nudges for t in ts),
            "malformed": sum(t.malformed for t in ts), "unknown_tools": sum(t.unknown_tools for t in ts),
            "desyncs": sum(t.desyncs for t in ts), "honesty": dict(Counter(t.honesty for t in ts)),
            "stalls": sum(t.recovery.get("stalls", 0) for t in ts), "cancels": sum(t.recovery.get("cancels", 0) for t in ts),
            "reconnects": sum(t.recovery.get("reconnects", 0) for t in ts),
            "cache_write_per_turn": (sum(t.cache_write for t in ts) / turns) if turns else None,
            "elisions": sum(t.elisions for t in ts),
            "stops": dict(Counter(t.stop or "?" for t in ts)),
        }

    grid = {task: {lab: cell_stats(cells[(task, lab)]) for lab in labels if (task, lab) in cells} for task in tasks}
    overall = {}
    for lab in labels:
        ts = [t for t in trials if t.label == lab]
        st = cell_stats(ts)
        per_task = [grid[task][lab]["pass_rate"] for task in tasks if lab in grid[task] and grid[task][lab]["pass_rate"] is not None]
        st["tasks"] = len(per_task)
        st["mean_task_pass_rate"] = _mean(per_task)
        st["cost_per_pass"] = (st["total_cost"] / st["passes"]) if st["passes"] else None
        overall[lab] = st
    return {"labels": labels, "tasks": tasks, "grid": grid, "overall": overall, "cells": cells}


def rel(target: Path, base: Path) -> str:
    return os.path.relpath(target, base)
