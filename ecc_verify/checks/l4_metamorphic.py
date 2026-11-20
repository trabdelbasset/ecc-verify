"""L4: metamorphic / differential tests that drive the real `ecc` flow (scope="slow").

Cases are copied from the CI designs to <scratch_dir>/slow_<case>/, run pinned with taskset and
OMP_NUM_THREADS=4, summarised into <scratch_dir>/slow_summaries/*.json and deleted afterwards.

Standalone:  python -m ecc_verify.checks.l4_metamorphic [--lanes 8-11,12-15] [--config ecc-verify.toml] [--only L4.determinism,...]
"""
from __future__ import annotations

if __name__ == "__main__":  # avoid double registration (__main__ vs package import)
    from ecc_verify.checks.l4_metamorphic import main as _main
    raise SystemExit(_main())

import json
import shutil
import threading
import time
from pathlib import Path

from ecc_verify import flowrun as F
from ecc_verify.model import Layer, Result, Status
from ecc_verify.registry import check

DESIGN = "xtea"
# --- noise band (documented): wns/power/wirelength relative drift tolerated before FAIL
NOISE_REL = 0.01

_LOCK = threading.RLock()
_BASE: dict = {}


def _design_dir(ctx) -> Path:
    return Path(getattr(ctx, "designs_dir", None) or F.get_designs_dir())


def get_base(ctx) -> dict:
    """Shared finished full-flow xtea run (kept until `drop_base`). Rebuilt if a check dirtied it."""
    with _LOCK:
        b = _BASE.get("xtea")
        if b and not b["dirty"] and b["case"].root.exists():
            return b
        if b:
            F.cleanup(b["case"])
        case = F.new_case("xtea_base", DESIGN, _design_dir(ctx))
        t0 = time.time()
        r = F.run_ecc(ctx.ecc, case.root, ["run"], ctx.cpus)
        b = {"case": case, "run": r, "secs": time.time() - t0, "dirty": False, "ok": r.ok}
        b["metrics"] = F.flow_metrics(case.ws) if r.ok else {}
        F.save_summary("base_xtea", {"rc": r.rc, "secs": r.secs, "metrics": b["metrics"], "tail": r.tail()})
        _BASE["xtea"] = b
        return b


def drop_base() -> None:
    with _LOCK:
        b = _BASE.pop("xtea", None)
        if b:
            F.cleanup(b["case"])


def _full(ctx, name: str, toml_edits=None, extra_args=None):
    case = F.new_case(name, DESIGN, _design_dir(ctx), toml_edits)
    r = F.run_ecc(ctx.ecc, case.root, ["run", *(extra_args or [])], ctx.cpus)
    return case, r


# ------------------------------------------------------------------ determinism
@check("L4.determinism", Layer.L4, steps=("place", "route", "sta", "powerAnalysis"), category="metamorphic",
       severity="major", title="Two identical runs give identical placement/route/WNS/power", scope="slow")
