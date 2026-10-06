"""One cheap LLM pass per agent trace: what strategy did it use? Cached next to the
trial as trial_N.strategy.json so the comparison page can show it without re-asking."""
from __future__ import annotations

import json
import re
from pathlib import Path

PROMPT = """You are analysing one trial of an AI agent playing Minecraft. Agents differ in how they act (some write code,
some pick from menus, some plan in tiers); describe its STRATEGY so that trials from different agents can be compared. Be concrete and neutral.

Return only JSON: {{"summary": "<2 sentences: what approach it took and how it went>",
"tags": ["<3-6 short lowercase tags, e.g. surveyed-first, single-batch-build, verified-with-scan, crafted-tools-early, recovered-from-error, gave-up, over-verified, wasted-turns>"],
"notable": ["<0-3 specific behaviours worth a human's attention: clever moves, mistakes, inefficiencies>"]}}

Task: {task}
Outcome: graded {verdict} (score {score}); the agent claimed: {claim}. {turns} turns, {seconds}s.
Grader checks: {checks}

Steps as the agent reported them (its note, what it did, then what came back):
{steps}
"""


def render_steps(steps: list[dict], budget: int = 9000) -> str:
    out = []
    per = max(300, budget // max(1, len(steps)))
    for s in steps:
        code = str(s.get("code") or "").strip()
        back = (s.get("error") or s.get("stdout") or "").strip().replace("\n", " | ")
        note = (s.get("text") or "").strip()
        out.append(f"[{s.get('turn')}] {note[:160]}\n  code: {code[:per]}\n  -> {back[:per // 2]}")
    return "\n".join(out)


def summarize_trial(path: Path, client, model: str, force: bool = False) -> dict | None:
    target = path.with_suffix(".strategy.json")
    if target.exists() and not force:
        return json.loads(target.read_text())
    d = json.loads(path.read_text())
    tr = d.get("trace") or {}
    steps = tr.get("steps") or []
    if not steps:
        return None  # scripted runs have no strategy to describe
    res = d.get("result") or {}
    done = tr.get("done") or {}
    prompt = PROMPT.format(task=(tr.get("prompt") or d["task"])[:1200],
                           verdict="PASS" if res.get("passed") else "FAIL", score=res.get("score"),
                           claim=("success" if done.get("success") else "failure") if done else "nothing (ran out of budget)",
                           turns=tr.get("turns"), seconds=d.get("seconds"), checks=res.get("checks"),
                           steps=render_steps(steps))
    resp = client.messages.create(model=model, max_tokens=700, messages=[{"role": "user", "content": prompt}])
    text = "".join(b.text for b in resp.content if b.type == "text")
    m = re.search(r"\{.*\}", text, re.S)
    try:
        out = json.loads(m.group(0)) if m else {}
    except json.JSONDecodeError:
        out = {}
    out = {"summary": str(out.get("summary", "")).strip() or text.strip()[:300],
           "tags": [str(t) for t in out.get("tags", [])][:6], "notable": [str(n) for n in out.get("notable", [])][:3],
           "model": model}
    target.write_text(json.dumps(out, indent=1))
    return out


def summarize_runs(paths: list[Path], api_key: str, model: str = "claude-haiku-4-5", force: bool = False, log=print) -> int:
    import anthropic
    client = anthropic.Anthropic(api_key=api_key or None)
    n = 0
    for root in paths:
        for f in sorted(Path(root).rglob("trial_*.json")):
            if f.name.endswith((".strategy.json", "_goal.json", "_agent.json", ".built.json", "_memory.json")):
                continue
            try:
                had = f.with_suffix(".strategy.json").exists()
                r = summarize_trial(f, client, model, force)
                if r and (force or not had):
                    n += 1
                    log(f"  strategy: {f.parent.name}/{f.name}: {r['summary'][:100]}")
            except Exception as e:  # noqa: BLE001
                log(f"  (strategy failed for {f}: {e})")
    return n
