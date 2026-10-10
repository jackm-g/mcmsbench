"""A fake agent that speaks protocol v1 without a server, for the protocol tests. FAKE_MODE picks its behaviour:

  ok        two subgoals (a frame each), a guard refusal, a night-skip request, goal_end, the trace on stdout and --out
  hang      a subgoal started, then waits for SIGTERM; on it writes its trace to --out only and exits
  no_trace  prints a line and exits 3 without a trace
  bare      a trace whose `done` is a bare flag and whose numbers are strings
  messages  asks for FAKE_NEED (else what the goal's `asked` check names, else leather) about the goal's directive,
            reads the reply from stdin, reports done
            when it was given what it asked for and blocked when not, and says so in its trace's `done`
"""
import argparse
import json
import os
import signal
import sys
import time

p = argparse.ArgumentParser()
for flag in ("host", "port", "username", "version", "goal-file", "max-seconds", "max-cost-usd", "out", "log"):
    p.add_argument(f"--{flag}", required=True)
p.add_argument("--backend")
a, extra = p.parse_known_args()
mode = os.environ.get("FAKE_MODE", "ok")
PREFIX = os.environ.get("FAKE_PREFIX", "@fake")
t0 = time.time()


def emit(event: str, **kw) -> None:
    print(f"{PREFIX} " + json.dumps({"event": event, **kw}), flush=True)


def write(trace: dict) -> None:
    with open(a.out, "w") as f:
        json.dump(trace, f)


raw = open(a.goal_file).read()
goal = json.loads(raw) if raw.lstrip().startswith("{") else {"prompt": raw}
argv_note = {"backend": a.backend, "extra": extra, "username": a.username, "goal_kind": "json" if "task_id" in goal else "text"}

if mode == "ok":
    print("starting up (a line without the prefix)", flush=True)
    emit("subgoal_started", subgoal="s1", text="gather")
    emit("subgoal_completed", subgoal="s1", text="gathered")
    emit("guard_refusal", what="dig", at=[1, 2, 3], why="not yours")
    emit("night_skip_request", held_s=45)
    emit("subgoal_failed", subgoal="s2", text="could not build")
    trace = {"solver": "fake", "model": "fake-1", "turns": 2, "seconds": round(time.time() - t0, 1), "cost_usd": 0.01,
             "steps": [{"turn": 1, "text": "gather"}, {"turn": 2, "text": "build"}],
             "done": {"success": True, "summary": "did it"}, "stop": "done", "goal": goal, "argv": argv_note}
    emit("goal_end", stop="done", summary="did it", seconds=trace["seconds"], cost_usd=0.01)
    emit("trace", trace=trace)
    write(trace)
    sys.exit(0)

if mode == "hang":
    def on_term(signum, frame):
        write({"turns": 1, "cost_usd": 0.0, "steps": [], "done": {"success": False, "summary": "stopped"},
               "stop": "terminated", "argv": argv_note})
        sys.exit(0)
    signal.signal(signal.SIGTERM, on_term)
    emit("subgoal_started", subgoal="s1", text="waiting")
    while True:
        time.sleep(0.05)

if mode == "no_trace":
    print("something went wrong", flush=True)
    sys.exit(3)

if mode == "messages":
    asked = [u.get("check") or {} for u in ((goal.get("check") or {}).get("uncovered") or []) if u.get("kind") == "asked"]
    need = os.environ.get("FAKE_NEED") or (asked[0].get("item") if asked else None) or "leather"
    re_ = (goal.get("directive") or {}).get("id")
    emit("ask", id="a1", to=os.environ.get("FAKE_TO", (goal.get("directive") or {}).get("from") or "Pat"), re=re_,
         need=[{"item": need, "count": 1}], text=f"have you got any {need}?")
    line = sys.stdin.readline()
    reply = json.loads(line) if line.strip() else {}
    got = bool((reply.get("gives") or {}).get(need))
    if got:
        emit("report", re=re_, status="done", text="made it, it's in your chest")
    else:
        emit("report", re=re_, status="blocked", missing=[{"item": need, "count": 1}], text=f"can't: no {need} anywhere")
    trace = {"turns": 1, "cost_usd": 0.0, "steps": [], "stop": "done", "argv": argv_note, "reply": reply,
             "done": {"success": got, "summary": "done" if got else f"blocked: no {need}"}}
    emit("trace", trace=trace)
    write(trace)
    sys.exit(0)

if mode == "bare":
    emit("trace", trace={"turns": "4", "cost_usd": "0.5", "done": True, "stop": "done", "steps": None})
    sys.exit(0)

sys.exit(f"unknown FAKE_MODE {mode}")
