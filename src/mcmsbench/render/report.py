"""Per-trial HTML report and per-run index: what the model wrote, what happened,
what the world looked like after each step."""
from __future__ import annotations

import html
import json
import re
from pathlib import Path

CSS = """body{font:14px/1.45 -apple-system,Helvetica,sans-serif;background:#16181c;color:#ddd;margin:0;padding:20px}
a{color:#7cc4ff} h1,h2{font-weight:600} .pass{color:#6fdc8c} .fail{color:#ff7b7b} .meta{color:#999}
pre{background:#0e0f12;border:1px solid #2a2d33;border-radius:6px;padding:10px;overflow:auto;max-height:420px;white-space:pre-wrap}
.step{display:grid;grid-template-columns:1fr 1fr;gap:16px;border-top:1px solid #2a2d33;padding:16px 0}
.step img{max-width:100%;border-radius:6px;background:#000} .final{display:flex;gap:16px;flex-wrap:wrap}
.final img{max-width:48%;border-radius:6px} table{border-collapse:collapse} td,th{padding:6px 12px;border-bottom:1px solid #2a2d33;text-align:left}
.thumb{height:90px;border-radius:4px}
.bars{display:flex;align-items:flex-end;gap:3px;height:60px;margin:6px 0} .bars div{width:14px;background:#4c8dd6;border-radius:2px 2px 0 0}
.bars div.b{background:#d66} .ms td.hit{color:#6fdc8c} .ms td.miss{color:#ff7b7b}"""


def _progress(trace: dict, res: dict) -> str:
    out = []
    prog = trace.get("progress") or []
    if prog:
        mx = max([p["placed"] + p["broken"] for p in prog] + [1])
        out.append("<h2>Progress</h2><p class=meta>blocks placed (blue) / broken (red) after each step</p><div class=bars>")
        for p in prog:
            out.append(f"<div title='{_e(p['label'])}: placed {p['placed']} broken {p['broken']} items {p['items']}' "
                       f"style='height:{max(2, 60 * p['placed'] / mx):.0f}px'></div>"
                       f"<div class=b title='broken {p['broken']}' style='height:{max(2, 60 * p['broken'] / mx):.0f}px'></div>")
        out.append("</div>")
    det = (res or {}).get("detail") or {}
    if "reached_at_step" in det:
        out.append("<h2>Milestones</h2><table class=ms><tr><th>milestone</th><th>first reached</th><th>at</th><th>holds at end</th></tr>")
        secs = det.get("reached_at_seconds", {})
        for name, step in det["reached_at_step"].items():
            end = det.get("holds_at_end", {}).get(name)
            t = secs.get(name)
            out.append(f"<tr><td>{_e(name)}</td><td class={'hit' if step else 'miss'}>{'step ' + str(step) if step else 'never'}</td>"
                       f"<td>{f'{t:.0f}s' if t is not None else '—'}</td>"
                       f"<td class={'hit' if end else 'miss'}>{'yes' if end else 'no'}</td></tr>")
        out.append("</table>")
    return "\n".join(out)


def _e(x) -> str:
    return html.escape(str(x))


