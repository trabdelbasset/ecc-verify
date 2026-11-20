"""L5 robustness: fault injection (wraps the vendored ecc_verify/vendor/fault_inject.py) and project relocation."""
from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path

from ecc_verify.model import Layer, Result
from ecc_verify.registry import REGISTRY, check
from ecc_verify import config, util
from ecc_verify.util import run

# fault_inject.py is vendored into the package; [fault_injection] path overrides it, target/scratch select the design
# whose workspace is mutated (copies only) and the scratch dir.
VENDORED_FI = Path(__file__).resolve().parents[1] / "vendor" / "fault_inject.py"
# Reference run (results/fault_injection_summary.md, hand-checked): FAIL verdicts are confirmed ECC defects, except
# C05 whose oracle is too strict (ECC prints a clean error naming the TOML problem and exits 1).
REFERENCE_FAIL = {"A02", "A03", "A05", "V07", "V08", "V09", "V10", "V11", "T01", "T02", "C06", "L01", "L02", "L03"}
KNOWN_ORACLE_FALSE_POSITIVE = {"C05"}
REFERENCE_GAPS = {"G01", "G02", "G03"}


def _load_fi():
    cfg = config.current()
    fi_path = Path(cfg.fault_path) if cfg.fault_path else VENDORED_FI
    if not fi_path.is_file():
        raise FileNotFoundError(f"fault_injection.path not found: {fi_path}")
    spec = importlib.util.spec_from_file_location("fault_inject", fi_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["fault_inject"] = mod
    spec.loader.exec_module(mod)
    return mod


@check("L5.fault_injection", Layer.L5, steps=("workspace", "sta", "drc", "lvs", "powerAnalysis"), category="fault",
       severity="critical", scope="global",
       title="fault-injection suite on one workspace (QoR/gate evidence handling)")
def fault_injection(ctx):
    fi = _load_fi()
    cfg = config.current()
    if not ctx.workspaces:
        return Result.skip("no workspace")
    ws = next((w for w in ctx.workspaces if w.name == cfg.fault_target), None)
    if ws is None:
        return Result.skip(f"fault-injection target {cfg.fault_target!r} is not among the selected designs "
                           f"(set [fault_injection] target or --fault-target)")
    scratch = Path(cfg.fault_scratch)
    scratch.mkdir(parents=True, exist_ok=True)
    rows = list(fi.run_suite(ws.project_dir, ecc=ctx.ecc, env=cfg.tool_env(), scratch=scratch))
    out = [{"fault": r.fault, "category": r.category, "expect": r.expect, "got": r.got, "verdict": r.verdict,
            "detail": r.detail} for r in rows]
    (ctx.out / "fault_injection.json").write_text(json.dumps(out, indent=1))
    fails = {r.fault for r in rows if r.verdict == "FAIL"}
    gaps = {r.fault for r in rows if r.verdict == "GAP-OPEN"}
    new_fail = sorted(fails - REFERENCE_FAIL - KNOWN_ORACLE_FALSE_POSITIVE)
    fixed = sorted(REFERENCE_FAIL - fails)
    real = sorted(fails - KNOWN_ORACLE_FALSE_POSITIVE)
    n = len(rows)
    detail = {"fail": sorted(fails), "gap_open": sorted(gaps), "new_vs_reference": new_fail, "fixed_vs_reference": fixed,
              "oracle_false_positive": sorted(fails & KNOWN_ORACLE_FALSE_POSITIVE)}
    ev = [str(ctx.out / "fault_injection.json")]
    base = [r for r in rows if r.fault == "BASE"]
    if base and base[0].verdict != "PASS":
        return Result.fail(f"baseline analysis crashed: {base[0].got}", evidence=ev, details=detail)
    if real or gaps:
        msg = (f"{len(real)} of {n - 1} faults expose ECC defects ({', '.join(real)}); {len(gaps)} coverage gaps still "
               f"open ({', '.join(sorted(gaps))}); new vs reference: {new_fail or 'none'}; fixed vs reference: "
               f"{fixed or 'none'}")
        return Result.fail(msg, measured=len(real), expected=0, evidence=ev, details=detail)
    return Result.ok(f"all {n - 1} faults handled", evidence=ev, details=detail)


@check("L5.relocate_status", Layer.L5, steps=("workspace", "CLI"), category="fault", severity="major", scope="global",
       title="a finished project copied to a new path can be opened with `ecc status`")
def relocate(ctx):
    if not ctx.workspaces:
        return Result.skip("no workspace")
    src = next((w.project_dir for w in ctx.workspaces if w.name == "xtea"), None) or ctx.workspaces[0].project_dir
    dst = util.VERIFY_RUNS / "relocate_unmodified"
    if dst.exists():
        shutil.rmtree(dst)
    util.VERIFY_RUNS.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dst, symlinks=True)
    rc, out, err = run([ctx.ecc, "status", "--json"], cwd=dst, timeout=120)
    try:
        j = json.loads(out)
    except Exception:  # noqa: BLE001
        j = None
    if rc == 0 and isinstance(j, list) and j and j[0].get("kind") != "error":
        return Result.ok("status works on the relocated copy", measured=rc)
    reason = (j[0].get("reason") or j[0].get("error")) if isinstance(j, list) and j else (err or out)[:200]
    return Result.fail(f"ecc status fails on an unmodified copy: rc={rc}: {reason}", measured=rc, expected=0,
                       evidence=[str(dst / "project.json")])
