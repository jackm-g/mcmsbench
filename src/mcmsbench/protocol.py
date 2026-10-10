"""The agent protocol (v1): how the bench runs an agent it knows nothing about. PROTOCOL.md is the contract; this is
the bench's side of it.

An agent is a command in its own checkout, described by a manifest (<name>.toml on
config.agent_path()). Per trial the runner gets the
player ready on the server, logs its stand-in client out, and starts the command with the server, the username, a goal
file and the budgets as flags. The agent logs in as that player (inventory and position live on the server), works
the goal, writes its trace and exits; the stand-in logs back in for the final reads. Grading never asks the agent: it
reads the server and the observer.

The agent reports on stdout in lines `<prefix> {json}` (prefix `@<name>` unless the manifest says otherwise), read by
their `event`:

  subgoal_started                    progress
  subgoal_completed / subgoal_failed a frame is taken (the world as it stands, for milestone timing)
  night_skip_request                 answered with the task's fast-night hook when it has one (the clock jumps to day)
  guard_refusal                      kept for the `guard` grader (an agent declaring the `guard` capability)
  death, block_broken, anything else kept in the event log
  goal_end                           the agent's run is over (stop, summary, seconds, cost_usd)
  trace                              the trace (also read from --out when the line never comes)

Lines without the prefix are passed through as progress. The runner also takes a frame every FRAME_EVERY seconds, so an
agent that reports nothing is graded on the same footing. With a day clock (`ctx.dawns`) it takes a `dawn_N` frame at
each dawn, and a task that ends at a dawn (`ctx.end_at_dawn`) has its agent held still and stopped there. SIGTERM asks
the agent to stop and write its trace; SIGKILL follows 15 s later. With a live grader (`ctx.goal_met`) the agent is
stopped as soon as the task passes on a world held still (SIGSTOP while it is confirmed, then SIGTERM + SIGCONT).
"""
from __future__ import annotations

import json
import os
import queue
import re
import shutil
import signal
import subprocess
import threading
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .config import AGENTS_DIR, agent_path

PROTOCOL_VERSION = 1
GRACE_SECONDS = 90          # past the task clock before the process is stopped: the agent stops at the clock itself
STEP_EVENTS = ("subgoal_completed", "subgoal_failed")
MAX_SUBGOALS = 2000      # the steps a trace keeps (a run of thousands is one long loop already)
LOST_CHECK_EVERY = 3.0      # seconds between reads of what ends a trial as failed (fail_on_death: the death counter)
GOAL_CHECK_EVERY = 10.0     # seconds between live grades; stretched when one takes long (a big plot to scan)
CONFIRM_SETTLE = 1.5        # a pass is graded again after the agent has been held still this long
FRAME_EVERY = 30.0          # seconds between the runner's own frames, whatever the agent reports
DAWN_POLL_EVERY = 5.0       # seconds between reads of the day clock (100 ticks: no night slips between two)
KILL_AFTER = 15.0           # seconds from SIGTERM to SIGKILL
CAPABILITIES = ("guard", "fast_nights")   # what a task may require of an agent beyond the protocol (Task.requires)


# ------------------------------------------------------------------ manifests

@dataclass
class Manifest:
    """<name>.toml (PROTOCOL.md §1). `args` items are strings (always passed) or lists (a group passed only when every
    placeholder in it has a value: ["--model", "{model}"] is left out when no model was given). Placeholders:
    {provider}, {model}, and {VAR} for any key under [vars]. `cwd` and `env` values may use ${ENV:-default}."""
    name: str
    command: list[str]
    cwd: str = "."
    prefix: str = ""
    args: list = field(default_factory=list)
    env: dict = field(default_factory=dict)
    capabilities: list[str] = field(default_factory=list)
    requires_files: list[str] = field(default_factory=list)   # relative to cwd: what a working checkout has
    provider: str = ""                                         # defaults for {provider} / {model}
    model: str = ""
    label: str = ""                                            # the model column when no model is given, e.g. "myagent ({provider})"
    vars: dict = field(default_factory=dict)
    goal_format: str = "json"                                  # "text": the goal file is the prompt alone (an agent that
                                                               # predates the JSON goal file); the rest of the goal is lost
    description: str = ""
    path: Path | None = None

    @property
    def line_prefix(self) -> str:
        return (self.prefix or f"@{self.name}") + " "

    def directory(self) -> Path:
        d = Path(expand_env(self.cwd)).expanduser()
        if not d.is_absolute():
            d = (self.path.parent if self.path else AGENTS_DIR) / d
        return d.resolve()


