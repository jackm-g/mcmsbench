import struct
from mcmsbench.rcon import _pack


def test_pack_layout():
    pkt = _pack(7, 2, "list")
    (length,) = struct.unpack("<i", pkt[:4])
    assert length == len(pkt) - 4 == 4 + 4 + 4 + 2
    rid, kind = struct.unpack("<ii", pkt[4:12])
    assert (rid, kind) == (7, 2)
    assert pkt[12:] == b"list\x00\x00"


def test_parse_inventory_and_mismatch():
    from mcmsbench.arena import inventory_mismatch, parse_inventory
    out = ('x has the following entity data: [{count: 5, Slot: 1b, id: "minecraft:birch_log"}, '
           '{count: 32, Slot: 10b, id: "minecraft:oak_planks"}, '
           '{components: {"minecraft:damage": 3}, count: 1, Slot: 11b, id: "minecraft:wooden_pickaxe"}]')
    inv = parse_inventory(out)
    assert [(i["name"], i["count"], i["slot"]) for i in inv] == [("birch_log", 5, 1), ("oak_planks", 32, 10), ("wooden_pickaxe", 1, 11)]
    client = [{"name": "oak_planks", "count": 36}, {"name": "birch_log", "count": 5}]
    assert inventory_mismatch(client, inv) == {"oak_planks": {"client": 36, "server": 32}, "wooden_pickaxe": {"client": 0, "server": 1}}
    assert inventory_mismatch(inv, inv) == {}