def trial_report(rec: dict, frames: dict[str, str], out: Path) -> Path:
    """rec: TrialRecord.to_dict(); frames: {label: relative image path}."""
    res = rec.get("result") or {}
    verdict = ("PASS" if res.get("passed") else "FAIL") if res else "ungraded"
    cls = "pass" if res.get("passed") else "fail"
    trace = rec.get("trace") or {}
    done = trace.get("done") or {}
    parts = [f"<!doctype html><meta charset=utf-8><title>{_e(rec['task'])} trial {rec['trial']}</title><style>{CSS}</style>",
             f"<h1>{_e(rec['task'])} — trial {rec['trial']} <span class={cls}>{verdict}</span></h1>",
             f"<p class=meta>agent={_e(rec['mode'])} · {rec['seconds']}s · turns={trace.get('turns', 0)} · "
             f"${trace.get('cost_usd', 0):.4f} · diff={_e(rec.get('diff'))} · stop={_e(trace.get('stop'))}</p>"]
    if res:
        parts.append(f"<p>score <b>{res['score']}</b> · checks: " + " ".join(
            f"<span class={'pass' if v else 'fail'}>{_e(k)}</span>" for k, v in res.get("checks", {}).items()) + "</p>")
        parts.append(f"<details><summary>grader detail</summary><pre>{_e(json.dumps(res.get('detail'), indent=1))}</pre></details>")
    if rec.get("final_stats") or rec.get("final_health") is not None:
        parts.append(f"<p class=meta>start={_e(rec.get('start'))} · stats={_e(rec.get('final_stats'))} · "
                     f"health={_e(rec.get('final_health'))} · time of day={_e(rec.get('final_time'))}</p>")
    if rec.get("error"):
        parts.append(f"<p class=fail>error: {_e(rec['error'])}</p>")
    for cv in rec.get("caveats") or []:
        parts.append(f"<p class=fail>caveat: {_e(cv.get('says'))}</p>")
    if done:
        parts.append(f"<p><b>Agent claimed:</b> {'success' if done.get('success') else 'not successful'} — {_e(done.get('summary'))}</p>")
    parts.append(_progress(trace, res))
    parts.append("<h2>Result</h2><div class=final>")
    for lbl in ("final_se", "final_nw", "final_top", "timelapse"):
        if lbl in frames:
            parts.append(f"<figure><img src='{frames[lbl]}'><figcaption class=meta>{lbl}</figcaption></figure>")
    parts.append("</div>")
    steps = [st for st in trace.get("steps") or [] if isinstance(st, dict)]
    if steps:
        parts.append("<h2>Steps</h2><p class=meta>as the agent reported them</p>")
        for i, st in enumerate(steps, 1):
            t = st.get("turn") if isinstance(st.get("turn"), int) else i
            img = frames.get(f"step_{t:02d}")
            parts.append("<div class=step><div>")
            parts.append(f"<b>step {t}</b> <span class=meta>{_e(st.get('seconds', ''))}s</span>")
            text = st.get("text") or st.get("key") or st.get("goal")
            if text:
                parts.append(f"<p><i>{_e(text)}</i></p>")
            body = st.get("code")
            if body is None:
                body = json.dumps({k: v for k, v in st.items() if k not in ("turn", "seconds", "text")}, default=str)[:2000]
            parts.append(f"<pre>{_e(body)}</pre>")
            if st.get("stdout"):
                parts.append(f"<pre>{_e(st['stdout'])}</pre>")
            if st.get("error"):
                parts.append(f"<pre class=fail>{_e(st['error'])}</pre>")
            parts.append(f"<p class=meta>{_e(st.get('observation', ''))}</p></div><div>")
            if img:
                parts.append(f"<img src='{img}'>")
            parts.append("</div></div>")
    timed = [(lbl, path) for lbl, path in frames.items() if re.fullmatch(r"t\d{4,}", lbl)]
    if timed:
        parts.append("<h2>Frames</h2><p class=meta>taken by the bench every 30 s, whatever the agent reported</p><div class=final>")
        for lbl, path in timed:
            parts.append(f"<figure><img src='{path}'><figcaption class=meta>{_e(lbl[1:].lstrip('0') or '0')} s</figcaption></figure>")
        parts.append("</div>")
    bench = rec.get("bench") or {}
    if bench:
        parts.append(f"<p class=meta>bench {_e(bench.get('sha') or '?')} · protocol {_e(bench.get('protocol'))} · "
                     f"task {_e(bench.get('task_hash'))} · agent {_e(bench.get('agent'))} ({_e(bench.get('model'))})</p>")
    out.write_text("\n".join(parts))
    return out


def run_index(run_dir: Path, rows: list[dict], summary: dict) -> Path:
    parts = [f"<!doctype html><meta charset=utf-8><title>{_e(run_dir.name)}</title><style>{CSS}</style>",
             f"<h1>{_e(run_dir.name)}</h1>", "<table><tr><th>task</th><th>trial</th><th>verdict</th><th>score</th>"
             "<th>turns</th><th>seconds</th><th>cost</th><th>result</th></tr>"]
    for r in rows:
        res = r.get("result") or {}
        v = ("PASS" if res.get("passed") else "FAIL") if res else "—"
        cls = "pass" if res.get("passed") else "fail"
        parts.append(f"<tr><td>{_e(r['task'])}</td><td><a href='{r['report']}'>trial {r['trial']}</a></td>"
                     f"<td class={cls}>{v}</td><td>{res.get('score', '—')}</td><td>{r['turns']}</td><td>{r['seconds']}</td>"
                     f"<td>${r['cost']:.4f}</td><td>{'<img class=thumb src=' + chr(39) + r['thumb'] + chr(39) + '>' if r.get('thumb') else ''}</td></tr>")
    parts.append("</table><h2>Summary</h2><pre>" + _e(json.dumps(summary, indent=1)) + "</pre>")
    out = run_dir / "index.html"
    out.write_text("\n".join(parts))
    return out
