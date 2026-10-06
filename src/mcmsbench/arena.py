"""Arena manager: resettable, isolated build plots on the local eval server.

Each trial gets its own plot at x = trial * spacing on a superflat world.
Reset is a handful of /fill commands over RCON — about a second, no restart.
The agent never touches this module.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass

from .config import ArenaConfig
from .rcon import Rcon
from .world.volume import Volume

# Vanilla 26.x renamed gamerules to snake_case (doDaylightCycle -> advance_time, etc.).
# init_world() validates each response, so a rename shows up as a warning, not silence.
GAMERULES = {
    "advance_time": "false",
    "advance_weather": "false",
    "spawn_mobs": "false",
    "mob_griefing": "false",
    "keep_inventory": "true",
    "random_tick_speed": "0",
    "immediate_respawn": "true",
}


OVERWORLD = "minecraft:overworld"
NETHER = "minecraft:the_nether"
# A nether plot is a room carved out of the solid netherrack under the bedrock ceiling (the
# mass above y~100 has no lava sea and few caves): floor at NETHER_FLOOR_Y, netherrack all round.
NETHER_FLOOR_Y = 96
CLEAR_ABOVE = 40      # overworld resets clear this far above the plot too
FLOOR_BLOCK = {OVERWORLD: ("grass_block", "dirt"), NETHER: ("netherrack", "netherrack")}


@dataclass
class Plot:
    trial: int
    origin: tuple[int, int, int]   # (x, floor_y, z) — floor_y is the top solid floor block
    volume: Volume                 # the air space above the floor that gets cleared and graded
    flat: bool = True              # superflat plot (uniform floor at floor_y) vs terrain
    dimension: str = OVERWORLD     # minecraft:overworld | minecraft:the_nether (flat plots only)

    @property
    def floor_y(self) -> int:
        return self.origin[1]

    def center(self) -> tuple[int, int, int]:
        (x0, _, z0), (x1, _, z1) = self.volume.min, self.volume.max
        return ((x0 + x1) // 2, self.floor_y + 1, (z0 + z1) // 2)

    def describe(self) -> str:
        (x0, y0, z0), (x1, y1, z1) = self.volume.min, self.volume.max
        where = ("You are in a netherrack room in the Nether. " if self.dimension == NETHER else "")
        return (f"{where}Your plot is x={x0}..{x1}, z={z0}..{z1}. The ground surface is at y={self.floor_y} "
                f"(stand on y={y0}); build between y={y0} and y={y1}.")


class Arena:
    def __init__(self, cfg: ArenaConfig, rcon: Rcon):
        # reset() runs /fill over the whole arena volume. Pointed at a live server
        # that erases other people's builds, so refuse anything but loopback.
        if cfg.host not in ("127.0.0.1", "localhost", "::1"):
            raise RuntimeError(f"arena host must be loopback, got {cfg.host!r} — arena resets run /fill")
        self.cfg = cfg
        self.rcon = rcon

    # -- geometry ---------------------------------------------------------
    def plot(self, trial: int, dimension: str = OVERWORLD, size: int | None = None, height: int | None = None) -> Plot:
        c = self.cfg
        dim = dimension if ":" in dimension else f"minecraft:{dimension}"
        size, height = size or c.size, height or c.height
        fy = NETHER_FLOOR_Y if dim == NETHER else c.floor_y
        x0, z0 = trial * c.spacing, 0
        vol = Volume((x0, fy + 1, z0), (x0 + size - 1, fy + height, z0 + size - 1))
        return Plot(trial, (x0, fy, z0), vol, dimension=dim)

    @staticmethod
    def _in(dimension: str, cmd: str) -> str:
        """Run a positional command in a dimension. Every player command (tp, spawnpoint, fill...) is
        relative to the executing dimension, which for RCON is the overworld."""
        return cmd if dimension == OVERWORLD else f"execute in {dimension} run {cmd}"

    # -- world ------------------------------------------------------------
    def gamerule(self, name: str, value: str) -> None:
        out = self.rcon(f"gamerule {name} {value}")
        if "Incorrect" in out or "Unknown" in out:
            print(f"[arena] warning: gamerule {name} not accepted by this server version: {out[:80]}")

    def init_world(self) -> None:
        """Idempotent one-time setup: freeze time/weather, no mobs, peaceful."""
        for k, v in GAMERULES.items():
            self.gamerule(k, v)
        self.rcon("time set day")
        self.rcon("weather clear")
        self.rcon("difficulty peaceful")

    def reset(self, plot: Plot) -> None:
        """Clear the plot's air space, rebuild a clean floor, remove dropped items and stray mobs.
        A nether plot is also walled and roofed in netherrack (a room), so summoned mobs stay put."""
        (x0, y0, z0), (x1, y1, z1) = plot.volume.min, plot.volume.max
        fy = plot.floor_y
        dim = plot.dimension
        self.rcon(self._in(dim, f"forceload add {x0 - 16} {z0 - 16} {x1 + 16} {z1 + 16}"))
        top, under = FLOOR_BLOCK.get(dim, FLOOR_BLOCK[OVERWORLD])
        if dim != OVERWORLD:
            # the shell: one block outside the plot on every side, then hollow it out below
            wall = top
            for ya in range(fy - 1, y1 + 2, 8):
                yb = min(ya + 7, y1 + 1)
                self._fill(x0 - 1, ya, z0 - 1, x1 + 1, yb, z1 + 1, wall, dim)
        # fill in slabs to stay well under any per-command block limit; the overworld also clears well
        # above the plot, where a start situation (a 30-block pillar) or a build may have left blocks
        clear_top = y1 if dim != OVERWORLD else y1 + CLEAR_ABOVE
        for ya in range(y0, clear_top + 1, 8):
            yb = min(ya + 7, clear_top)
            self._fill(x0, ya, z0, x1, yb, z1, "air", dim)
        self._fill(x0, fy, z0, x1, fy, z1, top, dim)
        self._fill(x0, fy - 1, z0, x1, fy - 1, z1, under, dim)
        box = f"x={x0 - 1},y={fy - 2},z={z0 - 1},dx={x1 - x0 + 2},dy={y1 - fy + 4},dz={z1 - z0 + 2}"
        self.rcon(self._in(dim, f"kill @e[type=item,{box}]"))
        self.rcon(self._in(dim, f"kill @e[type=!player,type=!item,{box}]"))
        if dim == OVERWORLD:
            self.clear_nether_portals(plot)

    def clear_nether_portals(self, plot: Plot) -> None:
        """Remove portal blocks in the Nether where a portal from this plot would link (x/8, z/8,
        any height): a portal left by an earlier trip would capture the next one."""
        cx, _, cz = plot.center()
        nx, nz, r = cx // 8, cz // 8, 20
        for ya in range(0, 128, 19):
            self.rcon(f"execute in {NETHER} run fill {nx - r} {ya} {nz - r} {nx + r} {min(ya + 18, 127)} {nz + r} "
                      f"minecraft:air replace minecraft:nether_portal")

    def _fill(self, x0, y0, z0, x1, y1, z1, block: str, dimension: str = OVERWORLD) -> str:
        out = self.rcon(self._in(dimension, f"fill {x0} {y0} {z0} {x1} {y1} {z1} minecraft:{block}"))
        if "Successfully" not in out and "No blocks were filled" not in out:
            raise RuntimeError(f"fill failed: {out!r}")
        return out

    def set_difficulty(self, difficulty: str) -> None:
        """Per task: hostile mobs (piglins, ghasts) are removed on peaceful, so a task that summons
        them needs easy or harder. Natural spawning stays off (spawn_mobs gamerule)."""
        self.rcon(f"difficulty {difficulty}")

    # -- players ----------------------------------------------------------
    def wait_for_player(self, username: str, timeout: float = 30) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if re.search(rf"\b{re.escape(username)}\b", self.rcon("list")):
                return
            time.sleep(0.5)
        raise TimeoutError(f"{username} did not appear in /list")

    def prepare_player(self, username: str, plot: Plot, inventory: dict[str, int] | None = None,
                       gamemode: str = "survival", spawn: tuple[int, int, int] | None = None) -> None:
        """Teleport a player into the plot (in the plot's dimension — it may have ended the last
        trial in the Nether) with a known inventory and no advancements. Never ops anyone."""
        x, y, z = spawn or plot.center()
        dim = getattr(plot, "dimension", OVERWORLD)
        self.rcon(f"gamemode {gamemode} {username}")
        self.rcon(f"clear {username}")
        self.rcon(f"effect clear {username}")
        self.rcon(f"advancement revoke {username} everything")
        self.rcon(self._in(dim, f"spawnpoint {username} {x} {y} {z}"))
        self.rcon(self._in(dim, f"tp {username} {x} {y} {z}"))
        for item, count in (inventory or {}).items():
            while count > 0:
                n = min(count, 64)
                self.rcon(f"give {username} minecraft:{item} {n}")
                count -= n

    def teleport(self, username: str, x: int, y: int, z: int, dimension: str = OVERWORLD) -> None:
        self.rcon(self._in(dimension, f"tp {username} {x} {y} {z}"))

    def server_dimension(self, username: str) -> str | None:
        m = re.search(r'"(minecraft:[a-z_]+)"', self.rcon(f"data get entity {username} Dimension"))
        return m.group(1) if m else None

    def server_position(self, username: str) -> tuple[float, float, float] | None:
        m = re.search(r"\[(-?[\d.]+)d, (-?[\d.]+)d, (-?[\d.]+)d\]", self.rcon(f"data get entity {username} Pos"))
        return tuple(round(float(v), 1) for v in m.groups()) if m else None

    def server_inventory(self, username: str) -> list[dict]:
        """The player's inventory as the SERVER sees it (via /data get). Ground truth for
        graders: the client's view can drift after inventory desyncs."""
        return parse_inventory(self.rcon(f"data get entity {username} Inventory"))

    def server_health(self, username: str) -> float | None:
        m = re.search(r"data: ([\d.]+)f", self.rcon(f"data get entity {username} Health"))
        return float(m.group(1)) if m else None

    def server_respawn(self, username: str) -> dict | None:
        """Where the player respawns, as the server holds it, and whether a bed stands there: {pos, bed}. None
        when no respawn point is set (a death goes to the world spawn). The bed's chunk is force-loaded for the
        check, so a bed far from the bot still counts. Read while the player is online."""
        pos = parse_respawn(self.rcon(f"data get entity {username} respawn"))
        if pos is None:
            pos = parse_respawn_legacy(*(self.rcon(f"data get entity {username} Spawn{a}") for a in "XYZ"))
        if pos is None:
            return None
        x, y, z = pos
        self.rcon(f"forceload add {x} {z}")
        try:
            bed = "passed" in self.rcon(f"execute if block {x} {y} {z} #minecraft:beds")
        finally:
            self.rcon(f"forceload remove {x} {z}")
        return {"pos": [x, y, z], "bed": bed}

    def server_food(self, username: str) -> int | None:
        m = re.search(r"data: (\d+)", self.rcon(f"data get entity {username} foodLevel"))
        return int(m.group(1)) if m else None

    def drain_food(self, username: str, target: int, timeout: float = 90) -> int | None:
        """Bring the player's food down to `target` (no command sets it): the hunger effect at amplifier 59 burns
        saturation, then about a point every 0.7 s, polled every 0.2 s, so it stops within a point. Returns the
        food it got to."""
        food = self.server_food(username)
        deadline = time.time() + timeout
        while food is not None and food > target and time.time() < deadline:
            self.rcon(f"effect give {username} minecraft:hunger 2 59 true")
            time.sleep(0.2)
            food = self.server_food(username)
        self.rcon(f"effect clear {username} minecraft:hunger")
        return food

    def server_time(self) -> int | None:
        """Time of day in ticks (0 = dawn, 6000 = noon, 13000 = night, 18000 = midnight)."""
        m = re.search(r"at (\d+) tick", self.rcon("time query day"))
        return int(m.group(1)) % 24000 if m else None

    def set_time(self, t) -> None:
        self.rcon(f"time set {t}")

    def set_weather(self, w: str) -> None:
        self.rcon(f"weather {w}")

    def block_is(self, x: int, y: int, z: int, block: str) -> bool:
        return "passed" in self.rcon(f"execute if block {x} {y} {z} minecraft:{block}")


