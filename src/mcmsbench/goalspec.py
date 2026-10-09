"""The check a goal file carries: the task's grader, rewritten as parts an agent can score on its own.

The bench grades from the server and never asks the agent. But an agent that knows when it is done can stop, and one
that does not plays on to the clock; so the goal file hands it the grader's terms, part by part. `from_grader` reads
them off a grader (a single inventory/placed/blocks grader maps to the same shape; anything else becomes a composite of
its required milestones). A part the agent can read live has a `check`; a part it cannot (a server counter, a test the
grader runs on the finished world) is listed under `uncovered`, with what it says.

The shapes (all positions absolute):

  {"kind": "inventory", "item": "*_log" | [...], "count": N}       (or "max": N)
  {"kind": "placed", "block": ..., "min": N}                       blocks this agent placed this trial
  {"kind": "have_or_placed", "item": ..., "count": N}
  {"kind": "blocks", "block": ..., "min": N, "radius": R, "bounds": [[x0,y0,z0],[x1,y1,z1]]}
  {"kind": "position", "x", "y", "z", "tolerance", "y_tolerance", "exact": true}
  {"kind": "rail_path", "from": [x,y,z], "to": [x,y,z], "powered_every": N}
  {"kind": "spawn", "center": [x, z], "min_from_spawn"?, "max_from_spawn"?, "inside"?}
  {"kind": "time", "between": [t0, t1]}                            day time, wrapping past 24000
  {"kind": "alive", "max_deaths": N, "min_health"?}
  {"kind": "hydrated", "crop": ..., "min": N}
  {"kind": "food", "min": N}  /  {"kind": "food_stock", "min_points": N}
  {"kind": "grader", "spec": {...}, "plot": [[...],[...]], "floor_y", "center"}   the grader's own spec, to run live
  {"kind": "all", "parts": [{"name", "role", "check", "proxy", "terminal"}], "uncovered": [...], "from": "grader"}

`check_agreement` sets an agent's last word on each part (its trace's `check.parts[].outcome`) beside the grader's.
"""
from __future__ import annotations

import contextlib

PASS, FAIL, UNKNOWN = "pass", "fail", "unknown"


def _glob(v):
    """An item or block pattern as it is stored: a string, or a list of strings for aliases (wood: any log)."""
    if isinstance(v, (list, tuple)):
        out = [str(x) for x in v if str(x).strip()]
        return out[0] if len(out) == 1 else out
    return str(v)



def from_grader(grader: dict | None, plot=None) -> dict | None:
    """The task-level check an eval grader implies, or None. A single inventory, placed or blocks grader maps to the
    same shape. With the trial's `plot`, any other grader (a milestones ladder, a structure, a position) becomes a
    composite: the required (and `must`) milestones an agent can read live, each a part, and the ones it cannot
    ("uncovered")."""
    if not isinstance(grader, dict):
        return None
    k = grader.get("kind")
    if plot is not None and k not in ("inventory", "placed", "blocks"):
        return composite_from_grader(grader, plot)
    try:
        if k == "inventory" and grader.get("item") and "count" in grader:
            return {"kind": "inventory", "item": _glob(grader["item"]), "count": int(grader["count"]), "from": "grader"}
        if k == "placed" and grader.get("block"):
            return {"kind": "placed", "block": _glob(grader["block"]), "min": int(grader.get("count", 1)), "from": "grader"}
        if k == "blocks" and grader.get("block"):
            return {"kind": "blocks", "block": _glob(grader["block"]), "min": int(grader.get("min", 1)), "radius": 16, "from": "grader"}
    except (TypeError, ValueError):
        return None
    return None



