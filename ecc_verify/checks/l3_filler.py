"""L3 filler: row coverage after filler insertion."""
from __future__ import annotations

from collections import defaultdict

from ecc_verify import phys
from ecc_verify.model import Layer, Result
from ecc_verify.registry import check


@check("L3.fill.row_coverage", Layer.L3, steps=("filler",), category="physics", severity="major",
       title="after filler every row is fully covered (sum of widths == row length)")
def row_coverage(ws):
    lef = ws.lef()
    d = ws.def_("filler")
    byrow = defaultdict(list)
    for c in d.comps.values():
        b = phys.comp_bbox(c, lef, d.dbu)
        byrow[b[1]].append((b[0], b[2], c.name))
    gaps, total_gap = [], 0
    rows = 0
    for r in d.rows:
        rows += 1
        x0, x1 = r.x, r.x + r.nx * r.sx
        cur = x0
        for a, b, n in sorted(byrow.get(r.y, [])):
            if a > cur:
                gaps.append((r.name, cur, a))
                total_gap += a - cur
            cur = max(cur, b)
        if cur < x1:
            gaps.append((r.name, cur, x1))
            total_gap += x1 - cur
    if gaps:
        return Result.fail(f"{len(gaps)} uncovered gaps in {len({g[0] for g in gaps})}/{rows} rows, total "
                           f"{total_gap / d.dbu:.2f} um (e.g. {gaps[:3]})", measured=len(gaps), expected=0,
                           details={"gaps": gaps[:20]})
    return Result.ok(f"{rows} rows fully covered", measured=0, expected=0)
