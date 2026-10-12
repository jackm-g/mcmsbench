"""The candidate ledgers under mining/: validate them, list them for triage, and digest the existing tasks.

    uv run python tools/mine/ledger.py check [--root mining]
    uv run python tools/mine/ledger.py list [--status candidate] [--tier tier-c] [--world flat] [--root mining]
    uv run python tools/mine/ledger.py tasks [--tasks tasks]

`check` reads every mining/*/ledger.yaml and reports what is off: a status, tier or world outside the vocabulary, a
source range outside the video, an id that is not a task slug or that repeats or collides with an existing task
(unless drafted), an overlap naming no task, a check kind the bench's graders don't know. `tasks` prints one line per
existing task (id, tier, tags, the first sentence of its design notes): the dedupe aid."""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import yaml

STATUSES = {"candidate", "approved", "drafted", "rejected"}
TIERS = {"tier-a", "tier-b", "tier-c", "tier-d"}
WORLDS = {"flat", "survival-1", "survival-2", "new-seed"}
TOP = {"video", "title", "channel", "duration", "watched", "coverage", "candidates"}
FIELDS = {"id", "status", "source", "did", "why", "world", "setup", "grader", "overlaps", "tier"}
SLUG = re.compile(r"^[a-z][a-z0-9_]*$")


def _hms(s) -> float:
    if isinstance(s, (int, float)):
        return float(s)
    secs = 0.0
    for p in str(s).split(":"):
        secs = secs * 60 + float(p)
    return secs


def task_ids(tasks_dir: Path) -> set[str]:
    return {p.stem for p in tasks_dir.glob("*.yaml")}


def grader_kinds() -> set[str] | None:
    """The bench's grader registry, or None when the bench is not importable (then kinds go unchecked)."""
    try:
        from mcmsbench.graders import REGISTRY       # noqa: PLC0415
        return set(REGISTRY)
    except Exception:                                 # pragma: no cover
        return None


def load_ledgers(root: Path) -> list[tuple[Path, dict]]:
    out = []
    for p in sorted(root.glob("*/ledger.yaml")):
        out.append((p, yaml.safe_load(p.read_text()) or {}))
    return out


def validate(ledger: dict, ids: set[str], kinds: set[str] | None, seen: set[str] | None = None,
             tasks_dir: Path | None = None) -> list[str]:
    """Problems with one ledger, as messages. `seen` collects candidate ids across ledgers for uniqueness."""
    bad: list[str] = []
    seen = seen if seen is not None else set()
    missing = TOP - set(ledger)
    if missing:
        bad.append(f"missing top-level keys: {sorted(missing)}")
    duration = ledger.get("duration")
    for i, c in enumerate(ledger.get("candidates") or []):
        tag = f"candidates[{i}]"
        if not isinstance(c, dict):
            bad.append(f"{tag}: not a mapping")
            continue
        cid = c.get("id", "")
        tag = f"{cid or tag}"
        if not isinstance(cid, str) or not SLUG.match(cid):
            bad.append(f"{tag}: id is not a task slug")
        elif cid in seen:
            bad.append(f"{tag}: id repeats across the ledgers")
        else:
            seen.add(cid)
        miss = FIELDS - set(c)
        if miss:
            bad.append(f"{tag}: missing {sorted(miss)}")
        status = c.get("status")
        if status not in STATUSES:
            bad.append(f"{tag}: status {status!r} not in {sorted(STATUSES)}")
        if c.get("tier") not in TIERS:
            bad.append(f"{tag}: tier {c.get('tier')!r} not in {sorted(TIERS)}")
        if c.get("world") not in WORLDS:
            bad.append(f"{tag}: world {c.get('world')!r} not in {sorted(WORLDS)}")
        if isinstance(cid, str) and cid in ids and status != "drafted":
            bad.append(f"{tag}: collides with the existing task {cid} (status drafted if it is that task)")
        if status == "drafted":
            t = c.get("task")
            if not t:
                bad.append(f"{tag}: drafted but names no task:")
            elif t not in ids or (tasks_dir and not (tasks_dir / f"{t}.yaml").exists()):
                bad.append(f"{tag}: drafted task {t} is not in tasks/")
        src = c.get("source") or {}
        try:
            f, t = _hms(src.get("from")), _hms(src.get("to"))
            if not f < t:
                bad.append(f"{tag}: source from {src.get('from')} is not before to {src.get('to')}")
            elif duration and t > float(duration) + 1:
                bad.append(f"{tag}: source to {src.get('to')} is past the video's {duration}s")
        except (TypeError, ValueError):
            bad.append(f"{tag}: source needs from/to timestamps")
        for o in c.get("overlaps") or []:
            if o not in ids:
                bad.append(f"{tag}: overlaps {o!r} is no task")
        g = c.get("grader") or {}
        steps = g.get("steps") if isinstance(g, dict) else None
        if kinds is not None and isinstance(steps, list):
            for s in steps:
                k = ((s or {}).get("check") or {}).get("kind") if isinstance(s, dict) else None
                if k not in kinds:
                    bad.append(f"{tag}: step {(s or {}).get('name')!r} check kind {k!r} is no grader")
        if isinstance(g, dict) and isinstance(g.get("kind"), str) and kinds is not None and g["kind"] not in kinds:
            bad.append(f"{tag}: grader kind {g['kind']!r} is no grader")
    return bad


