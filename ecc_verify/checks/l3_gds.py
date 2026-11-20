"""L3 GDS checks via KLayout (uv run --with klayout)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from ecc_verify import phys
from ecc_verify.model import Layer, Result
from ecc_verify.registry import check
from ecc_verify import config, util
from ecc_verify.util import run

PROBE = Path(__file__).resolve().parent.parent / "tools" / "gds_probe.py"
FEOL = {(2, 1), (9, 1), (41, 1)}


def _probe_cmd():
    """KLayout from the running interpreter if installed (`pip install ecc-verify[gds]`), else `uv run --with klayout`."""
    try:
        import klayout.db  # noqa: F401
        return [sys.executable]
    except ImportError:
        return ["uv", "run", "--with", "klayout", "python"]


def probe(ws, step):
    key = ("gds", step)
    if key in ws._cache:
        return ws._cache[key]
    f = ws.output_file(step, ".gds")
    if f is None:
        raise FileNotFoundError(f"no GDS in {step}")
    cfg = config.current()
    cfg.runs_dir.mkdir(parents=True, exist_ok=True)
    rc, out, err = run(_probe_cmd() + [str(PROBE), str(f)], cwd=str(cfg.runs_dir), timeout=cfg.timeout("gds_probe"))
    if rc != 0:
        raise RuntimeError(f"gds_probe rc={rc}: {err[-300:]}")
    ws._cache[key] = json.loads(out)
    return ws._cache[key]


def _layermap():
    s = set()
    for ln in util.LAYERMAP.read_text().splitlines():
        t = ln.split()
        if len(t) >= 4 and t[2].isdigit() and t[3].isdigit():
            s.add((int(t[2]), int(t[3])))
    return s


@check("L3.gds.loads", Layer.L3, steps=("Harden", "filler"), category="integrity", severity="critical",
       title="GDS loads in KLayout and its top cell is the design")
def loads(ws):
    probs = []
    for step in ("Harden", "filler"):
        p = probe(ws, step)
        if p["tops"] != [ws.top_name]:
            probs.append(f"{step}: top cells {p['tops']} != [{ws.top_name}]")
    if probs:
        return Result.fail("; ".join(probs), expected=[ws.top_name])
    return Result.ok(f"top cell {ws.top_name}, {probe(ws, 'Harden')['ncells']} cells")


@check("L3.gds.bbox_matches_die", Layer.L3, steps=("Harden", "filler"), category="physics", severity="major",
       title="GDS bbox equals DEF die area")
def bbox(ws):
    d = ws.def_("filler")
    die = [v / d.dbu for v in d.die]
    probs = []
    for step in ("Harden", "filler"):
        b = probe(ws, step)["bbox"]
        if any(abs(a - c) > 0.01 for a, c in zip(b, die)):
            probs.append(f"{step}: bbox {[round(x, 3) for x in b]} != die {die}")
    if probs:
        return Result.fail("; ".join(probs), tolerance="0.01 um")
    return Result.ok(f"bbox == die {die}", tolerance="0.01 um")


@check("L3.gds.std_cells_resolved", Layer.L3, steps=("Harden", "filler"), category="integrity", severity="major",
       title="every referenced std cell has geometry; std-cell layout (FEOL layers) merged")
def resolved(ws):
    d = ws.def_("filler")
    masters = {c.master for c in d.comps.values()}
    probs = []
    for step in ("Harden", "filler"):
        p = probe(ws, step)
        names = set(p["cell_names"])
        miss = sorted(m for m in masters if f"Master_{m}" not in names and m not in names)
        if miss:
            probs.append(f"{step}: {len(miss)} DEF masters have no GDS cell (e.g. {miss[:3]})")
        empty = [e for e in p["empty_cells"] if e.replace("Master_", "") in masters]
        if empty:
            probs.append(f"{step}: {len(empty)} empty/ghost std-cell GDS cells (e.g. {empty[:3]})")
    layers = {tuple(x) for x in probe(ws, "Harden")["layers"]}
    missing_feol = sorted(FEOL - layers)
    if missing_feol:
        probs.append(f"Harden GDS has no FEOL layers {missing_feol} (ACT/NW/POLY present in the PDK std-cell GDS): "
                     f"std-cell layout is metal-only abstract, not merged")
    if probs:
        return Result.fail("; ".join(probs), measured=sorted(layers), expected="FEOL layers present",
                           evidence=[ws.rel(ws.output_file("Harden", ".gds")), str(util.STD_CELL)])
    return Result.ok("all std cells resolved with FEOL geometry")


@check("L3.gds.no_zero_area", Layer.L3, steps=("Harden", "filler"), category="integrity", severity="minor",
       title="no zero-area shapes")
def zero_area(ws):
    tot = 0
    for step in ("Harden", "filler"):
        tot += probe(ws, step)["zero_area"]
    if tot:
        return Result.fail(f"{tot} zero-area shapes", measured=tot, expected=0)
    return Result.ok(f"no zero-area shapes ({probe(ws, 'Harden')['shapes_scanned']} scanned)", measured=0)


@check("L3.gds.layers_expected", Layer.L3, steps=("Harden", "filler"), category="integrity", severity="minor",
       title="layer/datatype set is a subset of the PDK layermap")
def layers(ws):
    allowed = _layermap()
    probs = []
    for step in ("Harden", "filler"):
        extra = sorted(set(map(tuple, probe(ws, step)["layers"])) - allowed)
        if extra:
            probs.append(f"{step}: layers not in layermap {extra}")
    if probs:
        return Result.fail("; ".join(probs), expected="subset of icsprout55.layermap",
                           evidence=[str(util.LAYERMAP)])
    return Result.ok(f"layers {probe(ws, 'Harden')['layers']} within layermap")
