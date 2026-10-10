"""Messages (PROTOCOL.md §5.1): the directive's id in the goal file, an agent's structured `ask` and `report`, the
responders that answer an ask (a reply on the agent's stdin, what is given put in the world), and the graders that
read the structured fields, never the words. Against tests/fake_agent.py and a stand-in server: no Minecraft."""
import json
import re
import sys

import pytest

from mcmsbench.graders import Context, Frame, grade
from mcmsbench.protocol import Agent, Inbox, TrialContext, load_manifest
from mcmsbench.responders import Responder, Responders, item_id, needs_of

FAKE = __import__("pathlib").Path(__file__).with_name("fake_agent.py")


class Server:
    """RCON as the responders use it: a chest at 1 2 3 with slots 0 and 1 taken; every command kept."""

    def __init__(self):
        self.sent = []

    def __call__(self, cmd):
        self.sent.append(cmd)
        if cmd.startswith("data get block 1 2 3 Items"):
            return '1, 2, 3 has the following block data: [{count: 1, Slot: 0b, id: "minecraft:stick"}, {count: 2, Slot: 1b, id: "minecraft:dirt"}]'
        if cmd.startswith("data get block"):
            return "The target block is not a block entity"
        return "ok"


def _manifest(tmp_path, capabilities=("messages",)):
    p = tmp_path / "fake.toml"
    p.write_text(f'command = ["{sys.executable}", "{FAKE}"]\nprefix = "@fake"\ncwd = "{tmp_path}"\n'
                 f"capabilities = {json.dumps(list(capabilities))}\n")
    return load_manifest(str(p))


def _run(tmp_path, monkeypatch, responders, capabilities=("messages",)):
    monkeypatch.setenv("FAKE_MODE", "messages")
    server, inbox = Server(), Inbox()
    answers = Responders(responders, "Pat", server, inbox, "player")
    goal = {"protocol": 1, "task_id": "t", "prompt": "make me an item frame", "directive": {"id": "m1", "from": "Pat"},
            "check": None}
    ctx = TrialContext("t", goal, "127.0.0.1", 25565, "player", "26.1", 30, 0.5, tmp_path / "run" / "trial_0",
                       hand_off=lambda: None, take_back=lambda: None, progress=lambda m: None,
                       on_event=lambda ev: answers.answer(ev) if ev.get("event") == "ask" else None, inbox=inbox)
    return Agent(_manifest(tmp_path, capabilities)).run(ctx), answers, server


# ------------------------------------------------------------------ the protocol: a whole exchange

def test_an_ask_is_answered_on_stdin_and_what_is_given_lands_in_the_world(tmp_path, monkeypatch):
    pat = Responder("Pat", {"leather": 2}, {"chest": [1, 2, 3]}, gives="in the mailbox", none="none, sorry")
    tr, answers, server = _run(tmp_path, monkeypatch, [pat])
    ask, reply, report = tr["messages"]
    assert (ask["dir"], ask["event"], ask["re"], ask["to"]) == ("out", "ask", "m1", "Pat")
    assert reply["dir"] == "in" and reply["delivered"] and reply["re"] == "a1" and reply["id"] == "m2"
    assert reply["gives"] == {"leather": 1} and reply["text"] == "in the mailbox" and reply["from"] == "Pat"
    assert report["event"] == "report" and report["status"] == "done"
    assert ask["t"] <= reply["t"] <= report["t"]
    assert tr["reply"]["gives"] == {"leather": 1} and tr["done"]["success"] is True     # the agent read the reply
    # the leather went in the chest's first free slot, and Pat has one left
    assert "item replace block 1 2 3 container.2 with minecraft:leather 1" in server.sent
    assert answers.left["pat"] == {"leather": 1} and answers.log[0]["gave"] == {"leather": 1}


def test_nobody_has_it_the_agent_reports_blocked_and_says_it_did_not(tmp_path, monkeypatch):
    tr, answers, server = _run(tmp_path, monkeypatch, [Responder("Pat", {}, {"inventory": True}, none="none, sorry")])
    ask, reply, report = tr["messages"]
    assert reply["gives"] == {} and reply["text"] == "none, sorry" and not any(c.startswith("give") for c in server.sent)
    assert report["status"] == "blocked" and report["missing"] == [{"item": "leather", "count": 1}]
    assert tr["done"]["success"] is False


def test_an_agent_without_the_capability_gets_no_stdin_and_the_record_says_so(tmp_path, monkeypatch):
    tr, _, _ = _run(tmp_path, monkeypatch, [Responder("Pat", {"leather": 1}, {"inventory": True})], capabilities=())
    reply = next(m for m in tr["messages"] if m["dir"] == "in")
    assert reply["delivered"] is False and tr["done"]["success"] is False     # it read nothing (stdin is empty)


def test_messages_is_a_capability_a_manifest_may_declare(tmp_path):
    assert "messages" in Agent(_manifest(tmp_path)).capabilities


def test_an_inbox_holds_what_is_sent_before_the_agent_runs():
    inbox, lines = Inbox(), []
    assert inbox.send({"id": "m2", "from": "Pat", "text": "early"}) is False and inbox.record == []
    inbox.attach(lines.append, t0=0.0)
    assert json.loads(lines[0])["text"] == "early" and inbox.record[0]["delivered"]


# ------------------------------------------------------------------ the responders, on their own

