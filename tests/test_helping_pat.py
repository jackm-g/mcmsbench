"""helping_pat: a scripted player, Pat, asks for things in chat (logs in Pat's chest, a correction to the count, come
find me, toss me bread), on seed 2. The task's terms, Pat and Pat's lines as they render, the gates, the goal file (a
multiplayer profile, no check), the grader against a fake server, and the pieces that are new with it (`players`,
`say` events, list-form `when` tests, the `event` grader's `say`, the `chat` grader). No server needed."""
import json
import time
from pathlib import Path

import pytest

from mcmsbench.arena import Plot
from mcmsbench.config import load as load_settings
from mcmsbench.graders import Context, Frame, grade
from mcmsbench.runner import EventRunner, build_goal, stops_on_pass
from mcmsbench.tasks import FILL_LIMIT, Say, Task, load
from mcmsbench.world.volume import Volume

SX, SY, SZ = -1, 63, 5             # seed 2's spawn (infra/worlds/2.json)
PLOT = Plot(0, (SX, SY - 1, SZ), Volume((SX - 48, SY - 12, SZ - 48), (SX + 48, SY + 28, SZ + 48)), flat=False)
TASK = load("helping_pat")
START = (SX, SY, SZ)
FMT = lambda text: TASK._fmt(text, PLOT, START).replace("{bot}", "player")     # noqa: E731
CHEST = (SX - 12, SY + 2, SZ - 4)
PATS = {"diamond": 3, "iron_ingot": 12, "emerald": 2}
EVENTS = TASK.render_events(PLOT, START, "player")


def _says():
    return [cmd.text for _, cmd, *_ in EVENTS if isinstance(cmd, Say)]


# ------------------------------------------------------------------ the task

def test_the_terms():
    w = TASK.world
    assert w.type == "survival" and w.seeds == [2] and w.difficulty == "easy" and not w.daylight
    assert TASK.inventory == {"stone_axe": 1, "bread": 8} and not TASK.scripted and TASK.check is False
    assert "Pat" in TASK.prompt and "chat" in TASK.prompt
    for secret in ("8", "12", "log", "bread", "campfire", "north"):     # what is asked is only ever said in chat
        assert secret not in TASK.prompt
    assert stops_on_pass(TASK)       # every part reads without acting: the bench stops the agent once Pat is all set


def test_seed_2_is_a_prepared_world():
    man = json.loads((Path(__file__).parents[1] / "infra" / "worlds" / "2.json").read_text())
    assert man["seed"] == 2 and tuple(man["spawn"]) == START


def test_pat_stands_outside_the_huts_door_in_adventure_mode():
    [(name, at, mode)] = TASK.render_players(PLOT, START)
    assert name == "Pat" and mode == "adventure" and at == (SX - 7, SY + 2, SZ - 4)
    door = f"setblock {SX - 9} {SY + 2} {SZ - 4} minecraft:oak_door[facing=east,half=lower]"
    assert door in TASK.render_setup(PLOT, START, "player")


def test_setup_fits_the_plot_and_the_fill_limit():
    lo, hi = PLOT.volume.min, PLOT.volume.max
    for c in TASK.render_setup(PLOT, START, "player"):
        p = c.split()
        if p[0] in ("fill", "setblock"):
            n = 6 if p[0] == "fill" else 3
            v = [int(t) for t in p[1:1 + n]]
            for i in range(0, n, 3):
                assert all(lo[k] <= v[i + k] <= hi[k] for k in range(3)), c       # Pat's places are graded: in the plot
            if p[0] == "fill":
                assert (abs(v[3] - v[0]) + 1) * (abs(v[4] - v[1]) + 1) * (abs(v[5] - v[2]) + 1) <= FILL_LIMIT
    chest = next(c for c in TASK.render_setup(PLOT, START, "player") if c.startswith(f"setblock {CHEST[0]} {CHEST[1]} {CHEST[2]} minecraft:chest"))
    for item, n in PATS.items():
        assert f'id:"minecraft:{item}",count:{n}' in chest


