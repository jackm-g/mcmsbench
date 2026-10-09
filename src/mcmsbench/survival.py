"""Survival arena: a small seeded terrain world, restored from a golden snapshot
before every trial.

Lifecycle:
  mcmsbench world prepare --seed N   generate once, validate (land + trees), snapshot to
                                   infra/worlds/N.tar with a manifest (spawn, trees, ...)
  WorldArena.reset(plot)           stop server, restore world/ from the tar, start, wait
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tarfile
import time
from dataclasses import dataclass
from pathlib import Path

from .arena import GAMERULES, Arena, Plot
from .config import ROOT, SurvivalConfig
from .rcon import Rcon, RconError
from .world.volume import Volume

COMPOSE = ROOT / "infra" / "docker-compose.yml"
LAND = {"grass_block", "dirt", "stone", "sand", "coarse_dirt", "podzol", "gravel", "snow_block", "red_sand",
        "terracotta", "mycelium", "rooted_dirt", "moss_block"}


@dataclass
class WorldManifest:
    seed: int
    spawn: tuple[int, int, int]      # standing position (feet)
    top_block: str
    logs_within_radius: int
    radius: int
    version: str

    def to_dict(self) -> dict:
        return {**self.__dict__, "spawn": list(self.spawn)}

    @classmethod
    def load(cls, path: Path) -> "WorldManifest":
        d = json.loads(path.read_text())
        d["spawn"] = tuple(d["spawn"])
        return cls(**d)


class ServerControl:
    """docker compose start/stop for one service, plus RCON readiness."""

    def __init__(self, cfg: SurvivalConfig):
        from . import lease
        lease.hold(cfg.port)            # held until the process exits; off unless MCMSBENCH_LOCK_DIR is set
        self.cfg = cfg

    def _compose(self, *args: str, env: dict | None = None) -> None:
        import os
        e = {**os.environ, **(env or {})}
        subprocess.run(["docker", "compose", "-f", str(COMPOSE), *args], check=True, env=e,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

    def stop(self) -> None:
        self._compose("stop", self.cfg.compose_service)

    def start(self, env: dict | None = None) -> None:
        """Start the stopped server. With `env` (SURVIVAL_* variables the compose file reads, e.g. the spawn
        protection) it goes through `up -d`, which recreates the container when a value changed: server.properties
        is rewritten from the environment at every start, so a property can only change this way."""
        if env:
            self._compose("up", "-d", self.cfg.compose_service, env=env)
        else:
            self._compose("start", self.cfg.compose_service)

    def recreate(self, seed: int) -> None:
        self._compose("up", "-d", "--force-recreate", self.cfg.compose_service, env={"SURVIVAL_SEED": str(seed)})

    def wait_ready(self, timeout: float = 180) -> Rcon:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                r = Rcon(self.cfg.host, self.cfg.rcon_port, self.cfg.rcon_password, timeout=5).connect()
                r("list")
                return r
            except (OSError, RconError):
                time.sleep(2)
        raise TimeoutError("survival server did not come up")

    @property
    def world_dir(self) -> Path:
        return ROOT / self.cfg.data_dir / "world"


def manifest_path(cfg: SurvivalConfig, seed: int) -> Path:
    return ROOT / cfg.worlds_dir / f"{seed}.json"


def tar_path(cfg: SurvivalConfig, seed: int) -> Path:
    return ROOT / cfg.worlds_dir / f"{seed}.tar"


class WorldArena(Arena):
    """Arena over the survival server. One plot per seed, centred on the manifest spawn."""

    def __init__(self, cfg: SurvivalConfig, rcon: Rcon, control: ServerControl):
        self.scfg = cfg
        self.control = control
        self.rcon = rcon
        self.manifest: WorldManifest | None = None
        self.radius = cfg.radius
        self.border = cfg.border
        self.daylight = False
        self.difficulty = "peaceful"
        self.rules = WorldRules()

    def configure(self, seed: int, radius: int | None = None, daylight: bool = False, difficulty: str = "peaceful",
                  border: int | None = None, rules: "WorldRules | None" = None):
        mp = manifest_path(self.scfg, seed)
        if not mp.exists():
            raise FileNotFoundError(f"no prepared world for seed {seed}: run `mcmsbench world prepare --seed {seed}`")
        self.manifest = WorldManifest.load(mp)
        self.radius = radius or self.scfg.radius
        self.border = border or self.scfg.border
        self.daylight, self.difficulty = daylight, difficulty
        self.rules = rules or WorldRules()

    def plot(self, trial: int) -> Plot:
        assert self.manifest, "configure() first"
        sx, sy, sz = self.manifest.spawn
        r = self.radius
        vol = Volume((sx - r, sy - 12, sz - r), (sx + r, sy + 28, sz + r))
        p = Plot(trial, (sx, sy - 1, sz), vol, flat=False)
        p.describe = lambda: (  # type: ignore[method-assign]
            f"You are in a small survival world (seed {self.manifest.seed}) standing at ({sx}, {sy}, {sz}) on "
            f"{self.manifest.top_block}. The world border is {self.border} blocks across, centred on you. "
            f"Work within about {r} blocks of where you stand; y varies with the terrain.")
        p.center = lambda: (sx, sy, sz)  # type: ignore[method-assign]
        return p

    def init_world(self) -> None:
        for k, v in GAMERULES.items():
            if k == "advance_time":
                v = "true" if self.daylight else "false"
            if k == "spawn_mobs":
                v = "true" if self.difficulty != "peaceful" else "false"
            if k == "keep_inventory":
                v = "true" if self.rules.keep_inventory else "false"
            if k == "random_tick_speed" and self.rules.random_tick_speed is not None:
                v = str(self.rules.random_tick_speed)
            self.gamerule(k, v)
        self.rcon("time set day")
        self.rcon("weather clear")
        self.rcon(f"difficulty {self.difficulty}")
        if self.manifest:
            sx, _, sz = self.manifest.spawn
            self.rcon(f"worldborder center {sx} {sz}")
            self.rcon(f"worldborder set {self.border}")

    def reset(self, plot: Plot) -> None:
        """Restore the golden world. Restarts the server; all clients get disconnected."""
        assert self.manifest
        tp = tar_path(self.scfg, self.manifest.seed)
        self.rcon.close()
        self.control.stop()
        wd = self.control.world_dir
        shutil.rmtree(wd, ignore_errors=True)
        with tarfile.open(tp) as t:
            t.extractall(wd.parent, filter="fully_trusted")
        self.control.start(env={"SURVIVAL_SEED": str(self.manifest.seed),
                                "SURVIVAL_SPAWN_PROTECTION": str(self.rules.spawn_protection)})
        self.rcon = self.control.wait_ready()
        self.init_world()
        (x0, _, z0), (x1, _, z1) = plot.volume.min, plot.volume.max
        self.rcon(f"forceload add {x0} {z0} {x1} {z1}")
        time.sleep(1.0)
        if self.rules.biome or not self.rules.passive_mobs:
            self.shape(plot)

    def shape(self, plot: Plot, settle: float = 6.0) -> dict:
        """Make the land around the world spawn what the rules say: repaint the biome (one chunk-sized fillbiome per
        chunk, under the per-command volume cap) and remove the livestock. The golden world only holds the chunks
        near spawn, so the rest generate here: force-load them, wait, work, and let them go."""
        assert self.manifest
        sx, sy, sz = self.manifest.spawn
        r = self.rules
        cx0, cz0 = (sx - r.biome_radius) >> 4, (sz - r.biome_radius) >> 4
        cx1, cz1 = (sx + r.biome_radius) >> 4, (sz + r.biome_radius) >> 4
        loads = list(chunk_blocks(cx0, cz0, cx1, cz1))
        for (a, b, c, d) in loads:
            self.rcon(f"forceload add {a * 16} {b * 16} {c * 16 + 15} {d * 16 + 15}")
        time.sleep(settle)
        out = {"biome_chunks": 0, "biome_failed": 0, "culled": 0}
        if r.biome:
            biome = r.biome if ":" in r.biome else f"minecraft:{r.biome}"
            pending = [(cx, cz) for cx in range(cx0, cx1 + 1) for cz in range(cz0, cz1 + 1)]
            for _ in range(3):                # a chunk still generating says "not loaded": give it another pass
                pending = [(cx, cz) for cx, cz in pending
                           if "biome entr" not in self.rcon(biome_fill_command(cx, cz, sy, biome)).lower()]
                if not pending:
                    break
                time.sleep(settle)
            out["biome_chunks"] = (cx1 - cx0 + 1) * (cz1 - cz0 + 1) - len(pending)
            out["biome_failed"] = len(pending)
        if not r.passive_mobs:
            # twice: a new chunk's entities load after the chunk does, so the first pass misses some (the prod eval,
            # 2026-09-25: pigs 150 blocks out, inside the box, after a cull of 10)
            box = cull_box(sx, sz, r.biome_radius)
            for n in range(2):
                if n:
                    time.sleep(settle)
                for mob in LIVESTOCK:
                    res = self.rcon(f"kill @e[type=minecraft:{mob},{box}]")
                    m = re.search(r"Killed (\d+)", res)
                    out["culled"] += int(m.group(1)) if m else (1 if res.startswith("Killed") else 0)
        for (a, b, c, d) in loads:
            self.rcon(f"forceload remove {a * 16} {b * 16} {c * 16 + 15} {d * 16 + 15}")
        (x0, _, z0), (x1, _, z1) = plot.volume.min, plot.volume.max
        self.rcon(f"forceload add {x0} {z0} {x1} {z1}")      # the plot stays loaded for the observer
        print(f"[arena] shaped: {out}")
        return out


LIVESTOCK = ("cow", "sheep", "pig", "chicken")
BIOME_Y = (-32, 95)          # blocks below / above the spawn's y a repainted biome reaches (128 tall x one chunk = the cap)


@dataclass
class WorldRules:
    """The server rules a task may ask of the survival arena beyond time and difficulty (tasks.WorldSpec)."""
    spawn_protection: int = 0
    keep_inventory: bool = True
    random_tick_speed: int | None = None
    biome: str | None = None
    biome_radius: int = 128
    passive_mobs: bool = True

    @classmethod
    def of(cls, world) -> "WorldRules":
        return cls(**{k: getattr(world, k) for k in cls.__dataclass_fields__ if hasattr(world, k)})


def chunk_blocks(cx0: int, cz0: int, cx1: int, cz1: int, side: int = 16):
    """The chunk rectangle in pieces of at most side x side chunks (forceload takes 256 chunks per command)."""
    for a in range(cx0, cx1 + 1, side):
        for b in range(cz0, cz1 + 1, side):
            yield a, b, min(a + side - 1, cx1), min(b + side - 1, cz1)


def biome_fill_command(cx: int, cz: int, y: int, biome: str) -> str:
    x, z = cx * 16, cz * 16
    return f"fillbiome {x} {y + BIOME_Y[0]} {z} {x + 15} {y + BIOME_Y[1]} {z + 15} {biome}"


def cull_box(sx: int, sz: int, radius: int) -> str:
    return f"x={sx - radius},y=-64,z={sz - radius},dx={2 * radius},dy=384,dz={2 * radius}"


# ------------------------------------------------------------------ prepare

def prepare(cfg: SurvivalConfig, seed: int, version: str, observer_username: str, log=print) -> WorldManifest:
    """Generate a world for `seed`, find a land spawn near (0,0) with trees nearby,
    apply gamerules/border, and snapshot it."""
    from .observer import ObserverClient

    ctl = ServerControl(cfg)
    log(f"[prepare] generating world for seed {seed} (recreating {cfg.compose_service})")
    try:
        ctl.stop()
    except subprocess.CalledProcessError:
        pass
    shutil.rmtree(ctl.world_dir, ignore_errors=True)
    ctl.recreate(seed)
    rcon = ctl.wait_ready(timeout=300)
    log("[prepare] server up; scouting spawn")
    for k, v in GAMERULES.items():
        rcon(f"gamerule {k} {v}")
    rcon("time set day"); rcon("weather clear"); rcon("difficulty peaceful")
    # NOTE: never set MAX_WORLD_SIZE on the server: vanilla clamps the border to ±that
    # radius around (0,0) regardless of the border centre, and a spawn beyond it is lethal.

    obs = ObserverClient(cfg.host, cfg.port, observer_username, version).connect()
    try:
        time.sleep(1.0)
        rcon(f"gamemode spectator {observer_username}")
        spawn, top, logs = _scout(rcon, obs, observer_username, cfg.radius, log)
    finally:
        obs.close()
    rcon(f"worldborder center {spawn[0]} {spawn[2]}")
    rcon(f"worldborder set {cfg.border}")
    rcon(f"setworldspawn {spawn[0]} {spawn[1]} {spawn[2]}")
    rcon("save-all flush")
    time.sleep(2.0)
    rcon.close()

    log("[prepare] snapshotting world")
    ctl.stop()
    out = tar_path(cfg, seed)
    out.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(out, "w") as t:
        t.add(ctl.world_dir, arcname="world")
    man = WorldManifest(seed, spawn, top, len(logs), cfg.radius, version)
    manifest_path(cfg, seed).write_text(json.dumps(man.to_dict(), indent=1))
    ctl.start()
    ctl.wait_ready().close()
    log(f"[prepare] done: {out} ({out.stat().st_size // 1_000_000} MB), manifest {manifest_path(cfg, seed)}")
    return man


LOG_TYPES = "oak_log,birch_log,spruce_log,jungle_log,acacia_log,dark_oak_log,cherry_log,mangrove_log,pale_oak_log"


def _scout(rcon, obs, name: str, radius: int, log) -> tuple[tuple[int, int, int], str, list]:
    """Find a spawn: walk a grid of candidate centres outward from (0,0); at each,
    count logs within `radius`; the first centre with enough trees wins, and spawn
    is the nearest dry land column to the trees. Chunks generate on demand."""
    for cx, cz in _grid(48, 288):
        rcon(f"forceload add {cx - radius} {cz - radius} {cx + radius} {cz + radius}")
        rcon(f"tp {name} {cx} 140 {cz}")
        time.sleep(4.0)
        logs = obs.find_blocks(LOG_TYPES, max_distance=radius, count=300)
        log(f"[prepare] centre ({cx}, {cz}): {len(logs)} logs")
        rcon(f"forceload remove {cx - radius} {cz - radius} {cx + radius} {cz + radius}")
        if len(logs) < 12:
            continue
        mx = sum(p[0] for p in logs) // len(logs)
        mz = sum(p[2] for p in logs) // len(logs)
        for dx, dz in _spiral(16):
            y, top = obs.column_top(mx + dx, mz + dz)
            if y is not None and top in LAND:
                return (mx + dx, y + 1, mz + dz), top, logs
    raise RuntimeError("no land with trees found within 288 blocks of (0,0) — try another seed")


def _grid(step: int, r: int):
    yield 0, 0
    for d in range(step, r + 1, step):
        for i in range(-d, d + 1, step):
            yield i, -d; yield i, d
        for i in range(-d + step, d, step):
            yield -d, i; yield d, i


def _spiral(r: int):
    yield 0, 0
    for d in range(1, r + 1):
        for i in range(-d, d + 1):
            yield i, -d; yield i, d
        for i in range(-d + 1, d):
            yield -d, i; yield d, i


def list_worlds(cfg: SurvivalConfig) -> list[WorldManifest]:
    d = ROOT / cfg.worlds_dir
    return [WorldManifest.load(p) for p in sorted(d.glob("*.json"))] if d.exists() else []