def test_needs_are_read_in_any_shape_and_item_names_without_their_namespace():
    assert needs_of({"need": [{"item": "minecraft:Leather", "count": 2}, "string", {"item": "string"}]}) == {"leather": 2, "string": 2}
    assert needs_of({"need": {"diamond": 1}}) == {"diamond": 1} and needs_of({"need": "feather"}) == {"feather": 1}
    assert needs_of({}) == {} and item_id("Slime Ball") == "slime_ball"


def test_an_ask_goes_to_who_it_names_or_the_requester_and_to_nobody_else():
    server, inbox = Server(), Inbox()
    inbox.attach(lambda line: None, t0=0.0)
    r = Responders([Responder("Pat", {"feather": 3}, {"at": [5, 6, 7]}), Responder("Sam", {"string": 1}, {"inventory": True})],
                   "Pat", server, inbox, "player")
    assert r.answer({"id": "a1", "need": [{"item": "feather", "count": 5}]})["gives"] == {"feather": 3}   # all Pat had
    assert server.sent[-1] == 'summon minecraft:item 5.5 7 7.5 {Item:{id:"minecraft:feather",count:3}}'
    assert r.answer({"id": "a2", "to": "SAM", "need": ["string"]})["gives"] == {"string": 1}
    assert server.sent[-1] == "give player minecraft:string 1"
    assert r.answer({"id": "a3", "to": "Alex", "need": ["string"]}) is None and r.log[-1]["heard_by"] is None
    again = r.answer({"id": "a4", "need": ["feather"]})
    assert again["gives"] == {} and again["id"] == "m4"                   # m1 the directive, m2, m3 the replies before


def test_a_chest_that_is_not_there_hands_over_on_the_ground_by_it():
    server = Server()
    r = Responders([Responder("Pat", {"leather": 1}, {"chest": [9, 9, 9]})], "Pat", server, None, "player")
    r.answer({"need": ["leather"]})
    assert server.sent[-1].startswith("summon minecraft:item 9.5 10 9.5")


def test_a_responder_spec_is_checked_and_templated():
    spec = {"name": "Pat", "has": {"Leather": 1}, "hand_over": {"chest": "{sx+1} 2 3"}}
    r = Responder.of(spec, lambda t: t.replace("{sx+1}", "11"))
    assert r.hand_over == {"chest": [11, 2, 3]} and r.has == {"leather": 1}
    for bad in ({"has": {}}, {"name": "P", "hand_over": {"mail": "1 2 3"}}, {"name": "P", "hand_over": {"at": "1 2"}}):
        with pytest.raises(ValueError):
            Responder.of(bad)


# ------------------------------------------------------------------ the graders: the fields, never the words

def _ctx(messages, claim=None, t=300.0):
    return Context({}, {}, None, None, None, frames=[Frame("f", {}, t=t)], messages=messages, claim=claim, seconds=t)


ASK = {"t": 40.0, "dir": "out", "event": "ask", "id": "a1", "to": "Pat", "need": [{"item": "leather", "count": 1}],
       "text": "anything"}
NO = {"t": 41.0, "dir": "in", "event": "message", "id": "m2", "from": "Pat", "gives": {}}
BLOCKED = {"t": 60.0, "dir": "out", "event": "report", "status": "blocked", "missing": [{"item": "leather"}], "text": "x"}


def test_asked_reads_the_need_and_the_time_not_the_text():
    assert grade({"kind": "asked", "item": "leather", "within": 150}, _ctx([ASK])).passed
    assert grade({"kind": "asked", "item": ["rabbit_hide", "minecraft:leather"]}, _ctx([ASK])).passed
    late = {**ASK, "t": 200.0}
    r = grade({"kind": "asked", "item": "leather", "within": 150}, _ctx([late]))
    assert not r.passed and r.checks == {"asked": True, "in_time": False} and r.score == 0.5
    said_it_only_in_words = {**ASK, "need": [], "text": "have you got any leather?"}
    assert not grade({"kind": "asked", "item": "leather"}, _ctx([said_it_only_in_words])).passed
    assert not grade({"kind": "asked", "item": "leather", "to": "Sam"}, _ctx([ASK])).passed
    assert not grade({"kind": "asked", "item": "leather"}, _ctx([{**ASK, "dir": "in"}])).passed   # said to it, not by it


def test_reported_reads_the_last_report_its_status_and_what_is_missing():
    spec = {"kind": "reported", "status": ["blocked", "failed"], "missing": "leather"}
    assert grade(spec, _ctx([ASK, NO, BLOCKED])).passed
    assert not grade(spec, _ctx([ASK, NO, {**BLOCKED, "missing": [{"item": "string"}]}])).passed
    assert not grade(spec, _ctx([ASK, NO, BLOCKED, {**BLOCKED, "t": 90.0, "status": "done", "missing": []}])).passed
    assert not grade(spec, _ctx([ASK, NO])).passed
    assert grade({"kind": "reported", "status": "done"}, _ctx([{**BLOCKED, "status": "Done"}])).passed


def test_claimed_reads_the_agents_own_last_word():
    assert grade({"kind": "claimed", "success": False}, _ctx([], {"success": False, "summary": "no leather"})).passed
    assert not grade({"kind": "claimed", "success": False}, _ctx([], {"success": True})).passed
    assert not grade({"kind": "claimed", "success": False}, _ctx([], None)).passed
    assert grade({"kind": "claimed", "success": True}, _ctx([], True)).passed


def test_a_frame_sees_only_the_messages_said_by_then():
    ctx = _ctx([ASK, NO, BLOCKED])
    early = ctx.at_frame(Frame("f", {}, t=50.0))
    assert [m.get("id") or m["event"] for m in early.messages] == ["a1", "m2"] and early.claim is None
