"""The event log: one JSON line per thing that happened in a trial, appended as it happens.

Each trial gets a `trial_N.jsonl` next to its `trial_N.json`; whatever the runner and the agent protocol notice goes
in with a timestamp the moment it is known, so a crash, a kill or a lost connection still leaves the record up to that
point. The `.json` record is the summary; this is the timeline.

Every event has `t` (epoch seconds), `kind`, whatever context the log was bound with (task, trial, bot), and the
event's own fields. Kinds, in the order a trial produces them:

  trial_start    agent, model, seed, start, world, plot
  setup          one setup command and what the server answered
  task_start     the goal as the agent got it, the agent's log path
  agent_event    a protocol event from the agent (subgoal_*, death, guard_refusal, block_broken...)
  live_grade     the grader on the live world mid-trial: passed, score, checks
  night_skipped  the clock jumped to morning on the agent's night_skip_request
  task_end       stop, turns, cost
  exception      a traceback the runner caught
  trial_end      the verdict and the final server reads

Nothing here ever raises into the runner: a log that cannot be written records why in `failed`.
"""
from __future__ import annotations

import functools
import json
import subprocess
import threading
import time
from pathlib import Path

@functools.lru_cache(maxsize=1)
def git_sha() -> str:
    """The code that ran, for the trial record; "" when git is not there to ask."""
    try:
        root = Path(__file__).resolve().parents[2]
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=root, capture_output=True, text=True, timeout=5)
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:  # noqa: BLE001
        return ""


class EventLog:
    """Append-only, thread-safe, never raises. `EventLog(None)` is a log that drops everything, so
    callers bind one unconditionally and never check."""

    def __init__(self, path: Path | str | None, **context):
        self.path = Path(path) if path else None
        self.context = {k: v for k, v in context.items() if v not in (None, "")}
        self._lock = threading.Lock()
        self.count = 0
        self.failed: str | None = None

    @classmethod
    def null(cls) -> "EventLog":
        return cls(None)

    @property
    def enabled(self) -> bool:
        return self.path is not None

    @property
    def trace_path(self) -> Path | None:
        """Where the summary trace for this log goes: the same base name, `.json`."""
        return self.path.with_suffix(".json") if self.path else None

    def bind(self, **context) -> "EventLog":
        """Add (or clear, with None) context fields that every later event carries."""
        for k, v in context.items():
            if v in (None, ""):
                self.context.pop(k, None)
            else:
                self.context[k] = v
        return self

    def emit(self, kind: str, **fields) -> dict:
        rec = {"t": round(time.time(), 3), "kind": kind, **self.context, **fields}
        if self.path is None:
            return rec
        try:
            line = json.dumps(rec, default=str, ensure_ascii=False)
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(line + "\n")
                self.count += 1
        except Exception as e:  # noqa: BLE001 — observability never fails the trial
            self.failed = f"{type(e).__name__}: {e}"
        return rec


def read_events(path: Path | str) -> list[dict]:
    """Every well-formed line of a .jsonl; a torn last line (a crash mid-write) is skipped."""
    out: list[dict] = []
    p = Path(path)
    if not p.exists():
        return out
    with p.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict):
                out.append(rec)
    return out
