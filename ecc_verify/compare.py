"""Compare a results.json against a baseline (or against nothing) and decide the exit code.

Verdict classes per (design, check)::

    NEW FAIL         FAIL/ERROR now, not failing in the baseline and not covered by a known issue   -> exit 1
    FIXED            failing in the baseline, PASS/WARN now
    STILL-FAILING    FAIL/ERROR now and in the baseline, or covered by a known issue (known=True / untracked)
    metric drift     a key metric moved beyond its tolerance; *worse* beyond tolerance gates (exit 1), better is reported

Without a baseline every FAIL/ERROR that no known issue covers is NEW (exit 1), i.e. "no FAIL".
"""
from __future__ import annotations

import fnmatch
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from ecc_verify.config import DEFAULT_TOLERANCES
from ecc_verify.known_issues import KnownIssues

BAD = ("FAIL", "ERROR")
GOOD = ("PASS", "WARN")
SCHEMA = 1


class CompareError(Exception):
    """Unusable baseline/results input (exit code 2)."""


# --------------------------------------------------------------------------- metric rules
# (regex, kind, tolerance key, better direction, label)
RULES = [
    (r"^(setup|hold)_ws$", "slack", None, "higher"),
    (r"^(setup|hold)_tns$", "tns", None, "higher"),
    (r"^(setup|hold)_nvp$", "nvp", "nvp_increase", "lower"),
    (r"^hpwl$", "rel", "hpwl_rel", "lower"),
    (r"^route_wl$", "rel", "route_wl_rel", "lower"),
    (r"^via_count$", "rel", "via_rel", "lower"),
    (r"^(std_cell_area|instance_area)$", "rel", "area_rel", "lower"),
    (r"^die_area$", "rel", "area_rel", "none"),
    (r"^cell_count$", "rel", "cells_rel", "lower"),
    (r"^core_util$", "abs", "util_abs", "none"),
    (r"^power_(total|dynamic|leakage)_uw$", "rel", "power_rel", "lower"),
    (r"^(drv_.*|drc_count|lvs_count|route_violations)$", "count", "drv_increase", "lower"),
    (r"^runtime\.", "rel_min", "runtime_rel", "lower"),
    (r"^memory\.", "rel_min", "memory_rel", "lower"),
    (r"^tclk_ns$", "info", None, "none"),
]
_RULES = [(re.compile(r), k, t, b) for r, k, t, b in RULES]


def rule_for(metric: str):
    for rx, kind, tol, better in _RULES:
        if rx.search(metric):
            return kind, tol, better
    return None


def _min_for(metric: str, tol: dict) -> float:
    return float(tol["runtime_min_s"] if metric.startswith("runtime.") else tol["memory_min_mb"])


def metric_drift(metric: str, base: float, cur: float, tol: dict, tclk: float | None = None) -> dict | None:
    """Evaluate one metric. Returns None when the metric is within tolerance (or has no rule / is informational),
    else {"direction": "worse"|"better"|"changed", "allowed": ..., "delta": ..., "rel": ...}."""
    r = rule_for(metric)
    if r is None:
        return None
    kind, key, better = r
    delta = cur - base
    rel = (delta / abs(base)) if base else (0.0 if delta == 0 else float("inf"))

    def verdict(allowed: float, direction_up_is_worse: bool | None):
        if abs(delta) <= allowed + 1e-12:
            return None
        if direction_up_is_worse is None:
            d = "changed"
        else:
            worse = (delta > 0) == direction_up_is_worse
            d = "worse" if worse else "better"
        return {"direction": d, "allowed": allowed, "delta": delta, "rel": rel}

    up_worse = {"lower": True, "higher": False, "none": None}[better]
    if kind == "info":
        return None
    if kind == "slack":
        allowed = max(float(tol["ws_abs_ns"]), float(tol["ws_rel_t"]) * (tclk or 0.0))
        return verdict(allowed, up_worse)
    if kind == "tns":
        allowed = max(float(tol["tns_abs_ns"]), float(tol["tns_rel"]) * abs(base))
        return verdict(allowed, up_worse)
    if kind in ("nvp", "count"):
        allowed = float(tol[key])
        if delta > allowed:
            return {"direction": "worse", "allowed": allowed, "delta": delta, "rel": rel}
        if delta < 0:
            return {"direction": "better", "allowed": allowed, "delta": delta, "rel": rel}
        return None
    if kind == "abs":
        return verdict(float(tol[key]), up_worse)
    if kind == "rel":
        return verdict(float(tol[key]) * abs(base), up_worse)
    if kind == "rel_min":
        allowed = max(float(tol[key]) * abs(base), _min_for(metric, tol))
        return verdict(allowed, up_worse)
    return None


