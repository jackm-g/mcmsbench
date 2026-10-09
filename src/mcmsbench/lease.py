"""Arena leases: one run at a time on each survival arena, across checkouts and users of one machine.

Off unless MCMSBENCH_LOCK_DIR is set (a shared box sets it; one checkout on a laptop has no need). A run that takes
control of a survival arena holds an exclusive flock on <dir>/survival-<port>.lock until the process exits, so a
crash lets it go. A second run on the same arena stops at once and says so. With MCMSBENCH_ONE_AT_A_TIME set, a run
first waits for <dir>/trial.lock: a machine too small for two arenas at once runs them one after another.

The files are opened read-only (flock needs no write access), so a lock file one user made serves the others.
"""
from __future__ import annotations

import fcntl
import os
from pathlib import Path

_held: dict[str, int] = {}


def hold(port: int, log=print) -> None:
    d = os.environ.get("MCMSBENCH_LOCK_DIR", "").strip()
    if not d:
        return
    root = Path(d)
    root.mkdir(parents=True, exist_ok=True)
    if os.environ.get("MCMSBENCH_ONE_AT_A_TIME", "").strip() not in ("", "0"):
        _take(root / "trial.lock", wait=True, log=log)
    _take(root / f"survival-{port}.lock", wait=False, log=log)


def _take(path: Path, wait: bool, log) -> None:
    if str(path) in _held:
        return
    fd = os.open(path, os.O_RDONLY | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        if not wait:
            os.close(fd)
            raise SystemExit(f"another run holds the arena behind {path} (pick another --slot, or wait for it)")
        log(f"[lease] another run is on this machine; waiting for {path}")
        fcntl.flock(fd, fcntl.LOCK_EX)
    _held[str(path)] = fd


def release_all() -> None:
    """Let every lease go (tests; a run's leases otherwise last until its process exits)."""
    for fd in _held.values():
        os.close(fd)
    _held.clear()