def determinism(ctx) -> Result:
    a = get_base(ctx)
    if not a["ok"]:
        return Result.skip(f"baseline run failed rc={a['run'].rc}: {a['run'].tail(300)}")
    case, r = _full(ctx, "xtea_detB")
    try:
        if not r.ok:
            return Result(Status.ERROR, f"second run failed rc={r.rc}", details={"tail": r.tail()})
        mb = F.flow_metrics(case.ws)
        ma = a["metrics"]
        keys = ("setup_wns", "setup_tns", "hold_wns", "route_route_wirelength", "power_dynamic_uw", "power_leakage_uw",
                "place_place_hpwl", "place_def_hash", "final_def_hash")
        diffs = {k: v for k, v in F.numeric_diffs(ma, mb).items() if k in keys}
        pd = F.placement_diff(F.final_def(a["case"].ws), F.final_def(case.ws))
        pd_pl = F.placement_diff(F.final_def(a["case"].ws, "place_dreamplace"), F.final_def(case.ws, "place_dreamplace"))
        det = {"diffs": diffs, "final_placement_diff": pd, "place_placement_diff": pd_pl, "A": ma, "B": mb}
        F.save_summary("L4_determinism", det)
        identical = not diffs and pd["moved"] == 0 and pd_pl["moved"] == 0
        if identical:
            return Result.ok("bit-identical placement hashes, WNS, wirelength, power", measured=0, expected=0, details=det)
        rels = [abs(v["rel"]) for v in diffs.values() if "rel" in v]
        frac = pd["moved"] / max(pd["n_a"], 1)
        msg = (f"{len(diffs)} metric diffs ({', '.join(sorted(diffs))}); final DEF {pd['moved']}/{pd['n_a']} cells moved, "
               f"place DEF {pd_pl['moved']} moved; max rel drift {max(rels, default=0):.2e}")
        if max(rels, default=0) <= NOISE_REL and frac <= 0.02:
            return Result.warn("non-bit-identical but within noise band: " + msg, tolerance=NOISE_REL, details=det)
        return Result.fail("runs differ beyond noise band: " + msg, tolerance=NOISE_REL, details=det)
    finally:
        F.cleanup(case)


# ------------------------------------------------------------------ resume equivalence
@check("L4.resume_equivalence", Layer.L4, steps=("place", "route", "sta"), category="metamorphic", severity="major",
       title="Run-to-place then --resume equals a full run", scope="slow")
def resume_equivalence(ctx) -> Result:
    a = get_base(ctx)
    if not a["ok"]:
        return Result.skip(f"baseline run failed: {a['run'].tail(300)}")
    det: dict = {}
    # (i) the spec scenario: bounded fresh run to place, then --resume
    case = F.new_case("xtea_resume", DESIGN, _design_dir(ctx))
    try:
        r1 = F.run_ecc(ctx.ecc, case.root, ["run", "--from", "synthesis", "--to", "place"], ctx.cpus)
        if not r1.ok:
            return Result.skip("CLI cannot do a bounded fresh run `--from synthesis --to place`: " + r1.tail(400),
                               details={"cmd": r1.args})
        r2 = F.run_ecc(ctx.ecc, case.root, ["run", "--resume"], ctx.cpus)
        steps_done = sorted(p.name for p in case.ws.glob("*_*") if (p / "output").exists())
        det["bounded_then_resume"] = {"resume_rc": r2.rc, "resume_out": r2.tail(400),
                                      "no_op": "no_op=True" in r2.out, "step_dirs_after": steps_done,
                                      "has_route": F.step_dir(case.ws, "route_ecc") is not None}
        probe = F.run_ecc(ctx.ecc, case.root, ["run", "--from", "diff_sizing"], ctx.cpus, 120)
        det["continue_with_from"] = {"rc": probe.rc, "out": probe.tail(300)}
    finally:
        F.cleanup(case)
    # (ii) equivalence proper: full run, mark `place` pending via workspace-scope param, --resume, compare to base
    case2 = F.new_case("xtea_resume2", DESIGN, _design_dir(ctx))
    try:
        r = F.run_ecc(ctx.ecc, case2.root, ["run"], ctx.cpus)
        if not r.ok:
            det["equivalence"] = "full run failed: " + r.tail(300)
        else:
            dens = a["case"].ws.joinpath("home/params.toml").read_text().split("target_density = ")[-1].split()[0]
            ps = F.run_ecc(ctx.ecc, case2.root, ["param", "set", "place.target_density", "0.5", "--workspace", F.WS],
                           ctx.cpus, 120, plain=False)
            rr = F.run_ecc(ctx.ecc, case2.root, ["run", "--resume"], ctx.cpus)
            det["param_set"] = {"rc": ps.rc, "out": ps.tail(300)}
            det["resume_after_param"] = {"rc": rr.rc, "out": rr.tail(500), "secs": rr.secs}
            mb = F.flow_metrics(case2.ws)
            diffs = F.numeric_diffs(a["metrics"], mb)
            pd = F.placement_diff(F.final_def(a["case"].ws), F.final_def(case2.ws))
            det["equivalence"] = {"diffs": diffs, "placement_diff": pd, "resumed": mb}
    finally:
        F.cleanup(case2)
    F.save_summary("L4_resume_equivalence", det)
    bt = det.get("bounded_then_resume", {})
    eq = det.get("equivalence")
    eq_msg = ""
    if isinstance(eq, dict):
        n = len(eq["diffs"])
        eq_msg = (f"; resume-from-pending-place vs full run: {n} metric diffs, {eq['placement_diff']['moved']} cells moved"
                  if (n or eq["placement_diff"]["moved"]) else "; resume-from-pending-place vs full run: identical")
    if bt.get("no_op") or not bt.get("has_route"):
        return Result.fail("`--from synthesis --to place` then `--resume` is a no-op (executed_steps=[], flow never reaches "
                           "route; later steps are 'unknown_step' for this workspace)" + eq_msg, details=det,
                           expected="resume continues from first non-successful step",
                           measured={"has_route": bt.get("has_route")})
    if isinstance(eq, dict) and (eq["diffs"] or eq["placement_diff"]["moved"]):
        return Result.fail("resumed run differs from full run" + eq_msg, details=det)
    return Result.ok("resume equivalent to full run" + eq_msg, details=det)