def parse_inventory(nbt_text: str) -> list[dict]:
    """Parse `/data get entity <p> Inventory` output into [{name, count, slot}]."""
    items = []
    for m in re.finditer(r"count: (\d+)(.*?)id: \"minecraft:([a-z0-9_]+)\"", nbt_text):
        slot = re.search(r"Slot: (\d+)b", m.group(2))
        items.append({"name": m.group(3), "count": int(m.group(1)), "slot": int(slot.group(1)) if slot else -1})
    return items


def parse_respawn(nbt_text: str) -> tuple[int, int, int] | None:
    """`/data get entity <p> respawn` (1.21.5+: respawn: {pos: [I; x, y, z], dimension: ..., ...})."""
    m = re.search(r"pos: \[I; (-?\d+), (-?\d+), (-?\d+)\]", nbt_text)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def parse_respawn_legacy(x_text: str, y_text: str, z_text: str) -> tuple[int, int, int] | None:
    """Older servers: SpawnX / SpawnY / SpawnZ, each 'X has the following entity data: 12'."""
    vals = [re.search(r"data: (-?\d+)\s*$", t.strip()) for t in (x_text, y_text, z_text)]
    return tuple(int(v.group(1)) for v in vals) if all(vals) else None   # type: ignore[return-value]


def inventory_mismatch(client: list[dict], server: list[dict]) -> dict:
    """Per-item count differences between what the bot believes and what the server holds."""
    from collections import Counter
    c = Counter(); s = Counter()
    for i in client:
        c[i["name"]] += i["count"]
    for i in server:
        s[i["name"]] += i["count"]
    return {k: {"client": c[k], "server": s[k]} for k in set(c) | set(s) if c[k] != s[k]}