UNCOVERED = {
    "stat": "a server counter ({name})",
    "functional": "a test the grader runs on the finished world with server commands",
    "rooms": "the rooms on each floor, read from the finished house: {rooms} (floor: rooms), each joined to the next by a "
             "door in the wall between them",
    "windows": "glass windows in each floor's outer walls, read from the finished house: {per_story} (floor: windows)",
    "event": "a moment the bench watched for during the trial",
    "entity_inside": "where the {entity} stands, from the server",
    "dug": "blocks dug ({block}), from the whole trial's world diff",
    "guard": "the live guard's refusals over the trial",
    "stayed": "the whole walk staying within {radius} blocks",
    "liquid_path": "a liquid path the grader traces in the final world",
    "herd": "the livestock the server counts in the plot at the end",
    "equipped": "the armour worn and items held, from the server (worn, not carried)",
    "intact": "the house built by {at} left standing: at most {max_broken} of its blocks broken from then on",
    "intact:setup": "the base the task started you in left as it was: none of its blocks broken or changed (crops "
                    "harvested, farmland trampled), from the whole trial's break log and the finished world",
    "walkable": "a way on foot between two places in the finished world (doors and gates open)",
    "container": "the container at {at} holding what the task asks for, read from the server at the end",
    "block_state": "the block at {at} in the finished world, in the state {state}",
    "day": "the trial lasting into day {min} (day 1 is the one it starts in; each dawn begins the next)",
    "food_stock": "food worth {min_points} points, carried or in the chests and barrels you placed",
    "exit_route": "the bot's walk out of the finished house: up to the top story, then out on foot (down the stairs, "
                  "out the door) without opening the upper walls, from the trial's frames and position track",
}
LIVE_GRADERS = ("structure", "stories", "furnishings", "level_site", "exact", "schematic", "story_walls")   # the grader's own code, live
# uncovered, but carrying the grader's spec on the plot (as a `grader` check): a house's rooms and windows, read from
# the finished world, and the walk out of it, from the trial's frames and track, for an agent with its own copy
OWN_COPY = ("rooms", "windows", "exit_route")
GRADER_SCAN_CELLS = 60_000         # a live scan bigger than this is not made: the part is unknown
BLOCKS_SCAN_REACH = 24             # blocks around the site a live `blocks` count reaches


def composite_from_grader(grader: dict, plot) -> dict:
    """{'kind': 'all', 'parts': [{'name', 'role', 'check', 'proxy', 'terminal'}], 'uncovered': [{'name', 'kind',
    'says', 'check'?}], 'from': 'grader'} from a grader and the trial's plot (positions resolved to absolute coordinates).
    A `stat` part stays uncovered (an agent cannot read the server's counter) but carries its spec as `check`; so does
    a `rooms`, `windows` or `exit_route` part, as a `grader` check on the plot."""
    if grader.get("kind") == "milestones":
        req = grader.get("required")
        required = [req] if isinstance(req, str) else list(req or [])
        steps = list(grader.get("steps") or [])
        chosen = [m for m in steps if (m.get("name") in required if required else True) or m.get("must")]
    else:
        required = [str(grader.get("kind"))]
        chosen = [{"name": str(grader.get("kind")), "check": grader}]
    parts, uncovered = [], []
    for m in chosen:
        spec = dict(m.get("check") or {})
        name = str(m.get("name"))
        live = _live_part(spec, plot)
        if live is None:
            key = "intact:setup" if spec.get("kind") == "intact" and spec.get("of") == "setup" else str(spec.get("kind"))
            says = UNCOVERED.get(key, "a check an agent cannot read live")
            with contextlib.suppress(KeyError, IndexError, ValueError):
                says = says.format(**spec)
            says = str(m.get("says") or says)       # the task's own words, where the kind's are too general
            u = {"name": name, "kind": spec.get("kind"), "says": says}
            if spec.get("kind") == "stat":
                # the counter and what it must reach, for an agent that keeps its own count (of its kills, say):
                # "a server counter (killed:zombie)" alone did not say how many
                u["check"] = {k: spec[k] for k in ("kind", "name", "min", "max", "op", "value") if k in spec}
            elif spec.get("kind") in OWN_COPY:
                # the grader's spec on this plot, for an agent that keeps its own copy of the grader: "two rooms on
                # each floor" alone did not say how small a room may be, or which floors
                c, lo, hi, floor_y = _plot_frame(plot)
                u["check"] = {"kind": "grader", "spec": spec, "plot": [list(lo), list(hi)], "floor_y": floor_y,
                              "center": list(c)}
            uncovered.append(u)
            continue
        role = "required" if (not required or name in required) else "must"
        parts.append({"name": name, "role": role, "check": live, "proxy": live.get("kind") == "hydrated",
                      "terminal": live.get("kind") == "alive"})
    return {"kind": "all", "parts": parts, "uncovered": uncovered, "from": "grader"}


def _plot_frame(plot):
    """(centre, volume min, volume max, floor_y) of a plot, as plain tuples."""
    c = tuple(int(v) for v in plot.center())
    lo, hi = tuple(plot.volume.min), tuple(plot.volume.max)
    floor_y = plot.floor_y if getattr(plot, "flat", False) else None
    return c, lo, hi, floor_y


