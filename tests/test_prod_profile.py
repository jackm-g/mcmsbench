"""The prod world profile (profiles/prod.yaml) and the three tasks built on it: the profile merge, the
survival arena's rules (spawn protection through the compose env, the biome repaint, the livestock cull),
the respawn point read from the server, and the respawn grader. No server needed."""
import json
import tarfile
from pathlib import Path

import pytest

from mcmsbench.arena import parse_respawn, parse_respawn_legacy
from mcmsbench.config import ROOT, SurvivalConfig
from mcmsbench.graders import Context, grade
from mcmsbench.survival import WorldArena, WorldRules, biome_fill_command
from mcmsbench.tasks import Task, load, with_profile
from mcmsbench.world.volume import Volume, diff

PROD_TASKS = ("respawn_at_nest", "night_in_spawn_protection", "bed_without_sheep", "tundra_provision", "farm_in_snow",
              "provision_at_base", "stranded_under_home")


class FakeRcon:
    def __init__(self, answers=None):
        self.answers = answers or {}
        self.cmds = []

    def __call__(self, cmd):
        self.cmds.append(cmd)
        for k, v in self.answers.items():
            if k in cmd:
                return v
        if cmd.startswith("fillbiome"):
            return "256 biome entries set between 0, 38, 0 and 15, 165, 15"
        return "ok"

    def close(self):
        pass


# ------------------------------------------------------------------ the profile

@pytest.mark.parametrize("tid", PROD_TASKS)
def test_prod_tasks_load_with_the_live_servers_rules(tid):
    t = load(tid)
    w = t.world
    assert t.profile == "prod"
    assert (w.type, w.difficulty, w.daylight, w.spawn_protection) == ("survival", "hard", True, 16)
    assert (w.keep_inventory, w.random_tick_speed, w.biome, w.passive_mobs) == (False, 3, "snowy_plains", False)
    assert w.border >= 2 * 130                  # the pads and the flock lie 90..120 blocks out
    assert t.guard and t.journal and t.spawn_first and t.effort is None   # [agent].effort, as live tasks
    assert "deaths" in t.stats


def test_a_tasks_own_keys_win_and_world_merges_key_by_key():
    d = with_profile({"profile": "prod", "prompt": "x", "guard": False, "world": {"border": 500, "seeds": [7]}})
    assert d["guard"] is False and d["journal"] is True
    assert d["world"]["border"] == 500 and d["world"]["seeds"] == [7] and d["world"]["difficulty"] == "hard"
    assert with_profile({"prompt": "x"}) == {"prompt": "x"}
    with pytest.raises(FileNotFoundError):
        with_profile({"profile": "nope", "prompt": "x"})


def test_prod_setup_renders_nbt_and_spreads_the_mobs():
    from mcmsbench.arena import Plot
    t = load("respawn_at_nest")
    plot = Plot(0, (154, 69, 163), Volume((106, 58, 115), (202, 98, 211)), flat=False)
    plot.center = lambda: (154, 70, 163)   # type: ignore[method-assign]
    cmds = t.render_setup(plot, (154, 70, 163), bot="player")
    assert 'summon minecraft:zombie 154 71 163 {Tags:["nest"],PersistenceRequired:1b}' in cmds
    assert "spreadplayers 154 163 4 12 false @e[tag=nest]" in cmds
    assert "fill 242 69 161 246 69 165 minecraft:stone" in cmds          # the pad 90 east
    assert "fill 242 58 161 246 68 165 minecraft:stone replace air" in cmds   # ...on a plinth, never floating
    sheep = load("bed_without_sheep").render_setup(plot, (154, 70, 163))
    assert "spreadplayers 274 163 2 8 false @e[tag=flock]" in sheep     # 120 east (north is sea)


# ------------------------------------------------------------------ the arena

