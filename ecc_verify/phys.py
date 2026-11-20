"""Physical-design helpers shared by L2/L3 checks."""
from __future__ import annotations

import re
from functools import lru_cache

DEF_STEPS = ("preFloorplan", "macroPlacement", "postFloorplan", "preplace", "place", "diff_sizing", "CTS",
             "legalization", "Timing optimization", "route", "filler", "lvs", "drc", "antennaCheck", "RCX",
             "powerAnalysis", "emir")
# steps that carry placement (all comps placed) / routing
PLACED_STEPS = ("place", "diff_sizing", "CTS", "legalization", "Timing optimization", "route", "filler")
FINAL_STEP = "filler"   # last step that changes the layout (later DEFs are copies)


def physical_only(ws) -> set:
    """Masters that exist in the LEF but not in the Liberty (fillers, taps, antenna diodes)."""
    lef, lib = ws.lef(), ws.liberty()
    return {m for m in lef.macros if m not in lib.cells}


def is_filler(master: str) -> bool:
    return master.startswith("FILL")


def orient_rect(rect, w, h, orient):
    """Transform a rect given in cell-local coordinates (um) to placed-cell-local coordinates (origin = lower
    left of the placed bbox)."""
    x0, y0, x1, y1 = rect
    o = orient
    if o == "N":
        r = (x0, y0, x1, y1)
    elif o == "S":
        r = (w - x1, h - y1, w - x0, h - y0)
    elif o == "FN":
        r = (w - x1, y0, w - x0, y1)
    elif o == "FS":
        r = (x0, h - y1, x1, h - y0)
    elif o in ("W", "FE"):  # 90 deg CCW (W) / flipped
        if o == "W":
            r = (h - y1, x0, h - y0, x1)
        else:
            r = (h - y1, w - x1, h - y0, w - x0)
    elif o in ("E", "FW"):
        if o == "E":
            r = (y0, w - x1, y1, w - x0)
        else:
            r = (y0, x0, y1, x1)
    else:
        r = (x0, y0, x1, y1)
    return r


def placed_size(w, h, orient):
    return (h, w) if orient in ("W", "E", "FW", "FE") else (w, h)


def comp_bbox(c, lef, dbu):
    """Placed bbox of a DEF component in DBU."""
    M = lef.macros[c.master]
    w, h = placed_size(M.w, M.h, c.orient)
    return (c.x, c.y, c.x + round(w * dbu), c.y + round(h * dbu))


def pin_center(c, pinname, lef, dbu):
    """Center (DBU) of the first LEF rect of a component pin, or None."""
    M = lef.macros.get(c.master)
    if not M or pinname not in M.pins or not M.pins[pinname]["rects"]:
        return None
    rs = M.pins[pinname]["rects"]
    x0 = min(r[1] for r in rs)
    y0 = min(r[2] for r in rs)
    x1 = max(r[3] for r in rs)
    y1 = max(r[4] for r in rs)
    r = orient_rect((x0, y0, x1, y1), M.w, M.h, c.orient)
    return (c.x + (r[0] + r[2]) / 2 * dbu, c.y + (r[1] + r[3]) / 2 * dbu)


def core_rect(d):
    """Core bbox (DBU) from the rows."""
    if not d.rows:
        return None
    x0 = min(r.x for r in d.rows)
    x1 = max(r.x + r.nx * r.sx for r in d.rows)
    y0 = min(r.y for r in d.rows)
    h = row_height(d)
    y1 = max(r.y for r in d.rows) + h
    return (x0, y0, x1, y1)


def row_height(d):
    ys = sorted({r.y for r in d.rows})
    if len(ys) > 1:
        return min(b - a for a, b in zip(ys, ys[1:]))
    return 0


def hpwl_um(d, lef, nets=None):
    """Half-perimeter wirelength (um) over signal nets using LEF pin centers / DEF port locations."""
    dbu = d.dbu
    total = 0.0
    n = 0
    for net in d.nets.values():
        if net.use not in ("SIGNAL", "CLOCK") or len(net.conns) < 2:
            continue
        xs, ys = [], []
        for inst, pin in net.conns:
            if inst == "PIN":
                p = d.pins.get(pin)
                if not p:
                    continue
                r = orient_rect((p.rect[0], p.rect[1], p.rect[2], p.rect[3]), 0, 0, "N")
                xs.append(p.x + (r[0] + r[2]) / 2)
                ys.append(p.y + (r[1] + r[3]) / 2)
            else:
                c = d.comps.get(inst)
                if not c:
                    continue
                pc = pin_center(c, pin, lef, dbu)
                if pc:
                    xs.append(pc[0])
                    ys.append(pc[1])
        if len(xs) >= 2:
            total += (max(xs) - min(xs)) + (max(ys) - min(ys))
            n += 1
    return total / dbu, n


def via_metals(vname, d):
    """(lower, upper) metal layer names of a via from DEF VIAS or tech-LEF naming (MET4_MET3_VIA3_2)."""
    v = d.vias.get(vname)
    if v and len(v) == 3:
        return (v[0], v[2])
    m = re.match(r"([A-Z0-9]+)_([A-Z0-9]+)_", vname)
    if m:
        return (m.group(2), m.group(1))
    return None


def layer_index(lef):
    order = {}
    i = 0
    for n, L in lef.layers.items():
        if L.type == "ROUTING":
            order[n] = i
            i += 1
    return order