# --------------------------------------------------------------------------- baseline IO
@dataclass
class Baseline:
    path: str
    provenance: dict
    designs: dict            # design -> {"checks": {id: {status,...}}, "metrics": {...}}


def load_baseline(path: str | Path) -> Baseline:
    p = Path(path)
    if not p.is_dir():
        raise CompareError(f"baseline directory not found: {p}")
    prov_f = p / "provenance.json"
    try:
        prov = json.loads(prov_f.read_text()) if prov_f.exists() else {}
        designs = {}
        for f in sorted(p.glob("*.json")):
            if f.name == "provenance.json":
                continue
            d = json.loads(f.read_text())
            if "checks" not in d:
                continue
            designs[d.get("design", f.stem)] = d
    except (json.JSONDecodeError, OSError) as e:
        raise CompareError(f"cannot read baseline {p}: {e}") from e
    if not designs:
        raise CompareError(f"baseline {p} contains no per-design JSON files (expected <design>.json)")
    return Baseline(str(p), prov, designs)


def load_results(path: str | Path) -> dict:
    p = Path(path)
    try:
        d = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise CompareError(f"cannot read results {p}: {e}") from e
    if not isinstance(d, dict) or "results" not in d:
        raise CompareError(f"{p} is not an ecc-verify results.json (no `results`)")
    return d


# --------------------------------------------------------------------------- comparison
@dataclass
class Comparison:
    entries: list = field(default_factory=list)       # verdict rows
    drift: list = field(default_factory=list)         # metric drift rows
    stale_known: list = field(default_factory=list)   # known-issue entries that matched no failing result
    notes: list = field(default_factory=list)
    baseline: dict | None = None                      # provenance of the baseline (None: no baseline)
    current: dict = field(default_factory=dict)       # meta/provenance of the current run
    counts: dict = field(default_factory=dict)
    exit_code: int = 0

    def of(self, kind: str) -> list:
        return [e for e in self.entries if e["kind"] == kind]

    def to_json(self) -> dict:
        return {"schema": SCHEMA, "exit_code": self.exit_code, "counts": self.counts, "baseline": self.baseline,
                "current": self.current, "notes": self.notes, "entries": self.entries, "drift": self.drift,
                "stale_known_issues": self.stale_known}


def _gating(metric: str, patterns_warn: list[str], runtime_downgrade: bool) -> bool:
    if any(fnmatch.fnmatchcase(metric, p) for p in patterns_warn):
        return False
    if runtime_downgrade and (metric.startswith("runtime.") or metric.startswith("memory.")):
        return False
    return True


