"""Task model: one tasks/<id>.yaml, with its profile (profiles/<name>.yaml) underneath."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

import yaml

from .arena import Plot
from .config import PROFILE_DIR, TASK_DIR
from .start import StartSpec, tunnel_commands


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


class Say(NamedTuple):
    """An event that is a scripted player's chat line, not an RCON command: `{at: 5, say: "hi", as: Pat}`."""
    player: str
    text: str


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
                                                         # `run` may be a list, `when` an RCON test that holds it (and all after it),
                                                         # or a list whose last command is the test; `say` (with `as`) is a
                                                         # scripted player's chat line, said before the event's `run`; with
                                                         # `players`, `at` counts from when the agent is on the server
    players: list[dict] = field(default_factory=list)    # scripted players the bench logs in alongside the bot: [{name: Pat,
                                                         # at: "x y z" (templated), gamemode: adventure}] (players/)
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
    fail_on_death: bool = False       # the trial ends at the player's first death: a required `alive` part can no longer
                                      # pass, and what follows (a respawn far from the work) grades nothing
    end_at_dawn: int | None = None    # the trial ends at this dawn (2: when day 3 begins), whatever the agent is doing: the
                                      # bench counts dawns, takes a `dawn_N` frame at each, and never stops it on a pass
    requires: list[str] = field(default_factory=list)   # agent capabilities the grading depends on (protocol.CAPABILITIES):
                                                        # an agent without one is run, and its trial carries a caveat
    tunnels: list[dict] = field(default_factory=list)   # [{path: [[dx, dy, dz], ...], width: 3, height: 3}]: walkable tunnels
                                                        # cut from the start (start.tunnel_commands), before setup, with
                                                        # their chunks force-loaded however far they run
    load_area: str | None = None      # "x0 z0 x1 z1" (templated): chunks kept loaded while setup runs, for a setup that
                                      # builds far outside the plot (an island 150 blocks off), tunnels' own boxes besides
    load_nether: str | None = None    # the same in the Nether ("x0 z0 x1 z1", Nether coordinates): its chunks generated and
                                      # kept loaded while setup runs, for a setup that builds there (a portal's far side)
    herd_watch: dict | None = None    # {types: [cow, chicken], grow: 4, poll: 2}: the runner's HerdWatcher tags the babies of
                                      # those kinds, counts the ones the bot kills (the `herd` grader's max_baby_kills) and
                                      # ages them `grow` times as fast (a calf is 20 real minutes otherwise); `strays: true`
                                      # removes adults of those kinds the world spawns (natural spawning, jockeys)
    requester: str | None = None      # who gives the directive (the prompt): the goal file's `directive.from` (PROTOCOL.md §5.1)
    responders: list[dict] = field(default_factory=list)   # who answers an agent's `ask`, and with what (responders.py)
    params: dict = field(default_factory=dict)        # the task's variant params (variants.py): {name: {default, choices|range}}
    param_values: dict = field(default_factory=dict)  # the values this instance was drawn with
    split: str = "public"                             # the split it was drawn for (public: the defaults)
    pack: str | None = None                           # heldout: the id of the pack its domains came from (variants.py);
                                                      # None: drawn from the task file's own, public, domains
    source: Path | None = None                        # its file, for drawing another instance

    def seed_for(self, trial: int) -> int:
        return self.world.seeds[trial % len(self.world.seeds)]

    def render_setup(self, plot: Plot, start: tuple[int, int, int] | None = None, bot: str = "player") -> list[str]:
        """Setup commands, templated. On a plot outside the overworld each command runs in that
        dimension (`execute in <dim> run ...`), so fills and summons land in the right world. A
        `fill` over the server's block limit is split into pieces under it (split_fill)."""
        dim = getattr(plot, "dimension", "minecraft:overworld")
        prefix = "" if dim == "minecraft:overworld" else f"execute in {dim} run "
        return [prefix + piece for c in self.setup for piece in split_fill(self._fmt(c, plot, start).replace("{bot}", bot))]

    def render_tunnels(self, plot: Plot, start: tuple[int, int, int] | None = None,
                       ) -> tuple[list[str], tuple[int, int, int, int] | None]:
        """The `tunnels`' commands, cut from the bot's start, and the (x0, z0, x1, z1) they cover (None: no tunnels).
        Run before setup, so what setup places along them (ore in a wall, a mob on the way) lands in the cut."""
        if not self.tunnels:
            return [], None
        at = start or plot.center()
        cmds: list[str] = []
        boxes = []
        for t in self.tunnels:
            c, box = tunnel_commands(t["path"], at, int(t.get("width", 3)), int(t.get("height", 3)))
            cmds += [piece for cmd in c for piece in split_fill(cmd)]
            boxes.append(box)
        return cmds, (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))

    def render_load_area(self, plot: Plot, start: tuple[int, int, int] | None = None,
                         also: tuple[int, int, int, int] | None = None) -> tuple[int, int, int, int] | None:
        """The (x0, z0, x1, z1) to keep loaded while setup runs: `load_area`, templated, joined with `also` (the
        tunnels' box). None when there is neither."""
        boxes = [b for b in (also,) if b is not None]
        if self.load_area:
            v = [int(n) for n in self._fmt(str(self.load_area), plot, start).split()]
            boxes.append((min(v[0], v[2]), min(v[1], v[3]), max(v[0], v[2]), max(v[1], v[3])))
        if not boxes:
            return None
        return (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))

    def render_events(self, plot: Plot, start: tuple[int, int, int] | None = None,
                      bot: str = "player") -> list[tuple[float, str | Say] | tuple[float, str | Say, str | tuple]]:
        """Timed events, templated like setup: [(seconds after the bot is placed, command)], in time order.
        A death mid-task, a mob summoned once the work has started, a change of weather: what a live
        server does to the bot without asking. `run` may be a list (its commands fire together, in order).
        `when: "execute unless entity @e[tag=wave]"` makes it wait past `at` until that test passes, and as
        events fire in order, every event after it waits too: the next wave of a fight once the last is dead.
        `when` may be a list: its commands run in order and the last is the test (a count stored to a score, then
        the score tested against a bound). A gated event is (at, command, test); its list's other commands follow
        it ungated. `say: "..."` is a scripted player's chat line (`as`: which, when the task has more than one),
        a Say in the command's place, said before the event's `run`."""
        dim = getattr(plot, "dimension", "minecraft:overworld")
        prefix = "" if dim == "minecraft:overworld" else f"execute in {dim} run "
        fmt = lambda text: prefix + self._fmt(str(text), plot, start).replace("{bot}", bot)   # noqa: E731
        out = []
        for e in self.events:
            at = float(e.get("at", 0))
            run = e.get("run")
            run = [] if run is None else [run] if isinstance(run, str) else list(run)
            if (not run and not e.get("say")) or at < 0:
                raise ValueError(f"event needs `at` (seconds >= 0) and `run` or `say`: {e!r}")
            cmds: list[str | Say] = [fmt(c) for c in run]
            if e.get("say"):
                cmds.insert(0, Say(self._speaker(e), self._fmt(str(e["say"]), plot, start).replace("{bot}", bot)))
            when = e.get("when")
            if isinstance(when, list):
                when = tuple(fmt(w) for w in when)
            elif when:
                when = fmt(when)
            for j, cmd in enumerate(cmds):
                out.append((at, cmd, when) if when and j == 0 else (at, cmd))
        return sorted(out, key=lambda t: t[0])     # stable: a list's commands, and same-time events, keep their order

    def _speaker(self, e: dict) -> str:
        """Who says an event's `say`: its `as`, or the task's only scripted player."""
        names = [str(p["name"]) for p in self.players]
        who = e.get("as") or (names[0] if len(names) == 1 else None)
        if who is None or str(who) not in names:
            raise ValueError(f"event says something, but as whom? `as` must name one of the task's players {names}: {e!r}")
        return str(who)

    def render_players(self, plot: Plot, start: tuple[int, int, int] | None = None) -> list[tuple[str, tuple[int, int, int], str]]:
        """The scripted players, templated: [(name, (x, y, z) where they stand at the start, game mode)]."""
        out = []
        for p in self.players:
            at = tuple(int(v) for v in self._fmt(str(p["at"]), plot, start).split())
            if len(at) != 3:
                raise ValueError(f"player {p.get('name')!r}: `at` is three numbers, x y z: {p!r}")
            out.append((str(p["name"]), at, str(p.get("gamemode", "adventure"))))
        return out

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

    def render_responders(self, plot: Plot, start=None):
        """The responders, their places templated like setup (responders.Responder)."""
        from .responders import Responder
        return [Responder.of(r, lambda text: self._fmt(text, plot, start)) for r in self.responders]

    def render_load_nether(self, plot: Plot, start=None) -> tuple[int, int, int, int] | None:
        """`load_nether`, templated, as (x0, z0, x1, z1); None when the task has none."""
        if not self.load_nether:
            return None
        v = [int(n) for n in self._fmt(str(self.load_nether), plot, start).split()]
        return (min(v[0], v[2]), min(v[1], v[3]), max(v[0], v[2]), max(v[1], v[3]))

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
    def from_yaml(cls, path: Path, split: str = "public", trial: int = 0, key: str | None = None) -> "Task":
        """The task in `path`, as trial `trial` of `split` draws it (variants.py): its `${...}` filled with the
        drawn params before it is read. A task without params is the same in every split."""
        from . import variants
        text = path.read_text()
        spec = variants.params_of(text)
        values: dict = {}
        pack = variants.pack_file(path.stem) if split == "heldout" and spec else None
        pack_text = pack.read_text() if pack else None
        if spec or "${" in text:
            if split == "heldout" and spec and key is None:
                key = variants.heldout_key()
            drawn_from = variants.pack_spec(path.stem, spec, pack_text) if pack_text else spec
            values = variants.draw(path.stem, drawn_from, split, trial, key)
            text = variants.render(text, values)
        d = with_profile(yaml.safe_load(text))
        d.pop("params", None)
        if pack_text:
            d["setup"] = list(d.get("setup") or []) + variants.pack_setup(pack_text, values)
        d.update(params=spec, param_values=values, split=split if spec else "public", source=path,
                 pack=variants.pack_id(pack_text) if pack_text else None)
        d.setdefault("id", path.stem)
        if "anchor" in d:
            d["anchor"] = tuple(d["anchor"])
        if "world" in d:
            d["world"] = WorldSpec(**d["world"])
        if "start" in d:
            d["start"] = StartSpec(**d["start"])
        return cls(**d)

    def instance(self, split: str, trial: int, key: str | None = None) -> "Task":
        """This task as trial `trial` of `split` draws it; itself when it has no params."""
        if not self.params or self.source is None:
            return self
        return Task.from_yaml(self.source, split, trial, key)

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


def load(task_id: str, split: str = "public", trial: int = 0) -> Task:
    p = TASK_DIR / f"{task_id}.yaml"
    if not p.exists():
        raise FileNotFoundError(f"no task {task_id!r}; available: {sorted(load_all())}")
    return Task.from_yaml(p, split, trial)
