"""schematic / rail_path / functional graders — synthetic, plus a fake RCON."""
from mcmsbench.graders import Context, grade
from mcmsbench.graders.mechanical import _resolve, rail_route
from mcmsbench.world.volume import Volume, diff

PLOT = Volume((100, 10, 100), (131, 40, 131))


def ctx(after, states=None, rcon=None, fmt=None):
    return Context({}, after, diff({}, after), PLOT, 9, None, [], [], {}, 20.0, 1000, (115, 10, 115),
                   states or {}, rcon, fmt)


HUT = {"kind": "schematic", "at": [2, 0, 2], "legend": {"P": "oak_planks", "G": "glass"},
       "layers": [["PPP", "P.P", "PPP"], ["PGP", "?.?", "PGP"], ["PPP", "PPP", "PPP"]]}


def build_hut(ox=102, oy=10, oz=102, skip=()):
    after = {}
    for y, layer in enumerate(HUT["layers"]):
        for z, row in enumerate(layer):
            for x, ch in enumerate(row):
                if ch in "PG" and (x, y, z) not in skip:
                    after[(ox + x, oy + y, oz + z)] = HUT["legend"][ch]
    return after


def test_schematic_exact_and_partial():
    r = grade(HUT, ctx(build_hut()))
    assert r.passed and r.score == 1.0
    r = grade(HUT, ctx(build_hut(skip=[(1, 2, 1)])))          # one roof block missing
    assert not r.passed and 0.9 < r.score < 1.0 and r.detail["mismatches"][0][3:] == ["oak_planks", "air"]
    filled = build_hut(); filled[(103, 10, 103)] = "dirt"       # interior must be air
    assert not grade(HUT, ctx(filled)).passed
    assert grade({**HUT, "min_match": 0.9}, ctx(build_hut(skip=[(1, 2, 1)]))).passed


def rails(x0, z0, n, y=10, powered_at=()):
    return {(x0 + i, y, z0): ("powered_rail" if i in powered_at else "rail") for i in range(n)}


def test_rail_route_and_grader():
    after = rails(100, 110, 20)
    r = rail_route(after, (100, 10, 110), (119, 10, 110))
    assert r["connected"] and len(r["path"]) == 20 and r["reach"] == 1.0
    broken = dict(after); del broken[(110, 10, 110)]
    r2 = rail_route(broken, (100, 10, 110), (119, 10, 110))
    assert not r2["connected"] and 0.4 < r2["reach"] < 0.6
    # slope: rail one block up still connects
    sloped = dict(after); sloped[(120, 11, 110)] = "rail"
    assert rail_route(sloped, (100, 10, 110), (120, 11, 110))["connected"]

    spec = {"kind": "rail_path", "from": [0, 0, 10], "to": [19, 0, 10], "powered_every": 8}
    powered = rails(100, 110, 20, powered_at=(0, 7, 14, 19))
    states = {(100 + i, 10, 110): {"powered": "true"} for i in (0, 7, 14, 19)}
    assert grade(spec, ctx(powered, states)).passed
    unpowered = grade(spec, ctx(powered, {}))                    # rails present but not powered
    assert not unpowered.passed and unpowered.checks["connected"] and not unpowered.checks["powered_rails_spaced"]
    assert not grade(spec, ctx(broken)).passed


class FakeRcon:
    def __init__(self, answers):
        self.answers = answers; self.cmds = []
    def __call__(self, cmd):
        self.cmds.append(cmd)
        for k, v in self.answers.items():
            if k in cmd:
                return v
        return "ok"


def test_functional_templates_and_asserts():
    after = {(105, 10, 105): "iron_door", (105, 11, 105): "iron_door", (107, 10, 105): "lever"}
    states = {(107, 10, 105): {"face": "floor", "facing": "north", "powered": "false"}}
    fmt = lambda t: t.format(ax=104, ay=10, az=104)
    c = ctx(after, states, FakeRcon({"iron_door[open=true]": "Test passed"}), fmt)
    assert _resolve(c, "setblock {find:lever} {state:lever|powered=true}") == \
        "setblock 107 10 105 minecraft:lever[face=floor,facing=north,powered=true]"
    assert _resolve(c, "execute if block {ax+1} {ay} {az+1} minecraft:iron_door") == "execute if block 105 10 105 minecraft:iron_door"
    spec = {"kind": "functional", "steps": [
        {"do": "setblock {find:lever} {state:lever|powered=true}"}, {"wait": 0},
        {"assert": "execute if block {ax+1} {ay} {az+1} minecraft:iron_door[open=true]"},
        {"assert_not": "execute if block {ax+1} {ay} {az+1} minecraft:air"}]}
    r = grade(spec, c)
    assert r.passed and r.score == 1.0 and c.rcon.cmds[0].startswith("setblock 107 10 105")
    no_server = grade(spec, ctx(after, states, None, fmt))
    assert not no_server.passed and no_server.checks == {"live_world": False}


def test_a_poke_updates_the_door_through_an_air_cell_beside_it():
    """A lever flipped by setblock updates its own neighbours only, so a door powered through the lever's block never
    hears of it (verified on the 26.1 arena, and anthropic-0926 redstone_door: two working doors graded broken). The
    poke sets and clears a barrier in the first air cell beside the door."""
    after = {(105, 10, 105): "iron_door", (105, 11, 105): "iron_door", (107, 10, 105): "lever"}
    fmt = lambda t: t.format(ax=105, ay=10, az=105)
    rcon = FakeRcon({"105 10 104 minecraft:air": "Test failed", "105 10 106 minecraft:air": "Test passed"})
    c = ctx(after, {}, rcon, fmt)
    spec = {"kind": "functional", "steps": [{"poke": "{ax} {ay} {az}"}]}
    r = grade(spec, c)
    assert r.detail["log"] == [{"poke": "105 10 105", "out": "updated via 105 10 106"}]
    assert rcon.cmds[-2:] == ["setblock 105 10 106 minecraft:barrier", "setblock 105 10 106 minecraft:air"]
    boxed = FakeRcon({"minecraft:air": "Test failed"})
    grade(spec, ctx(after, {}, boxed, fmt))
    assert not any(cmd.startswith("setblock") for cmd in boxed.cmds)          # nothing beside it is air: nothing set


def test_an_assert_within_polls_until_it_passes():
    answers = iter(["Test failed", "Test failed", "Test passed"])
    class Later(FakeRcon):
        def __call__(self, cmd):
            self.cmds.append(cmd)
            return next(answers) if cmd.startswith("execute") else "ok"
    rcon = Later({})
    spec = {"kind": "functional", "steps": [{"assert": "execute if entity @e[type=minecart]", "within": 3}]}
    r = grade(spec, ctx({}, {}, rcon, lambda t: t))
    assert r.passed and len([c for c in rcon.cmds if c.startswith("execute")]) == 3
    never = FakeRcon({"execute": "Test failed"})
    assert not grade({"kind": "functional", "steps": [{"assert": "execute if entity @e", "within": 0.6}]},
                     ctx({}, {}, never, lambda t: t)).passed