def compare(current: dict, baseline: Baseline | None, known: KnownIssues, cfg_tolerances: dict | None = None,
            ignore_metrics: list[str] | None = None, warn_only_metrics: list[str] | None = None,
            compare_metrics: bool = True) -> Comparison:
    """`current` is a loaded results.json (keys: results, metrics, design_config, meta)."""
    cmp_ = Comparison()
    meta = current.get("meta", {})
    prov_cur = meta.get("provenance", {})
    cmp_.current = {"date": meta.get("date"), "root": meta.get("root"), "ecc_version": meta.get("ecc_version"),
                    "provenance": prov_cur}
    known = known.with_design_configs(current.get("design_config", {}))
    for design, dc in current.get("design_config", {}).items():
        if dc and dc.get("_error"):
            cmp_.notes.append(f"invalid verify.toml ignored: {dc['_error']}")
    base_checks = {}
    if baseline is not None:
        cmp_.baseline = {"path": baseline.path, "provenance": baseline.provenance}
        for dn, d in baseline.designs.items():
            for cid, v in d["checks"].items():
                base_checks[(dn, cid)] = v.get("status")

    cur_status = {}
    for r in current["results"]:
        cur_status[(r["design"], r["check"])] = r
    for (design, cid), r in sorted(cur_status.items()):
        st = r["status"]
        bst = base_checks.get((design, cid))
        k = known.lookup(cid, design) if st in BAD else None
        row = {"design": design, "check": cid, "status": st, "baseline_status": bst,
               "message": (r.get("message") or "")[:300]}
        if st in BAD:
            if k is not None or bst in BAD:
                row.update(kind="still_failing", known=k is not None,
                           bugs=list(k.bugs) if k else [], note=k.note if k else "")
            else:
                row.update(kind="new_fail", known=False, bugs=[], note="")
            cmp_.entries.append(row)
        elif bst in BAD and st in GOOD:
            row.update(kind="fixed")
            cmp_.entries.append(row)
        elif bst in BAD:      # failing before, SKIP/missing now: not fixed, not failing: say so
            row.update(kind="unverified")
            cmp_.entries.append(row)
    if baseline is not None:
        for (design, cid), bst in sorted(base_checks.items()):
            if bst in BAD and (design, cid) not in cur_status and design in {r["design"] for r in current["results"]}:
                cmp_.entries.append({"design": design, "check": cid, "status": None, "baseline_status": bst,
                                     "kind": "unverified", "message": "check not run in the current results"})
        missing_designs = sorted(set(baseline.designs) - {r["design"] for r in current["results"]})
        if missing_designs:
            cmp_.notes.append(f"baseline designs absent from the current results (not compared): {', '.join(missing_designs)}")
        cmp_.notes += _provenance_notes(baseline.provenance, prov_cur)

    # ---- metrics
    host_diff = False
    if baseline is not None:
        bp, cp = baseline.provenance.get("host", {}), prov_cur.get("host", {})
        host_diff = bool(bp and cp and (bp.get("name") != cp.get("name") or bp.get("pin_cpus") != cp.get("pin_cpus")
                                        or bp.get("cpus") != cp.get("cpus")))
        if host_diff:
            cmp_.notes.append("baseline was produced on a different host/CPU set: runtime and memory drift is "
                              "reported but not gating")
    if baseline is not None and compare_metrics:
        ign = list(ignore_metrics or [])
        warn = list(warn_only_metrics or [])
        for design, cur_m in sorted((current.get("metrics") or {}).items()):
            b = baseline.designs.get(design)
            if not b:
                continue
            tol = dict(DEFAULT_TOLERANCES)
            tol.update(cfg_tolerances or {})
            tol.update(((current.get("design_config") or {}).get(design) or {}).get("tolerances", {}))
            bm = b.get("metrics", {})
            tclk = bm.get("tclk_ns") or cur_m.get("tclk_ns")
            for m in sorted(set(bm) & set(cur_m)):
                if any(fnmatch.fnmatchcase(m, p) for p in ign):
                    continue
                if not (isinstance(bm[m], (int, float)) and isinstance(cur_m[m], (int, float))):
                    continue
                d = metric_drift(m, float(bm[m]), float(cur_m[m]), tol, tclk)
                if d is None:
                    continue
                d.update(design=design, metric=m, baseline=bm[m], current=cur_m[m],
                         gating=d["direction"] in ("worse", "changed") and _gating(m, warn, host_diff))
                cmp_.drift.append(d)
            tb, tc = bm.get("tclk_ns"), cur_m.get("tclk_ns")
            if tb and tc and abs(tb - tc) > 1e-6 * abs(tb):
                cmp_.notes.append(f"{design}: clock period changed {tb} -> {tc} ns (slack drift is not comparable)")
    ran = {r["check"] for r in current["results"]}   # only entries for checks that actually ran can be stale
    cmp_.stale_known = [{"checks": list(e.checks), "designs": list(e.designs) if e.designs else None,
                         "bugs": list(e.bugs), "source": e.source} for e in known.unused()
                        if any(fnmatch.fnmatchcase(c, pat) for c in ran for pat in e.checks)]

    nf = len(cmp_.of("new_fail"))
    sf = cmp_.of("still_failing")
    reg = [d for d in cmp_.drift if d["gating"]]
    cmp_.counts = {"new_fail": nf, "fixed": len(cmp_.of("fixed")), "still_failing_known": sum(1 for e in sf if e["known"]),
                   "still_failing_untracked": sum(1 for e in sf if not e["known"]), "unverified": len(cmp_.of("unverified")),
                   "drift_regressions": len(reg), "drift_improvements": sum(1 for d in cmp_.drift if d["direction"] == "better"),
                   "drift_non_gating": sum(1 for d in cmp_.drift if d["direction"] != "better" and not d["gating"]),
                   "stale_known_issues": len(cmp_.stale_known)}
    cmp_.exit_code = 1 if (nf or reg) else 0
    return cmp_


