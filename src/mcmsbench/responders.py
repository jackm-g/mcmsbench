"""Responders: the people an agent works for, played by the bench as policies (PROTOCOL.md §5.1).

A task names who gives the directive (`requester`) and who answers when the agent asks for something (`responders`):

    requester: Pat
    responders:
      - name: Pat
        has: {leather: 1}                       # what they can give; {} for nobody who has it
        hand_over: {chest: "{sx+6} {sy} {sz}"}  # where what they give ends up: a chest, the ground ({at: "x y z"}),
                                                # or the agent's inventory ({inventory: true})
        gives: "oh, I've got some, it's in the mailbox"
        none: "sorry, I haven't got any"

An agent's `ask` ({"event": "ask", "id", "to", "need": [{"item", "count"}], "text"}) goes to the responder it names
(`to`, any case; none named: the requester). For each item asked for that the responder still has, as many as asked
and as it has, are handed over in the world (ordinary server state, the same for every agent however it hears of
them); the reply is a message ({"event": "message", "id", "from", "re": the ask's id, "text", "gives"}) given through
the trial's Inbox. An ask to nobody of that name is recorded and answered by no one. The words of an ask are never
read: only its `need`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

HAND_OVER = ("chest", "at", "inventory")


def item_id(v) -> str:
    """An item as the server names it, without the namespace: `minecraft:leather`, `Leather` -> leather."""
    return re.sub(r"\s+", "_", str(v or "").strip().lower()).removeprefix("minecraft:")


def needs_of(ev: dict) -> dict[str, int]:
    """An ask's `need` as {item: count}: a list of {item, count} (count 1 when left out), of item names, or a mapping."""
    need = ev.get("need") or []
    out: dict[str, int] = {}
    items = need.items() if isinstance(need, dict) else [
        (n.get("item"), n.get("count", 1)) if isinstance(n, dict) else (n, 1) for n in (need if isinstance(need, list) else [need])]
    for item, count in items:
        name = item_id(item)
        try:
            n = max(1, int(count))
        except (TypeError, ValueError):
            n = 1
        if name:
            out[name] = out.get(name, 0) + n
    return out


@dataclass
class Responder:
    name: str
    has: dict[str, int] = field(default_factory=dict)
    hand_over: dict = field(default_factory=lambda: {"inventory": True})     # chest: [x,y,z] | at: [x,y,z] | inventory
    gives: str = "here you go"
    none: str = "sorry, I haven't got any"

    @classmethod
    def of(cls, spec: dict, fmt=lambda text: text) -> "Responder":
        """A task's responder spec, its places templated with `fmt` (the task's {sx}...)."""
        if not isinstance(spec, dict) or not spec.get("name"):
            raise ValueError(f"a responder needs a `name`: {spec!r}")
        hand = dict(spec.get("hand_over") or {"inventory": True})
        if len(hand) != 1 or next(iter(hand)) not in HAND_OVER:
            raise ValueError(f"responder {spec['name']}: `hand_over` is one of {HAND_OVER}: {hand!r}")
        kind, where = next(iter(hand.items()))
        if kind in ("chest", "at"):
            xyz = [int(v) for v in fmt(str(where)).split()]
            if len(xyz) != 3:
                raise ValueError(f"responder {spec['name']}: `hand_over.{kind}` is x y z: {where!r}")
            hand = {kind: xyz}
        has = {item_id(k): int(v) for k, v in (spec.get("has") or {}).items()}
        return cls(str(spec["name"]), has, hand, str(spec.get("gives", cls.gives)), str(spec.get("none", cls.none)))


class Responders:
    """One trial's responders: `answer(ev)` for each `ask` the agent sends. `log` keeps what each ask got."""

    def __init__(self, responders: list[Responder], requester: str | None, rcon, inbox, bot: str):
        self.by_name = {r.name.lower(): r for r in responders}
        self.left = {r.name.lower(): dict(r.has) for r in responders}
        self.requester = requester
        self.rcon, self.inbox, self.bot = rcon, inbox, bot
        self.log: list[dict] = []
        self._n = 1                     # m1 is the directive in the goal file

    def answer(self, ev: dict) -> dict | None:
        """The reply to an `ask` (also sent through the inbox), or None when it went to nobody of that name."""
        to = str(ev.get("to") or self.requester or "")
        r = self.by_name.get(to.lower())
        need = needs_of(ev)
        if r is None:
            self.log.append({"ask": ev.get("id"), "to": to, "need": need, "heard_by": None})
            return None
        left = self.left[r.name.lower()]
        gives = {}
        for item, n in need.items():
            k = min(n, left.get(item, 0))
            if k > 0:
                gives[item] = k
                left[item] -= k
        out = [self._hand_over(r, item, n) for item, n in gives.items()]
        self._n += 1
        reply = {"id": f"m{self._n}", "from": r.name, "re": ev.get("id"), "text": r.gives if gives else r.none,
                 "gives": gives}
        self.log.append({"ask": ev.get("id"), "to": r.name, "need": need, "gave": gives, "reply": reply["id"],
                         "server": out})
        if self.inbox is not None:
            self.inbox.send(reply)
        return reply

    def _hand_over(self, r: Responder, item: str, n: int) -> str:
        kind, where = next(iter(r.hand_over.items()))
        if kind == "inventory":
            return self.rcon(f"give {self.bot} minecraft:{item} {n}")
        x, y, z = where
        if kind == "chest":
            held = self.rcon(f"data get block {x} {y} {z} Items")
            if "not a block entity" not in held and "rror" not in held:
                used = {int(s) for s in re.findall(r"Slot: (\d+)b", held)}
                free = next((s for s in range(27) if s not in used), None)
                if free is not None:
                    return self.rcon(f"item replace block {x} {y} {z} container.{free} with minecraft:{item} {n}")
        # on the ground: at the place named, or by the chest that was not there (or was full)
        return self.rcon(f'summon minecraft:item {x + 0.5} {y + 1} {z + 0.5} {{Item:{{id:"minecraft:{item}",count:{n}}}}}')
