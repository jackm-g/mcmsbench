"""probe: an arena's blocks as text over RCON, against a fake server that answers `execute ... if block` from a dict. The
legend, a side view, the ground from above, unloaded chunks, and force-loading only what was not loaded. No server."""
import re

import pytest

from mcmsbench.probe import cell, loaded, slice_view, top_view

TAGS = {"#minecraft:air": {"air", "cave_air"}, "#minecraft:replaceable": {"air", "cave_air", "lava", "water", "fire", "short_grass"},
        "#minecraft:fire": {"fire", "soul_fire"}}


class Server:
    """Blocks by (dimension, x, y, z); netherrack below y 40, air above, `unloaded` chunks not readable."""

    def __init__(self, blocks=None, unloaded=(), forced=()):
        self.blocks = blocks or {}
        self.unloaded = set(unloaded)
        self.forced = set(forced)
        self.sent: list[str] = []

    def block(self, dim, x, y, z):
        return self.blocks.get((dim, x, y, z), "netherrack" if y < 40 else "air")

    def __call__(self, cmd: str) -> str:
        self.sent.append(cmd)
        m = re.match(r"execute in minecraft:(\w+) (.*)", cmd)
        dim, rest = m.group(1), m.group(2)
        if q := re.match(r"run forceload query (-?\d+) (-?\d+)", rest):
            key = (dim, int(q.group(1)) >> 4, int(q.group(2)) >> 4)
            return f"Chunk at [{key[1]}, {key[2]}] is {'marked' if key in self.forced else 'not marked'} for force loading"
        if f := re.match(r"run forceload (add|remove) (-?\d+) (-?\d+)", rest):
            key = (dim, int(f.group(2)) >> 4, int(f.group(3)) >> 4)
            (self.unloaded.discard if f.group(1) == "add" else self.unloaded.add)(key)
            (self.forced.add if f.group(1) == "add" else self.forced.discard)(key)
            return "ok"
        b = re.match(r"if block (-?\d+) (-?\d+) (-?\d+) (\S+)", rest)
        x, y, z, want = int(b.group(1)), int(b.group(2)), int(b.group(3)), b.group(4)
        if (dim, x >> 4, z >> 4) in self.unloaded:
            return "That position is not loaded"
        name = self.block(dim, x, y, z)
        ok = name in TAGS[want] if want.startswith("#") else want == f"minecraft:{name}"
        return "Test passed" if ok else "Test failed"


def test_each_cell_reads_as_what_a_player_sees_there():
    s = Server({("the_nether", 0, 45, 0): "lava", ("the_nether", 1, 45, 0): "water", ("the_nether", 2, 45, 0): "fire",
                ("the_nether", 3, 45, 0): "short_grass", ("the_nether", 4, 45, 0): "nether_portal",
                ("the_nether", 5, 45, 0): "obsidian"})
    assert [cell(s, "the_nether", x, 45, 0) for x in range(7)] == ["L", "~", "f", ",", "P", "O", "."]
    assert cell(s, "the_nether", 0, 30, 0) == "#"
    assert cell(Server(unloaded={("the_nether", 0, 0)}), "the_nether", 3, 45, 3) == "?"


def test_a_side_view_runs_the_way_given_with_y_down_the_page():
    s = Server({("the_nether", -2, 40, 5): "lava"})
    out = slice_view(s, "the_nether", x=(0, -3), z=5, y=(39, 41)).splitlines()
    assert "x from 0 (left) to -3 (right)" in out[0]
    assert out[1:4] == ["   41 ....", "   40 ..L.", "   39 ####"]
    assert out[-1].split() == ["x", "0"]
    with pytest.raises(ValueError):
        slice_view(s, "the_nether", x=(0, 3), z=(0, 3), y=(39, 41))


def test_the_ground_from_above_is_the_height_stood_at():
    s = Server({("overworld", 1, 40, 0): "lava", ("overworld", 2, 40, 0): "dirt", ("overworld", 2, 41, 0): "dirt"})
    rows = top_view(s, "overworld", x=(0, 3), z=(0, 0), y=(35, 45)).splitlines()
    # x 0: ground at 39, stood at 40; x 1: lava under open air; x 2: dirt up to 41, stood at 42
    assert rows[1].split() == ["0", "40", "LL", "42", "40"]
    roof = Server({("overworld", 0, 45, 0): "dirt"})
    assert top_view(roof, "overworld", x=(0, 0), z=(0, 0), y=(35, 45)).splitlines()[1].split() == ["0", "##"]


def test_loading_marks_the_chunks_not_marked_and_lets_only_those_go():
    # x -20..10 is chunks -2, -1 and 0: chunk 0 is marked already (the plot's); -1 and -2 are loaded by a player who
    # is about to leave, not marked
    s = Server(forced={("the_nether", 0, 0)})
    with loaded(s, "the_nether", -20, 0, 10, 10, sleep=lambda _: None) as n:
        assert n == 2
        assert cell(s, "the_nether", -5, 45, 5) == "."
    adds = [c.split()[-2] for c in s.sent if "forceload add" in c]
    removes = [c.split()[-2] for c in s.sent if "forceload remove" in c]
    assert adds == removes == ["-32", "-16"]
    assert s.forced == {("the_nether", 0, 0)}
