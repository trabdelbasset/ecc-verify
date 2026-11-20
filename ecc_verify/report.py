"""Report generation: results.json, junit.xml, REPORT.md, report.html (self-contained)."""
from __future__ import annotations

import html
import json
import re
import time
from collections import Counter, OrderedDict, defaultdict
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

from ecc_verify.model import CATEGORIES, STEPS, Status
from ecc_verify.registry import REGISTRY, coverage_matrix

SEV_RANK = {"critical": 0, "major": 1, "minor": 2}
COMPONENT = (  # (check id regex, probable ECC component)
    (r"L3\.sta\.drv_clean", "Timing optimization (sizer DRV repair) / CTS buffering"),
    (r"L3\.so\.antenna|L3\.so\.checklist", "antennaCheck_ecc + checklist generator"),
    (r"L3\.cts", "CTS_ecc (clustering / max_fanout handling)"),
    (r"L3\.gds", "Harden_ecc / filler_ecc GDS writer"),
    (r"L3\.pl\.", "legalization_dreamplace / timing_optimization_sizer"),
    (r"L3\.rt\.", "route_ecc"),
    (r"L3\.rc\.", "RCX_ecc"),
    (r"L3\.sta", "sta_ecc"),
    (r"L3\.pw", "powerAnalysis_ecc"),
    (r"L3\.fp", "preFloorplan_ecc / postFloorplan_ecc"),
    (r"L3\.fn", "synthesis / optimisation netlist edits"),
    (r"L2\.qor|L1\.qor", "qor report engine (home/qor_report.json)"),
    (r"L2\.", "flow glue between steps"),
    (r"L1\.", "flow runner / project manifest"),
    (r"L0\.", "ecc CLI"),
    (r"L5\.fault", "qor report engine / evidence handling"),
    (r"L4\.|L5\.", "flow runner / process management"),
)


def component(check_id: str, message: str = "") -> str:
    for rx, c in COMPONENT:
        if re.match(rx, check_id):
            if check_id == "L1.logs_clean":
                m = re.search(r"/([A-Za-z_]+_[a-z_]+)/log/", message)
                return f"{m.group(1)} step log" if m else c
            return c
    return "unknown"


def _sig(msg: str) -> str:
    msg = msg.split("(")[0].split(" e.g.")[0].split(":")[0]
    s = re.sub(r"[-+]?\d+(\.\d+)?([eE][-+]?\d+)?", "#", msg)
    s = re.sub(r"\s+", " ", s)
    return s[:90]


def findings(rows):
    """Deduplicate FAIL/ERROR rows across designs: group by (check id, normalised first sentence)."""
    groups: "OrderedDict[tuple, dict]" = OrderedDict()
    for r in rows:
        if r["status"] not in ("FAIL", "ERROR"):
            continue
        spec = REGISTRY.get(r["check"], (None,))[0]
        key = (r["check"], _sig(r["message"].split(";")[0]))
        g = groups.setdefault(key, {"check": r["check"], "title": spec.title if spec else "", "status": r["status"],
                                    "severity": spec.severity if spec else "major", "designs": [], "message": r["message"],
                                    "evidence": r.get("evidence", []), "examples": {}})
        g["designs"].append(r["design"])
        g["examples"][r["design"]] = r["message"]
        if not g["evidence"]:
            g["evidence"] = r.get("evidence", [])
    out = list(groups.values())
    for g in out:
        g["component"] = component(g["check"], g["message"])
    out.sort(key=lambda g: (SEV_RANK.get(g["severity"], 3), g["check"]))
    return out


def coverage(layers=None):
    m = coverage_matrix(layers)
    cells = len(STEPS) * len(CATEGORIES)
    covered = sum(1 for s in STEPS for c in CATEGORIES if m[s][c] > 0)
    return m, covered, cells


def write_all(out: Path, rows, meta, layers=None, extra=None):
    out.mkdir(parents=True, exist_ok=True)
    fnd = findings(rows)
    cov, covered, cells = coverage(layers)
    specs = {k: {"layer": v[0].layer.value, "steps": list(v[0].steps), "category": v[0].category,
                 "severity": v[0].severity, "title": v[0].title, "scope": v[0].scope} for k, v in REGISTRY.items()}
    doc = {"meta": meta, "checks": specs, "results": rows, "findings": fnd,
           "coverage": {"matrix": cov, "covered": covered, "cells": cells, "percent": round(100 * covered / cells, 1)}}
    doc.update(extra or {})   # v2: per-design metrics, design_config (verify.toml), ...
    (out / "results.json").write_text(json.dumps(doc, indent=1, default=str))
    (out / "junit.xml").write_text(junit(rows))
    (out / "REPORT.md").write_text(markdown(rows, fnd, cov, covered, cells, meta))
    (out / "report.html").write_text(html_report(rows, fnd, cov, covered, cells, meta))
    return doc


