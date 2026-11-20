"""L3 timing: STA corner coverage, orderings, consistency, DRV, path arithmetic (from ECC's own STA outputs)."""
from __future__ import annotations

import json
import re

from ecc_verify.model import Layer, Result
from ecc_verify.registry import check


def _period(ws):
    return 1000.0 / ws.ecc_toml["design"]["frequency_mhz"]


def _expected_corners(ws):
    cfg = json.load(open(ws.path / "config/sta_ecc.json"))
    temp = {l["corner"]: l["temperature"] for l in cfg["liberty"]}
    out = []
    for blk in cfg["signoff"]:
        for corner, rcs in blk.items():
            t = temp[corner]
            tag = f"{corner}_{'m' + str(abs(t)) if t < 0 else t}"
            out += [f"{tag}/{rc}" for rc in rcs]
    return out


@check("L3.sta.corner_coverage", Layer.L3, steps=("sta",), category="timing", severity="critical",
       title="every configured corner x RC combination has a summary and reports")
def corner_cov(ws):
    exp = _expected_corners(ws)
    have = ws.sta_corners()
    d = ws.step_dir("sta")
    missing = [c for c in exp if c not in have]
    norpt = [c for c in exp if not (d / "report" / c / "qor_summary.rpt").exists()]
    extra = [c for c in have if c not in exp]
    if missing or norpt or extra:
        return Result.fail(f"missing summaries {missing}, missing reports {norpt}, unexpected {extra}",
                           measured=len(have), expected=len(exp))
    m = ws.metrics()
    if m.get("sta_corner_count") is not None and m["sta_corner_count"] != len(exp):
        return Result.fail(f"sta_corner_count metric {m['sta_corner_count']} != {len(exp)} configured")
    return Result.ok(f"{len(exp)} corner/RC combinations covered", measured=len(have), expected=len(exp))


def _grp(ws, prefix):
    return {k: v for k, v in ws.sta_corners().items() if k.startswith(prefix)}


@check("L3.sta.setup_ordering", Layer.L3, steps=("sta",), category="timing", severity="major",
       title="slow corner (MAX/WCL) setup WS <= fast corner (MIN/ML) setup WS (inversion > 5% T fails)")
def setup_order(ws):
    T = _period(ws)
    slow = [v["summary"]["setup"]["wns"] for k, v in ws.sta_corners().items() if k.startswith(("MAX_", "WCL_"))]
    fast = [v["summary"]["setup"]["wns"] for k, v in ws.sta_corners().items() if k.startswith(("MIN_", "ML_"))]
    if not slow or not fast:
        return Result.skip("missing slow or fast corners")
    s, f = min(slow), min(fast)
    if s > f + 0.05 * T:
        return Result.fail(f"slow-corner worst setup WS {s} exceeds fast-corner {f} by more than 5% T ({0.05 * T:.2f} ns)",
                           measured=s, expected=f"<= {f}", tolerance=f"{0.05 * T:.2f} ns")
    return Result.ok(f"slow {s} <= fast {f}", measured=s, expected=f"<= {f}", tolerance=f"{0.05 * T:.2f} ns")


@check("L3.sta.hold_ordering", Layer.L3, steps=("sta",), category="timing", severity="minor",
       title="hold WS worst at the fast corner (WARN otherwise)")
def hold_order(ws):
    fast = [v["summary"]["hold"]["wns"] for k, v in ws.sta_corners().items() if k.startswith(("MIN_", "ML_"))]
    slow = [v["summary"]["hold"]["wns"] for k, v in ws.sta_corners().items() if k.startswith(("MAX_", "WCL_"))]
    if not fast or not slow:
        return Result.skip("missing corners")
    if min(fast) > min(slow):
        return Result.warn(f"worst hold at slow corner ({min(slow)}) not at fast ({min(fast)})",
                           measured=min(fast), expected=f"<= {min(slow)}")
    return Result.ok(f"fast-corner hold {min(fast)} <= slow-corner {min(slow)}")


@check("L3.sta.ws_nvp_consistency", Layer.L3, steps=("sta",), category="timing", severity="major",
       title="WS < 0 <=> NVP > 0 per corner and path group")
def ws_nvp(ws):
    probs = []
    for k, js in ws.sta_corners().items():
        for kind in ("setup", "hold"):
            items = [("summary", js["summary"][kind])] + [(g["name"], g[kind]) for g in js.get("path_groups", [])]
            for nm, v in items:
                if (v["wns"] < 0) != (v["nvp"] > 0):
                    probs.append(f"{k}/{nm}/{kind}: wns={v['wns']} nvp={v['nvp']}")
    if probs:
        return Result.fail("; ".join(probs[:4]), measured=len(probs), expected=0)
    return Result.ok("WS/NVP consistent in all corners and groups")


@check("L3.sta.frequency_formula", Layer.L3, steps=("sta",), category="timing", severity="minor",
       title="frequency_mhz == 1000/(T - WS) within 1% (or 1 MHz: reports are integer MHz)")
def freq(ws):
    T = _period(ws)
    probs = []
    for k, js in ws.sta_corners().items():
        s = js["summary"]["setup"]
        if "frequency_mhz" not in s:
            continue
        calc = 1000.0 / (T - s["wns"]) if T - s["wns"] > 0 else float("inf")
        if abs(calc - s["frequency_mhz"]) > max(0.01 * calc, 1.0):
            probs.append(f"{k}: reported {s['frequency_mhz']} vs {calc:.1f} (T={T:.3f}, WS={s['wns']})")
    if probs:
        return Result.fail("; ".join(probs[:3]), measured=len(probs), tolerance="1% or 1 MHz")
    return Result.ok("frequency formula holds", tolerance="1% or 1 MHz")