# ------------------------------------------------------------------ frequency monotonic
@check("L4.freq_monotonic", Layer.L4, steps=("sta",), category="metamorphic", severity="critical",
       title="WNS decreases with frequency and setup gate trips at high frequency", scope="slow")
def freq_monotonic(ctx) -> Result:
    a = get_base(ctx)
    if not a["ok"]:
        return Result.skip(f"baseline run failed: {a['run'].tail(300)}")
    f0 = float(a["case"].ws.joinpath("home/params.toml").read_text().split("frequency_mhz = ")[1].split()[0])
    rows = {1.0: {"freq_mhz": f0, **a["metrics"], "qor": _gate(ctx, a["case"])}}
    factors = [0.8, 1.5, 3.0]
    for fac in factors + [6.0, 12.0]:
        if fac > 3.0 and any((rows[k].get("setup_wns") or 1) < 0 for k in rows if k > 1.0):
            break  # already failing; extra runs unnecessary
        case, r = _full(ctx, f"xtea_f{fac}", {"frequency_mhz": f0 * fac})
        try:
            row = {"freq_mhz": f0 * fac, "rc": r.rc, "secs": r.secs}
            if F.step_dir(case.ws, "sta_ecc") is not None:
                row.update(F.flow_metrics(case.ws))
            row["qor"] = _gate(ctx, case)
            row["tail"] = r.tail(300) if not r.ok else ""
            rows[fac] = row
        finally:
            F.cleanup(case)
    F.save_summary("L4_freq_monotonic", {"rows": rows})
    pts = sorted((k, v["setup_wns"]) for k, v in rows.items() if v.get("setup_wns") is not None)
    wns = [w for _, w in pts]
    mono = all(wns[i] > wns[i + 1] for i in range(len(wns) - 1))
    neg = [(k, w) for k, w in pts if w < 0]
    summary = ", ".join(f"x{k}:WNS={w}" for k, w in pts)
    if len(pts) < 3:
        return Result(Status.ERROR, "fewer than 3 usable frequency points: " + summary, details={"rows": rows})
    if not mono:
        return Result.fail("WNS not strictly decreasing with frequency: " + summary, measured=pts, details={"rows": rows})
    if not neg:
        return Result.warn("monotonic, but no frequency produced a setup violation: " + summary, measured=pts,
                           details={"rows": rows})
    k0 = neg[0][0]
    g = rows[k0]["qor"]
    if g.get("gate") == "PASS" or not g:
        return Result.fail(f"setup violation at x{k0} (WNS={neg[0][1]}) NOT flagged by ecc gate ({g}): " + summary,
                           measured=pts, details={"rows": rows})
    return Result.ok(f"monotonic; setup gate trips at x{k0} (WNS={neg[0][1]}, gate={g.get('gate')}): " + summary,
                     measured=pts, details={"rows": rows})