def junit(rows) -> str:
    by = defaultdict(list)
    for r in rows:
        by[r["design"]].append(r)
    parts = ['<?xml version="1.0" encoding="UTF-8"?>', "<testsuites>"]
    for d, rs in by.items():
        c = Counter(r["status"] for r in rs)
        parts.append(f'<testsuite name={quoteattr(d)} tests="{len(rs)}" failures="{c["FAIL"]}" errors="{c["ERROR"]}" '
                     f'skipped="{c["SKIP"]}">')
        for r in rs:
            parts.append(f'<testcase classname={quoteattr(d)} name={quoteattr(r["check"])} time="{r.get("seconds", 0):.2f}">')
            msg = quoteattr(r["message"][:500])
            if r["status"] == "FAIL":
                parts.append(f"<failure message={msg}>{escape(json.dumps(r.get('evidence', [])))}</failure>")
            elif r["status"] == "ERROR":
                parts.append(f"<error message={msg}>{escape(r.get('details', {}).get('traceback', ''))}</error>")
            elif r["status"] == "SKIP":
                parts.append(f"<skipped message={msg}/>")
            elif r["status"] == "WARN":
                parts.append(f"<system-out>WARN: {escape(r['message'][:500])}</system-out>")
            parts.append("</testcase>")
        parts.append("</testsuite>")
    parts.append("</testsuites>")
    return "\n".join(parts)


def _fmt(v, n=120):
    s = v if isinstance(v, str) else json.dumps(v, default=str)
    return s if len(s) <= n else s[:n] + "..."


def markdown(rows, fnd, cov, covered, cells, meta) -> str:
    cnt = Counter(r["status"] for r in rows)
    L = ["# ecc-verify report", "",
         f"- ECC: `{meta.get('ecc_version', '?')}`  | designs: {len(meta.get('designs', []))} | checks: {len(REGISTRY)} "
         f"| wall time: {meta.get('wall_seconds', 0):.0f} s | generated {meta.get('date', '')}",
         f"- Results: " + ", ".join(f"{k} {cnt.get(k, 0)}" for k in ("PASS", "FAIL", "WARN", "SKIP", "ERROR")),
         f"- Coverage: {covered}/{cells} step x category cells = **{100 * covered / cells:.0f}%**", "",
         "## Distinct findings (FAIL / ERROR)", ""]
    if not fnd:
        L.append("None.")
    for i, g in enumerate(fnd, 1):
        L += [f"### F{i}. `{g['check']}` ({g['severity']}) - {g['title']}", "",
              f"- designs ({len(g['designs'])}): {', '.join(sorted(set(g['designs'])))}",
              f"- probable ECC component: {g['component']}",
              f"- evidence: {'; '.join(g['evidence'][:3]) or 'see message'}",
              f"- message: {_fmt(g['message'], 400)}", ""]
    L += ["## Coverage matrix (checks per cell)", "", "| step | " + " | ".join(CATEGORIES) + " |",
          "|---|" + "---|" * len(CATEGORIES)]
    for s in STEPS:
        L.append(f"| {s} | " + " | ".join(str(cov[s][c]) if cov[s][c] else "." for c in CATEGORIES) + " |")
    checks = sorted({r["check"] for r in rows})
    designs = sorted({r["design"] for r in rows})
    sym = {"PASS": "P", "FAIL": "**F**", "WARN": "w", "SKIP": "-", "ERROR": "**E**"}
    L += ["", "## Results matrix (design x check)", "", "P pass, F fail, w warn, - skip, E error", "",
          "| check | " + " | ".join(designs) + " |", "|---|" + "---|" * len(designs)]
    idx = {(r["check"], r["design"]): r["status"] for r in rows}
    for c in checks:
        L.append(f"| {c} | " + " | ".join(sym.get(idx.get((c, d), ""), "") for d in designs) + " |")
    L += ["", "## WARN details (deduplicated)", ""]
    seen = set()
    for r in rows:
        if r["status"] == "WARN":
            k = (r["check"], _sig(r["message"]))
            if k not in seen:
                seen.add(k)
                L.append(f"- `{r['check']}` ({r['design']}): {_fmt(r['message'], 260)}")
    L += ["", "## SKIP / ERROR reasons", ""]
    seen = set()
    for r in rows:
        if r["status"] in ("SKIP", "ERROR"):
            k = (r["check"], r["status"], _sig(r["message"]))
            if k not in seen:
                seen.add(k)
                L.append(f"- {r['status']} `{r['check']}` ({r['design']}): {_fmt(r['message'], 260)}")
    return "\n".join(L) + "\n"


