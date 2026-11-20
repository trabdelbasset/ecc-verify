"""L3 floorplan physics: die/core/rows/IO pins/PDN."""
from __future__ import annotations

from collections import Counter

from ecc_verify import phys
from ecc_verify.model import Layer, Result
from ecc_verify.registry import check

FP_STEPS = ("postFloorplan", "filler")


@check("L3.fp.core_in_die", Layer.L3, steps=("postFloorplan", "filler"), category="physics", severity="critical",
       title="core (row bbox) lies inside the die")
def core_in_die(ws):
    probs = []
    for step in FP_STEPS:
        d = ws.def_(step)
        c = phys.core_rect(d)
        x0, y0, x1, y1 = d.die
        if not (d.rows and c and c[0] >= x0 and c[1] >= y0 and c[2] <= x1 and c[3] <= y1):
            probs.append(f"{step}: core {c} not in die {d.die}")
        if d.die[0] != 0 or d.die[1] != 0:
            probs.append(f"{step}: die origin not (0,0): {d.die}")
    if probs:
        return Result.fail("; ".join(probs), measured=probs, expected="core inside die")
    return Result.ok("core inside die in postFloorplan and final DEF")


@check("L3.fp.rows_tile_core", Layer.L3, steps=("postFloorplan", "filler"), category="physics", severity="critical",
       title="rows tile the core at site height, no overlap, alternating N/FS")
def rows_tile(ws):
    lef = ws.lef()
    probs = []
    for step in FP_STEPS:
        d = ws.def_(step)
        rows = sorted(d.rows, key=lambda r: r.y)
        if not rows:
            probs.append(f"{step}: no rows")
            continue
        site = lef.sites.get(rows[0].site)
        h = round(site[1] * d.dbu) if site else phys.row_height(d)
        w = round(site[0] * d.dbu) if site else rows[0].sx
        if rows[0].sx != w:
            probs.append(f"{step}: row step {rows[0].sx} != site width {w}")
        x0, nx = rows[0].x, rows[0].nx
        for i, r in enumerate(rows):
            if r.x != x0 or r.nx != nx or r.sx != rows[0].sx:
                probs.append(f"{step}: {r.name} extent differs from row0 (x={r.x}, nx={r.nx})")
                break
            if i and r.y - rows[i - 1].y != h:
                probs.append(f"{step}: gap/overlap between {rows[i - 1].name} and {r.name}: dy={r.y - rows[i - 1].y} "
                             f"!= {h}")
                break
            if i and r.orient == rows[i - 1].orient:
                probs.append(f"{step}: {r.name} orientation {r.orient} repeats")
                break
            if r.orient not in ("N", "FS"):
                probs.append(f"{step}: {r.name} orientation {r.orient} not N/FS")
                break
    if probs:
        return Result.fail("; ".join(probs[:4]), measured=probs)
    return Result.ok("rows tile core, alternating orientations")


@check("L3.fp.io_pins_on_boundary", Layer.L3, steps=("postFloorplan", "filler"), category="physics",
       severity="major", title="IO pins on die edge, on routing layers, non-overlapping, on tracks")
def io_pins(ws):
    lef = ws.lef()
    d = ws.def_("filler")
    allowed = {n for n, L in lef.layers.items() if L.type == "ROUTING"}
    x0, y0, x1, y1 = d.die
    off, bad_layer, overlap, off_track = [], [], [], []
    tracks = {}
    for (axis, start, num, step, layer) in d.tracks:
        tracks.setdefault((axis, layer), (start, num, step))
    boxes: dict = {}
    for p in d.pins.values():
        if p.use in ("POWER", "GROUND"):
            continue
        r = p.rect
        bx = (p.x + r[0], p.y + r[1], p.x + r[2], p.y + r[3])
        if not (bx[0] <= x0 or bx[2] >= x1 or bx[1] <= y0 or bx[3] >= y1):
            off.append(p.name)
        if p.layer not in allowed:
            bad_layer.append(f"{p.name}:{p.layer}")
        boxes.setdefault(p.layer, []).append((bx, p.name))
        # along-edge coordinate on track
        on_left_right = bx[0] <= x0 or bx[2] >= x1
        axis = "Y" if on_left_right else "X"   # TRACKS X lines are vertical lines; pin on top/bottom varies in x
        key = ("Y" if on_left_right else "X", p.layer)
        coord = p.y if on_left_right else p.x
        if key in tracks:
            s, n, st = tracks[key]
            if (coord - s) % st != 0:
                off_track.append(p.name)
    for layer, bs in boxes.items():
        bs.sort()
        for i, (a, na) in enumerate(bs):
            for b, nb in bs[i + 1:i + 40]:
                if b[0] >= a[2]:
                    break
                if b[0] < a[2] and b[1] < a[3] and a[1] < b[3]:
                    overlap.append(f"{na}/{nb}")
    msgs = []
    if off:
        msgs.append(f"{len(off)} pins not on die edge (e.g. {off[:3]})")
    if bad_layer:
        msgs.append(f"{len(bad_layer)} pins on non-routing layers (e.g. {bad_layer[:3]})")
    if overlap:
        msgs.append(f"{len(overlap)} overlapping pin pairs (e.g. {overlap[:3]})")
    if msgs:
        return Result.fail("; ".join(msgs), measured=len(off) + len(bad_layer) + len(overlap), expected=0,
                           details={"off_track": off_track[:20]})
    if off_track:
        return Result.warn(f"{len(off_track)}/{len(d.pins)} pins off the track grid (e.g. {off_track[:3]})",
                           measured=len(off_track), expected=0)
    return Result.ok(f"{len(d.pins)} pins on boundary and tracks", measured=0)


@check("L3.fp.pdn_present", Layer.L3, steps=("postFloorplan", "filler"), category="physics", severity="major",
       title="VDD/VSS special nets with stripes and follow-pin rails on every row")
def pdn(ws):
    probs = []
    summary = ""
    for step in FP_STEPS:
        d = ws.def_(step)
        for n in ("VDD", "VSS"):
            if n not in d.snets or not d.snets[n].shapes:
                probs.append(f"{step}: special net {n} missing/empty")
        if probs:
            continue
        layers = Counter(s[0] for n in ("VDD", "VSS") for s in d.snets[n].shapes if not s[2].startswith("VIA:"))
        rails = {s[4] for n in ("VDD", "VSS") for s in d.snets[n].shapes
                 if s[2] == "FOLLOWPIN" and s[4] == s[6]}
        want = {r.y for r in d.rows}
        want.add(max(want) + phys.row_height(d))
        miss = sorted(want - rails)
        if miss:
            probs.append(f"{step}: {len(miss)}/{len(want)} row boundaries have no follow-pin rail (y={miss[:3]})")
        if not any(l != "MET1" for l in layers):
            probs.append(f"{step}: no stripes above MET1 (layers {dict(layers)})")
        summary = f"{dict(layers)}"
    if probs:
        return Result.fail("; ".join(probs[:4]), measured=probs)
    return Result.ok(f"PDN present: shapes per layer {summary}")
