"""Task variants and splits (variants.py): params drawn per split and trial, `${...}` filled in before the file is read,
the held-out key kept from agents, and every task with params sound in every split: the public instance as written,
and each variant gradable (multi_room_house: a correct house of each shape passes, a wrong one fails). No server."""
import json
import os

import pytest

from mcmsbench import variants
from mcmsbench.arena import Arena
from mcmsbench.config import ArenaConfig
from mcmsbench.graders import Context, Frame, grade
from mcmsbench.tasks import Task, load, load_all
from mcmsbench.world.volume import diff

KEY = "test-key-not-a-secret"
SPEC = variants.params_of("""
params:
  s: {default: 9, choices: [7, 9, 11]}
  n: {default: 8, range: [6, 10]}
  w: {default: oak, choices: [oak, birch]}
prompt: x
""")
PARAMS = [t for t in load_all().values() if t.params]


# ------------------------------------------------------------------ the draw

def test_public_is_the_defaults_and_every_split_is_deterministic():
    assert variants.draw("t", SPEC, "public", 3) == {"s": 9, "n": 8, "w": "oak"}
    for split, key in (("varied", None), ("heldout", KEY)):
        assert variants.draw("t", SPEC, split, 2, key) == variants.draw("t", SPEC, split, 2, key)
    drawn = {json.dumps(variants.draw("t", SPEC, "varied", i), sort_keys=True) for i in range(20)}
    assert len(drawn) > 5                                     # trials are different instances
    assert all(6 <= variants.draw("t", SPEC, "varied", i)["n"] <= 10 for i in range(30))


def test_heldout_depends_on_the_key_and_is_never_the_public_instance():
    a = [variants.draw("t", SPEC, "heldout", i, KEY) for i in range(30)]
    b = [variants.draw("t", SPEC, "heldout", i, KEY + "x") for i in range(30)]
    assert a != b and all(v != variants.defaults(SPEC) for v in a)
    assert a != [variants.draw("t", SPEC, "varied", i) for i in range(30)]
    with pytest.raises(variants.VariantError, match="needs MCMSBENCH_HELDOUT_KEY"):
        variants.draw("t", SPEC, "heldout", 0, None)
    one = variants.params_of("params:\n  a: {default: 1, choices: [1]}\n")
    with pytest.raises(variants.VariantError, match="no held-out instance"):
        variants.draw("t", one, "heldout", 0, KEY)


def test_a_key_id_names_a_key_without_giving_it_away():
    assert variants.key_id(KEY) == variants.key_id(KEY) != variants.key_id(KEY + "x")
    assert len(variants.key_id(KEY)) == 8 and KEY not in variants.key_id(KEY)
    assert len(variants.new_key()) == 64 and variants.new_key() != variants.new_key()


def test_bad_params_are_refused():
    for bad in ("params:\n  a: {choices: [1, 2]}\n", "params:\n  a: {default: 3, choices: [1, 2]}\n",
                "params:\n  a: {default: 1, range: [2, 4]}\n", "params:\n  A: {default: 1, choices: [1]}\n",
                "params:\n  a: {default: 1, choices: [1], range: [1, 2]}\n"):
        with pytest.raises(variants.VariantError):
            variants.params_of(bad)


# ------------------------------------------------------------------ `${...}`

def test_expressions_formats_fields_and_conditions():
    v = {"s": 9, "d": -40, "z": 0, "shape": {"rooms": 3}, "wood": "oak"}
    assert variants.render("${s}x${s} ${s*s} ${(s-2)*(s-2)//2}", v) == "9x9 81 24"
    assert variants.render("{sx${off(d)}} {sz${off(z)}} ${d:+d}", v) == "{sx-40} {sz} -40"
    assert variants.render("${'three' if shape.rooms == 3 else 'two'} ${wood}_planks", v) == "three oak_planks"
    assert variants.render("${int(448 * s * s / 81)} ${max(s, 12)} ${s == 9}", v) == "448 12 true"
    assert variants.render("{{Items:[]}} {sx-3} {bot}", v) == "{{Items:[]}} {sx-3} {bot}"     # the bench's own: untouched


def test_a_task_file_is_data_not_code():
    for bad in ("${__import__('os')}", "${open('x')}", "${s.__class__}", "${[x for x in s]}", "${nope}", "${shape.nope}"):
        with pytest.raises(variants.VariantError):
            variants.render(bad, {"s": 9, "shape": {"rooms": 2}})


# ------------------------------------------------------------------ the task, the runner, the agent