def _arena(tmp_path, rules=None):
    worlds = tmp_path / "worlds"
    worlds.mkdir()
    (worlds / "1.json").write_text(json.dumps({"seed": 1, "spawn": [154, 70, 163], "top_block": "grass_block",
                                               "logs_within_radius": 63, "radius": 32, "version": "26.1"}))
    src = tmp_path / "src" / "world"
    src.mkdir(parents=True)
    (src / "level.dat").write_text("x")
    with tarfile.open(worlds / "1.tar", "w") as t:
        t.add(src, arcname="world")

    class Control:
        def __init__(self):
            self.env, self.rc = None, FakeRcon()
            self.world_dir = tmp_path / "data" / "world"

        def stop(self):
            pass

        def start(self, env=None):
            self.env = env

        def wait_ready(self):
            return self.rc

    cfg = SurvivalConfig(worlds_dir=str(worlds))
    ctl = Control()
    a = WorldArena(cfg, FakeRcon(), ctl)
    a.configure(1, daylight=True, difficulty="hard", border=320, rules=rules)
    return a, ctl


def test_reset_passes_spawn_protection_to_the_server_and_shapes_the_land(tmp_path, monkeypatch):
    monkeypatch.setattr("mcmsbench.survival.time.sleep", lambda s: None)
    rules = WorldRules(spawn_protection=16, keep_inventory=False, random_tick_speed=3, biome="snowy_plains",
                       biome_radius=128, passive_mobs=False)
    a, ctl = _arena(tmp_path, rules)
    a.reset(a.plot(0))
    assert ctl.env == {"SURVIVAL_SEED": "1", "SURVIVAL_SPAWN_PROTECTION": "16"}
    assert (ctl.world_dir / "level.dat").exists()
    cmds = ctl.rc.cmds
    assert "gamerule keep_inventory false" in cmds and "gamerule random_tick_speed 3" in cmds
    assert "gamerule spawn_mobs true" in cmds and "difficulty hard" in cmds
    fills = [c for c in cmds if c.startswith("fillbiome")]
    assert len(fills) == 17 * 17                                         # (154 +- 128) >> 4 = 1..17, 2..18
    for c in fills:
        x0, y0, z0, x1, y1, z1 = (int(v) for v in c.split()[1:7])
        assert (x1 - x0 + 1) * (y1 - y0 + 1) * (z1 - z0 + 1) <= 32768
        assert c.endswith("minecraft:snowy_plains")
    for mob in ("cow", "sheep", "pig", "chicken"):
        assert any(c.startswith(f"kill @e[type=minecraft:{mob},x=26,") for c in cmds)
    adds = [c for c in cmds if c.startswith("forceload add")]
    removes = [c for c in cmds if c.startswith("forceload remove")]
    assert len(removes) == len(adds) - 2                                 # the plot's own, before and after
    last_remove = max(i for i, c in enumerate(cmds) if c.startswith("forceload remove"))
    assert "forceload add 106 115 202 211" in cmds[last_remove:]          # the plot stays loaded for the observer


def test_default_rules_leave_the_arena_as_it_was(tmp_path, monkeypatch):
    monkeypatch.setattr("mcmsbench.survival.time.sleep", lambda s: None)
    a, ctl = _arena(tmp_path)
    a.reset(a.plot(0))
    assert ctl.env["SURVIVAL_SPAWN_PROTECTION"] == "0"
    assert "gamerule keep_inventory true" in ctl.rc.cmds and "gamerule random_tick_speed 0" in ctl.rc.cmds
    assert not any(c.startswith(("fillbiome", "kill @e[type=minecraft:")) for c in ctl.rc.cmds)


def test_a_biome_chunk_that_is_still_generating_gets_another_pass(tmp_path, monkeypatch):
    monkeypatch.setattr("mcmsbench.survival.time.sleep", lambda s: None)
    a, ctl = _arena(tmp_path, WorldRules(biome="snowy_plains", biome_radius=8))
    first = biome_fill_command(9, 9, 70, "minecraft:snowy_plains")
    seen = []

    def rc(cmd):
        seen.append(cmd)
        if cmd == first and seen.count(first) == 1:
            return "That position is not loaded"
        return FakeRcon()(cmd)
    a.rcon = rc
    out = a.shape(a.plot(0))
    assert seen.count(first) == 2 and out["biome_failed"] == 0


def test_compose_reads_the_spawn_protection_for_the_survival_arena():
    text = (ROOT / "infra" / "docker-compose.yml").read_text()
    assert "SPAWN_PROTECTION: ${SURVIVAL_SPAWN_PROTECTION:-0}" in text