# ------------------------------------------------------------------ what Pat says, and when

def test_pats_lines_in_order_and_the_correction_before_the_first_gate():
    says = _says()
    assert len(says) == 5 and "8 logs" in says[0] and "12 logs" in says[1] and "campfire" in says[2]
    assert "5 bread" in says[3] and "all set" in says[4]
    assert all(len(s) < 256 for s in says)                 # chat's limit
    assert [e[0] for e in EVENTS if isinstance(e[1], Say)] == [4.0, 50.0, 51.0, 52.0, 53.0]
    assert all(cmd.player == "Pat" for _, cmd, *_ in EVENTS if isinstance(cmd, Say))


def test_the_gates_count_logs_in_the_chest_find_the_bot_and_count_pats_bread():
    gated = [(cmd, when) for _, cmd, *w in EVENTS for when in w]
    assert len(gated) == 3
    (logs_say, logs), (near_say, near), (bread_say, bread) = gated
    assert logs == (f"execute store result score #logs bench_pat if items block {CHEST[0]} {CHEST[1]} {CHEST[2]} "
                    "container.* #minecraft:logs", "execute if score #logs bench_pat matches 12..")
    assert near == "execute at Pat if entity @a[name=player,distance=..4]"
    assert bread == ("execute store result score #bread bench_pat run clear Pat minecraft:bread 0",
                     "execute if score #bread bench_pat matches 5..")
    # the logs' gate says thanks first, then Pat goes to the camp: on the pad, beside the campfire
    i = next(i for i, e in enumerate(EVENTS) if e[1] is logs_say)
    assert EVENTS[i + 1] == (51.0, f"tp Pat {SX + 1} {SY + 6} {SZ - 45}")
    setup = TASK.render_setup(PLOT, START, "player")
    assert f"setblock {SX + 3} {SY + 6} {SZ - 45} minecraft:campfire" in setup
    assert f"fill {SX} {SY + 5} {SZ - 46} {SX + 4} {SY + 5} {SZ - 44} minecraft:cobblestone" in setup
    assert "scoreboard players reset * bench_pat" in setup


# ------------------------------------------------------------------ the goal file

def test_the_goal_file_is_multiplayer_names_pat_and_hands_over_no_check():
    goal = build_goal(TASK, PLOT, START, load_settings(), 0, seed=2, fast_nights=False, grader_stops=True,
                      max_seconds=900, max_cost=5)
    assert goal["profile"]["multiplayer"] is True and goal["profile"]["players"] == ["Pat"]
    assert goal["check"] is None and goal["world"]["seed"] == 2
    solo = build_goal(load("gather_wood"), PLOT, START, load_settings(), 0, seed=1, fast_nights=False,
                      grader_stops=True, max_seconds=600, max_cost=5)
    assert solo["profile"]["multiplayer"] is False and solo["profile"]["players"] == []


# ------------------------------------------------------------------ the grader against a fake server

def _snbt(held: dict) -> str:
    items = ", ".join(f'{{Slot: {i}b, count: {n}, id: "minecraft:{k}"}}' for i, (k, n) in enumerate(held.items()))
    return f"{CHEST[0]} {CHEST[1]} {CHEST[2]} has the following block data: [{items}]"


class Server:
    """Pat's chest: its contents, or gone."""

    def __init__(self, held: dict | None):
        self.held = held

    def __call__(self, cmd):
        if cmd.startswith("forceload query"):
            return "Chunk at [0, 0] in minecraft:overworld is marked for force loading"
        if cmd == f"data get block {CHEST[0]} {CHEST[1]} {CHEST[2]} Items":
            return "The target block is not a block entity" if self.held is None else _snbt(self.held)
        return "ok"