def _live_part(spec: dict, plot) -> dict | None:
    """One milestone's check as a live check, or None when an agent cannot read it live."""
    k = spec.get("kind")
    c, lo, hi, floor_y = _plot_frame(plot)
    if k == "inventory" and spec.get("item"):
        out = {"kind": "inventory", "item": _glob(spec["item"])}
        if "max" in spec and "count" not in spec:
            out["max"] = int(spec["max"])
        else:
            out["count"] = int(spec.get("count", 1))
        return out
    if k == "placed" and spec.get("block"):
        return {"kind": "placed", "block": _glob(spec["block"]), "min": int(spec.get("count", 1))}
    if k == "have_or_placed" and spec.get("item"):
        return {"kind": "have_or_placed", "item": _glob(spec["item"]), "count": int(spec.get("count", 1))}
    if k == "blocks" and spec.get("block"):
        return {"kind": "blocks", "block": _glob(spec["block"]), "min": int(spec.get("min", 1)),
                "radius": BLOCKS_SCAN_REACH, "bounds": [list(lo), list(hi)]}
    if k == "position":
        if "target_abs" in spec:
            t = spec["target_abs"]
            tx, ty, tz = c if t == "spawn" else tuple(t)
        elif "target_rel" in spec:
            rel = spec["target_rel"]
            dx, dy, dz = (rel[0], 0, rel[1]) if len(rel) == 2 else rel
            tx, ty, tz = c[0] + dx, c[1] + dy, c[2] + dz
        elif "target" in spec:
            t = spec["target"]
            tx, ty, tz = lo[0] + t[0], lo[1] + t[1], lo[2] + t[2]
        else:
            return None
        return {"kind": "position", "x": int(tx), "z": int(tz), "y": int(ty), "tolerance": float(spec.get("tolerance", 1.0)),
                "y_tolerance": float(spec.get("y_tolerance", 1.0)), "exact": True}
    if k == "rail_path" and spec.get("from") is not None and spec.get("to") is not None:
        a = spec.get("from_abs") or [lo[i] + spec["from"][i] for i in range(3)]
        b = spec.get("to_abs") or [lo[i] + spec["to"][i] for i in range(3)]
        return {"kind": "rail_path", "from": [int(v) for v in a], "to": [int(v) for v in b],
                "powered_every": int(spec.get("powered_every", 8))}
    if k == "respawn":
        out = {"kind": "spawn", "center": [c[0], c[2]]}
        for key in ("min_from_spawn", "max_from_spawn"):
            if key in spec:
                out[key] = float(spec[key])
        if spec.get("inside"):
            out["inside"] = True          # the bed in a closed room, not one outside the house
        return out
    if k == "world_time":
        return {"kind": "time", "between": [int(v) for v in spec.get("between", [0, 12000])]}
    if k == "survived":
        out = {"kind": "alive", "max_deaths": int(spec.get("max_deaths", 0))}
        if "min_health" in spec:
            out["min_health"] = float(spec["min_health"])
        return out
    if k == "hydrated":
        return {"kind": "hydrated", "crop": str(spec.get("crop", "wheat")), "min": int(spec.get("min", 1))}
    if k == "food":
        return {"kind": "food", "min": int(spec.get("min", 20))}
    if k == "food_stock" and "min_points" in spec and not spec.get("in_containers"):   # a chest is not read live
        return {"kind": "food_stock", "min_points": int(spec["min_points"])}
    if k in LIVE_GRADERS and not spec.get("with_setup"):     # a structure setup provides is not in the bot's record
        return {"kind": "grader", "spec": spec, "plot": [list(lo), list(hi)], "floor_y": floor_y, "center": list(c)}
    return None


def check_agreement(final: dict, result: dict, grader: dict | None) -> dict:
    """{part: {'live', 'grader', 'agree'}} for a composite's required parts: what the live check said at the end
    (pass/fail; unknown agrees with nothing) beside what the grader decided. Pure."""
    detail = (result or {}).get("detail") or {}
    holds = detail.get("holds_at_end") or {}
    out: dict = {}
    for p in final.get("parts") or []:
        if p.get("role") != "required":
            continue
        name = p["name"]
        truth = holds.get(name) if (grader or {}).get("kind") == "milestones" else (result or {}).get("passed")
        live = {PASS: True, FAIL: False}.get(p.get("outcome"))
        out[name] = {"live": p.get("outcome"), "grader": truth,
                     "agree": None if live is None or truth is None else live == bool(truth)}
    return out
