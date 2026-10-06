"""Task model: one tasks/<id>.yaml, with its profile (profiles/<name>.yaml) underneath."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .arena import Plot
from .config import PROFILE_DIR, TASK_DIR
from .start import StartSpec


@dataclass
class WorldSpec:
    """Which arena a task runs on. flat = superflat plots (default); survival = a
    seeded terrain world restored from a golden snapshot before every trial."""
    type: str = "flat"
    seeds: list[int] = field(default_factory=lambda: [1])   # survival: trial i uses seeds[i % len]
    radius: int | None = None                                # survival: override config radius
    border: int | None = None                                # survival: /worldborder diameter (config default 96)
    daylight: bool = False                                   # let the day/night cycle run
    difficulty: str = "peaceful"                             # flat plots honour this too (piglins/ghasts need easy+)
    dimension: str = "overworld"                             # flat: overworld | the_nether (a carved netherrack room)
    size: int | None = None                                  # flat: plot size override (x/z), e.g. a big room for a ghast
    height: int | None = None                                # flat: plot height override
    # the live server's rules (evals/profiles/prod.yaml sets them together); survival worlds only
    spawn_protection: int = 0            # server spawn-protection radius around the world spawn (the bot is never op)
    keep_inventory: bool = True          # False: a death drops everything, as on a real server
    random_tick_speed: int | None = None  # None = the arena's frozen 0; 3 = vanilla (water freezes, crops grow)
    biome: str | None = None             # repaint the terrain's biome (e.g. snowy_plains): mob types, freezing, snowfall
    biome_radius: int = 128              # ...this far (xz) around the world spawn
    passive_mobs: bool = True            # False: no cows, sheep, pigs or chickens within biome_radius (the tundra)

    @property
    def dimension_id(self) -> str:
        d = self.dimension
        return d if ":" in d else f"minecraft:{d}"


@dataclass
class Task:
    id: str
    prompt: str                                  # may use {ax},{ay},{az} (anchor) and {x0}.. {cx}.. (plot)
    anchor: tuple[int, int, int] = (0, 0, 0)     # plot-relative; (0,0,0) = first air block above floor at plot min
    inventory: dict[str, int] = field(default_factory=dict)
    grader: dict | None = None                   # None = ungraded (Discord free-form)
    trials: int = 1                              # per model run; a task that needs a spread says `trials: N`
    max_turns: int | None = None
    max_seconds: int | None = None
    exec_timeout: int | None = None              # seconds per code step (config default 120); long trips need more
    effort: str | None = None                    # thinking depth low|medium|high|xhigh|max; None = agent.effort
    max_cost_usd: float | None = None            # model spend cap for this run; None = agent.max_cost_usd
    movements: dict = field(default_factory=dict)  # can_dig / towers / scaffolding overrides
    tags: list[str] = field(default_factory=list)
    world: WorldSpec = field(default_factory=WorldSpec)
    start: StartSpec = field(default_factory=StartSpec)   # where/when the bot begins
    stats: list[str] = field(default_factory=lambda: ["deaths", "damage_taken", "walked"])  # scoreboard counters to track
    setup: list[str] = field(default_factory=list)   # RCON commands after the bot is placed; {bx},{by},{bz},{sx}.. templated
    xray: bool = True                                # False: find_blocks/collect only see blocks with an open face (no X-ray)
    guard: bool = False                              # run with the live guard on (work radius, natural-only digging, refusals counted)
    memories: list[dict] = field(default_factory=list)   # journal entries the bot starts with: [{text, at: [x,y,z] | "{ax} {ay} {az}"}]
    events: list[dict] = field(default_factory=list)     # timed RCON commands during the trial: [{at: seconds, run: "kill {bot}"}];
                                                         # `run` may be a list, `when` an RCON test that holds it (and all after it)
    check: dict | bool | None = None             # the task's own check, as a live milestone carries one (checks.py shape);
                                                 # None: what the grader implies (from_grader); False: no task check (a
                                                 # grader on something the task does not ask for)
    own: list[str] = field(default_factory=list)         # boxes "x0 y0 z0 x1 y1 z1" (templated) of setup blocks that are the bot's own
                                                         # build, as live's Built registry knows its base: the guard lets it change them
    curfew: bool | None = None     # the harness shelters the bot at night; None = on when the day/night cycle runs (agent/curfew.py)
    spawn_first: bool | None = None   # the harness gets a bed and sets the respawn point before the task (live: always; evals: opt in)
    journal: bool = False             # give the bot a memory journal even with no `memories` (live always has one)
    profile: str | None = None        # evals/profiles/<name>.yaml supplies defaults this file does not set
    scripted: bool = True             # False: no reference solutions in evals/scripted/<id>.py (--scripted/--broken refuse it)
    fast_nights: bool = True          # once the harness has the bot enclosed for the night, jump to morning (evals only;
                                      # needs world.daylight): a night is 10 real minutes of waiting otherwise
    end_at_dawn: int | None = None    # the trial ends at this dawn (2: when day 3 begins), whatever the agent is doing: the
                                      # bench counts dawns, takes a `dawn_N` frame at each, and never stops it on a pass
    requires: list[str] = field(default_factory=list)   # agent capabilities the grading depends on (protocol.CAPABILITIES):
                                                        # an agent without one is run, and its trial carries a caveat
    herd_watch: dict | None = None    # {types: [cow, chicken], grow: 4, poll: 2}: the runner's HerdWatcher tags the babies of
                                      # those kinds, counts the ones the bot kills (the `herd` grader's max_baby_kills) and
                                      # ages them `grow` times as fast (a calf is 20 real minutes otherwise); `strays: true`
                                      # removes adults of those kinds the world spawns (natural spawning, jockeys)

    def seed_for(self, trial: int) -> int:
        return self.world.seeds[trial % len(self.world.seeds)]

    def render_setup(self, plot: Plot, start: tuple[int, int, int] | None = None, bot: str = "player") -> list[str]:
        """Setup commands, templated. On a plot outside the overworld each command runs in that
        dimension (`execute in <dim> run ...`), so fills and summons land in the right world. A
        `fill` over the server's block limit is split into pieces under it (split_fill)."""
        dim = getattr(plot, "dimension", "minecraft:overworld")
        prefix = "" if dim == "minecraft:overworld" else f"execute in {dim} run "
        return [prefix + piece for c in self.setup for piece in split_fill(self._fmt(c, plot, start).replace("{bot}", bot))]

    def render_events(self, plot: Plot, start: tuple[int, int, int] | None = None,
                      bot: str = "player") -> list[tuple[float, str] | tuple[float, str, str]]:
        """Timed events, templated like setup: [(seconds after the bot is placed, command)], in time order.
        A death mid-task, a mob summoned once the work has started, a change of weather: what a live
        server does to the bot without asking. `run` may be a list (its commands fire together, in order).
        `when: "execute unless entity @e[tag=wave]"` makes it wait past `at` until that test passes, and as
        events fire in order, every event after it waits too: the next wave of a fight once the last is dead.
        A gated event is (at, command, test); its list's other commands follow it ungated."""
        dim = getattr(plot, "dimension", "minecraft:overworld")
        prefix = "" if dim == "minecraft:overworld" else f"execute in {dim} run "
        fmt = lambda text: prefix + self._fmt(str(text), plot, start).replace("{bot}", bot)   # noqa: E731
        out = []
        for e in self.events:
            at = float(e.get("at", 0))
            run = e.get("run")
            if not run or at < 0:
                raise ValueError(f"event needs `at` (seconds >= 0) and `run`: {e!r}")
            for j, cmd in enumerate([run] if isinstance(run, str) else list(run)):
                when = e.get("when") if j == 0 else None
                out.append((at, fmt(cmd), fmt(when)) if when else (at, fmt(cmd)))
        return sorted(out, key=lambda t: t[0])     # stable: a list's commands, and same-time events, keep their order

    def _fmt(self, text: str, plot: Plot, start) -> str:
        """Plot/anchor/spawn/start placeholders, with simple arithmetic: {ax+20}, {az-1}."""
        import re
        (x0, y0, z0), (x1, y1, z1) = plot.volume.min, plot.volume.max
        cx, cy, cz = plot.center()
        ax, ay, az = x0 + self.anchor[0], y0 + self.anchor[1], z0 + self.anchor[2]
        bx, by, bz = start or (cx, cy, cz)
        dist = int(round(((bx - cx) ** 2 + (bz - cz) ** 2) ** 0.5))   # start -> spawn, on the ground plane
        vars_ = dict(x0=x0, y0=y0, z0=z0, x1=x1, y1=y1, z1=z1, cx=cx, cy=cy, cz=cz,
                     sx=cx, sy=cy, sz=cz, ax=ax, ay=ay, az=az, bx=bx, by=by, bz=bz, dist=dist,
                     nx=cx // 8, nz=cz // 8)          # where a portal at the centre lands in the Nether (x/8, z/8)
        text = re.sub(r"\{([a-z][a-z0-9])([+-]\d+)\}",
                      lambda m: str(vars_[m.group(1)] + int(m.group(2))) if m.group(1) in vars_ else m.group(0), text)
        return text.replace("{bot}", "{{bot}}").format(**vars_)

    def render_own(self, plot: Plot, start=None) -> list[tuple[int, int, int]]:
        """The positions in the `own` boxes, templated like setup."""
        out = []
        for box in self.own:
            x0, y0, z0, x1, y1, z1 = (int(v) for v in self._fmt(str(box), plot, start).split())
            out += [(x, y, z) for x in range(min(x0, x1), max(x0, x1) + 1) for y in range(min(y0, y1), max(y0, y1) + 1)
                    for z in range(min(z0, z1), max(z0, z1) + 1)]
        return out

    def render_memories(self, plot: Plot, start=None) -> list[tuple[str, tuple[int, int, int] | None]]:
        """Seed notes for the journal, templated: [(text, (x, y, z) | None)]."""
        out = []
        for m in self.memories:
            at = m.get("at")
            if isinstance(at, str):
                at = tuple(int(v) for v in self._fmt(at, plot, start).split())
            elif at is not None:
                at = tuple(int(v) for v in at)
            out.append((self._fmt(str(m["text"]), plot, start), at))
        return out

    def render(self, plot: Plot, start: tuple[int, int, int] | None = None) -> str:
        """Fill prompt placeholders: plot corners {x0..z1}, centre/spawn {cx,cy,cz} (also {sx,sy,sz}),
        anchor {ax,ay,az}, the bot's actual start {bx,by,bz}, and {dist} = start->spawn distance."""
        (x0, y0, z0), (x1, y1, z1) = plot.volume.min, plot.volume.max
        cx, cy, cz = plot.center()
        ax, ay, az = x0 + self.anchor[0], y0 + self.anchor[1], z0 + self.anchor[2]
        return self._fmt(self.prompt, plot, start)

    @classmethod
    def from_yaml(cls, path: Path) -> "Task":
        d = with_profile(yaml.safe_load(path.read_text()))
        d.setdefault("id", path.stem)
        if "anchor" in d:
            d["anchor"] = tuple(d["anchor"])
        if "world" in d:
            d["world"] = WorldSpec(**d["world"])
        if "start" in d:
            d["start"] = StartSpec(**d["start"])
        return cls(**d)

    @classmethod
    def free_form(cls, prompt: str, **kw) -> "Task":
        return cls(id="freeform", prompt=prompt, grader=None, trials=1, **kw)


FILL_LIMIT = 32768     # vanilla's max_block_modifications: the most blocks one fill may touch
_FILL = re.compile(r"^fill (-?\d+) (-?\d+) (-?\d+) (-?\d+) (-?\d+) (-?\d+) (\S+)(.*)$")


def split_fill(cmd: str, limit: int = FILL_LIMIT) -> list[str]:
    """A `fill` over `limit` blocks as fills under it: slabs a few layers thick, each layer cut into strips along x
    when one layer alone is too big. Other commands, and fills that fit, come back as they are. A hollow or outline
    fill cannot be cut up (each piece would get its own shell): over the limit, that is an error in the task."""
    m = _FILL.match(cmd.strip())
    if not m:
        return [cmd]
    x0, y0, z0, x1, y1, z1 = (int(v) for v in m.groups()[:6])
    block, rest = m.group(7), m.group(8)
    x0, x1 = sorted((x0, x1)); y0, y1 = sorted((y0, y1)); z0, z1 = sorted((z0, z1))
    w, h, d = x1 - x0 + 1, y1 - y0 + 1, z1 - z0 + 1
    if w * h * d <= limit:
        return [cmd]
    if rest.split()[:1] and rest.split()[0] in ("hollow", "outline"):
        raise ValueError(f"fill too big to split ({w * h * d} blocks, {rest.strip()}): {cmd}")
    cols = max(1, min(w, limit // d))                 # x strips a whole layer of which fits
    layers = max(1, limit // (cols * d))
    return [f"fill {xa} {ya} {z0} {min(xa + cols - 1, x1)} {min(ya + layers - 1, y1)} {z1} {block}{rest}"
            for ya in range(y0, y1 + 1, layers) for xa in range(x0, x1 + 1, cols)]


def with_profile(d: dict) -> dict:
    """A task dict with its profile's defaults underneath: the task's own keys win, and `world` merges key by key
    (a task on the prod profile can still pick its border or its seed)."""
    name = d.get("profile")
    if not name:
        return d
    p = PROFILE_DIR / f"{name}.yaml"
    if not p.exists():
        raise FileNotFoundError(f"no profile {name!r}; available: {sorted(q.stem for q in PROFILE_DIR.glob('*.yaml'))}")
    base = yaml.safe_load(p.read_text()) or {}
    out = {**base, **d}
    if "world" in base or "world" in d:
        out["world"] = {**(base.get("world") or {}), **(d.get("world") or {})}
    return out


def load_all() -> dict[str, Task]:
    return {p.stem: Task.from_yaml(p) for p in sorted(TASK_DIR.glob("*.yaml"))}


def load(task_id: str) -> Task:
    p = TASK_DIR / f"{task_id}.yaml"
    if not p.exists():
        raise FileNotFoundError(f"no task {task_id!r}; available: {sorted(load_all())}")
    return Task.from_yaml(p)
