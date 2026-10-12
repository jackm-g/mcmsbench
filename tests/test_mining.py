"""The video-mining helpers (tools/mine): YouTube ids and timestamps, auto-captions deduped to one line each, clip
sections, and the candidate ledgers' shape against the real task ids and grader kinds. No network."""
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _tool(name: str):
    spec = importlib.util.spec_from_file_location(f"mine_{name}", ROOT / "tools" / "mine" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


vtt = _tool("vtt")

AUTO = """WEBVTT
Kind: captions
Language: en

00:00:00.000 --> 00:00:02.270 align:start position:0%
 
so<00:00:00.240><c> today</c><00:00:00.480><c> the</c><00:00:00.719><c> plan</c><00:00:01.040><c> is</c>

00:00:02.270 --> 00:00:02.280 align:start position:0%
so today the plan is
 

00:00:02.280 --> 00:00:04.230 align:start position:0%
so today the plan is
to<00:00:02.520><c> get</c><00:00:02.760><c> full</c><00:00:03.000><c> iron</c>

00:00:04.230 --> 00:00:04.240 align:start position:0%
to get full iron
 

00:00:04.240 --> 00:00:06.000 align:start position:0%
to get full iron
before<00:00:04.480><c> the</c><00:00:04.720><c> nether</c>&nbsp;&amp;<00:00:05.000><c> the</c><00:00:05.200><c> end</c>

00:00:06.000 --> 00:00:06.010 align:start position:0%
before the nether & the end
 
"""

MANUAL = """WEBVTT

1
00:00:01.000 --> 00:00:03.000
So today the plan
is to get full iron.

2
00:00:03.500 --> 00:00:05.000
Before the Nether.
"""


def test_rolling_auto_captions_are_deduped():
    assert vtt.parse_vtt(AUTO) == [
        (0.0, "so today the plan is"),
        (2.28, "to get full iron"),
        (4.24, "before the nether & the end"),
    ]


def test_manual_captions_pass_through():
    assert vtt.parse_vtt(MANUAL) == [(1.0, "So today the plan is to get full iron."), (3.5, "Before the Nether.")]


def test_hms_and_parse_hms_round_trip():
    assert vtt.hms(65) == "01:05"
    assert vtt.hms(3725) == "1:02:05"
    assert vtt.parse_hms("754") == 754.0
    assert vtt.parse_hms("12:34") == 754.0
    assert vtt.parse_hms("1:02:05") == 3725.0
    assert vtt.fname(3725) == "1-02-05"
    with pytest.raises(ValueError):
        vtt.parse_hms("twelve")


def test_video_id_from_the_url_shapes():
    for s in ["https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=10s", "https://youtu.be/dQw4w9WgXcQ?si=x",
              "https://www.youtube.com/shorts/dQw4w9WgXcQ", "https://www.youtube.com/live/dQw4w9WgXcQ", "dQw4w9WgXcQ"]:
        assert vtt.video_id(s) == "dQw4w9WgXcQ"
    with pytest.raises(ValueError):
        vtt.video_id("https://example.com/not-a-video")


def test_nearby_timestamps_share_one_section():
    assert vtt.sections([754, 790, 1075], pad=2, gap=60) == [(752, 792), (1073, 1077)]
    assert vtt.sections([1], pad=2) == [(0, 3)]


def test_merge_windows():
    lines = [(0, "a"), (5, "b"), (14, "c"), (16, "d"), (40, "e")]
    assert vtt.merge(lines, 15) == [(0, "a b c"), (16, "d"), (40, "e")]
    assert vtt.merge(lines, 0) == lines


# ------------------------------------------------------------------ the ledgers

ledger = _tool("ledger")

GOOD = {
    "video": "dQw4w9WgXcQ", "title": "t", "channel": "c", "duration": 1834, "watched": "2026-10-11",
    "coverage": "whole transcript",
    "candidates": [{
        "id": "fuel_the_furnace_from_a_tree", "status": "candidate",
        "source": {"from": "09:45", "to": "14:02"}, "did": "smelt 16 iron with no coal", "why": "fuel planning",
        "world": "flat", "setup": "a furnace, no coal",
        "grader": {"required": ["smelted"], "steps": [
            {"name": "smelted", "weight": 3, "check": {"kind": "stat", "name": "crafted:iron_ingot", "min": 16}},
            {"name": "alive", "weight": 1, "hold": True, "check": {"kind": "survived"}}]},
        "overlaps": ["smelt_iron"], "tier": "tier-c",
    }],
}
IDS = {"smelt_iron", "gather_wood"}
KINDS = {"stat", "survived", "milestones"}


def _bad(**changes):
    import copy
    l = copy.deepcopy(GOOD)
    l["candidates"][0].update(changes)
    return ledger.validate(l, IDS, KINDS)


def test_a_ledger_must_be_well_formed():
    assert ledger.validate(GOOD, IDS, KINDS) == []
    assert any("status" in m for m in _bad(status="maybe"))
    assert any("overlaps 'house_small'" in m for m in _bad(overlaps=["house_small"]))
    assert any("check kind 'vibes'" in m for m in _bad(grader={"steps": [{"name": "x", "check": {"kind": "vibes"}}]}))
    assert any("not before" in m for m in _bad(source={"from": "14:02", "to": "09:45"}))
    assert any("past the video" in m for m in _bad(source={"from": "14:02", "to": "40:00"}))
    assert any("collides" in m for m in _bad(id="smelt_iron"))
    assert ledger.validate(dict(GOOD), IDS, KINDS, seen={"fuel_the_furnace_from_a_tree"}) \
        == ["fuel_the_furnace_from_a_tree: id repeats across the ledgers"]
    assert any("names no task" in m for m in _bad(status="drafted"))
    assert any("not a task slug" in m for m in _bad(id="Fuel-It"))


def test_every_committed_ledger_is_well_formed():
    from mcmsbench.graders import REGISTRY
    from mcmsbench.tasks import load_all
    ids = set(load_all())
    seen: set[str] = set()
    bad = []
    for p, l in ledger.load_ledgers(ROOT / "mining"):
        bad += [f"{p.name}: {m}" for m in ledger.validate(l, ids, set(REGISTRY), seen, ROOT / "tasks")]
    assert not bad, bad


def test_the_task_digest_names_every_task():
    lines = ledger.digest(ROOT / "tasks")
    assert len(lines) == len(list((ROOT / "tasks").glob("*.yaml")))
    assert any(l.startswith("beacon ") and "tier-d" in l for l in lines)
