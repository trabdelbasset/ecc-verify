"""L3 placement physics (DEF-based)."""
from __future__ import annotations

from collections import defaultdict

from ecc_verify import phys
from ecc_verify.model import Layer, Result
from ecc_verify.registry import check

ALL_PLACED = ("place", "diff_sizing", "CTS", "legalization", "Timing optimization", "route", "filler")
LEGAL = ("legalization", "Timing optimization", "route", "filler")
LEGAL_ROWS = tuple(s for s in LEGAL)


def _each(ws, steps):
    for s in steps:
        if ws.step_dir(s) is not None:
            yield s, ws.def_(s)


@check("L3.pl.all_placed", Layer.L3, steps=ALL_PLACED, category="physics", severity="critical",
       title="every component PLACED or FIXED")
def all_placed(ws):
    probs = []
    for s, d in _each(ws, ALL_PLACED):
        bad = [c.name for c in d.comps.values() if c.status not in ("PLACED", "FIXED", "COVER")]
        if bad:
            probs.append(f"{s}: {len(bad)} unplaced (e.g. {bad[:3]})")
    if probs:
        return Result.fail("; ".join(probs), measured=probs, expected=0)
    return Result.ok("all components placed in all steps", measured=0)


@check("L3.pl.on_site_grid", Layer.L3, steps=LEGAL, category="physics", severity="critical",
       title="x on site grid, y on a row origin (legalized steps)")
def on_site_grid(ws):
    probs = []
    for s, d in _each(ws, LEGAL):
        if not d.rows:
            continue
        r0 = d.rows[0]
        ys = {r.y: r for r in d.rows}
        bad_x = bad_y = 0
        ex = []
        for c in d.comps.values():
            r = ys.get(c.y)
            if r is None:
                bad_y += 1
                ex.append((c.name, c.x, c.y))
            elif (c.x - r.x) % r.sx != 0:
                bad_x += 1
                ex.append((c.name, c.x, c.y))
        if bad_x or bad_y:
            probs.append(f"{s}: {bad_x} off site grid in x, {bad_y} not on a row y (e.g. {ex[:2]})")
    if probs:
        return Result.fail("; ".join(probs[:3]), measured=probs, expected=0)
    return Result.ok("all cells on site grid/rows")


@check("L3.pl.orientation_legal", Layer.L3, steps=LEGAL, category="physics", severity="major",
       title="orientation legal for the row (N/FN on N rows, FS/S on FS rows)")
def orientation(ws):
    probs = []
    for s, d in _each(ws, LEGAL):
        ro = {r.y: r.orient for r in d.rows}
        bad = []
        for c in d.comps.values():
            o = ro.get(c.y)
            if o is None:
                continue
            ok = c.orient in (("N", "FN") if o == "N" else ("FS", "S"))
            if not ok:
                bad.append((c.name, c.orient, o))
        if bad:
            probs.append(f"{s}: {len(bad)} cells with wrong orientation (e.g. {bad[:2]})")
    if probs:
        return Result.fail("; ".join(probs[:3]), measured=probs, expected=0)
    return Result.ok("orientations legal")


def find_overlaps(d, lef, limit=1000):
    """Per-row sweep (cells are one row high). Returns list of (a, b, overlap_dbu)."""
    rows = defaultdict(list)
    out = []
    for c in d.comps.values():
        b = phys.comp_bbox(c, lef, d.dbu)
        rows[b[1]].append((b[0], b[2], b[3], c.name))
    for y, cells in rows.items():
        cells.sort()
        reach = None
        for x0, x1, y1, n in cells:
            if reach and x0 < reach[0]:
                out.append((reach[1], n, reach[0] - x0))
                if len(out) >= limit:
                    return out
            if reach is None or x1 > reach[0]:
                reach = (x1, n)
    return out


@check("L3.pl.no_overlap", Layer.L3, steps=LEGAL, category="physics", severity="critical",
       title="no overlapping cells (sweep over LEF bboxes)")
def no_overlap(ws):
    lef = ws.lef()
    probs = []
    for s, d in _each(ws, LEGAL):
        # all cells here are single-row height (verified by the on_site_grid check), so a per-row sweep is exact
        ov = find_overlaps(d, lef)
        if ov:
            probs.append(f"{s}: {len(ov)} overlapping pairs (e.g. {ov[:3]})")
    if probs:
        return Result.fail("; ".join(probs[:3]), measured=probs, expected=0,
                           evidence=[ws.rel(ws.step_dir(s)) for s in LEGAL[:1]], details={"problems": probs})
    return Result.ok("no overlaps in legalization/timing-opt/route/filler", measured=0)


@check("L3.pl.in_core", Layer.L3, steps=LEGAL, category="physics", severity="critical",
       title="every std cell inside the core")
def in_core(ws):
    lef = ws.lef()
    probs = []
    for s, d in _each(ws, LEGAL):
        cr = phys.core_rect(d)
        bad = []
        for c in d.comps.values():
            b = phys.comp_bbox(c, lef, d.dbu)
            if b[0] < cr[0] or b[1] < cr[1] or b[2] > cr[2] or b[3] > cr[3]:
                bad.append((c.name, b))
        if bad:
            probs.append(f"{s}: {len(bad)} cells outside core {cr} (e.g. {bad[:2]})")
    if probs:
        return Result.fail("; ".join(probs[:3]), measured=probs, expected=0)
    return Result.ok("all cells inside core")


