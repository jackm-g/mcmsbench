import fcntl
import os
import threading

import pytest

from mcmsbench import lease


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    monkeypatch.setenv("MCMSBENCH_LOCK_DIR", str(tmp_path))
    monkeypatch.delenv("MCMSBENCH_ONE_AT_A_TIME", raising=False)
    yield
    lease.release_all()


def _other_run(path):
    """Another run's hold: a separate open file description, as another process would have."""
    fd = os.open(path, os.O_RDONLY | os.O_CREAT, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fd


def test_off_without_a_lock_dir(monkeypatch, tmp_path):
    monkeypatch.delenv("MCMSBENCH_LOCK_DIR")
    lease.hold(25567)
    assert list(tmp_path.iterdir()) == []


def test_a_held_arena_stops_the_second_run(tmp_path):
    fd = _other_run(tmp_path / "survival-25568.lock")
    try:
        with pytest.raises(SystemExit, match="another run holds the arena"):
            lease.hold(25568)
        lease.hold(25567)                                  # another slot is free
        lease.hold(25567)                                  # and holding it again is a no-op
    finally:
        os.close(fd)


def test_one_at_a_time_waits_for_the_other_run(monkeypatch, tmp_path):
    monkeypatch.setenv("MCMSBENCH_ONE_AT_A_TIME", "1")
    fd = _other_run(tmp_path / "trial.lock")
    done = threading.Event()
    t = threading.Thread(target=lambda: (lease.hold(25568, log=lambda _: None), done.set()), daemon=True)
    t.start()
    assert not done.wait(0.3)                              # waiting while the other run plays
    os.close(fd)
    assert done.wait(5)
