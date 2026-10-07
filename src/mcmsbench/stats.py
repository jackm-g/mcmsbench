"""Server-truth counters via vanilla scoreboard objectives.

Objectives are created once per server (idempotent) and the bot's scores reset at
the start of each trial, so every value is "since this trial began". Nothing here
depends on the client: kills, deaths, damage and distance come from the server.
"""
from __future__ import annotations

import hashlib
import re

from .rcon import Rcon

# alias -> (criterion, divisor for readability)
BUILTIN = {
    "deaths": ("deathCount", 1),
    "kills": ("totalKillCount", 1),
    "damage_taken": ("minecraft.custom:minecraft.damage_taken", 10),   # tenths of a heart -> hearts... /10 = half-hearts? keep simple: raw/10
    "damage_dealt": ("minecraft.custom:minecraft.damage_dealt", 10),
    # NOTE: the server barely credits walk_one_cm for mineflayer's movement packets; the runner
    # also records trace.distance_travelled from per-step positions, which is what to use.
    "walked": ("minecraft.custom:minecraft.walk_one_cm", 100),          # -> blocks
    "sprinted": ("minecraft.custom:minecraft.sprint_one_cm", 100),
    "boated": ("minecraft.custom:minecraft.boat_one_cm", 100),          # -> blocks (a diagnostic: see walked's note)
    "swum": ("minecraft.custom:minecraft.swim_one_cm", 100),
    "sleep": ("minecraft.custom:minecraft.sleep_in_bed", 1),
    "jumps": ("minecraft.custom:minecraft.jump", 1),
    "time_since_death": ("minecraft.custom:minecraft.time_since_death", 20),  # -> seconds
}


# Pseudo-stats: not scoreboard criteria but a server question with a 0/1 answer, read the same
# way and usable by the same graders (`stat`, milestones per frame):
#   advancement:<path>   the bot has minecraft:<path>, e.g. advancement:story/enter_the_nether
#   dimension:<name>     the bot is in minecraft:<name> right now, e.g. dimension:the_nether
PSEUDO = ("advancement", "dimension")


def is_pseudo(name: str) -> bool:
    return ":" in name and name.split(":", 1)[0] in PSEUDO


def pseudo_query(name: str, player: str) -> str:
    """The `execute if ...` command whose 'passed' answer means 1."""
    kind, what = name.split(":", 1)
    what = what if ":" in what else f"minecraft:{what}"
    if kind == "advancement":
        return f"execute if entity @a[name={player},advancements={{{what}=true}}]"
    # a selector with a position argument only matches entities in the execution dimension
    return f"execute in {what} if entity @a[name={player},distance=0..]"


def criterion_for(name: str) -> tuple[str, int]:
    """'deaths' | 'mined:stone' | 'crafted:wooden_pickaxe' | 'killed:zombie' | 'custom:jump'."""
    if name in BUILTIN:
        return BUILTIN[name]
    if is_pseudo(name):
        return name, 1
    if ":" in name:
        kind, what = name.split(":", 1)
        if kind in ("mined", "crafted", "killed", "killed_by", "used", "broken", "picked_up", "dropped"):
            return f"minecraft.{kind}:minecraft.{what}", 1
        if kind == "custom":
            return f"minecraft.custom:minecraft.{what}", 1
    raise ValueError(f"unknown stat {name!r}")


def objective_name(name: str) -> str:
    """Scoreboard-safe, <=16 chars, stable."""
    slug = re.sub(r"[^a-z0-9]", "_", name.lower())
    if len(slug) <= 14:
        return "b_" + slug
    return "b_" + slug[:8] + hashlib.md5(name.encode()).hexdigest()[:6]


class Stats:
    def __init__(self, rcon: Rcon, player: str, names: list[str]):
        self.rcon, self.player = rcon, player
        self.names = list(dict.fromkeys(names))  # dedupe, keep order

    def setup(self) -> None:
        for n in self.names:
            if is_pseudo(n):
                continue
            crit, _ = criterion_for(n)
            self.rcon(f"scoreboard objectives add {objective_name(n)} {crit}")  # "already exists" is fine
        self.reset()

    def reset(self) -> None:
        for n in self.names:
            if is_pseudo(n):
                kind, what = n.split(":", 1)
                if kind == "advancement":
                    self.rcon(f"advancement revoke {self.player} only {what if ':' in what else 'minecraft:' + what}")
                continue
            self.rcon(f"scoreboard players set {self.player} {objective_name(n)} 0")

    def read(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for n in self.names:
            if is_pseudo(n):
                out[n] = 1 if "passed" in self.rcon(pseudo_query(n, self.player)).lower() else 0
                continue
            raw = self.rcon(f"scoreboard players get {self.player} {objective_name(n)}")
            m = re.search(r"has (-?\d+)", raw)
            _, div = criterion_for(n)
            v = int(m.group(1)) if m else 0
            out[n] = v / div if div != 1 else v
        return out
