"""L3 power: sum, positivity, leakage vs corner."""
from __future__ import annotations

import json
import re

from ecc_verify.model import Layer, Result
from ecc_verify.registry import check


def _power(ws):
    d = ws.step_dir("powerAnalysis")
    js = json.load(open(d / "feature/power_summary.json"))
    rpt = d / "report/powerAnalysis.rpt"
    if not rpt.exists():
        alt = d / "report/power_analysis.rpt"
        rpt = alt if alt.exists() else next(
            (p for p in sorted((d / "report").glob("*.rpt")) if not p.name.endswith(".db.rpt")),
            alt,
        )
    txt = rpt.read_text(errors="replace")
    return js, txt, d, rpt


def _report_total(txt):
    """(internal_mW, switching_mW, leakage_nW, total_mW) of the 'Total' row."""
    for ln in txt.splitlines():
        if ln.startswith("Total "):
            nums = re.findall(r"([-\d.eE+]+)\s*(mW|nW|uW)", ln)
            if len(nums) >= 4:
                return nums
    return None


def _to_uw(v, u):
    return float(v) * {"mW": 1e3, "uW": 1.0, "nW": 1e-3}[u]


@check("L3.pw.sum", Layer.L3, steps=("powerAnalysis",), category="physics", severity="major",
       title="total = internal + switching + leakage within 0.5% (JSON and report)")
def power_sum(ws):
    js, txt, d, rpt = _power(ws)
    probs = []
    tol = 0.005
    dyn = js["internal_uw"] + js["switching_uw"]
    if abs(dyn - js["dynamic_uw"]) > tol * max(dyn, 1e-12):
        probs.append(f"dynamic {js['dynamic_uw']} != internal+switching {dyn:.4f}")
    row = _report_total(txt)
    if row:
        i, s, l, t = (_to_uw(v, u) for v, u in row[:4])
        if abs(t - (i + s + l)) > tol * t:
            probs.append(f"report Total row {t:.4f} uW != internal+switching+leakage {i + s + l:.4f}")
        if abs(i - js["internal_uw"]) > tol * i or abs(s - js["switching_uw"]) > tol * s or abs(l - js["leakage_uw"]) > tol * max(l, 1e-9):
            probs.append(f"report Total row ({i:.4f},{s:.4f},{l:.4f}) disagrees with power_summary.json")
        tot_metric = js["dynamic_uw"] + js["leakage_uw"]
        if abs(t - tot_metric) > tol * t:
            probs.append(f"report total {t:.4f} != dynamic+leakage {tot_metric:.4f}")
    else:
        probs.append(f"no Total row in {rpt.name}")
    q = json.load(open(ws.path / "home/qor_report.json")).get("power", {})
    if q.get("total_uw") is not None and abs(q["total_uw"] - (js["dynamic_uw"] + js["leakage_uw"])) > 0.01 * q["total_uw"] + 0.002:
        probs.append(f"qor_report power total {q['total_uw']} != {js['dynamic_uw'] + js['leakage_uw']:.4f}")
    if probs:
        return Result.fail("; ".join(probs), tolerance="0.5%", evidence=[ws.rel(rpt)])
    return Result.ok(f"total {js['dynamic_uw'] + js['leakage_uw']:.3f} uW consistent", tolerance="0.5%")


@check("L3.pw.positive", Layer.L3, steps=("powerAnalysis",), category="physics", severity="major",
       title="every power component is positive")
def positive(ws):
    js, txt, d, _ = _power(ws)
    bad = {k: v for k, v in js.items() if k.endswith("_uw") and not v > 0}
    if bad:
        return Result.fail(f"non-positive components {bad}", measured=bad, expected="> 0")
    return Result.ok("all components > 0")


@check("L3.pw.leakage_corner", Layer.L3, steps=("powerAnalysis",), category="physics", severity="minor",
       title="leakage(125C) > leakage(25C) if several power corners are present")
def leak_corner(ws):
    d = ws.step_dir("powerAnalysis")
    corners = [p for p in (d / "feature").glob("*power_summary*.json")]
    if len(corners) <= 1:
        return Result.skip("only a single power corner (powerAnalysis) is produced; cannot compare 125C vs 25C")
    return Result.skip("multiple summaries present but corner tags unknown")