def digest(tasks_dir: Path) -> list[str]:
    """One line per task: id, tier, tags, the first sentence of its design notes."""
    try:
        from mcmsbench.tasks import load_all       # noqa: PLC0415  (task files hold ${params}; the loader fills them)
        loaded = {k: (t.tags, t.prompt) for k, t in load_all().items()}
    except Exception:                                 # pragma: no cover
        loaded = {}
    out = []
    for p in sorted(tasks_dir.glob("*.yaml")):
        text = p.read_text()
        notes = " ".join(l[1:].strip() for l in text.splitlines() if l.startswith("#"))
        first = re.split(r"(?<=[.!?])\s", notes, maxsplit=1)[0] if notes else ""
        tags, prompt = loaded.get(p.stem, ([], ""))
        tier = next((t for t in tags if t.startswith("tier-")), "?")
        rest = ",".join(t for t in tags if not t.startswith("tier-"))
        line = first or " ".join((prompt or "").split())
        out.append(f"{p.stem:24} {tier:6} {rest:40} {line[:110]}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("check", "list"):
        s = sub.add_parser(name)
        s.add_argument("--root", default="mining")
        s.add_argument("--tasks", default="tasks")
    s = sub.add_parser("tasks")
    s.add_argument("--tasks", default="tasks")
    sub.choices["list"].add_argument("--status")
    sub.choices["list"].add_argument("--tier")
    sub.choices["list"].add_argument("--world")
    a = ap.parse_args()

    if a.cmd == "tasks":
        print("\n".join(digest(Path(a.tasks))))
        return 0
    ids = task_ids(Path(a.tasks))
    ledgers = load_ledgers(Path(a.root))
    if a.cmd == "check":
        seen: set[str] = set()
        kinds = grader_kinds()
        bad = []
        for p, l in ledgers:
            bad += [f"{p}: {m}" for m in validate(l, ids, kinds, seen, Path(a.tasks))]
        print("\n".join(bad) if bad else f"{len(ledgers)} ledger(s), {len(seen)} candidate(s): ok")
        return 1 if bad else 0
    rows = []
    for p, l in ledgers:
        for c in l.get("candidates") or []:
            if a.status and c.get("status") != a.status or a.tier and c.get("tier") != a.tier \
                    or a.world and c.get("world") != a.world:
                continue
            did = " ".join(str(c.get("did", "")).split())
            rows.append(f"{c.get('status', '?'):9} {c.get('tier', '?'):6} {c.get('world', '?'):10} "
                        f"{c.get('id', '?'):32} {l.get('video', '?')} {c.get('source', {}).get('from', '?')}  "
                        f"{did[:90]}  ~{','.join(c.get('overlaps') or []) or '-'}")
    print("\n".join(rows) if rows else "no candidates")
    return 0


if __name__ == "__main__":
    sys.exit(main())