def expand_env(text: str) -> str:
    """${VAR} and ${VAR:-default} from the environment."""
    def sub(m: re.Match) -> str:
        name, default = m.group(1), m.group(3)
        return os.environ.get(name) or (default if default is not None else "")
    return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(:-([^}]*))?\}", sub, text)


def manifest_files() -> dict[str, Path]:
    """name -> manifest file over agent_path(), in order: the first manifest of a name wins."""
    found: dict[str, Path] = {}
    for entry in agent_path():
        for f in [entry] if entry.is_file() else sorted(entry.glob("*.toml")) if entry.is_dir() else []:
            found.setdefault(f.stem, f)
    return found


def load_manifest(name_or_path: str) -> Manifest:
    p = Path(name_or_path)
    if not p.suffix:
        have = manifest_files()
        if name_or_path not in have:
            raise SystemExit(f"no agent manifest {name_or_path!r}; available: {sorted(have)}. Set MCMSBENCH_AGENT_PATH "
                             f"to the directories holding your manifests (PROTOCOL.md §1), or pass a manifest's path")
        p = have[name_or_path]
    if not p.exists():
        raise SystemExit(f"no agent manifest at {p}")
    raw = tomllib.loads(p.read_text())
    raw.setdefault("name", p.stem)
    unknown = set(raw) - set(Manifest.__dataclass_fields__) - {"path"}
    if unknown or "path" in raw:
        raise ValueError(f"{p}: unknown keys {sorted(unknown | ({'path'} & set(raw)))}")
    cmd = raw.get("command")
    if isinstance(cmd, str) or not cmd:
        raise ValueError(f"{p}: `command` must be a non-empty list")
    if raw.get("goal_format", "json") not in ("json", "text"):
        raise ValueError(f"{p}: goal_format is json or text")
    bad = set(raw.get("capabilities") or []) - set(CAPABILITIES)
    if bad:
        raise ValueError(f"{p}: unknown capabilities {sorted(bad)} (known: {list(CAPABILITIES)})")
    return Manifest(**raw, path=p)


def list_manifests() -> list[Manifest]:
    return [load_manifest(str(p)) for _, p in sorted(manifest_files().items())]


_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def render_args(items: list, values: dict) -> list[str]:
    def fill(s: str) -> str | None:
        missing = [k for k in _PLACEHOLDER.findall(s) if not values.get(k)]
        return None if missing else _PLACEHOLDER.sub(lambda m: str(values[m.group(1)]), s)
    out: list[str] = []
    for it in items:
        if isinstance(it, list):
            filled = [fill(str(s)) for s in it]
            if all(f is not None for f in filled):
                out += filled
        else:
            f = fill(str(it))
            if f is None:
                raise ValueError(f"argument {it!r} needs a value for {_PLACEHOLDER.findall(str(it))}: put it in a group")
            out.append(f)
    return out


# ------------------------------------------------------------------ one trial's hand-off

@dataclass
class TrialContext:
    """What the runner hands an agent run."""
    task_id: str
    goal: dict                                  # the goal file's contents (see PROTOCOL.md)
    host: str
    port: int
    username: str
    version: str
    max_seconds: float
    max_cost_usd: float
    stem: Path                                  # <run>/<task>/trial_N: the agent's files go next to it
    hand_off: Callable[[], None]                # log the stand-in out: the player is the agent's
    take_back: Callable[[], None]               # log the stand-in back in for the final reads
    progress: Callable[[str], None] = print
    after_step: Callable[[str], None] | None = None   # take a frame with this label
    goal_met: Callable[[], bool] | None = None        # the grader on the live world; None: not gradable mid-trial
    night_skip: Callable[[], None] | None = None      # the task's fast-night hook; None: real nights
    on_event: Callable[[dict], None] | None = None    # every protocol event but the trace, as the agent sent it
    dawns: Callable[[], int] | None = None            # the dawns seen so far (the runner's day clock); None: no clock
    end_at_dawn: int | None = None                    # stop the agent at this dawn (the task's end), frame taken first
    lost: Callable[[], str | None] | None = None      # why the trial can no longer pass (a death), or None: polled like
                                                      # goal_met, and the agent is stopped once it says why
    log: object | None = None                         # the trial's EventLog
    stop: threading.Event | None = None


