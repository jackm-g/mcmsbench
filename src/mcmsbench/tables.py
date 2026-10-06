"""Block and item tables the graders read.

NATURAL: block names that occur in the world by themselves (terrain, plants, liquids). A grader counting what an agent
*built* leaves these out: a dirt block an agent knocked loose and set down again is terrain, not a wall.

FOOD_POINTS: what an item restores to the hunger bar (vanilla, minecraft-data's foods.json for 26.1). Only food a
player eats to be fed: rotten flesh, spider eyes, poisonous potatoes and pufferfish are left out (they poison), and so
are the fish buckets (not eaten). The `food_stock` grader reads it.
"""
from __future__ import annotations

import fnmatch

NATURAL = [
    "*_log", "*_wood", "*_leaves", "*_sapling", "stone", "deepslate", "cobbled_deepslate", "dirt", "grass_block",
    "coarse_dirt", "podzol", "rooted_dirt", "mud", "sand", "red_sand", "gravel", "clay", "andesite", "diorite",
    "granite", "tuff", "calcite", "dripstone_block", "pointed_dripstone", "sandstone", "red_sandstone", "*_ore",
    "netherrack", "snow", "snow_block", "powder_snow", "ice", "packed_ice", "moss_block", "moss_carpet",
    "short_grass", "grass", "tall_grass", "fern", "large_fern", "dead_bush", "*_flower", "dandelion", "poppy",
    "blue_orchid", "allium", "azure_bluet", "*_tulip", "oxeye_daisy", "cornflower", "lily_of_the_valley",
    "sunflower", "lilac", "rose_bush", "peony", "kelp", "kelp_plant", "seagrass", "tall_seagrass", "vine",
    "glow_lichen", "cactus", "sugar_cane", "bamboo", "pumpkin", "melon", "brown_mushroom", "red_mushroom",
    "*_mushroom_block", "mushroom_stem", "lily_pad", "sweet_berry_bush", "cobweb", "hanging_roots",
    "azalea", "flowering_azalea", "spore_blossom", "big_dripleaf", "small_dripleaf", "cave_vines*",
    "leaf_litter", "bush", "firefly_bush", "wildflowers", "cactus_flower", "short_dry_grass", "tall_dry_grass",
    "pale_moss*", "pale_hanging_moss", "pink_petals", "pitcher_plant", "torchflower", "*_coral*", "sea_pickle",
    "water", "lava", "air", "cave_air", "void_air",
]


def is_natural(block: str) -> bool:
    return any(fnmatch.fnmatchcase(block, pat) for pat in NATURAL)


FOOD_POINTS: dict[str, int] = {
    "apple": 4, "golden_apple": 4, "enchanted_golden_apple": 4, "golden_carrot": 6,
    "bread": 5, "cookie": 2, "pumpkin_pie": 8, "melon_slice": 2, "sweet_berries": 2, "glow_berries": 2,
    "carrot": 3, "potato": 1, "baked_potato": 5, "beetroot": 1, "dried_kelp": 1, "chorus_fruit": 4, "honey_bottle": 6,
    "mushroom_stew": 6, "beetroot_soup": 6, "rabbit_stew": 10, "suspicious_stew": 6,
    "beef": 3, "cooked_beef": 8, "porkchop": 3, "cooked_porkchop": 8, "mutton": 2, "cooked_mutton": 6,
    "chicken": 2, "cooked_chicken": 6, "rabbit": 3, "cooked_rabbit": 5,
    "cod": 2, "cooked_cod": 5, "salmon": 2, "cooked_salmon": 6, "tropical_fish": 1,
}


def food_points(inventory) -> int:
    """The food points in an inventory ([{name, count}]): what it would put back on the hunger bar, eaten."""
    return sum(FOOD_POINTS.get(str(i.get("name", "")).removeprefix("minecraft:"), 0) * int(i.get("count", 0))
               for i in inventory or [])