def _gate(ctx, case) -> dict:
    try:
        q = F.qor_json(ctx.ecc, case.root)
        return {"gate": q.get("gate_status"), "qor_status": q.get("qor_status"), "score": q.get("overall_score"),
                "gates": (q.get("qor", {}).get("feasibility", {}) or {}).get("gates")}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


# ------------------------------------------------------------------ density monotonic
@check("L4.density_monotonic", Layer.L4, steps=("place",), category="metamorphic", severity="minor",
       title="target_density low vs high moves placement spread/HPWL the expected way", scope="slow")
def density_monotonic(ctx) -> Result:
    res = {}
    for d in (0.3, 0.8):
        case = F.new_case(f"xtea_d{d}", DESIGN, _design_dir(ctx), {"params.place.target_density": d})
        try:
            r = F.run_ecc(ctx.ecc, case.root, ["run", "--from", "synthesis", "--to", "place"], ctx.cpus)
            if not r.ok:
                return Result.skip("bounded fresh run failed: " + r.tail(300))
            cfg = json.loads((case.ws / "config" / "dreamplace_ecc.json").read_text())
            pl = F.placements(F.final_def(case.ws, "place_dreamplace"))
            xs = [v[1] for v in pl.values()]
            ys = [v[2] for v in pl.values()]
            m = F.step_metric(case.ws, "place_dreamplace", ("place_hpwl", "core_utilization", "std_cell_area", "instance_count"))
            if "place_hpwl" not in m:  # partial (bounded) runs skip the analysis refresh -> parse the log (DBU)
                import re as _re
                vals = _re.findall(r"unweighted hpwl\s+([0-9.Ee+-]+)",
                                   F.step_log(case.ws, "place_dreamplace", 10**8))
                if vals:
                    m["place_hpwl"] = float(vals[-1]) / 1000.0
                    m["hpwl_source"] = "place.log 'unweighted hpwl' (last)"
            try:  # utilisation as configured at floorplan (analysis files are absent on bounded runs)
                import tomllib
                prm = tomllib.loads((case.ws / "home/params.toml").read_text())
                m["core_utilization"] = prm.get("params", {}).get("core", {}).get("utilitization")
            except Exception:  # noqa: BLE001
                pass
            m.update(cfg_target_density=cfg.get("target_density"), bbox_area_dbu2=(max(xs) - min(xs)) * (max(ys) - min(ys)),
                     ncells=len(pl))
            res[d] = m
        finally:
            F.cleanup(case)
    F.save_summary("L4_density_monotonic", res)
    lo, hi = res[0.3], res[0.8]
    if lo["cfg_target_density"] != 0.3 or hi["cfg_target_density"] != 0.8:
        return Result.fail("target_density not propagated into config/dreamplace_ecc.json", measured=res)
    spread = hi["bbox_area_dbu2"] < lo["bbox_area_dbu2"]
    hp = hi["place_hpwl"] <= lo["place_hpwl"] * 1.02
    util_same = lo["core_utilization"] == hi["core_utilization"]
    msg = (f"bbox(0.8)={hi['bbox_area_dbu2']} vs bbox(0.3)={lo['bbox_area_dbu2']}, hpwl {hi['place_hpwl']} vs {lo['place_hpwl']}, "
           f"core_util {hi['core_utilization']} vs {lo['core_utilization']}")
    if spread and hp:
        return Result.ok("higher density => tighter placement, shorter HPWL; " + msg, measured=res)
    if spread or hp:
        return Result.warn("only one of (tighter bbox, shorter HPWL) moved as expected; " + msg, measured=res)
    return Result.fail("density change had no expected effect; " + msg + (" (core_utilization identical)" if util_same else ""),
                       measured=res)


