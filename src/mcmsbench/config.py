"""Load mcmsbench.toml + .env into typed settings: the arenas the bench runs on, and the default trial budgets.

Nothing here is about an agent. What an agent needs to run (its command, its model, its keys) is its manifest's
(<name>.toml, found on agent_path()) and its own checkout's.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
# a checkout has tasks/ at its root; an installed wheel carries them as package data
DATA = ROOT if (ROOT / "tasks").is_dir() else Path(__file__).resolve().parent / "_data"
TASK_DIR = DATA / "tasks"
PROFILE_DIR = DATA / "profiles"
AGENTS_DIR = ROOT / "agents"          # optional: manifests kept in this checkout (none ship with the bench)


@dataclass
class ArenaConfig:
    host: str = "127.0.0.1"
    port: int = 25565
    rcon_port: int = 25575
    version: str = "26.1"
    floor_y: int = -61
    size: int = 32
    height: int = 24
    spacing: int = 256
    bot_username: str = "player"
    observer_username: str = "observer"
    rcon_password: str = ""


@dataclass
class SurvivalConfig:
    host: str = "127.0.0.1"
    port: int = 25567
    rcon_port: int = 25577
    compose_service: str = "arena-survival"
    data_dir: str = "infra/data-survival"
    worlds_dir: str = "infra/worlds"
    radius: int = 48
    border: int = 96
    bot_username: str = "player"
    observer_username: str = "observer"
    rcon_password: str = ""


@dataclass
class TrialConfig:
    """Budgets for a task that does not set its own."""
    max_seconds: int = 600
    max_cost_usd: float = 5.0


@dataclass
class Settings:
    arena: ArenaConfig = field(default_factory=ArenaConfig)
    survival: SurvivalConfig = field(default_factory=SurvivalConfig)
    trial: TrialConfig = field(default_factory=TrialConfig)
    slot: str = ""                        # "" = the default arenas; else the [slots.<name>] overlay in use


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load(path: Path | None = None, slot: str | None = None) -> Settings:
    """mcmsbench.toml + .env. A slot (`--slot NAME`, or $MCMSBENCH_SLOT) layers `[slots.NAME.*]` over the base
    sections: a second survival arena, so a run can go alongside another (its own ports, service and data dir)."""
    load_dotenv(ROOT / ".env")
    path = path or ROOT / "mcmsbench.toml"
    raw = tomllib.loads(path.read_text()) if path.exists() else {}
    slot = (slot if slot is not None else os.environ.get("MCMSBENCH_SLOT", "")).strip()
    slots = raw.pop("slots", {}) or {}
    if slot:
        if slot not in slots:
            raise SystemExit(f"no slot {slot!r} in {path}; available: {sorted(slots)}")
        raw = _merge(raw, slots[slot])
    pw = os.environ.get("RCON_PASSWORD", "")
    return Settings(ArenaConfig(**raw.get("arena", {}), rcon_password=pw),
                    SurvivalConfig(**raw.get("survival", {}), rcon_password=pw),
                    TrialConfig(**raw.get("trial", {})), slot)


def agent_path() -> list[Path]:
    """Where agent manifests are looked for: each entry of $MCMSBENCH_AGENT_PATH (separated by os.pathsep, from the
    environment or .env; a relative entry is relative to this checkout), then agents/. An entry is a directory of
    <name>.toml manifests or a single manifest file."""
    load_dotenv(ROOT / ".env")
    raw = os.environ.get("MCMSBENCH_AGENT_PATH", "")
    return [(ROOT / Path(e.strip()).expanduser()).resolve() for e in raw.split(os.pathsep) if e.strip()] + [AGENTS_DIR]