class Agent:
    """One manifest with this run's provider and model."""

    def __init__(self, manifest: Manifest, provider: str | None = None, model: str | None = None,
                 extra_args: list[str] | None = None):
        self.manifest = manifest
        self.name = manifest.name
        self.provider = provider or manifest.provider
        self.model_arg = model or manifest.model
        self.extra_args = list(extra_args or [])
        label = manifest.label.format(provider=self.provider or "", model=self.model_arg or "") if manifest.label else ""
        self.model = self.model_arg or label or self.provider or self.name
        self.dir = manifest.directory()

    @property
    def capabilities(self) -> set[str]:
        return set(self.manifest.capabilities)

    def check_installed(self) -> None:
        """Refuse early, with what to do, when the agent's checkout is not there or not set up."""
        if not self.dir.is_dir():
            raise SystemExit(f"agent {self.name}: no checkout at {self.dir} (see {self.manifest.path})")
        for f in self.manifest.requires_files:
            if not (self.dir / f).exists():
                raise SystemExit(f"agent {self.name}: {self.dir / f} is missing (see {self.manifest.path})")
        exe = self.manifest.command[0]
        if not (shutil.which(exe) or (self.dir / exe).exists()):
            raise SystemExit(f"agent {self.name}: {exe!r} is not on PATH")

    def command(self, ctx: TrialContext, goal_file: Path, out: Path, log: Path) -> list[str]:
        values = {"provider": self.provider, "model": self.model_arg,
                  **{k: expand_env(str(v)) for k, v in self.manifest.vars.items()}}
        return (list(self.manifest.command)
                + ["--host", str(ctx.host), "--port", str(ctx.port), "--username", str(ctx.username),
                   "--version", str(ctx.version), "--goal-file", str(goal_file),
                   "--max-seconds", str(int(ctx.max_seconds)), "--max-cost-usd", str(ctx.max_cost_usd),
                   "--out", str(out), "--log", str(log)]
                + render_args(self.manifest.args, values) + self.extra_args)

    def environment(self) -> dict:
        """The bench's environment, the manifest's additions, and the protocol version; never the held-out key (an
        agent that could read it could draw the held-out instances itself), nor where the held-out pack is."""
        from .variants import KEY_ENV, PACK_ENV
        env = {**os.environ, **{k: expand_env(str(v)) for k, v in self.manifest.env.items()},
               "MCMSBENCH_PROTOCOL": str(PROTOCOL_VERSION)}
        env.pop(KEY_ENV, None)
        env.pop(PACK_ENV, None)
        return env

    def run(self, ctx: TrialContext) -> dict:
        tag = f"[{self.name}]"
        stem = Path(ctx.stem)
        stem.parent.mkdir(parents=True, exist_ok=True)
        goal_file = Path(f"{stem}_goal.json")
        goal_file.write_text(json.dumps(ctx.goal, indent=1, default=str))     # kept with the record either way
        if self.manifest.goal_format == "text":
            goal_file = Path(f"{stem}_goal.txt")
            goal_file.write_text(str(ctx.goal.get("prompt", "")))
        out, log = Path(f"{stem}_agent.json"), Path(f"{stem}_agent.jsonl")
        out.unlink(missing_ok=True)
        elog = ctx.log
        if elog is not None:
            elog.emit("task_start", prompt=str(ctx.goal.get("prompt", ""))[:3000], agent=self.name, model=self.model,
                      agent_log=str(log))

        trace: dict | None = None
        output: list[str] = []
        steps = 0
        timer_frames = 0
        subgoals: list[dict] = []        # the agent's steps as it reported them, for the loop measure (loops.py)
        refusals: list = []
        stopped_by: str | None = None
        ended = False                   # the agent sent goal_end: its run is over, whatever the process still writes
        t0 = time.time()
        prefix = self.manifest.line_prefix
        cmd = self.command(ctx, goal_file, out, log)
        ctx.hand_off()
        proc = None
        try:
            proc = subprocess.Popen(cmd, cwd=self.dir, env=self.environment(), stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, bufsize=1, start_new_session=True)
            done = threading.Event()

            def watchdog() -> None:
                deadline = time.monotonic() + ctx.max_seconds + GRACE_SECONDS
                while not done.wait(1.0):
                    stop = ctx.stop is not None and ctx.stop.is_set()
                    if stop or time.monotonic() > deadline:
                        ctx.progress(f"{tag} {'stop asked' if stop else 'past the clock'}: terminating")
                        terminate(proc)
                        return

            threading.Thread(target=watchdog, daemon=True).start()
            # the agent's lines come through a queue, so this thread can grade and take frames between them: the
            # observer and RCON stay on this one thread
            lines: queue.Queue = queue.Queue()

            def reader() -> None:
                for raw in proc.stdout:
                    lines.put(raw)
                lines.put(None)

            threading.Thread(target=reader, daemon=True).start()
            next_check = time.monotonic() + GOAL_CHECK_EVERY
            next_lost = time.monotonic() + LOST_CHECK_EVERY
            next_frame = time.monotonic() + FRAME_EVERY
            next_dawn = time.monotonic() + DAWN_POLL_EVERY
            dawns_seen = 0
            try:
                while True:
                    try:
                        line = lines.get(timeout=1.0)
                    except queue.Empty:
                        line = ""
                    if line is None:
                        break
                    running = stopped_by is None and not ended and proc.poll() is None
                    if running and ctx.dawns is not None and time.monotonic() >= next_dawn:
                        next_dawn = time.monotonic() + DAWN_POLL_EVERY
                        n = self._dawns(ctx, tag, dawns_seen)
                        last = ctx.end_at_dawn is not None and n >= ctx.end_at_dawn
                        if last:            # the world as the task ends: the agent held still before the frame
                            with_suppress(lambda: signal_group(proc, signal.SIGSTOP))
                        for k in range(dawns_seen + 1, n + 1):
                            ctx.progress(f"{tag} dawn {k} (day {k + 1}) at {time.time() - t0:.0f} s")
                            if ctx.after_step:
                                self._frame(ctx, f"dawn_{k}", tag)
                        dawns_seen = n
                        if last:
                            stopped_by = "dawn"
                            ctx.progress(f"{tag} day {n + 1} has begun: the task ends here, stopping the agent")
                            terminate(proc, held=True)
                            running = False
                    if running and ctx.after_step and time.monotonic() >= next_frame:
                        timer_frames += 1
                        self._frame(ctx, f"t{int(time.time() - t0):04d}", tag)
                        next_frame = time.monotonic() + FRAME_EVERY
                    # not once the agent has said its run is over: its client is logging off, and a grade then reads
                    # the player as dead with no respawn point
                    if running and ctx.goal_met is not None and time.monotonic() >= next_check:
                        t = time.monotonic()
                        met = self._grade(ctx, tag)
                        next_check = time.monotonic() + max(GOAL_CHECK_EVERY, 4 * (time.monotonic() - t))
                        if met and self._confirm(proc, ctx, tag, t0):
                            stopped_by = "grader"
                            ctx.progress(f"{tag} the grader passes at {time.time() - t0:.0f} s: stopping the agent")
                            terminate(proc, held=True)
                    if running and stopped_by is None and ctx.lost is not None and time.monotonic() >= next_lost:
                        next_lost = time.monotonic() + LOST_CHECK_EVERY
                        why = self._lost(ctx, tag)
                        if why:
                            with_suppress(lambda: signal_group(proc, signal.SIGSTOP))
                            stopped_by = "lost"
                            ctx.progress(f"{tag} {why} at {time.time() - t0:.0f} s: the task can no longer pass, stopping the agent")
                            if ctx.after_step:
                                self._frame(ctx, "lost", tag)
                            terminate(proc, held=True)
                    if not line:
                        continue
                    line = line.rstrip("\n")
                    if not line.startswith(prefix):
                        output.append(line)
                        ctx.progress(f"{tag} {line}")
                        continue
                    try:
                        ev = json.loads(line[len(prefix):])
                    except json.JSONDecodeError:
                        ev = None
                    if not isinstance(ev, dict):
                        output.append(line)
                        continue
                    kind = ev.get("event")
                    if kind == "trace":
                        trace = ev.get("trace") if isinstance(ev.get("trace"), dict) else None
                        continue
                    if ctx.on_event:
                        ctx.on_event(ev)
                    if elog is not None:
                        elog.emit("agent_event", **{k: v for k, v in ev.items() if k not in ("t", "kind")})
                    if kind == "subgoal_started":
                        ctx.progress(f"{tag} {ev.get('subgoal')}: {ev.get('text', '')}")
                    elif kind in STEP_EVENTS:
                        steps += 1
                        if len(subgoals) < MAX_SUBGOALS:
                            subgoals.append({"t": round(time.time() - t0, 1), "event": kind,
                                             "subgoal": str(ev.get("subgoal") or "")[:200], "text": str(ev.get("text") or "")[:200]})
                        ctx.progress(f"{tag} {ev.get('subgoal')} {kind.removeprefix('subgoal_')}: {ev.get('text', '')}")
                        if ctx.after_step and stopped_by is None:
                            self._frame(ctx, f"step_{steps:02d}", tag)
                            next_frame = time.monotonic() + FRAME_EVERY
                    elif kind == "guard_refusal":
                        refusals.append({k: v for k, v in ev.items() if k != "event"})
                    elif kind == "night_skip_request":
                        if ctx.night_skip is None:
                            ctx.progress(f"{tag} asked to skip the night: this task has no fast nights")
                        else:
                            try:
                                ctx.night_skip()
                                ctx.progress(f"{tag} night fast-forwarded after {ev.get('held_s', '?')} s sheltered")
                            except Exception as e:  # noqa: BLE001 — a failed skip is a real night, not a failed trial
                                ctx.progress(f"{tag} the night could not be fast-forwarded: {e}")
                    elif kind == "goal_end":
                        ended = True
                        try:
                            cost = float(ev.get("cost_usd") or 0)
                        except (TypeError, ValueError):
                            cost = 0.0
                        ctx.progress(f"{tag} goal {ev.get('stop')}: {ev.get('summary', '')} ({ev.get('seconds')}s, ${cost:.4f})")
                proc.wait()
            finally:
                done.set()
            if trace is None and out.exists():
                try:
                    trace = json.loads(out.read_text())
                except json.JSONDecodeError:
                    trace = None
        finally:
            if proc is not None and proc.poll() is None:
                terminate(proc)
                with_suppress(lambda: proc.wait(KILL_AFTER + 5))
            ctx.take_back()

        tail = "\n".join(output)[-20000:]
        code = proc.returncode if proc is not None else None
        if not isinstance(trace, dict):
            msg = f"{self.name} exited {code} without a trace: {tail[-500:] or '(no output)'}"
            trace = {"turns": 0, "cost_usd": 0.0, "steps": [], "done": None, "stop": "error", "error": msg}
        trace = normalize_trace(trace)
        trace["solver"] = self.name
        trace["agent"] = self.name
        trace["model"] = trace.get("model") if trace.get("model") not in (None, "", "-") else self.model
        trace["frames"] = steps
        trace["subgoals"] = subgoals
        trace["timer_frames"] = timer_frames
        trace["exit_code"] = code
        trace["guard_refusals"] = refusals
        if stopped_by:
            # the run ended on the bench's word, not the agent's: the grader passed, the task's last dawn came, or the
            # task could no longer pass (fail_on_death: the player died)
            trace["stopped_by"] = stopped_by
            trace["stop"] = {"grader": "grader_passed", "dawn": "dawn_reached", "lost": "task_lost"}[stopped_by]
        trace["stdout"] = tail
        trace["agent_log"] = str(log)
        trace.setdefault("seconds", round(time.time() - t0, 1))
        if trace.get("stop") not in ("error", "killed"):
            trace.pop("error", None)    # the runner reads `error` as a crashed trial
        if elog is not None:
            elog.emit("task_end", stop=trace.get("stop"), turns=trace.get("turns", 0), cost_usd=trace.get("cost_usd", 0.0))
        return trace

    @staticmethod
    def _frame(ctx: TrialContext, label: str, tag: str) -> None:
        try:
            ctx.after_step(label)
        except Exception as e:  # noqa: BLE001 — a missed frame is not a failed trial
            ctx.progress(f"{tag} frame {label} failed: {e}")

    @staticmethod
    def _dawns(ctx: TrialContext, tag: str, seen: int) -> int:
        try:
            return max(seen, int(ctx.dawns()))
        except Exception as e:  # noqa: BLE001 — a missed read is caught up by the next one
            ctx.progress(f"{tag} day clock read failed: {type(e).__name__}: {e}")
            return seen

    @staticmethod
    def _lost(ctx: TrialContext, tag: str) -> str | None:
        try:
            return ctx.lost()
        except Exception as e:  # noqa: BLE001 — a failed read is not a lost trial
            ctx.progress(f"{tag} lost check failed: {type(e).__name__}: {e}")
            return None

    @staticmethod
    def _grade(ctx: TrialContext, tag: str) -> bool:
        try:
            return bool(ctx.goal_met())
        except Exception as e:  # noqa: BLE001 — a failed live grade is not a failed trial
            ctx.progress(f"{tag} live grade failed: {type(e).__name__}: {e}")
            return False

    def _confirm(self, proc, ctx: TrialContext, tag: str, t0: float) -> bool:
        """A pass seen while the agent acts may not last: a block it had already sent lands after the snapshot. The
        agent is held still (SIGSTOP), what it sent is given time to land, and the grader asked again on a world nothing
        is changing. Passing, the agent stays held for the caller to stop; not, it is let go and plays on."""
        if proc.poll() is not None:
            return True                 # nothing to hold (it has exited): the pass stands, the final grade decides
        with_suppress(lambda: signal_group(proc, signal.SIGSTOP))
        time.sleep(CONFIRM_SETTLE)
        again = self._grade(ctx, tag)
        if not again:
            ctx.progress(f"{tag} the grader passed at {time.time() - t0:.0f} s but not on a settled world: playing on")
            with_suppress(lambda: signal_group(proc, signal.SIGCONT))
        return again