CSS = """
:root{--bg:#121011;--panel:#1c1a1b;--fg:#ece7e4;--mut:#9a9290;--or:#EF6C00;--pass:#2e7d32;--fail:#c62828;
--warn:#f9a825;--skip:#546e7a;--err:#8e24aa}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,sans-serif;padding:16px}
h1,h2,h3{color:var(--or);margin:20px 0 8px}h1{margin-top:0}
table{border-collapse:collapse;margin:8px 0;font-size:12px}th,td{border:1px solid #2c292a;padding:3px 7px;text-align:center}
th{background:var(--panel);color:var(--or)}td.l,th.l{text-align:left}
.PASS{background:var(--pass)}.FAIL{background:var(--fail)}.WARN{background:var(--warn);color:#000}.SKIP{background:var(--skip)}
.ERROR{background:var(--err)}.sev{padding:1px 6px;border-radius:3px;background:var(--or);color:#000;font-size:11px}
.card{background:var(--panel);border-left:4px solid var(--or);padding:8px 12px;margin:8px 0}
code{background:#000;padding:1px 4px;border-radius:3px}.mut{color:var(--mut)}.wrap{overflow-x:auto}
"""


def html_report(rows, fnd, cov, covered, cells, meta) -> str:
    cnt = Counter(r["status"] for r in rows)
    e = html.escape
    h = [f"<!doctype html><html><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
         f"<title>ecc-verify report</title><style>{CSS}</style></head><body>",
         "<h1>ecc-verify report</h1>",
         f"<p class=mut>ECC {e(str(meta.get('ecc_version', '?')))} &middot; {len(meta.get('designs', []))} designs &middot; "
         f"{len(REGISTRY)} checks &middot; wall {meta.get('wall_seconds', 0):.0f}s &middot; {e(meta.get('date', ''))}</p>",
         "<p>" + " ".join(f"<span class='sev {k}' style='background:none'><b>{k}</b> {cnt.get(k, 0)}</span>"
                          for k in ("PASS", "FAIL", "WARN", "SKIP", "ERROR")) + "</p>",
         f"<h2>Coverage {100 * covered / cells:.0f}% ({covered}/{cells} cells)</h2><div class=wrap><table><tr><th class=l>step</th>"
         + "".join(f"<th>{c}</th>" for c in CATEGORIES) + "</tr>"]
    for s in STEPS:
        h.append(f"<tr><td class=l>{e(s)}</td>")
        for c in CATEGORIES:
            n = cov[s][c]
            op = min(1.0, 0.25 + 0.15 * n) if n else 0
            h.append(f"<td style='background:rgba(239,108,0,{op:.2f})'>{n or ''}</td>")
        h.append("</tr>")
    h.append("</table></div><h2>Distinct findings</h2>")
    if not fnd:
        h.append("<p>None.</p>")
    for i, g in enumerate(fnd, 1):
        h.append(f"<div class=card><b>F{i}</b> <code>{e(g['check'])}</code> <span class=sev>{g['severity']}</span> "
                 f"{e(g['title'])}<br><span class=mut>designs ({len(set(g['designs']))}):</span> "
                 f"{e(', '.join(sorted(set(g['designs']))))}<br><span class=mut>component:</span> {e(g['component'])}<br>"
                 f"<span class=mut>evidence:</span> {e('; '.join(g['evidence'][:3]) or 'see message')}<br>"
                 f"{e(_fmt(g['message'], 500))}</div>")
    checks = sorted({r["check"] for r in rows})
    designs = sorted({r["design"] for r in rows})
    idx = {(r["check"], r["design"]): r for r in rows}
    h.append("<h2>Results matrix</h2><div class=wrap><table><tr><th class=l>check</th>"
             + "".join(f"<th title='{e(d)}'>{e(d[:7])}</th>" for d in designs) + "</tr>")
    for c in checks:
        h.append(f"<tr><td class=l>{e(c)}</td>")
        for d in designs:
            r = idx.get((c, d))
            if r:
                h.append(f"<td class={r['status']} title='{e(_fmt(r['message'], 300))}'>{r['status'][0]}</td>")
            else:
                h.append("<td></td>")
        h.append("</tr>")
    h.append("</table></div></body></html>")
    return "".join(h)
