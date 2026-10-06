"""Render ground-truth snapshots to images: isometric view, top-down heightmap,
timelapse GIF. Pure Pillow, version-independent — it draws from the observer's
sparse {(x,y,z): block} snapshots, so it works on any server version."""
from __future__ import annotations

import colorsys
import hashlib
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from ..world.volume import Snapshot, Volume, XYZ

# top-face colour per block; sides are derived by shading. Unknown blocks get a stable hash colour.
PALETTE: dict[str, tuple[int, int, int]] = {
    "grass_block": (94, 157, 52), "dirt": (134, 96, 67), "stone": (125, 125, 125), "cobblestone": (110, 110, 110),
    "bedrock": (60, 60, 60), "sand": (219, 207, 163), "gravel": (136, 126, 126), "oak_planks": (188, 152, 98),
    "spruce_planks": (114, 84, 48), "oak_log": (109, 85, 50), "oak_leaves": (60, 120, 40), "glass": (200, 230, 240),
    "glass_pane": (200, 230, 240), "oak_door": (150, 110, 60), "water": (63, 118, 228), "lava": (207, 92, 20),
    "torch": (255, 220, 100), "crafting_table": (140, 100, 60), "chest": (160, 120, 60), "rail": (170, 150, 130),
    "powered_rail": (190, 150, 90), "iron_block": (220, 220, 220), "oak_fence": (170, 135, 85),
    "smooth_stone": (160, 160, 160), "stone_bricks": (120, 120, 120), "bricks": (150, 90, 80),
    "obsidian": (30, 20, 50), "diamond_block": (100, 230, 220), "gold_block": (250, 220, 80),
    "white_wool": (235, 235, 235), "red_wool": (180, 50, 50), "blue_wool": (50, 70, 180),
}
TRANSLUCENT = {"glass", "glass_pane", "water"}


def color_for(block: str) -> tuple[int, int, int]:
    if block in PALETTE:
        return PALETTE[block]
    base = block.replace("_stairs", "").replace("_slab", "").replace("_wall", "")
    if base in PALETTE:
        return PALETTE[base]
    h = int(hashlib.md5(block.encode()).hexdigest()[:6], 16) / 0xFFFFFF
    r, g, b = colorsys.hsv_to_rgb(h, 0.45, 0.75)
    return int(r * 255), int(g * 255), int(b * 255)


def _shade(c: tuple[int, int, int], f: float) -> tuple[int, int, int]:
    return tuple(max(0, min(255, int(v * f))) for v in c)  # type: ignore[return-value]


def _font(size: int = 12):
    try:
        return ImageFont.truetype("/System/Library/Fonts/Menlo.ttc", size)
    except Exception:  # noqa: BLE001
        return ImageFont.load_default()


