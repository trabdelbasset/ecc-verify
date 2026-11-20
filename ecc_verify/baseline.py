"""Baseline ("memory"): per-design JSON of check verdicts and key metrics plus run provenance.

    <baseline_dir>/
        provenance.json      ecc version/build_commit, ecc-ci-designs commit, PDK version, host, cpus, date
        <design>.json        {"design", "checks": {id: {status, message}}, "metrics": {...}, "provenance": {...}}
        _global.json         verdicts of the global checks (design "<global>")

The files are small, stable (sorted keys) and diff-friendly, so the directory can live in git.
"""
from __future__ import annotations

import json
from pathlib import Path

from ecc_verify.compare import CompareError, load_results

SCHEMA = 1
GLOBAL = "<global>"


def _fname(design: str) -> str:
    return "_global.json" if design == GLOBAL else f"{design}.json"


def export(results: str | Path, out: str | Path, designs: list[str] | None = None) -> dict:
    """Write/refresh a baseline directory from a results.json. Existing files of other designs are kept."""
    doc = load_results(results)
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    by: dict = {}
    for r in doc["results"]:
        by.setdefault(r["design"], {})[r["check"]] = {"status": r["status"], "message": (r.get("message") or "")[:300]}
    prov = (doc.get("meta") or {}).get("provenance") or {}
    metrics = doc.get("metrics") or {}
    written = []
    for design in sorted(by):
        if designs and design not in designs and design != GLOBAL:
            continue
        d = {"schema": SCHEMA, "design": design, "checks": dict(sorted(by[design].items())),
             "metrics": dict(sorted((metrics.get(design) or {}).items())),
             "provenance": {"date": prov.get("date"), "ecc_build_commit": (prov.get("ecc") or {}).get("build_commit"),
                            "ecc_version": (prov.get("ecc") or {}).get("version")}}
        (out / _fname(design)).write_text(json.dumps(d, indent=1, sort_keys=True) + "\n")
        written.append(design)
    p = {"schema": SCHEMA, **prov, "source_results": str(Path(results).resolve()),
         "layers": (doc.get("meta") or {}).get("layers"), "slow": (doc.get("meta") or {}).get("slow")}
    p["exported_designs"] = sorted(written)
    (out / "provenance.json").write_text(json.dumps(p, indent=1, sort_keys=True, default=str) + "\n")
    if not written:
        raise CompareError(f"no designs exported from {results}")
    return {"out": str(out), "designs": written, "provenance": p}


def show(path: str | Path) -> str:
    from ecc_verify.compare import load_baseline
    b = load_baseline(path)
    lines = [f"baseline {b.path}: {len(b.designs)} designs"]
    for k in ("date", "ecc", "designs", "pdk", "host", "layers", "slow"):
        if k in b.provenance:
            lines.append(f"  {k}: {json.dumps(b.provenance[k], default=str)}")
    for dn, d in sorted(b.designs.items()):
        c = {}
        for v in d["checks"].values():
            c[v["status"]] = c.get(v["status"], 0) + 1
        lines.append(f"  {dn:28s} {len(d['metrics']):3d} metrics  {c}")
    return "\n".join(lines)