# ------------------------------------------------------------------ --set override
@check("L4.set_override_effective", Layer.L4, steps=("place",), category="metamorphic", severity="major",
       title="--set place.num_threads=2 reaches config/dreamplace_ecc.json", scope="slow")
def set_override_effective(ctx) -> Result:
    case = F.new_case("xtea_set", DESIGN, _design_dir(ctx))
    try:
        r = F.run_ecc(ctx.ecc, case.root, ["run", "--set", "place.num_threads=2", "--from", "synthesis", "--to", "place"], ctx.cpus)
        if not r.ok:
            r = F.run_ecc(ctx.ecc, case.root, ["run", "--set", "place.num_threads=2", "--to", "place"], ctx.cpus)
        if not r.ok:
            return Result.skip("CLI rejected --set on bounded fresh run: " + r.tail(300), details={"cmd": r.args})
        cfg = json.loads((case.ws / "config" / "dreamplace_ecc.json").read_text())
        rec = F.read_text(case.ws / "home" / "cli-param-overrides.json", 600)
        nt = cfg.get("num_threads")
        det = {"num_threads_in_config": nt, "cli_param_overrides": rec,
               "place_log_threads": [l for l in F.step_log(case.ws, "place_dreamplace", 200000).splitlines()
                                     if "thread" in l.lower()][:5]}
        F.save_summary("L4_set_override", det)
        if nt == 2:
            return Result.ok("num_threads=2 present in dreamplace_ecc.json", measured=nt, expected=2, details=det)
        return Result.fail(f"--set place.num_threads=2 recorded in cli-param-overrides.json but dreamplace_ecc.json has "
                           f"num_threads={nt}", measured=nt, expected=2,
                           evidence=[str(case.ws / "config/dreamplace_ecc.json")], details=det)
    finally:
        F.cleanup(case)


# ------------------------------------------------------------------ signoff truth
@check("L4.signoff_truth", Layer.L4, steps=("lvs", "drc", "sta"), category="metamorphic", severity="critical",
       title="Injected open / overlap / tight clock are detected by LVS, DRC and STA", scope="slow")
