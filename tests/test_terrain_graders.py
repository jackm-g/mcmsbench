"""Terrain-aware structure grading and the milestone ladder — synthetic, no server."""
from mcmsbench.graders import Context, Frame, grade
from mcmsbench.world.volume import Volume, diff

PLOT = Volume((0, 50, 0), (63, 100, 63))


def terrain(h=64, size=64, slope=False):
    """Natural ground: stone below dirt below grass. Optional slope rising +1 per 8 blocks in x."""
    world = {}
    for x in range(size):
        for z in range(size):
            top = h + (x // 8 if slope else 0)
            for y in range(top - 4, top - 1):
                world[(x, y, z)] = "stone"
            world[(x, top - 1, z)] = "dirt"
            world[(x, top, z)] = "grass_block"
    return world


def walls(x0, y0, z0, w, d, h, mat="oak_planks", door=True):
    out = {(x0 + x, y0 + y, z0 + z): mat for y in range(h) for x in range(w) for z in range(d)
           if x in (0, w - 1) or z in (0, d - 1)}
    if door:
        out.pop((x0 + w // 2, y0, z0)); out.pop((x0 + w // 2, y0 + 1, z0))
    return out


def roof(x0, y, z0, w, d, mat="oak_planks"):
    return {(x0 + x, y, z0 + z): mat for x in range(w) for z in range(d)}


def ctx(before, after, pos=None, inv=None, frames=()):
    return Context(before, after, diff(before, after), PLOT, None, pos, inv or [], list(frames))


HOUSE = {"kind": "structure", "min_footprint": 25, "min_interior": 8, "min_placed": 40}


def test_house_on_natural_ground_no_floor_y():
    ground = terrain()
    after = dict(ground); after.update(walls(10, 65, 10, 7, 7, 3)); after.update(roof(10, 68, 10, 7, 7))
    r = grade(HOUSE, ctx(ground, after))
    assert r.passed, r.to_dict()


def test_house_missing_roof_on_terrain_fails():
    ground = terrain()
    after = dict(ground); after.update(walls(10, 65, 10, 7, 7, 3))
    r = grade(HOUSE, ctx(ground, after))
    assert not r.passed and not r.checks["has_roof"]


def test_hole_above_ground_is_a_leak_not_an_entrance():
    ground = terrain()
    w = walls(10, 65, 10, 7, 7, 3); w.pop((16, 67, 13))  # hole in the east wall, 2 up
    after = dict(ground); after.update(w); after.update(roof(10, 68, 10, 7, 7))
    r = grade(HOUSE, ctx(ground, after))
    assert not r.passed and not r.checks["enclosed"]


def test_walled_off_cave_is_not_a_house():
    """A natural cavity closed with a few blocks: enclosed and roofed, but too little was built."""
    ground = terrain()
    cavity = {(x, y, z) for x in range(20, 27) for y in range(58, 61) for z in range(20, 27)}
    for c in cavity:
        ground.pop(c, None)
    after = dict(ground)
    after.update({(20, y, 23): "cobblestone" for y in range(58, 61)})  # 3 blocks "placed" at the mouth
    r = grade(HOUSE, ctx(ground, after))
    assert not r.passed and not r.checks["min_placed"]


def test_house_on_slope_with_raised_doorway():
    ground = terrain(slope=True)
    x0 = 16  # ground here is y=66 (x//8 == 2)
    w = walls(x0, 67, 10, 7, 7, 3)
    after = dict(ground); after.update(w); after.update(roof(x0, 70, 10, 7, 7))
    r = grade(HOUSE, ctx(ground, after))
    assert r.passed, r.to_dict()


def test_level_site():
    flat = terrain(); sloped = terrain(slope=True)
    house = walls(10, 65, 10, 5, 5, 3)
    a = dict(flat); a.update(house)
    assert grade({"kind": "level_site", "tolerance": 0}, ctx(flat, a)).passed
    # on the slope, walls at y=69 float over the x=30,31 columns (ground 67): footing gaps
    house2 = walls(30, 69, 10, 5, 5, 3)
    b = dict(sloped); b.update(house2)
    r = grade({"kind": "level_site", "tolerance": 1}, ctx(sloped, b))
    assert not r.passed and not r.checks["footing"] and r.detail["footing_gaps"] > 0
    # fill under those columns: footing ok; interior ground is 67/68 -> spread 1
    fill = {(x, 68, z): "dirt" for x in (30, 31) for z in range(10, 15)}
    c = dict(b); c.update(fill)
    r = grade({"kind": "level_site", "tolerance": 0}, ctx(sloped, c))
    assert r.passed, r.to_dict()   # the fill is the new ground: levelled
    # a placed floor makes the interior perfectly level
    d = dict(c); d.update(roof(30, 69, 10, 5, 5))
    assert grade({"kind": "level_site", "tolerance": 0}, ctx(sloped, d)).passed


def test_have_or_placed_and_dug():
    ground = terrain()
    after = dict(ground); after[(5, 65, 5)] = "crafting_table"; del after[(7, 64, 7)]; del after[(7, 63, 7)]
    c = ctx(ground, after, inv=[{"name": "cobblestone", "count": 3}])
    assert grade({"kind": "have_or_placed", "item": "crafting_table"}, c).passed
    assert not grade({"kind": "have_or_placed", "item": "furnace"}, c).passed
    assert grade({"kind": "dug", "block": ["dirt", "grass_block"], "count": 2}, c).passed
    assert not grade({"kind": "dug", "block": "stone", "count": 1}, c).passed


MILESTONES = {
    "kind": "milestones", "required": "house",
    "steps": [
        {"name": "wood", "weight": 1, "check": {"kind": "inventory", "item": "*_log", "count": 1}},
        {"name": "table", "weight": 1, "check": {"kind": "have_or_placed", "item": "crafting_table"}},
        {"name": "house", "weight": 3, "check": HOUSE},
    ],
}


def test_milestones_progress_and_partial_credit():
    ground = terrain()
    f1 = Frame("step_01", dict(ground), (5, 65, 5), [{"name": "oak_log", "count": 12}])
    s2 = dict(ground); s2[(5, 65, 5)] = "crafting_table"
    f2 = Frame("step_02", s2, (5, 65, 6), [{"name": "oak_planks", "count": 40}])  # logs consumed: wood still counts
    s3 = dict(s2); s3.update(walls(10, 65, 10, 7, 7, 3)); s3.update(roof(10, 68, 10, 7, 7))
    r = grade(MILESTONES, ctx(ground, s3, inv=[], frames=[f1, f2]))
    assert r.passed and r.score == 1.0
    assert r.detail["reached_at_step"] == {"wood": 1, "table": 2, "house": 3}
    # never built the house: partial credit, fail
    r2 = grade(MILESTONES, ctx(ground, s2, inv=[], frames=[f1, f2]))
    assert not r2.passed and abs(r2.score - 2 / 5) < 1e-9 and r2.detail["reached_at_step"]["house"] is None
    # house built then destroyed before the end: required must hold at the end
    f3 = Frame("step_03", s3, None, [])
    r3 = grade(MILESTONES, ctx(ground, s2, inv=[], frames=[f1, f2, f3]))
    assert not r3.passed and r3.detail["reached_at_step"]["house"] == 3 and not r3.detail["holds_at_end"]["house"]


def test_stray_blocks_do_not_break_the_structure():
    ground = terrain()
    after = dict(ground); after.update(walls(10, 65, 10, 7, 7, 3)); after.update(roof(10, 68, 10, 7, 7))
    after[(3, 65, 3)] = "crafting_table"; after[(40, 65, 40)] = "dirt"   # table + a scaffold block far away
    r = grade(HOUSE, ctx(ground, after))
    assert r.passed, r.to_dict()
    assert r.detail["placed_blocks"] == 70 + 49  # walls minus doorway, plus roof


def test_blocks_placed_over_grass_count_as_built():
    ground = terrain()
    for x in range(10, 17):
        for z in range(10, 17):
            ground[(x, 65, z)] = "short_grass"          # tall grass covering the site
    ground[(3, 65, 3)] = "short_grass"
    after = dict(ground); after.update(walls(10, 65, 10, 7, 7, 3)); after.update(roof(10, 68, 10, 7, 7))
    for x in range(11, 16):
        for z in range(11, 16):
            after.pop((x, 65, z), None)                  # interior grass trampled
    after[(3, 65, 3)] = "crafting_table"
    c = ctx(ground, after)
    assert c.diff.placed and c.diff.replaced, "the setup should produce both placed and replaced entries"
    assert grade(HOUSE, c).passed
    assert grade({"kind": "have_or_placed", "item": "crafting_table"}, c).passed
    assert grade({"kind": "level_site", "tolerance": 0}, c).passed


def _hut(x0=0, y0=65, z0=0, n=5, h=3, door=(2, 0)):
    """A 5x5 plank hut on a grass floor at y0-1: walls h high, a roof, a 1x2 doorway in the z0 wall."""
    walls = {(x, y, z): "oak_planks" for x in range(x0, x0 + n) for y in range(y0, y0 + h) for z in range(z0, z0 + n)
             if x in (x0, x0 + n - 1) or z in (z0, z0 + n - 1)}
    roof = {(x, y0 + h, z): "oak_planks" for x in range(x0, x0 + n) for z in range(z0, z0 + n)}
    for dy in (0, 1):
        del walls[(x0 + door[0], y0 + dy, z0 + door[1])]
    ground = {(x, y0 - 1, z): "grass_block" for x in range(x0 - 3, x0 + n + 3) for z in range(z0 - 3, z0 + n + 3)}
    return {**walls, **roof}, ground


def test_a_stray_scaffold_block_on_a_wall_does_not_unmake_the_house():
    """anthropic-0926 survival_house (Opus): one dirt block left from roofing touched the east wall, widened the box
    by a layer of air, and a sound house graded unenclosed and roofless."""
    from mcmsbench.graders import structural as S
    house, ground = _hut()
    placed = dict(house)
    placed[(5, 66, 2)] = "dirt"                               # the scaffold, against the east wall
    after = {**ground, **placed}
    a = S.analyze(after, placed, Volume((-10, 60, -10), (20, 80, 20)))
    assert a.enclosed and a.has_roof and a.has_entrance and a.bbox["max"][0] == 4, a.to_dict()
    assert S.trim_strays({**house, (5, 66, 2): "dirt"}).keys() == house.keys()


def test_a_wall_on_leaf_litter_stands_on_the_ground():
    from mcmsbench.graders import structural as S
    house, ground = _hut()
    # one wall column stands on leaf litter over the grass: still on the ground
    lifted = {(x, y + 1, z) if (x, z) == (4, 3) else (x, y, z): b for (x, y, z), b in house.items()}
    after = {**ground, (4, 65, 3): "leaf_litter", **lifted}
    assert S.site_levelness(after, lifted)["footing_ok"]
    after[(4, 65, 3)] = "air"                                  # a real gap under the wall
    assert not S.site_levelness(after, lifted)["footing_ok"]