def test_a_task_draws_its_instance_and_a_task_without_params_is_the_same_everywhere():
    t = load("multi_room_house")
    assert t.split == "public" and t.param_values == variants.defaults(t.params)
    v = t.instance("varied", 4)
    assert v.split == "varied" and v.param_values == variants.draw("multi_room_house", t.params, "varied", 4)
    plain = load("gather_wood")
    assert not plain.params and plain.instance("varied", 4) is plain and plain.split == "public"


def test_the_record_says_which_split_and_instance(monkeypatch):
    from mcmsbench.runner import bench_info

    class A:
        name, provider, model, capabilities = "a", None, "m", set()
        manifest = type("M", (), {"path": None})()
    monkeypatch.setenv(variants.KEY_ENV, KEY)
    inst = load("multi_room_house").instance("heldout", 0, KEY)
    info = bench_info(inst, A())
    assert info["split"] == "heldout" and info["params"] == inst.param_values and info["key_id"] == variants.key_id(KEY)
    assert bench_info(load("gather_wood"), A())["split"] == "public" and "params" not in bench_info(load("gather_wood"), A())


def test_agents_never_get_the_key_nor_the_pack(monkeypatch):
    from mcmsbench.protocol import Agent, Manifest
    monkeypatch.setenv(variants.KEY_ENV, KEY)
    monkeypatch.setenv(variants.PACK_ENV, "/somewhere/private")
    env = Agent(Manifest(name="x", command=["true"])).environment()
    assert variants.KEY_ENV not in env and variants.PACK_ENV not in env and env["MCMSBENCH_PROTOCOL"]


def test_the_goal_command_shows_a_held_out_instance_only_when_told_to(capsys):
    from mcmsbench.cli import main
    with pytest.raises(SystemExit):
        main(["goal", "--task", "multi_room_house", "--split", "heldout"])
    assert "--reveal" in capsys.readouterr().err


# ------------------------------------------------------------------ every task with params, in every split

def test_the_variants_selector_is_the_tasks_with_params_and_never_a_tag_agents_see():
    from mcmsbench.runner import select_tasks
    assert {t.id for t in select_tasks(None, False, ["variants"])} == {t.id for t in PARAMS} and len(PARAMS) >= 6
    assert not any("variants" in t.tags for t in load_all().values())


@pytest.mark.parametrize("task", PARAMS, ids=[t.id for t in PARAMS])
def test_every_instance_reads_and_its_params_are_used(task):
    text = task.source.read_text()
    for name in task.params:
        assert f"${{{name}" in text or f"{name}." in text, f"{task.id}: param {name} is never used"
    for split, key in (("public", None), ("varied", None), ("heldout", KEY)):
        for trial in range(8):
            inst = task.instance(split, trial, key)
            assert inst.id == task.id and inst.prompt and "${" not in inst.prompt
            assert "${" not in json.dumps([inst.setup, inst.events, inst.grader, inst.inventory], default=str)
            assert all(isinstance(n, int) and n > 0 for n in inst.inventory.values())


# ------------------------------------------------------------------ multi_room_house: each shape can be built and graded

PLOT = Arena(ArenaConfig(), lambda cmd: "").plot(0)
MRH = load("multi_room_house")
SHAPES = [(c, w) for c in MRH.params["shape"]["choices"] for w in MRH.params["windows"]["choices"]]


