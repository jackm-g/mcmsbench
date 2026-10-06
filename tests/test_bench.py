"""The bench's own seams: no agent imported, the goal file, task versions, capability caveats, slots."""
import ast
import sys
from pathlib import Path

from mcmsbench import config
from mcmsbench.cli import offline_plot
from mcmsbench.protocol import Agent, Manifest
from mcmsbench.runner import build_goal, caveats_for, live_gradable, task_hash
from mcmsbench.tasks import load, load_all

SRC = Path(__file__).resolve().parents[1] / "src" / "mcmsbench"


def test_the_bench_imports_no_agent():
    """Nothing under src/ imports an agent's code: an agent is a process the bench starts, never a module."""
    allowed = set(sys.stdlib_module_names) | {"__future__", "mcmsbench", "dotenv", "yaml", "PIL", "anthropic"}
    bad = []
    for f in SRC.rglob("*.py"):
        for node in ast.walk(ast.parse(f.read_text())):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                [node.module or ""] if isinstance(node, ast.ImportFrom) and not node.level else []
            bad += [f"{f.relative_to(SRC)}: {n}" for n in names if n.split(".")[0] not in allowed]
    assert not bad, bad                                   # a new dependency belongs in pyproject.toml and here


def test_no_task_carries_agent_notes():
    """`notes` were hints for one agent's API; the tasks are the same for every agent."""
    for tid, t in load_all().items():
        assert not hasattr(t, "notes"), tid
        assert "## Notes" not in t.prompt, tid


def goal_for(tid: str, trial: int = 0) -> dict:
    s = config.load()
    t = load(tid)
    plot, start = offline_plot(t, s, trial)
    return build_goal(t, plot, start, s, trial, seed=t.seed_for(trial) if t.world.type == "survival" else None,
                      fast_nights=bool(t.fast_nights and t.world.daylight), grader_stops=live_gradable(t.grader),
                      max_seconds=float(t.max_seconds or 600), max_cost=float(t.max_cost_usd or 5))


def test_the_goal_file():
    g = goal_for("shelter_in_desert")
    assert g["protocol"] == 1 and g["task_id"] == "shelter_in_desert" and g["trial"] == 0
    assert g["prompt"].startswith("Build a shelter here")
    assert g["check"]["kind"] == "all" and {p["name"] for p in g["check"]["parts"]} == {"alive", "morning"}
    assert g["movements"]["can_dig"] is True
    assert g["profile"]["fastNights"] is True and g["profile"]["graderStops"] is True
    assert g["profile"]["border"]["radius"] == 48 and g["world"]["seed"] == 1 and g["world"]["type"] == "survival"
    assert g["budget"] == {"max_seconds": 900.0, "max_cost_usd": 5.0}
    assert len(g["start"]) == 3 and g["plot"]["min"] < g["plot"]["max"]


def test_the_goal_carries_what_an_agent_harness_may_use():
    g = goal_for("make_safe")
    assert g["options"]["guard"] is True and g["options"]["xray"] is True
    g = goal_for("provision_at_base")
    assert g["options"]["journal"] is True and g["options"]["own"] and g["profile"]["spawnProtection"]["radius"] == 16
    assert g["world"]["keep_inventory"] is False


def test_a_task_that_tests_the_harness_has_no_check():
    t = load("bed_first")
    assert t.check is False and goal_for("bed_first")["check"] is None


def test_task_hash_follows_the_profile(monkeypatch, tmp_path):
    a = task_hash("farm_in_snow")
    assert a == task_hash("farm_in_snow") and len(a) == 16
    prof = tmp_path / "profiles"
    prof.mkdir()
    (prof / "prod.yaml").write_text((config.PROFILE_DIR / "prod.yaml").read_text() + "\nstats: [deaths]\n")
    import mcmsbench.tasks as T
    monkeypatch.setattr(T, "PROFILE_DIR", prof)
    assert task_hash("farm_in_snow") != a


def test_a_missing_capability_is_a_caveat():
    plain = Agent(Manifest("plain", ["true"], capabilities=["fast_nights"]))
    guarded = Agent(Manifest("guarded", ["true"], capabilities=["guard", "fast_nights"]))
    t = load("make_safe")
    assert t.requires == ["guard"]
    assert caveats_for(t, guarded) == []
    (cv,) = caveats_for(t, plain)
    assert cv["missing_capabilities"] == ["guard"] and "not comparable" in cv["says"]
    assert caveats_for(load("gather_wood"), plain) == []


def test_slots_overlay_the_survival_arena(tmp_path):
    p = tmp_path / "m.toml"
    p.write_text('[survival]\nport = 1\n[slots.2.survival]\nport = 2\ncompose_service = "s2"\n')
    assert config.load(p).survival.port == 1
    s = config.load(p, slot="2")
    assert (s.survival.port, s.survival.compose_service, s.slot) == (2, "s2", "2")
