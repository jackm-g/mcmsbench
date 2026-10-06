"""Render the comparison as one static HTML file."""
from __future__ import annotations

import html
import statistics
from pathlib import Path

from . import Trial, aggregate, rel

CSS = """body{font:14px/1.45 -apple-system,Helvetica,sans-serif;background:#16181c;color:#ddd;margin:0;padding:24px;max-width:1500px}
a{color:#7cc4ff;text-decoration:none} h1,h2,h3{font-weight:600} h2{margin-top:36px;border-top:1px solid #2a2d33;padding-top:20px}
table{border-collapse:collapse;margin:8px 0} td,th{padding:6px 12px;border-bottom:1px solid #2a2d33;text-align:left;vertical-align:top}
th{color:#9aa;font-weight:500} .num{text-align:right;font-variant-numeric:tabular-nums} .meta{color:#999} .small{font-size:12px}
.p100{background:#173d25}.p50{background:#3d3a17}.p0{background:#3d1b1b} .pass{color:#6fdc8c}.fail{color:#ff7b7b}.warn{color:#f5c26b}
.builds{display:grid;gap:14px} .builds figure{margin:0;background:#0e0f12;border:1px solid #2a2d33;border-radius:8px;padding:8px;max-width:560px}
.builds img{width:100%;border-radius:4px;background:#000} .thumbs img{height:54px;margin:2px;border-radius:3px;border:1px solid #333}
.tag{display:inline-block;background:#26303d;border-radius:10px;padding:1px 8px;margin:1px;font-size:12px;color:#b9d4f5}
.bar{display:inline-block;height:9px;background:#4c8dd6;border-radius:2px;vertical-align:middle}"""


def e(x) -> str:
    return html.escape(str(x))


def f(x, nd=2, prefix="", suffix="") -> str:
    return "—" if x is None else f"{prefix}{x:.{nd}f}{suffix}"


def pct(x) -> str:
    return "—" if x is None else f"{100 * x:.0f}%"


def _pclass(rate) -> str:
    return "" if rate is None else "p100" if rate >= 0.999 else "p0" if rate <= 0.001 else "p50"