HUT = {(SX - 13 + dx, SY + 2, SZ - 6): "oak_planks" for dx in range(5)} | {CHEST: "chest"}
SAID = [{"at": 4.0, "t": 4.2, "say": _says()[0], "as": "Pat"}, {"at": 50.0, "t": 50.1, "say": _says()[1], "as": "Pat"},
        {"at": 51.0, "t": 140.0, "say": _says()[2], "as": "Pat"}, {"at": 51.0, "t": 140.1, "run": "tp Pat 0 69 -40"},
        {"at": 52.0, "t": 190.0, "say": _says()[3], "as": "Pat"}, {"at": 53.0, "t": 205.0, "say": _says()[4], "as": "Pat"}]
CHAT = [{"t": 4.2, "from": "Pat", "text": _says()[0]}, {"t": 9.0, "from": "player", "text": "on it!"}]


def _ctx(held=None, *, events=SAID, chat=CHAT, broke=(), after=None, deaths=0):
    held = {**PATS, "oak_log": 9, "birch_log": 3} if held is None else held
    stats = {"deaths": deaths, "damage_taken": 0}
    after = dict(HUT) if after is None else after
    frames = [Frame("step_01", after, (SX, SY + 6, SZ - 43), [], t=210.0, stats=stats)]
    return Context(dict(HUT), after, None, PLOT.volume, None, (SX, SY + 6, SZ - 43), [], frames, stats, 20.0, 6000,
                   START, rcon=Server(held), fmt=FMT, seconds=220.0, start=START, setup=dict(HUT), broke=list(broke),
                   events=list(events), chat=list(chat))


def test_everything_done_passes():
    r = grade(TASK.grader, _ctx())
    assert r.passed and r.score == 1.0, r.detail["steps"]


def test_eight_logs_misses_the_correction():
    r = grade(TASK.grader, _ctx({**PATS, "oak_log": 8}, events=SAID[:2], chat=CHAT))
    assert not r.passed and not r.checks["logs_in"] and not r.checks["found_pat"] and r.checks["pats_things"]


def test_taking_pats_diamonds_fails():
    r = grade(TASK.grader, _ctx({"iron_ingot": 12, "emerald": 2, "oak_log": 12}))
    assert not r.passed and not r.checks["pats_things"] and r.checks["logs_in"]


def test_never_finding_pat_or_never_feeding_pat_fails():
    r = grade(TASK.grader, _ctx(events=SAID[:4]))
    assert not r.passed and not r.checks["found_pat"] and not r.checks["fed_pat"] and r.checks["logs_in"]
    r = grade(TASK.grader, _ctx(events=SAID[:5]))
    assert not r.passed and r.checks["found_pat"] and not r.checks["fed_pat"]


def test_breaking_into_pats_hut_fails():
    wall = (SX - 11, SY + 2, SZ - 6)
    after = {p: b for p, b in HUT.items() if p != wall}
    r = grade(TASK.grader, _ctx(after=after, broke=[(80.0, *wall, "oak_planks", None)]))
    assert not r.passed and not r.checks["pats_places"]


def test_saying_nothing_costs_a_point_but_passes():
    r = grade(TASK.grader, _ctx(chat=CHAT[:1]))
    assert r.passed and not r.checks["answered"] and r.score < 1.0


# ------------------------------------------------------------------ the new pieces on their own

def test_say_events_need_a_speaker_and_say_before_they_run():
    two = Task(id="t", prompt="p", players=[{"name": "Pat", "at": "0 0 0"}, {"name": "Sam", "at": "1 0 0"}],
               events=[{"at": 1, "say": "hi {bot}", "as": "Sam", "run": "say after"}])
    assert two.render_events(PLOT, START, "player") == [(1.0, Say("Sam", "hi player")), (1.0, "say after")]
    with pytest.raises(ValueError, match="as whom"):
        Task(id="t", prompt="p", players=two.players, events=[{"at": 1, "say": "hi"}]).render_events(PLOT)
    with pytest.raises(ValueError, match="as whom"):
        Task(id="t", prompt="p", events=[{"at": 1, "say": "hi"}]).render_events(PLOT)
    with pytest.raises(ValueError):
        Task(id="t", prompt="p", players=[{"name": "Pat", "at": "0 0"}]).render_players(PLOT)


