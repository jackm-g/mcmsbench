"""Timed trial events: templated like setup, fired on their own clock while the solver works."""
import time

import pytest

from mcmsbench.arena import Plot
from mcmsbench.tasks import Task
from mcmsbench.world.volume import Volume


def plot():
    return Plot(0, (0, 64, 0), Volume((-48, 65, -48), (48, 100, 48)), flat=False)


def test_task_events_are_templated_and_ordered(tmp_path):
    y = tmp_path / "t.yaml"
    y.write_text("prompt: x\nworld: {type: survival}\nevents:\n"
                 "  - {at: 30, run: 'summon zombie {bx+3} {by} {bz}'}\n  - {at: 5, run: 'kill {bot}'}\n")
    t = Task.from_yaml(y)
    assert t.render_events(plot(), start=(10, 70, -5), bot="player") == [(5.0, "kill player"), (30.0, "summon zombie 13 70 -5")]
    with pytest.raises(ValueError):
        Task(id="t", prompt="p", events=[{"at": 3}]).render_events(plot())
    with pytest.raises(ValueError):
        Task(id="t", prompt="p", events=[{"at": -1, "run": "kill {bot}"}]).render_events(plot())
    assert Task(id="t", prompt="p").render_events(plot()) == []


def test_event_runner_fires_in_order_and_records_output():
    from mcmsbench.runner import EventRunner
    sent = []

    def rcon(cmd):
        sent.append(cmd)
        return "Killed player" if cmd.startswith("kill") else "ok"
    t0 = time.time()
    ev = EventRunner("h", 0, "pw", [(0.3, "summon zombie 1 2 3"), (0.0, "kill player")], t0, rcon=rcon)
    deadline = time.time() + 5
    while len(ev.fired) < 2 and time.time() < deadline:
        time.sleep(0.05)
    fired = ev.stop()
    assert sent == ["kill player", "summon zombie 1 2 3"]
    assert [f["run"] for f in fired] == sent and fired[0]["out"] == "Killed player" and fired[1]["at"] == 0.3
    # stopping early cancels what has not fired
    ev = EventRunner("h", 0, "pw", [(30, "kill player")], time.time(), rcon=rcon)
    assert ev.stop() == [] and sent[-1] == "summon zombie 1 2 3"


def test_gated_events_wait_for_their_test_and_hold_the_rest(tmp_path):
    y = tmp_path / "t.yaml"
    y.write_text("prompt: x\nworld: {type: survival}\nevents:\n"
                 "  - {at: 1, when: 'execute unless entity @e[tag=w]', run: ['summon zombie {bx+9} {by} {bz}', 'say two']}\n"
                 "  - {at: 1, run: 'say after'}\n")
    t = Task.from_yaml(y)
    assert t.render_events(plot(), start=(10, 70, -5)) == [
        (1.0, "summon zombie 19 70 -5", "execute unless entity @e[tag=w]"), (1.0, "say two"), (1.0, "say after")]
    with pytest.raises(ValueError):
        Task(id="t", prompt="p", events=[{"at": 1, "run": []}]).render_events(plot())

    from mcmsbench.runner import EventRunner
    sent, alive = [], {"w": True}

    def rcon(cmd):
        sent.append(cmd)
        if cmd.startswith("execute unless"):
            return "Test failed" if alive["w"] else "Test passed"
        return "ok"
    EventRunner.POLL = 0.05
    try:
        ev = EventRunner("h", 0, "pw", [(0.0, "summon zombie", "execute unless entity @e[tag=w]"), (0.0, "say after")],
                         time.time(), rcon=rcon)
        time.sleep(0.3)
        assert "summon zombie" not in sent and "say after" not in sent     # held by the test, and the one after it too
        alive["w"] = False
        deadline = time.time() + 5
        while len(ev.fired) < 2 and time.time() < deadline:
            time.sleep(0.05)
        fired = ev.stop()
        assert [f["run"] for f in fired] == ["summon zombie", "say after"]
        # a test that never passes is cancelled by stop()
        alive["w"] = True
        ev = EventRunner("h", 0, "pw", [(0.0, "summon zombie", "execute unless entity @e[tag=w]")], time.time(), rcon=rcon)
        time.sleep(0.15)
        assert ev.stop() == []
    finally:
        EventRunner.POLL = 1.0
