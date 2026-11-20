#!/usr/bin/env python3
"""Fault-injection harness for ECC workspaces (QoR / signoff robustness).

Method
------
Take a *finished* ECC project (``<project>/default`` workspace). For every fault:

1. copy the project to a scratch dir (the original is never touched),
2. apply one mutation (artifact corruption, metric perturbation, config error,
   flow-state tampering),
3. re-run the analysis only (``ecc report qor --plain`` + read
   ``home/qor_report.json``) - no EDA tool is re-run, so each fault costs ~1-3 s,
4. evaluate an *oracle*:
   - ``no_crash``    : rc == 0 and no Python traceback (robustness),
   - ``state``       : expected feasibility/score status (correctness),
   - ``monotone``    : score/dimension must not improve after a degradation
                       (metamorphic relation),
   - ``gap``         : documents a known missing check; PASS means the gap is
                       still there (regression guard for the future fix).

Flow-level faults (corrupt LEF/SDC/RTL, kill mid-step, missing tool) are in
``FLOW_FAULTS`` and run the real flow; use ``--flow`` (slow).

Usage::

    python fault_inject.py <finished_project_dir> [--ecc ecc] [--json out.json] [--flow]
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

# ----------------------------------------------------------------- helpers


def jload(p: Path):
    return json.loads(p.read_text())


def jdump(p: Path, d) -> None:
    # allow_nan so we can inject NaN/Infinity literals on purpose
    p.write_text(json.dumps(d, indent=2, allow_nan=True))


def walk(obj, fn: Callable[[dict], None]) -> None:
    if isinstance(obj, dict):
        fn(obj)
        for v in obj.values():
            walk(v, fn)
    elif isinstance(obj, list):
        for v in obj:
            walk(v, fn)


def metric_files(ws: Path) -> list[Path]:
    return sorted(ws.glob("*/analysis/qor_metrics.json"))


def set_metric(ws: Path, metric_id: str, value: Any) -> int:
    """Set every record whose id/metric_id == metric_id. Returns #records hit."""
    hits = 0
    for f in metric_files(ws):
        d = jload(f)
        n = 0

        def fn(rec):
            nonlocal n
            if rec.get("id") == metric_id or rec.get("metric_id") == metric_id or rec.get("name") == metric_id:
                if "value" in rec:
                    rec["value"] = value
                    n += 1

        walk(d, fn)
        if n:
            jdump(f, d)
            hits += n
    return hits


def scale_metric(ws: Path, metric_id: str, factor: float) -> int:
    hits = 0
    for f in metric_files(ws):
        d = jload(f)
        n = 0

        def fn(rec):
            nonlocal n
            if (rec.get("id") == metric_id or rec.get("metric_id") == metric_id) and isinstance(rec.get("value"), (int, float)):
                rec["value"] = rec["value"] * factor
                n += 1

        walk(d, fn)
        if n:
            jdump(f, d)
            hits += n
    return hits


def get_metric(ws: Path, metric_id: str):
    out = []
    for f in metric_files(ws):
        walk(jload(f), lambda r: out.append(r.get("value")) if (r.get("id") == metric_id or r.get("metric_id") == metric_id) and "value" in r else None)
    return out


def step_dir(ws: Path, prefix: str) -> Path | None:
    c = sorted(p for p in ws.iterdir() if p.is_dir() and p.name.lower().startswith(prefix.lower()))
    return c[0] if c else None


def set_step_state(ws: Path, step_name_sub: str, state: str) -> int:
    f = ws / "home" / "flow.json"
    d = jload(f)
    n = 0
    for s in d.get("steps", []):
        if step_name_sub.lower() in str(s.get("name", "")).lower():
            s["state"] = state
            n += 1
    jdump(f, d)
    return n


def set_param(ws: Path, key: str, raw_value: str) -> None:
    f = ws / "home" / "params.toml"
    txt = f.read_text() if f.exists() else ""
    if "[params]" not in txt:
        txt += "\n[params]\n"
    txt = re.sub(rf"(?m)^{re.escape(key)}\s*=.*\n?", "", txt)
    txt = txt.replace("[params]", f"[params]\n{key} = {raw_value}", 1)
    f.write_text(txt)