def _house(s: int, rooms: int, windows: int, *, inner_doors=True):
    """A correct house of this shape, local to the anchor: walls 7 high, the second floor's slab at 3, the roof at 7,
    stairs up the west wall, partitions evenly spaced with a door in each on each floor, `windows` panes a floor."""
    stairs, well = [(1, k, 2 + k) for k in range(4)], {(1, 3, 2), (1, 3, 3), (1, 3, 4)}
    n = s - 2
    parts = [1 + round(n * k / rooms) for k in range(1, rooms)]
    w: dict = {}
    for x in range(s):
        for z in range(s):
            for y in range(7):
                if x in (0, s - 1) or z in (0, s - 1):
                    w[(x, y, z)] = "oak_planks"
            w[(x, 7, z)] = "oak_planks"
            if 0 < x < s - 1 and 0 < z < s - 1 and (x, 3, z) not in well:
                w[(x, 3, z)] = "oak_planks"
    for px in parts:
        for z in range(1, s - 1):
            for y in (0, 1, 2, 4, 5, 6):
                w[(px, y, z)] = "oak_planks"
        if inner_doors:
            for y in (0, 1, 4, 5):
                w[(px, y, s // 2)] = "oak_door"
    w[(2, 0, 0)] = w[(2, 1, 0)] = "oak_door"
    for p in stairs:
        w[p] = "oak_stairs"
    panes = [(s - 1, 2), (s - 1, s - 3), (s - 2, s - 1)][:windows]       # east wall twice, south wall once
    for y in (1, 5):
        for a, b in panes:
            w[(a, y, b)] = "glass_pane"
    ox, oy, oz = (PLOT.volume.min[i] + MRH.anchor[i] for i in range(3))
    at = lambda x, y, z: (ox + x, oy + y, oz + z)      # noqa: E731
    snap = {at(*p): b for p, b in w.items()}
    states = {at(*p): {"facing": "south", "half": "bottom"} for p in stairs}
    up, out = at(2, 4, s - 2), at(2, 0, -3)
    track = [(610.0, *up), (640.0, *at(1, 1, 3)), (660.0, *at(2, 0, 1)), (670.0, *out)]
    return Context({}, snap, diff({}, snap), PLOT.volume, PLOT.floor_y, out, [], [Frame("built", snap, up, [], t=600.0)],
                   {}, 20.0, 6000, PLOT.center(), after_states=states, track=track, seconds=700.0)


def _instance(shape, windows):
    """multi_room_house filled in with these values (the public instance's file, its draw replaced)."""
    values = {**variants.defaults(MRH.params), "shape": shape, "windows": windows}
    import yaml
    from mcmsbench.tasks import with_profile
    d = with_profile(yaml.safe_load(variants.render(MRH.source.read_text(), values)))
    return d["grader"], d["inventory"]


@pytest.mark.parametrize("shape,windows", SHAPES, ids=[f"{c['s']}x{c['s']}-{c['rooms']}rooms-{w}win" for c, w in SHAPES])
def test_each_shape_built_right_passes_and_built_as_the_public_house_fails(shape, windows):
    grader, inventory = _instance(shape, windows)
    s, rooms = shape["s"], shape["rooms"]
    r = grade(grader, _house(s, rooms, windows))
    assert r.passed and r.score == 1.0, (shape, windows, r.detail["steps"])
    # the materials given cover the build
    planks = sum(1 for b in _house(s, rooms, windows).after.values() if b.endswith("planks"))
    assert next(n for k, n in inventory.items() if k.endswith("_planks")) >= planks
    assert next(n for k, n in inventory.items() if k.endswith("_door")) >= 2 * (rooms - 1) + 1
    # the public instance's house is no answer to another shape
    if (s, rooms) != (9, 2):
        assert not grade(grader, _house(9, 2, 2)).passed
    # an open gap for a door is one room, not two
    assert not grade(grader, _house(s, rooms, windows, inner_doors=False)).checks["rooms"]


# ------------------------------------------------------------------ the other tasks: every draw can be done

def _all(task_id: str):
    t = load(task_id)
    return [t.instance("varied", i) for i in range(24)] + [t.instance("heldout", i, KEY) for i in range(24)] + [t]


def test_tower_every_start_and_outcrop_is_on_the_flattened_land_and_the_stone_is_enough():
    from mcmsbench.arena import Plot
    from mcmsbench.world.volume import Volume
    plot = Plot(0, (154, 69, 163), Volume((90, 58, 99), (218, 98, 227)), flat=False)
    sx, sy, sz = 154, 70, 163
    for t in _all("tower_under_threat"):
        v = t.param_values
        size, height = v["size"], v["height"]
        setup = t.render_setup(plot, (sx, sy, sz), "player")
        tp = next(c for c in setup if c.startswith("tp player "))
        bx, _, bz = (int(n) for n in tp.split()[2:5])
        assert abs(bx - sx) <= 60 and abs(bz - sz) <= 60                      # on the land (128 across), in the border
        assert 38 <= ((bx - sx) ** 2 + (bz - sz) ** 2) ** 0.5 <= 52
        stone = next(c for c in setup if c.endswith("minecraft:stone") and "{" not in c and int(c.split()[2]) == sy)
        x0, _, z0, x1, _, z1 = (int(n) for n in stone.split()[1:7])
        assert all(-64 <= p - c <= 63 for p, c in ((x0, sx), (x1, sx), (z0, sz), (z1, sz)))
        assert ((x0 + x1) / 2 - bx) ** 2 + ((z0 + z1) / 2 - bz) ** 2 <= 30 ** 2     # near the start
        assert max(abs(x0 - sx), abs(x1 - sx), abs(z0 - sz), abs(z1 - sz)) > 12    # not on the site
        tower = next(m["check"] for m in t.grader["steps"] if m["name"] == "tower")
        assert (tower["min_side"], tower["max_side"], tower["min_height"]) == (size, size, height)
        assert t.inventory["cobblestone"] >= 4 * (size - 1) * height + size * size + 32
        words = {(-1, 0): "east", (1, 0): "west", (0, 1): "north", (0, -1): "south", (-1, -1): "south-east", (1, 1): "north-west"}
        sign = lambda n: (n > 0) - (n < 0)      # noqa: E731
        assert f"blocks {words[(sign(bx - sx), sign(bz - sz))]} of you" in t.prompt
        assert f"A {v['undead']} and a {v['other']} are at" in t.prompt


def test_helping_pat_every_camp_is_in_the_plot_and_every_ask_can_be_met():
    from mcmsbench.arena import Plot
    from mcmsbench.tasks import Say
    from mcmsbench.world.volume import Volume
    plot = Plot(0, (-1, 62, 5), Volume((-49, 51, -43), (47, 91, 53)), flat=False)
    start = (-1, 63, 5)
    for t in _all("helping_pat"):
        v = t.param_values
        name, asked, bread = v["name"], v["first"] + v["more"], v["bread"]
        [(who, _, mode)] = t.render_players(plot, start)
        assert who == name and mode == "adventure"
        events = t.render_events(plot, start, "player")
        says = [c.text for _, c, *_ in events if isinstance(c, Say)]
        assert all(c.player == name for _, c, *_ in events if isinstance(c, Say))
        assert f"{v['first']} logs" in says[0] and f"{asked} logs" in says[1] and asked > v["first"]
        assert f"toss me {bread} bread" in says[3] and t.inventory["bread"] >= bread + 2
        setup = t.render_setup(plot, start, "player")
        pad = next(c for c in setup if c.endswith("minecraft:cobblestone"))
        x0, py, z0, x1, _, z1 = (int(n) for n in pad.split()[1:7])
        assert all(plot.volume.contains(p) for p in ((x0, py, z0), (x1, py + 6, z1)))
        tp = next(c for _, c, *_ in events if isinstance(c, str) and c.startswith(f"tp {name} "))
        px, ty, pz = (int(n) for n in tp.split()[2:5])
        assert x0 <= px <= x1 and z0 <= pz <= z1 and ty == py + 1                 # Pat stands on the pad
        logs = next(m["check"] for m in t.grader["steps"] if m["name"] == "logs_in")
        assert logs["items"] == {"*_log": asked}
        assert any(f"clear {name} minecraft:bread 0" in str(w) for _, _, *w in events)


def test_housesitter_every_crop_has_its_seed_in_the_chest_and_its_block_graded():
    for t in _all("housesitter_beetroot"):
        crop, count = t.param_values["crop"], t.param_values["count"]
        chest = next(c for c in t.setup if "Items" in c and "iron_hoe" in c)
        assert f'id:\\"minecraft:{crop["item"]}\\",count:{count + 8}' in chest or \
               f'id:"minecraft:{crop["item"]}",count:{count + 8}' in chest
        steps = {m["name"]: m["check"] for m in t.grader["steps"]}
        assert steps["planted"] == {"kind": "blocks", "block": crop["block"], "min": count}
        assert steps["watered"]["crop"] == crop["block"] and steps["watered"]["min"] == count
        assert f"at least {count} {crop['planted']} planted" in " ".join(t.prompt.split())


def test_two_story_house_every_ask_is_carried():
    for t in _all("two_story_house"):
        v = t.param_values
        inv = t.inventory
        assert inv[f"{v['wood']}_door"] > v["doors"] and inv["chest"] > v["chests"]
        assert inv["red_carpet"] + inv["white_carpet"] >= v["rugs"]
        assert inv[f"{v['wood']}_planks"] >= 400 * v["side"] * v["side"] / 36


def test_nether_highway_the_cargo_carried_is_the_cargo_graded():
    for t in _all("nether_highway"):
        item, count = t.param_values["cargo"]["item"], t.param_values["count"]
        assert t.inventory[item] == count
        delivered = next(m["check"] for m in t.grader["steps"] if m["name"] == "delivered")
        assert delivered["items"] == {item: count}
        assert f"Take the {count} {t.param_values['cargo']['words']}" in " ".join(t.prompt.split())


# ------------------------------------------------------------------ held-out packs: domains no task file shows

PACKED = """params:
  target:
    default: {id: table, words: "a table"}
    choices: [{id: table, words: "a table"}, {id: chest, words: "a chest"}]
  n: {default: 2, range: [1, 3]}
prompt: "make ${target.words}, ${n} of them"
setup: ["setblock {sx} {sy} {sz} minecraft:${target.id}"]
grader: {kind: placed, block: "${target.id}", count: ${n}}
"""
PACK = """params:
  target:
    choices: [{id: anvil, words: "an anvil"}, {id: lodestone, words: "a lodestone"}]
setup: ["setblock {sx+1} {sy} {sz} minecraft:iron_ore", "say ${target.id}"]
"""


def _packed(tmp_path, monkeypatch, pack: str | None = PACK):
    task = tmp_path / "tasks" / "packed.yaml"
    task.parent.mkdir()
    task.write_text(PACKED)
    d = tmp_path / "private"
    d.mkdir()
    if pack is not None:
        (d / "packed.yaml").write_text(pack)
    monkeypatch.setenv(variants.PACK_ENV, str(d))
    return task


def test_a_pack_gives_the_heldout_split_its_own_domains_and_setup(tmp_path, monkeypatch):
    task = _packed(tmp_path, monkeypatch)
    held = [Task.from_yaml(task, "heldout", i, KEY) for i in range(16)]
    assert {t.param_values["target"]["id"] for t in held} == {"anvil", "lodestone"}       # never the file's
    assert {t.param_values["n"] for t in held} <= {1, 2, 3}                               # the file's own range still
    t = held[0]
    assert t.pack == variants.pack_id(PACK) and f"make {t.param_values['target']['words']}" in t.prompt
    assert t.setup[-2:] == ["setblock {sx+1} {sy} {sz} minecraft:iron_ore", f"say {t.param_values['target']['id']}"]
    assert t.grader["block"] == t.param_values["target"]["id"]
    # the public and varied splits never read it
    for split in ("public", "varied"):
        for i in range(8):
            u = Task.from_yaml(task, split, i)
            assert u.param_values["target"]["id"] in ("table", "chest") and u.pack is None and len(u.setup) == 1


def test_a_pack_that_does_not_fit_its_task_is_refused(tmp_path, monkeypatch):
    for bad, why in (("params:\n  colour:\n    choices: [red]\n", "no param 'colour'"),
                     ("params:\n  target:\n    choices: [{id: anvil}]\n", "needs the fields"),
                     ("params:\n  target:\n    choices: []\n", "no choices"),
                     ("params:\n  n:\n    range: [3, 1]\n", "range"),
                     ("params:\n  n: {choices: [1], range: [1, 2]}\n", "one of")):
        root = tmp_path / str(abs(hash(bad)))
        root.mkdir()
        task = _packed(root, monkeypatch, bad)
        with pytest.raises(variants.VariantError, match=why):
            Task.from_yaml(task, "heldout", 0, KEY)


def test_heldout_without_a_pack_says_its_domains_are_public(tmp_path, monkeypatch):
    from mcmsbench.runner import bench_info, caveats_for

    class A:
        name, provider, model, capabilities = "a", None, "m", set()
        manifest = type("M", (), {"path": None})()
    monkeypatch.setattr("mcmsbench.runner.task_hash", lambda task_id: "h")
    task = _packed(tmp_path, monkeypatch, pack=None)
    t = Task.from_yaml(task, "heldout", 0, KEY)
    assert t.pack is None and (t.param_values["target"]["id"], t.param_values["n"]) != ("table", 2)   # the file's own
    assert bench_info(t, A())["pack_id"] is None
    [cv] = caveats_for(t, A())
    assert cv["tag"] == "public_heldout" and "public" in cv["says"]
    (tmp_path / "private" / "packed.yaml").write_text(PACK)
    t = Task.from_yaml(task, "heldout", 0, KEY)
    assert bench_info(t, A())["pack_id"] == variants.pack_id(PACK) and caveats_for(t, A()) == []


def test_the_pack_directory_is_the_environments_else_heldout_in_the_checkout(tmp_path, monkeypatch):
    monkeypatch.delenv(variants.PACK_ENV, raising=False)
    monkeypatch.setattr("mcmsbench.config.ROOT", tmp_path)
    assert variants.heldout_dir() is None
    (tmp_path / "heldout").mkdir()
    assert variants.heldout_dir() == tmp_path / "heldout"
    monkeypatch.setenv(variants.PACK_ENV, str(tmp_path / "elsewhere"))
    assert variants.heldout_dir() is None                                                  # named but not there
    (tmp_path / "elsewhere").mkdir()
    assert variants.heldout_dir() == tmp_path / "elsewhere"