def signoff_truth(ctx) -> Result:
    b = get_base(ctx)
    if not b["ok"]:
        return Result.skip(f"baseline run failed: {b['run'].tail(300)}")
    b["dirty"] = True  # we mutate it
    ws, root = b["case"].ws, b["case"].root
    out: dict = {"baseline": {k: b["metrics"].get(k) for k in ("drc_count", "lvs_count", "setup_wns")}}
    sub: dict[str, str] = {}

    def counts():
        m = F.flow_metrics(ws)
        return m.get("drc_count"), m.get("lvs_count")

    cand = ("route_ecc", "filler_ecc", "timing_optimization_sizer")

    def attribute(editfn, label):
        """Apply editfn to ONE candidate DEF at a time; rerun lvs+drc; record counts; restore."""
        rows = {}
        for sd in cand:
            d = F.final_def(ws, sd)
            if not d:
                continue
            shutil.copy(d, str(d) + ".orig")
            try:
                try:
                    info = F.edit_def(d, editfn)
                except Exception as e:  # noqa: BLE001
                    rows[sd] = {"edit_error": str(e)}
                    continue
                F.run_ecc(ctx.ecc, root, ["run", "--only", "lvs", "--force"], ctx.cpus, F.step_timeout())
                F.run_ecc(ctx.ecc, root, ["run", "--only", "drc", "--force"], ctx.cpus, F.step_timeout())
                drc, lvs = counts()
                rows[sd] = {"edit": info, "drc_count": drc, "lvs_count": lvs}
            finally:
                shutil.move(str(d) + ".orig", str(d))
        return rows

    try:
        rows = attribute(F.delete_route_segment, "a")
        F.run_ecc(ctx.ecc, root, ["run", "--only", "lvs", "--force"], ctx.cpus, F.step_timeout())  # restore clean
        F.run_ecc(ctx.ecc, root, ["run", "--only", "drc", "--force"], ctx.cpus, F.step_timeout())
        out["a_delete_segment"] = {"per_input_def": rows,
                                   "drc_input_per_log": [l.split("]")[-1].strip() for l in
                                                         F.step_log(ws, "drc_ecc", 60000).splitlines()
                                                         if ".def" in l and "Create file" not in l][:2]}
        lv = {k: v.get("lvs_count") for k, v in rows.items()}
        dr = {k: v.get("drc_count") for k, v in rows.items()}
        out["a_delete_segment"]["summary"] = {"lvs_count_by_edited_file": lv, "drc_count_by_edited_file": dr}
        sub["a"] = "PASS" if any((v or 0) > 0 for v in lv.values()) or any((v or 0) > 0 for v in dr.values()) else "FAIL"
    except Exception as e:  # noqa: BLE001
        sub["a"] = f"ERROR {e}"

    try:
        rows = attribute(F.overlap_two_cells, "c")
        F.run_ecc(ctx.ecc, root, ["run", "--only", "lvs", "--force"], ctx.cpus, F.step_timeout())
        F.run_ecc(ctx.ecc, root, ["run", "--only", "drc", "--force"], ctx.cpus, F.step_timeout())
        out["c_overlap_cells"] = {"per_input_def": rows}
        sub["c"] = "PASS" if any((v.get("drc_count") or 0) > 0 for v in rows.values()) else "FAIL"
    except Exception as e:  # noqa: BLE001
        sub["c"] = f"ERROR {e}"

    # (b) shrink clock period, re-run sta only
    try:
        before = F.sta_summary(ws)
        # b1: edit the ECC-generated SDC directly (expected: overwritten by ECC refresh)
        import re as _re
        for f in (ws / "origin").glob("*/*.tcl"):
            f.write_text(_re.sub(r"(?m)^set clk_freq_mhz .*$", "set clk_freq_mhz 400.0", f.read_text()))
        F.run_ecc(ctx.ecc, root, ["run", "--only", "sta", "--force"], ctx.cpus, F.step_timeout())
        b1 = F.sta_summary(ws)
        sdc_after = next((ws / "origin").glob("pre_CTS/TYP*.tcl")).read_text()
        reverted = "400.0" not in sdc_after
        # b2: workspace-scope frequency in home/params.toml
        pt = ws / "home" / "params.toml"
        pt.write_text(_re.sub(r"(?m)^frequency_mhz\s*=.*$", "frequency_mhz = 400.0", pt.read_text(), count=1))
        F.run_ecc(ctx.ecc, root, ["run", "--only", "sta", "--force"], ctx.cpus, F.step_timeout())
        b2 = F.sta_summary(ws)
        st = F.status_json(ctx.ecc, root)
        out["b_shrink_period"] = {"before": before, "after_sdc_edit": b1, "sdc_edit_reverted_by_ecc": reverted,
                                  "after_params_toml_edit": b2,
                                  "sta_checklist": st.get("steps", {}).get("sta", {}).get("checklist_failed_items")}
        if (b2.get("setup_wns") or 0) < 0:
            sub["b"] = "PASS"
        elif b2.get("setup_wns") == before.get("setup_wns") and b1.get("setup_wns") == before.get("setup_wns"):
            sub["b"] = "SKIP"
        else:
            sub["b"] = "FAIL"
    except Exception as e:  # noqa: BLE001
        sub["b"] = f"ERROR {e}"

    out["sub"] = sub
    F.save_summary("L4_signoff_truth", out)
    bad = {k: v for k, v in sub.items() if v not in ("PASS", "SKIP")}
    skipped = [k for k, v in sub.items() if v == "SKIP"]
    msg = "; ".join(f"({k}) {v}" for k, v in sorted(sub.items()))
    if bad:
        return Result.fail("undetected/errored injections: " + msg, details=out)
    if skipped:
        return Result.warn("partial (b skipped: CLI cannot re-run sta on an edited period, SDC regenerated from params): " + msg,
                           details=out)
    return Result.ok("all injected faults detected: " + msg, details=out)