# ------------------------------------------------------------------ the respawn point

def test_respawn_parses_both_nbt_shapes():
    out = 'player has the following entity data: {pos: [I; 170, 71, 150], dimension: "minecraft:overworld", angle: 0.0f}'
    assert parse_respawn(out) == (170, 71, 150)
    assert parse_respawn("Found no elements matching respawn") is None
    assert parse_respawn_legacy("player has the following entity data: -12", "b ... data: 64", "b ... data: 7") == (-12, 64, 7)
    assert parse_respawn_legacy("Found no elements matching SpawnX", "", "") is None


def test_server_respawn_checks_for_a_bed_in_a_loaded_chunk():
    from mcmsbench.arena import Arena
    from mcmsbench.config import ArenaConfig
    rc = FakeRcon({"data get entity player respawn": "player has the following entity data: {pos: [I; 170, 71, 150]}",
                   "execute if block 170 71 150 #minecraft:beds": "Test passed"})
    a = Arena(ArenaConfig(), rc)
    assert a.server_respawn("player") == {"pos": [170, 71, 150], "bed": True}
    assert "forceload add 170 150" in rc.cmds and "forceload remove 170 150" in rc.cmds
    none = Arena(ArenaConfig(), FakeRcon({"data get entity": "Found no elements matching"}))
    assert none.server_respawn("player") is None


def _ctx(respawn):
    return Context({}, {}, diff({}, {}), Volume((0, 0, 0), (1, 1, 1)), None, (154, 70, 163), [], [], {"deaths": 0},
                   20.0, 1000, (154, 70, 163), respawn=respawn)


def test_respawn_grader_wants_a_bed_outside_the_zone_and_near_the_work():
    spec = {"kind": "respawn", "min_from_spawn": 17, "max_from_spawn": 70}
    assert grade(spec, _ctx({"pos": [154, 70, 123], "bed": True})).passed               # 40 north
    assert not grade(spec, _ctx({"pos": [160, 70, 165], "bed": True})).passed           # inside the zone
    assert not grade(spec, _ctx({"pos": [154, 70, 45], "bed": True})).passed            # at the flock, 118 out
    assert not grade(spec, _ctx({"pos": [154, 70, 123], "bed": False})).passed          # the bed is gone
    assert not grade(spec, _ctx(None)).passed                                           # never set
    assert grade({"kind": "respawn"}, _ctx({"pos": [1, 2, 3], "bed": True})).passed


def test_respawn_in_milestones_is_graded_once_on_the_final_state():
    t = load("bed_without_sheep")
    ok = grade(t.grader, _ctx({"pos": [154, 70, 123], "bed": True}))
    assert ok.passed and ok.checks["bed_spawn"]
    bad = grade(t.grader, _ctx({"pos": [154, 70, 163], "bed": False}))        # the start's spawnpoint, no bed
    assert not bad.passed and not bad.checks["bed_spawn"]


def test_every_task_still_loads():
    from mcmsbench.tasks import load_all
    tasks = load_all()
    assert set(PROD_TASKS) <= set(tasks)
    assert all(isinstance(t, Task) for t in tasks.values())


# ------------------------------------------------------------------ food and farms

def test_drain_food_burns_hunger_down_to_the_target_and_clears_the_effect(monkeypatch):
    from mcmsbench.arena import Arena
    from mcmsbench.config import ArenaConfig
    monkeypatch.setattr("mcmsbench.arena.time.sleep", lambda s: None)
    levels = iter([20, 18, 12, 7, 4, 4])

    class Rc(FakeRcon):
        def __call__(self, cmd):
            self.cmds.append(cmd)
            return f"player has the following entity data: {next(levels)}" if "foodLevel" in cmd else "ok"
    rc = Rc()
    assert Arena(ArenaConfig(), rc).drain_food("player", 4) == 4
    assert sum("effect give player minecraft:hunger" in c for c in rc.cmds) == 4
    assert rc.cmds[-1] == "effect clear player minecraft:hunger"
    assert load("tundra_provision").start.food == 4