def _provenance_notes(b: dict, c: dict) -> list[str]:
    notes = []
    be, ce = (b.get("ecc") or {}), (c.get("ecc") or {})
    if be.get("build_commit") and ce.get("build_commit") and be["build_commit"] != ce["build_commit"]:
        notes.append(f"ecc build changed: {be['build_commit'][:9]} ({be.get('version')}) -> {ce['build_commit'][:9]} ({ce.get('version')})")
    bd, cd = (b.get("designs") or {}), (c.get("designs") or {})
    if bd.get("commit") and cd.get("commit") and bd["commit"] != cd["commit"]:
        notes.append(f"design sources changed: {bd['commit'][:9]} -> {cd['commit'][:9]}")
    bp, cp = (b.get("pdk") or {}), (c.get("pdk") or {})
    if bp.get("fingerprint") and cp.get("fingerprint") and bp["fingerprint"] != cp["fingerprint"]:
        notes.append(f"PDK changed: {bp.get('version') or bp['fingerprint']} -> {cp.get('version') or cp['fingerprint']}")
    return notes


# --------------------------------------------------------------------------- rendering
def _fmt(v) -> str:
    if isinstance(v, float):
        return f"{v:.6g}"
    return str(v)


def render_markdown(c: Comparison) -> str:
    L = []
    verdict = "FAIL (exit 1)" if c.exit_code else "OK (exit 0)"
    L.append(f"# ecc-verify compare: {verdict}\n")
    n = c.counts
    L.append(f"NEW FAIL **{n['new_fail']}** | FIXED **{n['fixed']}** | STILL-FAILING known **{n['still_failing_known']}**, "
             f"untracked **{n['still_failing_untracked']}** | metric regressions **{n['drift_regressions']}** "
             f"(improvements {n['drift_improvements']}, non-gating {n['drift_non_gating']})\n")
    if c.baseline:
        bp = c.baseline["provenance"]
        L.append(f"Baseline: `{c.baseline['path']}` ({_prov_line(bp)})  ")
    else:
        L.append("Baseline: none (every FAIL not covered by a known issue is NEW)  ")
    L.append(f"Current: {_prov_line(c.current.get('provenance', {}))}, root `{c.current.get('root')}`\n")
    for note in c.notes:
        L.append(f"- note: {note}")
    if c.notes:
        L.append("")

    def table(rows, cols):
        out = ["| " + " | ".join(h for h, _ in cols) + " |", "|" + "|".join("---" for _ in cols) + "|"]
        for r in rows:
            out.append("| " + " | ".join(str(f(r)).replace("|", "\\|").replace("\n", " ") for _, f in cols) + " |")
        return out

    nf = c.of("new_fail")
    L.append(f"## NEW FAIL ({len(nf)})\n")
    L += table(nf, [("Design", lambda r: r["design"]), ("Check", lambda r: f"`{r['check']}`"),
                    ("Status", lambda r: r["status"]), ("Baseline", lambda r: r["baseline_status"] or "absent"),
                    ("Message", lambda r: r["message"][:200])]) if nf else ["none"]
    reg = [d for d in c.drift if d["gating"]]
    L.append(f"\n## Metric regressions ({len(reg)})\n")
    L += table(reg, _drift_cols()) if reg else ["none"]
    fx = c.of("fixed")
    L.append(f"\n## FIXED ({len(fx)})\n")
    L += table(fx, [("Design", lambda r: r["design"]), ("Check", lambda r: f"`{r['check']}`"),
                    ("Now", lambda r: r["status"])]) if fx else ["none"]
    sk = [e for e in c.of("still_failing") if e["known"]]
    L.append(f"\n## STILL-FAILING, known ({len(sk)})\n")
    if sk:
        grouped: dict = {}
        for e in sk:
            g = grouped.setdefault((e["check"], tuple(e["bugs"]), e["note"]), [])
            g.append(e["design"])
        L += table(sorted(grouped.items()), [("Check", lambda kv: f"`{kv[0][0]}`"), ("Bug", lambda kv: ", ".join(kv[0][1]) or "known"),
                                              ("Designs", lambda kv: f"{len(kv[1])}: " + ", ".join(sorted(kv[1]))),
                                              ("Note", lambda kv: kv[0][2][:160])])
    else:
        L.append("none")
    su = [e for e in c.of("still_failing") if not e["known"]]
    if su:
        L.append(f"\n## STILL-FAILING, untracked ({len(su)}): in the baseline but in no known-issues entry\n")
        L += table(su, [("Design", lambda r: r["design"]), ("Check", lambda r: f"`{r['check']}`"),
                        ("Message", lambda r: r["message"][:200])])
    un = c.of("unverified")
    if un:
        L.append(f"\n## Not verified now ({len(un)}): failed in the baseline, SKIP or not run now\n")
        L += table(un, [("Design", lambda r: r["design"]), ("Check", lambda r: f"`{r['check']}`"),
                        ("Now", lambda r: r["status"] or "not run")])
    imp = [d for d in c.drift if d["direction"] == "better"]
    nong = [d for d in c.drift if d["direction"] != "better" and not d["gating"]]
    if imp:
        L.append(f"\n## Metric improvements beyond tolerance ({len(imp)})\n")
        L += table(imp, _drift_cols())
    if nong:
        L.append(f"\n## Metric drift, non-gating ({len(nong)})\n")
        L += table(nong, _drift_cols())
    if c.stale_known:
        L.append(f"\n## Known-issue entries with no failing result ({len(c.stale_known)}): candidates to close\n")
        L += table(c.stale_known, [("Checks", lambda r: ", ".join(r["checks"])), ("Designs", lambda r: ", ".join(r["designs"] or ["all"])),
                                   ("Bugs", lambda r: ", ".join(r["bugs"]))])
    return "\n".join(L) + "\n"