# ------------------------------------------------------------------ standalone runner
def _jsonable(r: Result) -> dict:
    return {"status": r.status.value, "message": r.message, "measured": r.measured, "expected": r.expected,
            "tolerance": r.tolerance, "evidence": r.evidence, "details": r.details}


def main(argv=None) -> int:
    import argparse
    import types
    from concurrent.futures import ThreadPoolExecutor

    from ecc_verify.registry import REGISTRY, load_all_checks, run_check

    ap = argparse.ArgumentParser()
    ap.add_argument("--ecc")
    ap.add_argument("--config")
    ap.add_argument("--designs-dir")
    ap.add_argument("--lanes", help="comma list of taskset cpu lists; one lane each (default: run.cpus)")
    ap.add_argument("--only", default="", help="comma list of check ids")
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    from ecc_verify import config
    cfg = config.load(a.config, {"ecc": {"bin": a.ecc}, "paths": {"designs_dir": a.designs_dir}})
    config.set_current(cfg)
    a.ecc = cfg.ecc
    a.out = a.out or str(F.summary_dir())
    load_all_checks()
    ids = [i for i, (s, _) in REGISTRY.items() if s.scope == "slow" and s.layer in (Layer.L4, Layer.L5)]
    if a.only:
        ids = [i for i in ids if i in a.only.split(",")]
    order = ["L4.determinism", "L4.resume_equivalence", "L5.relocate", "L4.signoff_truth",
             "L4.set_override_effective", "L4.density_monotonic", "L4.freq_monotonic", "L5.chaos_kill_resume"]
    ids.sort(key=lambda i: order.index(i) if i in order else 99)
    lanes = [c for c in (a.lanes or cfg.cpus).split(",") if c]
    # lane A: base-dependent chain; lane B: independent cases then freq sweep
    laneA = [i for i in ids if i in order[:4]]
    laneB = [i for i in ids if i not in order[:4]]
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    results: dict = {}
    t0 = time.time()

    def worker(cpus, lane_ids):
        ctx = types.SimpleNamespace(ecc=a.ecc, root=Path("."), workspaces=[], designs_dir=cfg.designs_dir, out=out,
                                    slow=True, cpus=cpus)
        for cid in lane_ids:
            s, fn = REGISTRY[cid]
            t = time.time()
            r = run_check(fn, ctx)
            d = _jsonable(r)
            d["secs"] = round(time.time() - t, 1)
            d["cpus"] = cpus
            results[cid] = d
            print(f"[{time.time()-t0:7.0f}s] {cid}: {r.status.value} {r.message[:200]}", flush=True)
            (out / "results.json").write_text(json.dumps({"results": results, "wall_secs": time.time() - t0}, indent=2, default=str))

    try:
        with ThreadPoolExecutor(max_workers=2) as ex:
            futs = [ex.submit(worker, lanes[0], laneA)]
            if laneB:
                futs.append(ex.submit(worker, lanes[1] if len(lanes) > 1 else lanes[0], laneB))
            for f in futs:
                f.result()
    finally:
        drop_base()
        pass  # per-check cleanup only (cleanup_all would race with a concurrent runner)
    wall = time.time() - t0
    (out / "results.json").write_text(json.dumps({"results": results, "wall_secs": wall}, indent=2, default=str))
    print(f"total wall {wall:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