def _farm_ctx(after, rcon):
    return Context({}, after, diff({}, after), Volume((0, 0, 0), (60, 90, 60)), None, (0, 70, 0), [], [], {"deaths": 0},
                   20.0, 1000, (0, 70, 0), rcon=rcon)


def test_hydrated_counts_wheat_on_wet_farmland_after_the_tick_burst(monkeypatch):
    monkeypatch.setattr("mcmsbench.graders.mechanical.time.sleep", lambda s: None)
    wheat = {(x, 70, z): "wheat" for x in (38, 39) for z in range(5)}
    wet = {f"execute if block {x} 69 {z} minecraft:farmland[moisture=7]" for x, _, z in list(wheat)[:7]}

    class Rc(FakeRcon):
        def __call__(self, cmd):
            self.cmds.append(cmd)
            return "Test passed" if cmd in wet else "Test failed"
    rc = Rc()
    spec = {"kind": "hydrated", "crop": "wheat", "min": 6, "ticks": 400, "wait": 12, "restore": 3}
    r = grade(spec, _farm_ctx(wheat, rc))
    assert r.passed and r.detail["hydrated"] == 7 and r.detail["crops"] == 10
    assert rc.cmds[0] == "gamerule random_tick_speed 400" and rc.cmds[-1] == "gamerule random_tick_speed 3"
    wet.clear()
    dry = grade(spec, _farm_ctx(wheat, Rc()))
    assert not dry.passed and dry.checks == {"planted": True, "hydrated": False}
    assert not grade(spec, _farm_ctx({}, Rc())).passed                    # nothing planted: no burst at all


def test_farm_in_snow_setup_freezes_the_river_at_soil_level():
    from mcmsbench.arena import Plot
    plot = Plot(0, (154, 69, 163), Volume((106, 58, 115), (202, 98, 211)), flat=False)
    plot.center = lambda: (154, 70, 163)   # type: ignore[method-assign]
    cmds = load("farm_in_snow").render_setup(plot, (154, 70, 163))
    assert "fill 194 67 149 196 68 177 minecraft:water" in cmds
    assert "fill 194 69 149 196 69 177 minecraft:ice" in cmds             # the grass level: broken, it is water there
    assert all(int(c.split()[1]) >= 154 + 17 for c in cmds)               # the whole field is past the zone


def test_provision_at_base_leaves_the_chicken_in_the_furnace_and_the_base_the_bots_own():
    from mcmsbench.arena import Plot
    plot = Plot(0, (154, 69, 163), Volume((106, 58, 115), (202, 98, 211)), flat=False)
    plot.center = lambda: (154, 70, 163)   # type: ignore[method-assign]
    t = load("provision_at_base")
    start = (184, 72, 163)                                                # 30 east, past the zone
    cmds = t.render_setup(plot, start, bot="player")
    assert 'setblock 182 72 160 minecraft:furnace[facing=south]{Items:[{Slot:0b,id:"minecraft:chicken",count:7}]}' in cmds
    assert "damage player 2 minecraft:generic" in cmds
    own = t.render_own(plot, start)
    assert len(own) == 13 * 3 * 13 and (182, 72, 160) in own and (182, 72, 165) in own   # furnace, a wheat
    assert (154, 70, 163) not in own


def test_the_eval_night_skipper_sets_morning_on_its_own_connection(monkeypatch):
    import mcmsbench.runner as R
    opened = []

    class Rc(FakeRcon):
        def __init__(self, *a, **k):
            super().__init__({"time query day": "The time is 14210"})
            opened.append(self)

        def connect(self):
            return self

    logged = []

    class Log:
        def emit(self, kind, **kw):
            logged.append((kind, kw))
    monkeypatch.setattr(R, "Rcon", Rc)
    R.night_skipper("127.0.0.1", 25577, "pw", Log())()
    assert opened[0].cmds == ["time query day", f"time set {R.NIGHT_SKIP_TO}"]
    assert logged[0][0] == "night_skipped" and "14210" in logged[0][1]["before"]
    assert load("night_in_spawn_protection").fast_nights and load("night_in_spawn_protection").world.daylight