def render_iso(snap: Snapshot, vol: Volume, path: Path, *, bot: XYZ | None = None, floor_y: int | None = None,
               highlight: set[XYZ] | None = None, tile: int = 14, title: str = "", corner: str = "se") -> Path:
    """2:1 isometric projection looking down from one corner ("se" = camera at
    +x,+z; "nw" = camera at -x,-z, i.e. the opposite side). Blocks outside `vol`
    are ignored; a floor is drawn at floor_y so builds sit on something. The
    vertical extent is cropped to what's actually built."""
    (x0, y0, z0), (x1, y1, z1) = vol.min, vol.max
    fy = floor_y if floor_y is not None else y0 - 1
    blocks = {p: b for p, b in snap.items() if vol.contains(p)}
    y1 = min(y1, max([p[1] for p in blocks] + [fy + 1, bot[1] if bot else fy]) + 2)
    if floor_y is not None:
        for x in range(x0, x1 + 1):
            for z in range(z0, z1 + 1):
                blocks.setdefault((x, fy, z), "grass_block")
    w, h = tile, tile // 2          # top diamond: width w, height h
    sh = int(tile * 0.58)           # vertical edge of a block, in px
    flip = corner == "nw"

    def proj(x: int, y: int, z: int) -> tuple[float, float]:
        lx, lz = (x1 - x, z1 - z) if flip else (x - x0, z - z0)
        ly = y - fy
        return (lx - lz) * (w / 2), (lx + lz) * (h / 2) - ly * sh

    sx, sz, sy = x1 - x0 + 1, z1 - z0 + 1, y1 - fy + 1
    width = int((sx + sz) * w / 2) + 2 * tile
    height = int((sx + sz) * h / 2 + sy * sh) + 3 * tile + 20
    ox, oy = sz * w / 2 + tile, sy * sh + tile + 20
    img = Image.new("RGB", (width, height), (24, 26, 30))
    d = ImageDraw.Draw(img, "RGBA")
    if title:
        d.text((8, 4), title, fill=(220, 220, 220), font=_font(12))

    # painter's algorithm: far to near, low to high
    order = (lambda p: (-(p[0] + p[2]), p[1], -p[0])) if flip else (lambda p: (p[0] + p[2], p[1], p[0]))
    for (x, y, z) in sorted(blocks, key=order):
        if y > y1:
            continue
        b = blocks[(x, y, z)]
        c = color_for(b)
        alpha = 140 if b in TRANSLUCENT else 255
        u, v = proj(x, y, z)
        u += ox; v += oy
        top = [(u, v), (u + w / 2, v + h / 2), (u, v + h), (u - w / 2, v + h / 2)]
        left = [(u - w / 2, v + h / 2), (u, v + h), (u, v + h + sh), (u - w / 2, v + h / 2 + sh)]
        right = [(u + w / 2, v + h / 2), (u, v + h), (u, v + h + sh), (u + w / 2, v + h / 2 + sh)]
        d.polygon(left, fill=_shade(c, 0.62) + (alpha,))
        d.polygon(right, fill=_shade(c, 0.8) + (alpha,))
        d.polygon(top, fill=c + (alpha,))
        if highlight and (x, y, z) in highlight:
            d.polygon(top, outline=(255, 240, 80, 110))
    if bot is not None and vol.expand(2).contains(bot):
        bx, by, bz = bot
        u, v = proj(bx, by, bz)
        u += ox; v += oy
        d.ellipse([u - 5, v - 2 * sh + 2, u + 5, v - 2 * sh + 12], fill=(255, 80, 80), outline=(255, 255, 255))
        d.line([(u, v - 2 * sh + 12), (u, v + h / 2)], fill=(255, 80, 80), width=2)
        d.text((u + 7, v - 2 * sh), "bot", fill=(255, 120, 120), font=_font(11))
    img.save(path)
    return path


def render_topdown(snap: Snapshot, vol: Volume, path: Path, *, bot: XYZ | None = None, floor_y: int | None = None,
                   scale: int = 8, title: str = "") -> Path:
    """Top-down map: each column coloured by its highest block, shaded by height."""
    (x0, y0, z0), (x1, y1, z1) = vol.min, vol.max
    fy = floor_y if floor_y is not None else y0 - 1
    top: dict[tuple[int, int], tuple[int, str]] = {}
    for (x, y, z), b in snap.items():
        if vol.contains((x, y, z)) and ((x, z) not in top or y > top[(x, z)][0]):
            top[(x, z)] = (y, b)
    sx, sz = x1 - x0 + 1, z1 - z0 + 1
    img = Image.new("RGB", (sx * scale, sz * scale + 20), (24, 26, 30))
    d = ImageDraw.Draw(img)
    if title:
        d.text((4, 3), title, fill=(220, 220, 220), font=_font(11))
    ymax = max([y for y, _ in top.values()] + [fy + 1])
    for x in range(x0, x1 + 1):
        for z in range(z0, z1 + 1):
            y, b = top.get((x, z), (fy, "grass_block"))
            f = 0.55 + 0.45 * (y - fy) / max(1, ymax - fy)
            c = _shade(color_for(b), f)
            px, pz = (x - x0) * scale, (z - z0) * scale + 20
            d.rectangle([px, pz, px + scale - 1, pz + scale - 1], fill=c)
    if bot is not None and vol.expand(2).contains(bot):
        px, pz = (bot[0] - x0) * scale + scale // 2, (bot[2] - z0) * scale + 20 + scale // 2
        d.ellipse([px - 4, pz - 4, px + 4, pz + 4], fill=(255, 80, 80), outline=(255, 255, 255))
    img.save(path)
    return path


def timelapse(frames: list[Path], path: Path, ms: int = 700) -> Path | None:
    imgs = [Image.open(f).convert("P", palette=Image.ADAPTIVE) for f in frames if f.exists()]
    if not imgs:
        return None
    imgs[0].save(path, save_all=True, append_images=imgs[1:], duration=[ms] * (len(imgs) - 1) + [ms * 3], loop=0)
    return path