def test_the_event_runner_speaks_and_runs_a_list_test_in_order():
    sent, said, count = [], [], {"n": 0}

    def rcon(cmd):
        sent.append(cmd)
        if cmd == "execute if score #n s matches 3..":
            return "Test passed" if count["n"] >= 3 else "Test failed"
        return "ok"

    def speak(who, text):
        said.append((who, text))
        return "said"
    EventRunner.POLL = 0.05
    try:
        ev = EventRunner("h", 0, "pw", [(0.0, Say("Pat", "hello")),
                                         (0.0, Say("Pat", "thanks"), ("store #n s", "execute if score #n s matches 3..")),
                                         (0.0, "tp Pat 1 2 3")], time.time(), rcon=rcon, speak=speak)
        time.sleep(0.3)
        assert said == [("Pat", "hello")] and "tp Pat 1 2 3" not in sent      # held by the list's test
        assert sent[:2] == ["store #n s", "execute if score #n s matches 3.."]  # its commands in order, the last the test
        count["n"] = 3
        deadline = time.time() + 5
        while len(ev.fired) < 3 and time.time() < deadline:
            time.sleep(0.05)
        fired = ev.stop()
    finally:
        EventRunner.POLL = 1.0
    assert said == [("Pat", "hello"), ("Pat", "thanks")] and sent[-1] == "tp Pat 1 2 3"
    assert fired[0] == {**fired[0], "say": "hello", "as": "Pat", "out": "said"} and fired[2]["run"] == "tp Pat 1 2 3"
    # no one to speak: recorded, not raised
    ev = EventRunner("h", 0, "pw", [(0.0, Say("Pat", "hi"))], time.time(), rcon=rcon)
    deadline = time.time() + 5
    while not ev.fired and time.time() < deadline:
        time.sleep(0.05)
    assert ev.stop()[0]["out"].startswith("error")


def test_the_chat_grader_counts_the_bots_lines_after_the_ask():
    chat = [{"t": 1.0, "from": "player", "text": "hello?"}, {"t": 4.2, "from": "Pat", "text": "hey! could you get me 8"},
            {"t": 9.0, "from": "player", "text": "Sure, on it"}, {"t": 30.0, "from": "player", "text": "done"}]
    ctx = _ctx(chat=chat)
    assert grade({"kind": "chat"}, ctx).detail["lines"] == 3
    assert grade({"kind": "chat", "after": "hey! could you", "min": 2}, ctx).passed
    assert not grade({"kind": "chat", "after": "hey! could you", "min": 3}, ctx).passed
    assert grade({"kind": "chat", "match": "^sure"}, ctx).detail["lines"] == 1
    assert not grade({"kind": "chat", "after": "never said"}, ctx).passed
    assert grade({"kind": "chat", "from": "Pat"}, ctx).passed
    # a frame sees only the lines before it
    assert ctx.at_frame(Frame("f", {}, None, [], t=5.0)).chat == chat[:2]


def test_with_players_the_events_clock_starts_when_the_agent_is_on():
    import threading
    online = {"names": "Pat"}
    sent = []

    def rcon(cmd):
        sent.append(cmd)
        return f"There are 2 of a max of 8 players online: {online['names']}" if cmd == "list" else "ok"
    handed = threading.Event()
    EventRunner.POLL = 0.05
    try:
        t0 = time.time()
        ev = EventRunner("h", 0, "pw", [(0.0, "say first")], t0, rcon=rcon, listener="player", handed=handed)
        online["names"] = "Pat, player"              # the stand-in, before the hand-off: not yet
        time.sleep(0.25)
        assert "say first" not in sent and "list" not in sent
        handed.set()
        online["names"] = "Pat"                       # handed off, the agent not in yet
        time.sleep(0.25)
        assert "say first" not in sent and "list" in sent
        online["names"] = "Pat, player"
        deadline = time.time() + 5
        while not ev.fired and time.time() < deadline:
            time.sleep(0.05)
        fired = ev.stop()
    finally:
        EventRunner.POLL = 1.0
    assert fired[0]["run"] == "say first" and ev.joined is not None and ev.joined >= 0.4