def bin_density(d, lef, nbin_target=8, skip=lambda m: m.startswith(("FILLER", "FILLCAP"))):
    cr = phys.core_rect(d)
    W, H = cr[2] - cr[0], cr[3] - cr[1]
    bh = max(H // nbin_target, 4 * phys.row_height(d), 1)
    bw = max(W // nbin_target, bh, 1)
    nx, ny = -(-W // bw), -(-H // bh)
    grid = [[0.0] * nx for _ in range(ny)]
    for c in d.comps.values():
        if skip(c.master):
            continue
        x0, y0, x1, y1 = phys.comp_bbox(c, lef, d.dbu)
        for j in range(max((y0 - cr[1]) // bh, 0), min((y1 - cr[1] - 1) // bh, ny - 1) + 1):
            by0, by1 = cr[1] + j * bh, cr[1] + (j + 1) * bh
            oy = min(y1, by1) - max(y0, by0)
            if oy <= 0:
                continue
            for i in range(max((x0 - cr[0]) // bw, 0), min((x1 - cr[0] - 1) // bw, nx - 1) + 1):
                bx0, bx1 = cr[0] + i * bw, cr[0] + (i + 1) * bw
                ox = min(x1, bx1) - max(x0, bx0)
                if ox > 0:
                    grid[j][i] += ox * oy
    peak = 0.0
    for j in range(ny):
        for i in range(nx):
            bwid = min(cr[0] + (i + 1) * bw, cr[2]) - (cr[0] + i * bw)
            bhei = min(cr[1] + (j + 1) * bh, cr[3]) - (cr[1] + j * bh)
            peak = max(peak, grid[j][i] / (bwid * bhei))
    return peak, (nx, ny)


@check("L3.pl.density_bins", Layer.L3, steps=("legalization", "Timing optimization", "filler"), category="physics",
       severity="major", title="bin density <= 1.0; peak reported vs target_density")
def density(ws):
    lef = ws.lef()
    target = ws.param("target_density", None)
    dp = ws.params.get("dreamplace", {}).get("target_density", target)
    target = dp if dp is not None else target
    msgs, over, warn, peaks = [], [], [], {}
    for s in ("legalization", "Timing optimization", "filler"):
        if ws.step_dir(s) is None:
            continue
        pk, grid = bin_density(ws.def_(s), lef)
        peaks[s] = round(pk, 3)
        if pk > 1.0 + 1e-6:
            over.append(f"{s}: peak bin density {pk:.3f} > 1.0")
        elif target and s == 'legalization' and pk > target + 0.35:
            warn.append(f"{s}: peak {pk:.2f} vs target_density {target}")
    if over:
        return Result.fail("; ".join(over), measured=peaks, expected="<= 1.0")
    if warn:
        return Result.warn("; ".join(warn), measured=peaks, expected=f"target {target}", tolerance="legalization peak <= target+0.35")
    return Result.ok(f"peak density per step {peaks} (target {target})", measured=peaks, expected=f"target {target}")


@check("L3.pl.hpwl_recomputed", Layer.L3, steps=("place", "legalization"), category="physics", severity="major",
       title="HPWL recomputed from DEF vs reported place_hpwl; legalization HPWL growth sane")
def hpwl(ws):
    lef = ws.lef()
    rep = ws.step_metrics("place").get("place_hpwl")
    if rep is None:
        return Result.skip("no place_hpwl metric")
    h, n = phys.hpwl_um(ws.def_("place"), lef)
    probs = []
    if abs(h - rep) > 0.05 * max(h, rep):
        probs.append(f"recomputed {h:.1f} um vs reported {rep:.1f} um over {n} nets")
    hl, _ = phys.hpwl_um(ws.def_("legalization"), lef)
    if hl > 1.5 * h:
        return Result.warn(f"legalization HPWL {hl:.0f} is {hl / h:.2f}x global-placement HPWL {h:.0f}",
                           measured=hl, expected=f"<= 1.5 x {h:.0f}")
    if probs:
        return Result.fail("; ".join(probs), measured=h, expected=rep, tolerance="5%")
    return Result.ok(f"HPWL {h:.1f} vs reported {rep:.1f} um", measured=h, expected=rep, tolerance="5%")


@check("L3.pl.util_growth", Layer.L3, steps=("legalization", "Timing optimization"), category="cross-step",
       severity="major", title="timing optimisation must not blow up core utilization / std-cell area")
def util_growth(ws):
    a, b = ws.step_metrics("legalization"), ws.step_metrics("Timing optimization")
    ua, ub = a.get("core_utilization"), b.get("core_utilization")
    sa, sb = a.get("std_cell_area"), b.get("std_cell_area")
    if not (ua and ub and sa and sb):
        return Result.skip("missing utilization metrics")
    lef = ws.lef()
    d0, d1 = ws.def_("legalization"), ws.def_("Timing optimization")
    cr = phys.core_rect(d1)
    core = (cr[2] - cr[0]) * (cr[3] - cr[1]) / d1.dbu ** 2
    ra = sum(lef.macros[c.master].w * lef.macros[c.master].h for c in d0.comps.values() if not phys.is_filler(c.master))
    rb = sum(lef.macros[c.master].w * lef.macros[c.master].h for c in d1.comps.values() if not phys.is_filler(c.master))
    ratio = rb / ra
    limit = 1.5
    msg = (f"core utilization {ra / core:.2f} -> {rb / core:.2f} (std-cell area x{ratio:.2f}, cells "
           f"{a.get('std_cell_count')} -> {b.get('std_cell_count')}; metrics {ua} -> {ub})")
    if ratio > limit or rb / core > 0.80:
        return Result.fail("timing optimisation grows the design: " + msg, measured=round(ratio, 3),
                           expected=f"<= x{limit} and utilization <= 0.80", tolerance=f"x{limit}",
                           evidence=[ws.rel(ws.step_dir("Timing optimization") / "analysis/qor_metrics.json")])
    return Result.ok(msg, measured=round(ratio, 3), tolerance=f"x{limit}")