def signal_group(proc, sig) -> None:
    """A signal to the agent's whole process group: a launcher (node bin/x.js, uv run) and what it started."""
    try:
        os.killpg(os.getpgid(proc.pid), sig)
    except (ProcessLookupError, PermissionError):
        proc.send_signal(sig)


def terminate(proc, held: bool = False) -> None:
    """SIGTERM (the agent stops and writes its trace), SIGKILL KILL_AFTER seconds later if it is still there. `held`:
    the agent is stopped (SIGSTOP) and gets SIGCONT after the SIGTERM, so it wakes up to it."""
    if proc.poll() is not None:
        return
    with_suppress(lambda: signal_group(proc, signal.SIGTERM))
    if held:
        with_suppress(lambda: signal_group(proc, signal.SIGCONT))

    def kill() -> None:
        if proc.poll() is None:
            with_suppress(lambda: signal_group(proc, signal.SIGKILL))
    t = threading.Timer(KILL_AFTER, kill)
    t.daemon = True
    t.start()


def with_suppress(fn) -> None:
    try:
        fn()
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass


def normalize_trace(trace: dict) -> dict:
    """An agent's trace with the required keys in the bench's shape (PROTOCOL.md): `done` as {success, summary} (a bare
    flag is kept as `done_flag`), numbers as numbers, `steps` a list."""
    t = dict(trace)
    done = t.get("done")
    if done is not None and not isinstance(done, dict):
        t["done_flag"] = done
        t["done"] = {"success": bool(done), "summary": f"{'done' if done else 'not done'} ({t.get('stop')})"}
    try:
        t["turns"] = int(t.get("turns") or 0)
    except (TypeError, ValueError):
        t["turns"] = 0
    for k in ("cost_usd", "seconds"):
        if k in t:
            try:
                t[k] = float(t.get(k) or 0.0)
            except (TypeError, ValueError):
                t[k] = 0.0
    t.setdefault("cost_usd", 0.0)
    if not isinstance(t.get("steps"), list):
        t["steps"] = []
    return t
