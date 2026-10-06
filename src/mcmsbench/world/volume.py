"""Block volumes, snapshots and diffs — the grading primitive.

A Snapshot is sparse: {(x, y, z): block_name} for non-air blocks only.
Ground truth snapshots come from the observer bot, never from the agent.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator

XYZ = tuple[int, int, int]
Snapshot = dict[XYZ, str]


@dataclass(frozen=True)
class Volume:
    min: XYZ
    max: XYZ  # inclusive

    def __post_init__(self):
        if any(a > b for a, b in zip(self.min, self.max)):
            raise ValueError(f"degenerate volume {self.min}..{self.max}")

    def contains(self, p: XYZ) -> bool:
        return all(lo <= v <= hi for lo, v, hi in zip(self.min, p, self.max))

    def __iter__(self) -> Iterator[XYZ]:
        (x0, y0, z0), (x1, y1, z1) = self.min, self.max
        for x in range(x0, x1 + 1):
            for y in range(y0, y1 + 1):
                for z in range(z0, z1 + 1):
                    yield (x, y, z)

    @property
    def size(self) -> XYZ:
        return tuple(b - a + 1 for a, b in zip(self.min, self.max))  # type: ignore[return-value]

    @property
    def count(self) -> int:
        sx, sy, sz = self.size
        return sx * sy * sz

    def expand(self, n: int) -> "Volume":
        return Volume(tuple(v - n for v in self.min), tuple(v + n for v in self.max))  # type: ignore[arg-type]

    def to_dict(self) -> dict:
        return {"min": list(self.min), "max": list(self.max)}

    @classmethod
    def from_dict(cls, d: dict) -> "Volume":
        return cls(tuple(d["min"]), tuple(d["max"]))  # type: ignore[arg-type]


@dataclass
class BlockDiff:
    placed: Snapshot = field(default_factory=dict)             # air -> block
    broken: Snapshot = field(default_factory=dict)             # block -> air
    replaced: dict[XYZ, tuple[str, str]] = field(default_factory=dict)  # block -> other block

    @property
    def changed(self) -> set[XYZ]:
        return set(self.placed) | set(self.broken) | set(self.replaced)

    def summary(self) -> dict:
        return {"placed": len(self.placed), "broken": len(self.broken), "replaced": len(self.replaced)}


def diff(before: Snapshot, after: Snapshot) -> BlockDiff:
    d = BlockDiff()
    for p, name in after.items():
        old = before.get(p)
        if old is None:
            d.placed[p] = name
        elif old != name:
            d.replaced[p] = (old, name)
    for p, name in before.items():
        if p not in after:
            d.broken[p] = name
    return d


def snapshot_to_json(s: Snapshot) -> list[list]:
    return [[x, y, z, n] for (x, y, z), n in sorted(s.items())]


def snapshot_from_json(rows: list[list]) -> Snapshot:
    return {(x, y, z): n for x, y, z, n in rows}