def render(trials: list[Trial], out: Path, title: str = "MCMSBench comparison") -> Path:
    agg = aggregate(trials)
    labels, tasks, grid, overall, cells = agg["labels"], agg["tasks"], agg["grid"], agg["overall"], agg["cells"]
    base = out.parent
    P = [f"<!doctype html><meta charset=utf-8><title>{e(title)}</title><style>{CSS}</style><h1>{e(title)}</h1>",
         f"<p class=meta>{len(trials)} trials · {len(tasks)} tasks · {len(labels)} agents</p>"]

    # ---------------- overall
    P.append("<h2>Overall</h2><table><tr><th>model</th><th class=num>tasks</th><th class=num>mean task pass rate</th>"
             "<th class=num>trials passed</th><th class=num>mean score</th><th class=num>mean turns</th><th class=num>mean time</th>"
             "<th class=num>total cost</th><th class=num>cost / pass</th></tr>")
    for lab in labels:
        o = overall[lab]
        P.append(f"<tr><td><b>{e(lab)}</b></td><td class=num>{o['tasks']}</td><td class='num {_pclass(o['mean_task_pass_rate'])}'>{pct(o['mean_task_pass_rate'])}</td>"
                 f"<td class=num>{o['passes']}/{o['graded']}</td><td class=num>{f(o['score'])}</td><td class=num>{f(o['turns'], 1)}</td>"
                 f"<td class=num>{f(o['seconds'], 0, suffix='s')}</td><td class=num>{f(o['total_cost'], 2, '$')}</td><td class=num>{f(o['cost_per_pass'], 3, '$')}</td></tr>")
    P.append("</table>")

    # ---------------- tool calling + honesty
    P.append("<h2>Tool calling and honesty</h2><table><tr><th>model</th><th class=num>steps</th><th class=num>step error rate</th>"
             "<th class=num>timeouts</th><th class=num>malformed args</th><th class=num>unknown tools</th><th class=num>text-only nudges</th>"
             "<th>how runs ended</th><th class=num>hallucinated success</th><th class=num>underclaimed</th><th class=num>inventory desyncs</th>"
             "<th class=num>stalls</th><th class=num>cancelled steps</th><th class=num>reconnects</th>"
             "<th class=num>cache writes/turn</th><th class=num>elisions</th></tr>")
    for lab in labels:
        o = overall[lab]; h = o["honesty"]
        hs = h.get("hallucinated_success", 0)
        P.append(f"<tr><td><b>{e(lab)}</b></td><td class=num>{o['steps']}</td><td class=num>{pct(o['error_rate'])}</td><td class=num>{o['timeouts']}</td>"
                 f"<td class=num>{o['malformed']}</td><td class=num>{o['unknown_tools']}</td><td class=num>{o['nudges']}</td>"
                 f"<td class=small>{e(', '.join(f'{k}×{v}' for k, v in sorted(o['stops'].items())))}</td>"
                 f"<td class='num {'fail' if hs else ''}'>{hs}</td><td class=num>{h.get('underclaimed', 0)}</td><td class='num {'warn' if o['desyncs'] else ''}'>{o['desyncs']}</td>"
                 f"<td class=num>{o['stalls']}</td><td class='num {'warn' if o['cancels'] else ''}'>{o['cancels']}</td><td class='num {'fail' if o['reconnects'] else ''}'>{o['reconnects']}</td>"
                 f"<td class=num>{f(o['cache_write_per_turn'], 0)}</td><td class=num>{o['elisions']}</td></tr>")
    P.append("</table><p class='meta small'>step error rate = code actions that raised / all code actions · hallucinated success = the model "
             "called done(success=true) and the grader disagreed · desyncs = steps where the client inventory differed from the server's · "
             "stalls = goto/approach aborted for lack of progress · cancelled = steps abandoned at the time budget · reconnects = hard resets of the bot session · "
             "cache writes/turn = prompt tokens written to the cache per model call (a clean turn writes only its new output; a rewrite of "
             "history, from an elision or a changed setting, writes the whole conversation) · elisions = times old tool results were shortened</p>")

    # ---------------- grid
    P.append("<h2>Task × model</h2><table><tr><th>task</th>" + "".join(f"<th>{e(l)}</th>" for l in labels) + "</tr>")
    for task in tasks:
        P.append(f"<tr><td><a href='#{e(task)}'>{e(task)}</a></td>")
        for lab in labels:
            c = grid[task].get(lab)
            if not c:
                P.append("<td class=meta>—</td>"); continue
            P.append(f"<td class='{_pclass(c['pass_rate'])}'><b>{c['passes']}/{c['graded']}</b> <span class=meta>score {f(c['score'])}</span><br>"
                     f"<span class='meta small'>{f(c['turns'], 1)} turns · {f(c['seconds'], 0, suffix='s')} · {f(c['cost'], 3, '$')}</span></td>")
        P.append("</tr>")
    P.append("</table>")

    # ---------------- per task
    for task in tasks:
        P.append(f"<h2 id='{e(task)}'>{e(task)}</h2>")
        cols = [l for l in labels if (task, l) in cells]
        P.append(f"<div class=builds style='grid-template-columns:repeat({max(1, len(cols))},minmax(0,1fr))'>")
        for lab in cols:
            ts = sorted(cells[(task, lab)], key=lambda t: (-(t.score or 0), t.trial))
            best = ts[0]
            img = best.asset("final_se.png") or best.asset("final_nw.png")
            gif = best.asset("timelapse.gif")
            P.append("<figure>")
            if img:
                link = rel(best.report, base) if best.report else rel(img, base)
                P.append(f"<a href='{e(link)}'><img src='{e(rel(img, base))}'></a>")
            v = "PASS" if best.passed else "FAIL" if best.passed is not None else "—"
            P.append(f"<figcaption><b>{e(lab)}</b> · trial {best.trial} <span class={'pass' if best.passed else 'fail'}>{v}</span> "
                     f"score {f(best.score)} · {best.turns} turns · {best.seconds:.0f}s · ${best.cost:.3f}"
                     + (f" · <a href='{e(rel(gif, base))}'>timelapse</a>" if gif else "") + "</figcaption>")
            c = best.construction or {}
            if c.get("blocks") is not None:
                mats = ", ".join(f"{k}×{v}" for k, v in list((c.get("materials") or {}).items())[:4])
                P.append(f"<div class='meta small'>built {c.get('blocks')} blocks{(' (' + e(mats) + ')') if mats else ''}"
                         + (f" · size {'×'.join(map(str, c['size']))}" if c.get("size") else "")
                         + (f" · waste {c['waste']}" if c.get("waste") else "") + (f" · stray {c['stray_blocks']}" if c.get("stray_blocks") else "") + "</div>")
            if best.strategy:
                P.append(f"<p class=small>{e(best.strategy.get('summary', ''))}</p><div>" +
                         "".join(f"<span class=tag>{e(t)}</span>" for t in best.strategy.get("tags", [])) + "</div>")
            others = [t for t in ts[1:]]
            if others:
                P.append("<div class=thumbs>")
                for t in others:
                    ti = t.asset("final_se.png")
                    tv = "pass" if t.passed else "fail"
                    inner = f"<img src='{e(rel(ti, base))}' title='trial {t.trial}'>" if ti else f"trial {t.trial}"
                    P.append(f"<a class={tv} href='{e(rel(t.report, base)) if t.report else '#'}'>{inner}</a>")
                P.append("</div>")
            P.append("</figure>")
        P.append("</div>")

        # milestones / checks
        names: list[str] = []
        for lab in cols:
            for t in cells[(task, lab)]:
                for k in (t.milestones or t.checks):
                    if k not in names:
                        names.append(k)
        if names:
            timed = any(v is not None for lab in cols for t in cells[(task, lab)] for v in t.milestones.values())
            P.append("<table><tr><th>" + ("milestone (median time reached · hit rate)" if timed else "check (hit rate)") + "</th>"
                     + "".join(f"<th>{e(l)}</th>" for l in cols) + "</tr>")
            for nm in names:
                P.append(f"<tr><td>{e(nm)}</td>")
                for lab in cols:
                    ts = cells[(task, lab)]
                    hits = [t for t in ts if t.checks.get(nm)]
                    times = [t.milestones.get(nm) for t in ts if t.milestones.get(nm) is not None]
                    med = f"{statistics.median(times):.0f}s · " if times else ""
                    cls = "pass" if len(hits) == len(ts) else "fail" if not hits else "warn"
                    P.append(f"<td class={cls}>{med}{len(hits)}/{len(ts)}</td>")
                P.append("</tr>")
            P.append("</table>")
        # notable strategies across all trials
        strat = [(lab, t) for lab in cols for t in cells[(task, lab)] if t.strategy and t.strategy.get("notable")]
        if strat:
            P.append("<details><summary class=meta>notable behaviours</summary><ul class=small>")
            for lab, t in strat:
                for n in t.strategy["notable"][:3]:
                    P.append(f"<li><b>{e(lab)}</b> t{t.trial}: {e(n)}</li>")
            P.append("</ul></details>")
    out.write_text("\n".join(P))
    return out