def _drift_cols():
    return [("Design", lambda r: r["design"]), ("Metric", lambda r: f"`{r['metric']}`"),
            ("Baseline", lambda r: _fmt(r["baseline"])), ("Current", lambda r: _fmt(r["current"])),
            ("Delta", lambda r: _fmt(r["delta"])), ("Allowed", lambda r: _fmt(r["allowed"])),
            ("Verdict", lambda r: r["direction"] + ("" if r["gating"] or r["direction"] == "better" else " (non-gating)"))]


def _prov_line(p: dict) -> str:
    if not p:
        return "no provenance"
    ecc = p.get("ecc") or {}
    bits = [f"ecc {ecc.get('version', '?')} ({(ecc.get('build_commit') or '?')[:9]})",
            f"designs {((p.get('designs') or {}).get('commit') or '?')[:9]}",
            f"host {(p.get('host') or {}).get('name', '?')}", f"{p.get('date', '?')}"]
    return ", ".join(bits)


def render_console(c: Comparison, max_rows: int = 40) -> str:
    L = []
    for e in c.of("new_fail")[:max_rows]:
        L.append(f"  NEW FAIL  {e['design']}: {e['check']}: {e['message'][:140]}")
    for d in [d for d in c.drift if d["gating"]][:max_rows]:
        L.append(f"  REGRESSION  {d['design']}: {d['metric']} {_fmt(d['baseline'])} -> {_fmt(d['current'])} "
                 f"(allowed +-{_fmt(d['allowed'])})")
    for e in c.of("fixed")[:max_rows]:
        L.append(f"  FIXED     {e['design']}: {e['check']}")
    n = c.counts
    L.append(f"[ecc-verify] compare: {n['new_fail']} NEW FAIL, {n['fixed']} FIXED, {n['still_failing_known']} known + "
             f"{n['still_failing_untracked']} untracked STILL-FAILING, {n['drift_regressions']} metric regressions "
             f"-> exit {c.exit_code}")
    return "\n".join(L)


def write_outputs(c: Comparison, out_dir: Path) -> None:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "compare.json").write_text(json.dumps(c.to_json(), indent=1, default=str))
    (out_dir / "compare.md").write_text(render_markdown(c))
