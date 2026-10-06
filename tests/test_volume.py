from mcmsbench.world.volume import Volume, diff


def test_volume_iter_and_contains():
    v = Volume((0, 0, 0), (1, 1, 1))
    assert v.count == 8 and len(list(v)) == 8
    assert v.contains((1, 1, 1)) and not v.contains((2, 0, 0))
    assert v.expand(1).min == (-1, -1, -1)


def test_diff_categories():
    before = {(0, 0, 0): "stone", (1, 0, 0): "dirt"}
    after = {(0, 0, 0): "stone", (1, 0, 0): "cobblestone", (2, 0, 0): "oak_planks"}
    d = diff(before, after)
    assert d.placed == {(2, 0, 0): "oak_planks"}
    assert d.replaced == {(1, 0, 0): ("dirt", "cobblestone")}
    assert d.broken == {}
    assert d.changed == {(1, 0, 0), (2, 0, 0)}