# ----------------------------------------------------------------- model


def parse_plain(text: str) -> dict:
    """Parse `ecc report qor --plain` key=value output (values may be quoted)."""
    import ast
    out = {}
    for m in re.finditer(r'(\w+)=("([^"]*)"|\S+)', text):
        k, v = m.group(1), m.group(3) if m.group(3) is not None else m.group(2)
        out[k] = v
    if "dimensions" in out:
        try:
            out["dimensions"] = {d["dimension"]: d for d in ast.literal_eval(out["dimensions"])}
        except Exception:
            out["dimensions"] = {}
    if "overall_score" in out:
        try:
            out["overall_score"] = None if out["overall_score"] in ("None", "null", "") else float(out["overall_score"])
        except ValueError:
            pass
    return out


@dataclass
class Outcome:
    rc: int
    stdout: str
    stderr: str
    report: dict | None
    plain: dict = field(default_factory=dict)

    @property
    def crashed(self) -> bool:
        return self.rc != 0 or "Traceback" in self.stderr or "Traceback" in self.stdout

    @property
    def score(self):
        return self.plain.get("overall_score")

    @property
    def status(self):
        return self.plain.get("qor_status")

    @property
    def feas(self):
        return self.plain.get("gate_status")

    def dim(self, key):
        d = (self.plain.get("dimensions") or {}).get(key)
        return d.get("score") if isinstance(d, dict) else None

    def brief(self) -> str:
        if self.crashed:
            tail = (self.stderr or self.stdout).strip().splitlines()[-1:] or [""]
            return f"CRASH rc={self.rc} {tail[0][:80]}"
        return f"feas={self.feas} status={self.status} score={self.score}"


@dataclass
class Fault:
    fid: str
    category: str
    desc: str
    mutate: Callable[[Path], Any]
    expect: str  # human-readable expectation
    oracle: Callable[[Outcome, Outcome], bool]  # (baseline, faulted) -> ok
    kind: str = "check"  # check | gap


@dataclass
class Result:
    fault: str
    category: str
    expect: str
    got: str
    verdict: str  # PASS | FAIL | SKIP | GAP-OPEN | GAP-CLOSED
    detail: str = ""


# ----------------------------------------------------------------- oracles


def no_crash(_b: Outcome, f: Outcome) -> bool:
    return not f.crashed


def graceful_not_rated(_b, f) -> bool:
    return not f.crashed and f.status in ("NOT_RATED", "FAIL") and f.feas in ("UNKNOWN", "NOT_VERIFIED", "PHYSICAL_FAIL")


def not_pass(_b, f) -> bool:
    """Weakest acceptable outcome: no crash and the design is NOT reported as clean."""
    return not f.crashed and f.feas != "PASS"


def physical_fail(_b, f) -> bool:
    return not f.crashed and f.feas == "PHYSICAL_FAIL" and f.score == 0.0


def not_better(key: str | None = None):
    def o(b: Outcome, f: Outcome) -> bool:
        if f.crashed:
            return False
        bv = b.dim(key) if key else b.score
        fv = f.dim(key) if key else f.score
        if bv is None or fv is None:
            return fv is None or bv is None
        return fv <= bv + 1e-9

    return o


def strictly_worse(key: str):
    def o(b: Outcome, f: Outcome) -> bool:
        bv, fv = b.dim(key), f.dim(key)
        return not f.crashed and bv is not None and fv is not None and fv < bv

    return o


def in_range(_b, f) -> bool:
    """Scores must stay inside [0, 100] (or be null)."""
    if f.crashed:
        return False
    vals = [f.score] + [d.get("score") for d in (f.plain.get("dimensions") or {}).values()]
    return all(v is None or 0.0 <= v <= 100.0 for v in vals)


def not_better_nor_crash(b, f) -> bool:
    """Removing/invalidating evidence must never raise the composite."""
    if f.crashed:
        return False
    if f.score is None or b.score is None:
        return True
    return f.score <= b.score + 1e-9


