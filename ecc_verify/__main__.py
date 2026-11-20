"""ecc-verify CLI.

    ecc-verify run      --root <designs> --out <dir> [--baseline <dir>] [--designs a,b] [--layers L0,L1,..] [--slow]
    ecc-verify compare  --baseline <dir> --results <results.json> [--out <dir>]
    ecc-verify baseline export --results <results.json> --out <baseline_dir>
    ecc-verify baseline show   <baseline_dir>
    ecc-verify list | config show | config example

Exit codes:  0 = no new FAIL vs baseline (no FAIL not covered by a known issue when there is no baseline),
             1 = new FAILs or gating metric regressions,   2 = usage / infrastructure error.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import dataclasses
import json
import multiprocessing
import os
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

from ecc_verify import __version__, baseline as baseline_mod, compare as compare_mod, config, provenance, report
from ecc_verify.compare import CompareError
from ecc_verify.config import ConfigError
from ecc_verify.known_issues import KnownIssues
from ecc_verify.model import Layer
from ecc_verify.registry import REGISTRY, load_all_checks, run_check
from ecc_verify.runner import _row, _worker
from ecc_verify.workspace import discover

LAYER_KEY = {"L0": Layer.L0, "L1": Layer.L1, "L2": Layer.L2, "L3": Layer.L3, "L4": Layer.L4, "L5": Layer.L5}
EXIT_OK, EXIT_FAIL, EXIT_ERROR = 0, 1, 2


@dataclasses.dataclass
class Ctx:
    ecc: str
    root: Path
    workspaces: list
    designs_dir: Path | None
    out: Path
    slow: bool
    cpus: str
    cfg: "config.Config | None" = None


def select(layers, only, scope_pred):
    ids = []
    for cid, (spec, fn) in REGISTRY.items():
        if layers and spec.layer not in layers:
            continue
        if only and not re.search(only, cid):
            continue
        if scope_pred(spec.scope):
            ids.append(cid)
    return sorted(ids)


# --------------------------------------------------------------------------- argument plumbing
def _add_config_opts(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("configuration (override ecc-verify.toml and ECC_VERIFY_* environment variables)")
    g.add_argument("--config", help="ecc-verify.toml (default: $ECC_VERIFY_CONFIG or ./ecc-verify.toml)")
    g.add_argument("--ecc", help="ecc binary (default: `ecc` on PATH)")
    g.add_argument("--pdk-root", help="official PDK root (default: $CHIPCOMPILER_ICS55_PDK_ROOT, else `ecc pdk show --json`)")
    g.add_argument("--oss-cad-bin", help="oss-cad-suite bin dir with yosys/iverilog/vvp (default: yosys on PATH)")
    g.add_argument("--opensta-bin", help="OpenSTA (`sta`) binary for L3.oss.opensta_timing")
    g.add_argument("--openroad-bin", help="OpenROAD (`openroad`) binary for L3.oss.openroad_*")
    g.add_argument("--klayout-bin", help="KLayout (`klayout`) binary for L3.oss.klayout_*")
    g.add_argument("--openpdk-root", help="optional unofficial community KLayout/OpenRCX deck checkout (not official PDK)")
    g.add_argument("--oss-docker-image", help="Docker image (e.g. hpretl/iic-osic-tools:latest) supplying sta/openroad/klayout")
    g.add_argument("--designs-dir", help="pristine design sources (ecc-ci-designs) for the slow L4/L5 checks")
    g.add_argument("--scratch-dir", help="scratch directory for copies and work dirs")
    g.add_argument("--cpus", help="taskset cpu list for real `ecc run` calls (slow checks)")
    g.add_argument("--pin-cpus", help="taskset cpu list for pinned tools of the static run (Yosys miter)")
    g.add_argument("--fault-target", help="design the fault-injection suite runs on (default APU)")
    g.add_argument("--fault-script", help="alternative fault_inject.py (default: vendored)")


def _cli_overrides(a) -> dict:
    g = lambda n: getattr(a, n, None)  # noqa: E731
    return {"ecc": {"bin": g("ecc")},
            "paths": {"pdk_root": g("pdk_root"), "oss_cad_bin": g("oss_cad_bin"),
                      "opensta_bin": g("opensta_bin"), "openroad_bin": g("openroad_bin"),
                      "klayout_bin": g("klayout_bin"), "openpdk_root": g("openpdk_root"),
                      "oss_docker_image": g("oss_docker_image"),
                      "designs_dir": g("designs_dir"),
                      "scratch_dir": g("scratch_dir"), "known_issues": g("known_issues"), "baseline": g("baseline")},
            "run": {"cpus": g("cpus"), "pin_cpus": g("pin_cpus"), "workers": g("workers")},
            "fault_injection": {"target": g("fault_target"), "path": g("fault_script")}}


def load_config(a) -> "config.Config":
    cfg = config.load(getattr(a, "config", None), _cli_overrides(a))
    config.set_current(cfg)
    return cfg


def _known(a, cfg) -> KnownIssues:
    if getattr(a, "no_known_issues", False):
        return KnownIssues([], "(disabled)")
    return KnownIssues.resolve(getattr(a, "known_issues", None) or cfg.known_issues)


def _gate(doc, a, cfg, out: Path) -> int:
    """Compare `doc` (results) against the baseline (if any), write compare.md/json, print, return the exit code."""
    base_dir = getattr(a, "baseline", None) or cfg.baseline
    base = compare_mod.load_baseline(base_dir) if base_dir else None
    known = _known(a, cfg)
    c = compare_mod.compare(doc, base, known, cfg.tolerances, cfg.compare.get("ignore_metrics"),
                            cfg.compare.get("warn_only_metrics"))
    compare_mod.write_outputs(c, out)
    print(compare_mod.render_console(c), flush=True)
    print(f"[ecc-verify] compare report: {out / 'compare.md'}", flush=True)
    return EXIT_FAIL if c.exit_code else EXIT_OK


# --------------------------------------------------------------------------- run
def cmd_run(a) -> int:
    t0 = time.time()
    cfg = load_config(a)
    root = Path(a.root)
    if not root.is_dir():
        raise ConfigError(f"--root is not a directory: {root}")
    load_all_checks()
    bad = [x for x in (a.layers.split(",") if a.layers else []) if x.strip() not in LAYER_KEY]
    if bad:
        raise ConfigError(f"unknown layer(s) in --layers: {', '.join(bad)} (choose from {', '.join(LAYER_KEY)})")
    layers = {LAYER_KEY[x.strip()] for x in a.layers.split(",")} if a.layers else None
    designs = [d for d in a.designs.split(",")] if a.designs else None
    wss = discover(root, designs)
    if not wss:
        print(f"error: no finished workspaces under {root} (expected <root>/<design>/default/home/flow.json"
              f"{'; designs filter: ' + a.designs if a.designs else ''})", file=sys.stderr)
        return EXIT_ERROR
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    design_ids = select(layers, a.only, lambda s: s == "design")
    global_ids = select(layers, a.only, lambda s: s == "global" or (s == "slow" and a.slow))
    if global_ids and not (shutil.which(cfg.ecc) or os.access(cfg.ecc, os.X_OK)):
        raise ConfigError(f"ecc binary not found: {cfg.ecc!r} (use --ecc, [ecc] bin in ecc-verify.toml or put ecc on PATH)")
    if any(i.startswith("L3.gds") or i.startswith("L3.fn") for i in design_ids) and not cfg.pdk_root:
        print("warning: PDK root unknown (CHIPCOMPILER_ICS55_PDK_ROOT / --pdk-root): L3.gds and L3.fn checks will "
              "ERROR or SKIP", file=sys.stderr)
    ctx = Ctx(cfg.ecc, root, wss, Path(cfg.designs_dir) if cfg.designs_dir else None, out, a.slow, cfg.cpus, cfg)
    rows, metrics_by, design_cfg = [], {}, {}
    print(f"[ecc-verify] {len(wss)} designs, {len(design_ids)} design checks, {len(global_ids)} global checks "
          f"(ecc={cfg.ecc}, pdk={cfg.pdk_root or '-'}, pin_cpus={cfg.pin_cpus}, config={cfg.config_file or 'defaults'})",
          flush=True)

    def run_globals():
        res = []
        for cid in global_ids:
            spec, fn = REGISTRY[cid]
            t = time.time()
            r = run_check(fn, ctx)
            res.append(_row("<global>", cid, r, time.time() - t))
            print(f"  [global] {cid}: {r.status.value} ({time.time() - t:.0f}s)", flush=True)
        return res

    # biggest designs first for better packing
    order = sorted(wss, key=lambda w: -sum(f.stat().st_size for f in (w.path / "route_ecc/output").glob("*.def.gz")
                                          ) if (w.path / "route_ecc/output").exists() else 0)
    workers = max(1, min(cfg.workers, 16))
    cfgd = cfg.to_dict()
    with cf.ThreadPoolExecutor(1) as gex, cf.ProcessPoolExecutor(workers, mp_context=multiprocessing.get_context("spawn")) as pex:
        gf = gex.submit(run_globals) if global_ids else None
        futs = {pex.submit(_worker, (str(root), w.name, design_ids, cfgd)): w.name for w in order} if design_ids else {}
        for f in cf.as_completed(futs):
            name, rs, mets, dcfg = f.result()
            rows += rs
            metrics_by[name] = mets
            if dcfg:
                design_cfg[name] = dcfg
            c = {s: sum(1 for r in rs if r["status"] == s) for s in ("PASS", "FAIL", "WARN", "SKIP", "ERROR")}
            print(f"  [design] {name}: {c}", flush=True)
        if gf:
            rows += gf.result()
    prov = provenance.collect(cfg, root)
    ver = (prov.get("ecc") or {}).get("version") or "?"
    meta = {"ecc_version": ver, "designs": [w.name for w in wss], "wall_seconds": time.time() - t0,
            "date": datetime.now().strftime("%Y-%m-%d %H:%M"), "root": str(root), "layers": a.layers or "all",
            "slow": a.slow, "provenance": prov, "config": {k: v for k, v in cfg.to_dict().items() if k != "source"}}
    rows.sort(key=lambda r: (r["check"], r["design"]))
    doc = report.write_all(out, rows, meta, layers, extra={"metrics": metrics_by, "design_config": design_cfg})
    cnt = {}
    for r in rows:
        cnt[r["status"]] = cnt.get(r["status"], 0) + 1
    print(f"[ecc-verify] done in {meta['wall_seconds']:.0f}s: {cnt}; {len(doc['findings'])} distinct findings; "
          f"coverage {doc['coverage']['percent']}%; output {out}", flush=True)
    if a.no_gate:
        return EXIT_OK
    return _gate(json.loads((out / "results.json").read_text()), a, cfg, out)


# --------------------------------------------------------------------------- compare / baseline / misc
def cmd_compare(a) -> int:
    cfg = load_config(a)
    if not (a.baseline or cfg.baseline):
        raise CompareError("--baseline <dir> is required (or paths.baseline in ecc-verify.toml)")
    doc = compare_mod.load_results(a.results)
    out = Path(a.out) if a.out else Path(a.results).resolve().parent
    return _gate(doc, a, cfg, out)


def cmd_baseline_export(a) -> int:
    r = baseline_mod.export(a.results, a.out, a.designs.split(",") if a.designs else None)
    p = r["provenance"]
    print(f"[ecc-verify] baseline written to {r['out']}: {len(r['designs'])} designs "
          f"({', '.join(r['designs'][:20])}); ecc {(p.get('ecc') or {}).get('version')} "
          f"build {((p.get('ecc') or {}).get('build_commit') or '?')[:9]}")
    return EXIT_OK


def cmd_baseline_show(a) -> int:
    print(baseline_mod.show(a.baseline))
    return EXIT_OK


def cmd_list(a) -> int:
    load_all_checks()
    for k, v in sorted(REGISTRY.items()):
        print(f"{k:40s} {v[0].layer.value:16s} {v[0].category:11s} {v[0].severity:8s} {v[0].scope:7s} {v[0].title}")
    return EXIT_OK


def cmd_config(a) -> int:
    if a.what == "example":
        sys.stdout.write(config.EXAMPLE_TOML)
        return EXIT_OK
    cfg = load_config(a)
    print(config.describe(cfg))
    print(f"known issues       {_known(a, cfg).path or '(none)'}")
    return EXIT_OK


def cmd_known(a) -> int:
    cfg = load_config(a)
    k = _known(a, cfg)
    print(f"# {k.path}")
    for e in k.entries:
        print(f"{', '.join(e.bugs) or '-':40s} {', '.join(e.checks)}  designs={', '.join(e.designs) if e.designs else 'all'}")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ecc-verify", description=__doc__.split("\n\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"ecc-verify {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="run the verification checks and gate against the baseline",
                       description="Run checks over <root>/<design>/default workspaces. Accepts the output of ecc's "
                                   "nix/scripts/run-designs.sh and an ecc-ci-designs checkout run in place.")
    r.add_argument("--root", required=True, help="projects root (each <root>/<design>/default is a finished workspace)")
    r.add_argument("--out", required=True)
    r.add_argument("--designs", help="comma separated design names (default: all)")
    r.add_argument("--layers", help="comma separated subset of L0,L1,L2,L3,L4,L5")
    r.add_argument("--only", help="regex on check id")
    r.add_argument("--slow", action="store_true", help="also run scope=slow checks (real ecc runs)")
    r.add_argument("--workers", type=int, help="design worker processes (default 6)")
    r.add_argument("--baseline", help="baseline directory to compare against (default: paths.baseline)")
    r.add_argument("--known-issues", help="known_issues.toml (default: ./known_issues.toml, else packaged)")
    r.add_argument("--no-known-issues", action="store_true", help="treat every FAIL as new")
    r.add_argument("--no-gate", action="store_true", help="do not compare; exit 0 after the run (reports only)")
    _add_config_opts(r)
    r.set_defaults(fn=cmd_run)

    c = sub.add_parser("compare", help="compare a results.json against a baseline directory",
                       description="Reports NEW FAIL, FIXED, STILL-FAILING (known) and metric drift; writes "
                                   "compare.md/compare.json; exit 0 = no new FAIL / regression, 1 = new FAIL or regression.")
    c.add_argument("--results", required=True)
    c.add_argument("--baseline", help="baseline directory (default: paths.baseline)")
    c.add_argument("--out", help="directory for compare.md/compare.json (default: next to results.json)")
    c.add_argument("--known-issues")
    c.add_argument("--no-known-issues", action="store_true")
    _add_config_opts(c)
    c.set_defaults(fn=cmd_compare)

    b = sub.add_parser("baseline", help="baseline management")
    bs = b.add_subparsers(dest="bcmd", required=True)
    be = bs.add_parser("export", help="write a baseline directory from a results.json")
    be.add_argument("--results", required=True)
    be.add_argument("--out", required=True, help="baseline directory")
    be.add_argument("--designs", help="only these designs (others in the directory are kept)")
    be.set_defaults(fn=cmd_baseline_export)
    bw = bs.add_parser("show", help="print the provenance and per-design summary of a baseline")
    bw.add_argument("baseline")
    bw.set_defaults(fn=cmd_baseline_show)

    ls = sub.add_parser("list", help="list registered checks")
    ls.set_defaults(fn=cmd_list)
    cf_ = sub.add_parser("config", help="show the effective configuration, or print an example ecc-verify.toml")
    cf_.add_argument("what", choices=("show", "example"))
    _add_config_opts(cf_)
    cf_.add_argument("--known-issues")
    cf_.set_defaults(fn=cmd_config)
    ki = sub.add_parser("known-issues", help="list the known issues in effect")
    ki.add_argument("--known-issues")
    _add_config_opts(ki)
    ki.set_defaults(fn=cmd_known)
    return p


def main(argv=None) -> int:
    p = build_parser()
    a = p.parse_args(argv)          # argparse exits with 2 on usage errors
    try:
        return a.fn(a)
    except (ConfigError, CompareError, FileNotFoundError, NotADirectoryError, PermissionError) as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except Exception as e:  # noqa: BLE001  infrastructure failure, not a verification verdict
        import traceback
        traceback.print_exc()
        print(f"error: infrastructure failure: {type(e).__name__}: {e}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