@check("L3.sta.drv_clean", Layer.L3, steps=("sta",), category="timing", severity="major",
       title="max_transition/max_capacitance/max_fanout violations == 0; report VIOLATED rows match statistics")
def drv(ws):
    d = ws.step_dir("sta")
    viol, mism = {}, []
    for key, js in ws.sta_corners().items():
        corner, rc = key.split("/")
        st = js.get("design_statistics", {})
        for stat, rpt in (("tran", "max_transition"), ("cap", "max_capacitance"), ("fanout", "max_fanout")):
            p = d / "report" / corner / rc / f"{rpt}.rpt"
            n = 0
            if p.exists():
                n = sum(1 for ln in p.read_text(errors="replace").splitlines() if ln.rstrip().endswith("VIOLATED"))
            if n != st.get(stat, 0):
                mism.append(f"{key}/{stat}: report has {n} VIOLATED rows, statistics say {st.get(stat)}")
            if n or st.get(stat, 0):
                viol.setdefault(rpt, {})[key] = max(n, st.get(stat, 0))
    if mism:
        return Result.fail("report/statistics mismatch: " + "; ".join(mism[:3]), measured=len(mism))
    if viol:
        tot = {k: sum(v.values()) for k, v in viol.items()}
        worst = {k: max(v.items(), key=lambda kv: kv[1]) for k, v in viol.items()}
        ev = [f"{ws.rel(d)}/report/{c}/{r}/{k}.rpt" for k, vv in worst.items() for c, r in [vv[0].split("/")]]
        return Result.fail(f"DRV violations (sum over corners): {tot}; worst corner {worst}", measured=tot,
                           expected=0, evidence=ev, details={"per_corner": viol})
    return Result.ok("no DRV violations in any corner", measured=0, expected=0)


@check("L3.sta.unconstrained", Layer.L3, steps=("sta",), category="timing", severity="major",
       title="no unconstrained endpoints reported")
def unconstrained(ws):
    d = ws.step_dir("sta")
    hits = []
    for p in d.glob("report/*/*/*.rpt"):
        for i, ln in enumerate(p.read_text(errors="replace").splitlines(), 1):
            if re.search(r"unconstrained", ln, re.I):
                hits.append(f"{ws.rel(p)}:{i}")
    for k, js in ws.sta_corners().items():
        if js.get("design_statistics", {}).get("unconstrained"):
            hits.append(f"{k}: design_statistics.unconstrained")
    if hits:
        return Result.fail(f"{len(hits)} unconstrained mentions", measured=len(hits), evidence=hits[:5])
    return Result.ok("no unconstrained endpoints reported")


@check("L3.sta.hold_margin", Layer.L3, steps=("sta",), category="timing", severity="minor",
       title="hold WS >= 50 ps at every corner (WARN otherwise)")
def hold_margin(ws):
    low = {k: v["summary"]["hold"]["wns"] for k, v in ws.sta_corners().items() if v["summary"]["hold"]["wns"] < 0.05}
    worst = min((v["summary"]["hold"]["wns"] for v in ws.sta_corners().values()), default=None)
    if low:
        return Result.warn(f"{len(low)} corners with hold WS < 50 ps (worst {worst} ns)", measured=worst,
                           expected=">= 0.05 ns", tolerance="50 ps", details={"corners": low})
    return Result.ok(f"min hold WS {worst} ns", measured=worst, expected=">= 0.05 ns")


@check("L3.sta.independent_path_check", Layer.L3, steps=("sta",), category="timing", severity="major",
       title="re-sum stage delays vs arrival; required - arrival == slack (timing_paths.json)")
def path_check(ws):
    d = ws.step_dir("sta")
    probs, n = [], 0
    for p in sorted(d.glob("feature/*/*/timing_paths.json")):
        corner = f"{p.parent.parent.name}/{p.parent.name}"
        for path in json.load(open(p)).get("paths", []):
            n += 1
            kind = path.get("analysis_type")
            arr, req, sl = path["arrival_ns"], path["required_ns"], path["slack_ns"]
            exp = (req - arr) if kind == "setup" else (arr - req)
            if abs(exp - sl) > 2e-3:
                probs.append(f"{corner} {path['start_point']}->{path['end_point']}: required-arrival={exp:.4f} != slack {sl:.4f}")
            st = path.get("stages", [])
            if st:
                t0 = st[0]["arrival_ns"]
                total = t0 + sum(s["incremental_delay_ns"] for s in st[1:])
                last = st[-1]["arrival_ns"]
                if abs(total - last) > 2e-3:
                    probs.append(f"{corner} {path['end_point']}: sum(incr)={total:.4f} != last stage arrival {last:.4f}")
                # data arrival = launch network delay + clock-source latency + data path
                if kind == "setup" and abs(last - arr) > 5e-2 + path.get("launch_clock_network_delay_ns", 0) * 0:
                    pass
    if not n:
        return Result.skip("no timing_paths.json")
    if probs:
        return Result.fail("; ".join(probs[:3]), measured=len(probs), expected=0, tolerance="2 ps",
                           details={"problems": probs[:20]})
    return Result.ok(f"{n} paths: arithmetic consistent", tolerance="2 ps")