def flagged(b, f) -> bool:
    """Physically impossible input must be rejected (UNKNOWN/NOT_RATED), not scored."""
    return not f.crashed and (f.status == "NOT_RATED" or f.feas in ("UNKNOWN",))


def unchanged(b: Outcome, f: Outcome) -> bool:
    return not f.crashed and f.score == b.score and f.feas == b.feas


# ----------------------------------------------------------------- catalogue


def _antenna_violations(ws: Path):
    for p in ws.glob("*/feature/antennaCheck.step.json"):
        d = jload(p)
        hit = []
        walk(d, lambda r: (r.__setitem__("violation_count", 37), hit.append(1)) if "violation_count" in r else None)
        if not hit:
            d["violation_count"] = 37
        jdump(p, d)
        return 1
    return 0


def _truncate(p: Path | None):
    if p and p.exists():
        b = p.read_bytes()
        p.write_bytes(b[: len(b) // 2])
        return 1
    return 0


def _first_metric_file(ws: Path, stepprefix: str) -> Path | None:
    s = step_dir(ws, stepprefix)
    p = s / "analysis" / "qor_metrics.json" if s else None
    return p if p and p.exists() else None


def _del(p: Path | None):
    if p and p.exists():
        p.unlink()
        return 1
    return 0


def _set_json(ws: Path, pattern: str, key: str, value):
    n = 0
    for p in ws.glob(pattern):
        d = jload(p)
        hit = []
        walk(d, lambda r: (r.__setitem__(key, value), hit.append(1)) if key in r else None)
        if hit:
            jdump(p, d)
            n += 1
    return n


def _corrupt_corner(ws: Path, nvp, wns, delete_other=False):
    files = sorted(ws.glob("sta*/feature/*/*/qor_summary.json"))
    if not files:
        return 0
    target = next((f for f in files if "MAX" in str(f)), files[0])
    d = jload(target)
    n = []

    def fn(r):
        if isinstance(r.get("setup"), dict):
            r["setup"]["nvp"] = nvp
            r["setup"]["wns"] = wns
            n.append(1)
    walk(d, fn)
    jdump(target, d)
    if delete_other:
        for f in files:
            if f != target:
                f.unlink()
                break
    return len(n)


def _dup_conflict(ws: Path):
    """Append a duplicate drc_count record with a conflicting value."""
    for f in metric_files(ws):
        d = jload(f)
        found = []
        walk(d, lambda r: found.append(r) if r.get("id") == "drc_count" or r.get("metric_id") == "drc_count" else None)
        if found:
            rec = dict(found[0])
            rec["value"] = 999
            for k in ("metrics", "records"):
                if isinstance(d.get(k), list):
                    d[k].append(rec)
                    jdump(f, d)
                    return 1
    return 0


ARTIFACT_FAULTS: list[Fault] = [
    # --- missing / corrupt evidence
    Fault("A01", "evidence", "delete DRC step qor_metrics.json",
          lambda ws: _del(_first_metric_file(ws, "drc")), "feasibility UNKNOWN, score null, no crash", graceful_not_rated),
    Fault("A02", "evidence", "truncate STA qor_metrics.json to half",
          lambda ws: _truncate(_first_metric_file(ws, "sta")), "no crash; UNKNOWN/NOT_RATED", graceful_not_rated),
    Fault("A03", "evidence", "empty (0-byte) home/flow.json",
          lambda ws: ((ws / "home" / "flow.json").write_text(""), 1)[1], "clean error message, no traceback", no_crash),
    Fault("A04", "evidence", "flow.json is valid JSON but wrong type ([])",
          lambda ws: (ws / "home" / "flow.json").write_text("[]"), "clean error, no traceback", no_crash),
    Fault("A05", "evidence", "delete all per-corner STA qor_summary.json",
          lambda ws: sum(_del(p) for p in ws.glob("sta*/feature/*/*/qor_summary.json")), "evidence downgraded, score must not rise", not_better_nor_crash),
    Fault("A06", "evidence", "qor_metrics.json schema_version = 99",
          lambda ws: [jdump(f, {**jload(f), "schema_version": 99}) for f in metric_files(ws)], "rejected as UNKNOWN, not silently scored", graceful_not_rated),
    # --- adversarial values
    Fault("V01", "value", "drc_count = NaN", lambda ws: set_metric(ws, "drc_count", float("nan")), "UNKNOWN (not PASS), no crash", graceful_not_rated),
    Fault("V02", "value", "drc_count = \"abc\"", lambda ws: set_metric(ws, "drc_count", "abc"), "UNKNOWN, no crash", graceful_not_rated),
    Fault("V03", "value", "drc_count = -1", lambda ws: set_metric(ws, "drc_count", -1), "UNKNOWN/invalid, never PASS", graceful_not_rated),
    Fault("V04", "value", "drc_count = true (bool)", lambda ws: set_metric(ws, "drc_count", True), "UNKNOWN, never counted as 1 or 0", graceful_not_rated),
    Fault("V05", "value", "sta_setup_wns = +Infinity", lambda ws: set_metric(ws, "sta_setup_wns", float("inf")), "UNKNOWN, no crash (psi raises on non-finite)", no_crash),
    Fault("V06", "value", "sta_setup_wns = NaN", lambda ws: set_metric(ws, "sta_setup_wns", float("nan")), "UNKNOWN, no crash", no_crash),
    Fault("V07", "value", "core_utilization = 52 (percent, not fraction)", lambda ws: set_metric(ws, "core_utilization", 52.0), "rejected as out-of-range (unit error), not scored", flagged),
    Fault("V08", "value", "place_hpwl = 0 (zero denominator)", lambda ws: set_metric(ws, "place_hpwl", 0), "I_place UNKNOWN; score must not rise", not_better_nor_crash),
    Fault("V09", "value", "duplicate drc_count record with value 999", _dup_conflict, "conflict detected or worst value wins (FAIL)", physical_fail),
    # --- semantic: must flip feasibility
    Fault("S01", "semantic", "drc_count = 5", lambda ws: set_metric(ws, "drc_count", 5), "PHYSICAL_FAIL, score 0", physical_fail),
    Fault("S02", "semantic", "lvs_count = 1", lambda ws: set_metric(ws, "lvs_count", 1), "PHYSICAL_FAIL, score 0", physical_fail),
    Fault("S03", "semantic", "sta_setup_wns = -0.001 ns", lambda ws: set_metric(ws, "sta_setup_wns", -0.001), "PHYSICAL_FAIL", physical_fail),
    Fault("S04", "semantic", "sta_hold_wns = -0.001 ns", lambda ws: set_metric(ws, "sta_hold_wns", -0.001), "PHYSICAL_FAIL", physical_fail),
    Fault("S05", "semantic", "harden_artifact_missing_count = 1", lambda ws: set_metric(ws, "harden_artifact_missing_count", 1), "PHYSICAL_FAIL", physical_fail),
    Fault("S06", "semantic", "flow.json: DRC step state -> Failed", lambda ws: set_step_state(ws, "drc", "Failed"), "NOT_VERIFIED, score null", graceful_not_rated),
    # --- metamorphic: degradation must not improve score
    Fault("M01", "metamorphic", "route_wirelength x2", lambda ws: scale_metric(ws, "route_wirelength", 2.0), "Q_I not better", not_better("interconnect")),
    Fault("G03", "gap", "route_wirelength x2 (routed WL doubles)", lambda ws: scale_metric(ws, "route_wirelength", 2.0),
          "SHOULD lower Q_I; today invisible (I_place degraded path)", lambda b, f: f.score == b.score, kind="gap"),
    Fault("M02", "metamorphic", "place_grwl x1.6", lambda ws: scale_metric(ws, "place_grwl", 1.6), "Q_I strictly worse", strictly_worse("interconnect")),
    Fault("M03", "metamorphic", "place_congestion_egr_overflow_total += 1000", lambda ws: set_metric(ws, "place_congestion_egr_overflow_total", 1000), "Q_I not better", not_better("interconnect")),
    Fault("M04", "metamorphic", "sta_setup_tns x100 (WS unchanged)", lambda ws: scale_metric(ws, "sta_setup_tns", 100.0), "score not better (ideally worse: TNS ignored)", not_better()),
    Fault("M05", "metamorphic", "core_utilization = 0.95", lambda ws: set_metric(ws, "core_utilization", 0.95), "Q_A strictly worse", strictly_worse("area")),
    # --- config
    Fault("C01", "config", "qor_profile = \"bogus\"", lambda ws: set_param(ws, "qor_profile", '"bogus"'), "CONFIG WARNING, falls back to balanced", unchanged),
    Fault("C02", "config", "qor_power_budget_w = -1", lambda ws: set_param(ws, "qor_power_budget_w", "-1"), "warning, Q_P null, no crash", no_crash),
    Fault("C03", "config", "qor_power_budget_w = \"abc\"", lambda ws: set_param(ws, "qor_power_budget_w", '"abc"'), "warning, Q_P null, no crash", no_crash),
    Fault("C04", "config", "qor_power_budget_w = 1e-12 W", lambda ws: set_param(ws, "qor_power_budget_w", "1e-12"), "Q_P = 0, no crash", no_crash),
    Fault("C05", "config", "params.toml is not valid TOML", lambda ws: (ws / "home" / "params.toml").write_text("[params\nqor_profile = "), "clean error / default, no traceback", no_crash),
    # --- from static review (fail-open hypotheses)
    Fault("L01", "fail-open", "delete lvs_ecc/feature/*step.json (LVS evidence gone)",
          lambda ws: sum(_del(p) for p in ws.glob("lvs_ecc/feature/*step.json")), "LVS gate NOT pass", not_pass),
    Fault("L02", "fail-open", "lvs step.json violations = null",
          lambda ws: _set_json(ws, "lvs_ecc/feature/*step.json", "violations", None), "LVS gate NOT pass", not_pass),
    Fault("L03", "fail-open", "delete drc_ecc/feature step json",
          lambda ws: sum(_del(p) for p in ws.glob("drc_ecc/feature/*step.json")), "DRC gate NOT pass", not_pass),
    Fault("T01", "fail-open", "worst setup corner qor_summary: setup.nvp=3.0 (float), wns=-0.5",
          lambda ws: _corrupt_corner(ws, nvp=3.0, wns=-0.5), "setup gate FAIL or UNKNOWN, never PASS", not_pass),
    Fault("T02", "fail-open", "one STA corner qor_summary deleted + another set wns=-0.5",
          lambda ws: _corrupt_corner(ws, nvp=3, wns=-0.5, delete_other=True), "not PASS", not_pass),
    Fault("C06", "config", "qor_power_budget_w = inf", lambda ws: set_param(ws, "qor_power_budget_w", "inf"), "warning, no crash", no_crash),
    Fault("C07", "config", "frequency_max = \"fast\" (string)", lambda ws: set_param(ws, "frequency_max", '"fast"'), "warning, Q_T UNKNOWN, no crash", no_crash),
    Fault("V10", "value", "clock_path_min_buffer > clock_path_max_buffer", lambda ws: set_metric(ws, "clock_path_min_buffer", 50), "all scores in [0,100]", in_range),
    Fault("V11", "value", "core_utilization = -0.3", lambda ws: set_metric(ws, "core_utilization", -0.3), "rejected (impossible value)", flagged),
    Fault("K01", "fail-open", "home/checklist.json truncated", lambda ws: _truncate(ws / "home" / "checklist.json"), "no crash; not silently 'ready'", no_crash),
    # --- known gaps (verdict GAP-OPEN means the weakness is still present)
    Fault("G01", "gap", "antennaCheck violation_count = 37", _antenna_violations,
          "SHOULD block signoff; today feasibility stays PASS", lambda b, f: f.feas == b.feas and f.score == b.score, kind="gap"),
    Fault("G02", "gap", "sta_hold_tns x100 with hold WS unchanged", lambda ws: scale_metric(ws, "sta_hold_tns", 100.0),
          "SHOULD lower score; today unchanged", lambda b, f: f.score == b.score, kind="gap"),
]


# ----------------------------------------------------------------- runner


# QoR analysis reads analysis/, feature/, home/, config/ — not layout databases.
_IGNORE = shutil.ignore_patterns("*.gds", "*.def.gz", "*.v.gz", "*.png", "*.spef*", "data", "geometry", "origin")


def relocate(project: Path) -> None:
    """ECC projects are not relocatable: project.json pins absolute root/workspace paths
    (manifest_invalid otherwise). Rewrite them for a copied project."""
    pj = project / "project.json"
    d = jload(pj)
    old = d.get("root_path")
    if old and old != str(project):
        pj.write_text(pj.read_text().replace(old, str(project)))


def analyse(project: Path, ecc: str, env: dict | None) -> Outcome:
    r = subprocess.run(
        [ecc, "report", "qor", "--project", str(project), "--workspace", "default", "--plain"],
        capture_output=True, text=True, env=env, timeout=600,
    )
    # NB: `ecc report qor` does NOT rewrite home/qor_report.json; use --plain output.
    return Outcome(r.returncode, r.stdout, r.stderr, None, parse_plain(r.stdout))


def run_suite(project: Path, ecc: str = "ecc", env: dict | None = None,
              faults: Iterable[Fault] = ARTIFACT_FAULTS, scratch: Path | None = None) -> Iterable[Result]:
    project = Path(project).resolve()
    scratch = Path(scratch or tempfile.mkdtemp(prefix="ecc-fi-")).resolve()
    base_dir = scratch / "baseline" / project.name
    if base_dir.exists():
        shutil.rmtree(base_dir)
    shutil.copytree(project, base_dir, symlinks=True, ignore=_IGNORE)
    relocate(base_dir)
    base = analyse(base_dir, ecc, env)
    yield Result("BASE", "baseline", "no crash", base.brief(), "FAIL" if base.crashed else "PASS")
    for fault in faults:
        d = scratch / fault.fid / project.name
        if d.exists():
            shutil.rmtree(d)
        shutil.copytree(base_dir, d, symlinks=True)
        relocate(d)
        ws = d / "default"
        try:
            hit = fault.mutate(ws)
        except Exception as e:  # mutation itself failed (artifact absent)
            yield Result(fault.fid, fault.category, fault.expect, f"mutation error: {e}", "SKIP")
            continue
        if hit in (0, None) and not isinstance(hit, list) and fault.category in ("value", "semantic", "metamorphic", "evidence"):
            if hit == 0:
                yield Result(fault.fid, fault.category, fault.expect, "target not present", "SKIP", fault.desc)
                continue
        out = analyse(d, ecc, env)
        ok = fault.oracle(base, out)
        if fault.kind == "gap":
            verdict = "GAP-OPEN" if ok else "GAP-CLOSED"
        else:
            verdict = "PASS" if ok else "FAIL"
        yield Result(fault.fid, fault.category, fault.expect, out.brief(), verdict, fault.desc)
        if verdict in ("PASS", "GAP-OPEN", "GAP-CLOSED"):
            shutil.rmtree(d.parent, ignore_errors=True)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("project", type=Path)
    ap.add_argument("--ecc", default=shutil.which("ecc") or "ecc")
    ap.add_argument("--json", type=Path)
    ap.add_argument("--scratch", type=Path)
    ap.add_argument("--only", nargs="*")
    a = ap.parse_args()
    faults = [f for f in ARTIFACT_FAULTS if not a.only or f.fid in a.only]
    results = []
    for r in run_suite(a.project, a.ecc, dict(os.environ), faults, a.scratch):
        results.append(r)
        print(f"{r.verdict:<10} {r.fault:<5} {r.category:<11} {r.got[:70]:<70} | {r.detail}")
    if a.json:
        a.json.write_text(json.dumps([r.__dict__ for r in results], indent=2))
    bad = [r for r in results if r.verdict == "FAIL"]
    print(f"\n{len(results)} cases, {len(bad)} FAIL, {sum(r.verdict=='SKIP' for r in results)} SKIP, "
          f"{sum(r.verdict=='GAP-OPEN' for r in results)} GAP-OPEN")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
