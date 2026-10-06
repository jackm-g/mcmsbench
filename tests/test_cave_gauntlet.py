"""cave_gauntlet: the `cavern` start situation (a sealed, lit, irregular cave the waves can be summoned into) and the
task's waves, each gated on the last being dead. No server needed."""
import re

from mcmsbench.arena import Plot
from mcmsbench.start import CAVERN_ROOF, StartSpec, cavern_commands, resolve_start, situation_commands
from mcmsbench.tasks import load
from mcmsbench.world.volume import Volume

TASK = load("cave_gauntlet")


def _cave(start):
    """{(x, z): air rows over the floor}, the pillars' and torches' columns, from the commands."""
    open_, pillars, torches = {}, [], []
    for c in cavern_commands(start, 30)[1:]:
        if m := re.match(r"fill (-?\d+) (-?\d+) (-?\d+) (-?\d+) (-?\d+) (-?\d+) minecraft:air$", c):
            x0, y0, z0, x1, y1, z1 = map(int, m.groups())
            assert x0 == x1 and y0 == start[1]
            open_.update({(x0, z): y1 - y0 + 1 for z in range(z0, z1 + 1)})
        elif c.endswith(" minecraft:stone") and "replace" not in c:
            pillars.append(tuple(int(v) for v in c.split()[1:4:2]))
        elif "torch" in c:
            torches.append(tuple(int(v) for v in c.split()[1:4:2]))
    return open_, pillars, torches


def test_cavern_is_sealed_irregular_and_lit():
    x, y, z = start = (154, 50, 163)
    cmds = cavern_commands(start, 30)
    assert cmds == cavern_commands(start, 30)                   # seeded by the spot: a rerun is the same cave
    assert cmds[0] == f"fill {x - 16} {y - 1} {z - 16} {x + 15} {y + CAVERN_ROOF} {z + 15} minecraft:stone"
    open_, pillars, torches = _cave(start)
    xs, zs = [c[0] for c in open_], [c[1] for c in open_]
    assert x - 15 <= min(xs) and max(xs) <= x + 14 and z - 15 <= min(zs) and max(zs) <= z + 14   # inside the shell
    assert max(xs) - min(xs) >= 25 and max(zs) - min(zs) >= 25  # about 30 across
    assert 450 < len(open_) < 30 * 30                           # corners and the wall line cut in: not a box
    assert set(open_.values()) <= set(range(3, CAVERN_ROOF + 1)) and open_[(x, z)] == CAVERN_ROOF   # a dome
    assert all(p not in torches for p in pillars) and all(t in open_ for t in torches) and len(torches) >= 9
    # every open column within 8 (manhattan) of a torch: light 6+ everywhere, nothing spawns (a torch whose spot is
    # wall moves to the open floor nearest it, or the corners go dark)
    assert all(min(abs(cx - tx) + abs(cz - tz) for tx, tz in torches) <= 8 for cx, cz in open_)
    assert situation_commands(StartSpec(situation="cavern", width=30), start) == cmds


def test_cavern_start_is_depth_under_the_ground_and_summon_spots_are_open():
    spec = StartSpec(situation="cavern", width=30, depth=20)
    assert resolve_start(spec, (0, 71, 0), "t", 0, 1, lambda x, z: (70, "grass_block")) == (0, 50, 0)
    spots = [(9, 0), (-9, 0), (0, 9), (0, -9), (7, 7), (-7, 7), (7, -7), (-7, -7), (0, 0)]
    for i in range(300):                                        # any spot the world puts it
        x, z = i * 37 - 5000, 2000 - i * 13
        open_, pillars, _ = _cave((x, 40 + i % 25, z))
        for dx, dz in spots:
            assert (x + dx, z + dz) in open_ and (x + dx, z + dz) not in pillars, (i, dx, dz)


def test_waves_are_summoned_into_the_open_one_after_another():
    assert TASK.start.situation == "cavern" and TASK.world.difficulty == "hard"
    assert TASK.inventory == {"iron_sword": 1, "cooked_porkchop": 19}
    start = (154, 50, 163)
    plot = Plot(0, (154, 69, 163), Volume((90, 57, 99), (218, 97, 227)), flat=False)
    setup = TASK.render_setup(plot, start)
    assert "item replace entity player weapon.offhand with minecraft:shield" in setup
    events = TASK.render_events(plot, start)
    summons = [c for c in setup + [e[1] for e in events] if c.startswith("summon")]
    mobs = [c.split()[1].split(":")[1] for c in summons]
    assert mobs == ["zombie", "zombie", "zombie", "skeleton", "zombie", "skeleton"]
    assert all('Tags:["gauntlet"]' in c and "PersistenceRequired:1b" in c for c in summons)
    assert all('mainhand:{id:"minecraft:bow"' in c for c in summons if "skeleton" in c)   # NBT summons come unarmed
    open_, pillars, _ = _cave(start)
    for c in summons:
        sx, sy, sz = (int(v) for v in c.split()[2:5])
        assert sy == start[1] and (sx, sz) in open_ and (sx, sz) not in pillars, c
    gated = [e for e in events if len(e) == 3]
    assert [e[1].split()[1] for e in gated] == ["minecraft:zombie", "minecraft:skeleton", "minecraft:zombie"]
    assert all(e[2] == "execute unless entity @e[tag=gauntlet]" for e in gated)
    assert events.index(gated[0]) == 0                          # wave 2 opens the queue: nothing fires ahead of it
