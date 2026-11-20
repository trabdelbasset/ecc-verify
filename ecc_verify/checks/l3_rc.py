"""L3 RC: SPEF coverage, cap per um, corner ordering."""
from __future__ import annotations

from ecc_verify.model import Layer, Result
from ecc_verify.registry import check
from ecc_verify.parsers._io import norm


def routed_lengths(ws):
    d = ws.def_("route")
    out = {}
    for n in d.nets.values():
        if n.use in ("SIGNAL", "CLOCK", "") and len(n.conns) >= 2 and (n.segs or n.vias):
            out[n.name] = sum(abs(s[3] - s[1]) + abs(s[4] - s[2]) for s in n.segs) / d.dbu
    return out


@check("L3.rc.spef_coverage", Layer.L3, steps=("RCX",), category="physics", severity="critical",
       title="every routed signal net appears in every corner's SPEF")
def coverage(ws):
    routed = routed_lengths(ws)
    probs = []
    files = ws.spef_files()
    if not files:
        return Result.fail("no SPEF files")
    for c in files:
        sp = ws.spef(c)
        miss = [n for n in routed if n not in sp.nets]
        if miss:
            probs.append(f"{c}: {len(miss)}/{len(routed)} routed nets missing (e.g. {miss[:3]})")
    if probs:
        return Result.fail("; ".join(probs[:3]), measured=len(probs), expected=0)
    return Result.ok(f"{len(routed)} routed nets present in all {len(files)} SPEF files")


@check("L3.rc.cap_per_um", Layer.L3, steps=("RCX",), category="physics", severity="major",
       title="per-net total cap / routed length within 0.05-0.5 fF/um (nets >= 10 um)")
def cap_per_um(ws):
    routed = routed_lengths(ws)
    lo, hi = 0.05, 0.5
    sp = ws.spef("Cworst_125C")
    out, n = [], 0
    for name, L in routed.items():
        if L < 10 or name not in sp.nets:
            continue
        n += 1
        r = sp.nets[name] / L
        if not lo <= r <= hi:
            out.append((name, round(r, 3), round(L, 1)))
    frac = len(out) / max(n, 1)
    if frac > 0.02:
        return Result.fail(f"{len(out)}/{n} nets ({frac:.1%}) outside {lo}-{hi} fF/um (e.g. {out[:3]})",
                           measured=frac, expected="<= 2% outliers", tolerance=f"{lo}-{hi} fF/um")
    if out:
        return Result.warn(f"{len(out)}/{n} outlier nets (name, fF/um, um): {out[:3]}", measured=frac,
                           tolerance=f"{lo}-{hi} fF/um", details={"outliers": out[:30]})
    return Result.ok(f"{n} nets within {lo}-{hi} fF/um", tolerance=f"{lo}-{hi} fF/um")


@check("L3.rc.corner_ordering", Layer.L3, steps=("RCX",), category="physics", severity="major",
       title="Cworst total cap >= Cbest per net for >= 99% of nets")
def corner_order(ws):
    files = ws.spef_files()
    probs = []
    for t in ("125C", "m40C"):
        w, b = f"Cworst_{t}", f"Cbest_{t}"
        if w not in files or b not in files:
            continue
        sw, sb = ws.spef(w), ws.spef(b)
        common = [n for n in sw.nets if n in sb.nets]
        bad = [n for n in common if sw.nets[n] < sb.nets[n] * 0.999]
        frac = 1 - len(bad) / max(len(common), 1)
        if frac < 0.99:
            probs.append(f"{t}: only {frac:.2%} of {len(common)} nets have Cworst >= Cbest (e.g. {bad[:2]})")
        if sw.total_cap_ff < sb.total_cap_ff:
            probs.append(f"{t}: total Cworst {sw.total_cap_ff:.0f} < Cbest {sb.total_cap_ff:.0f} fF")
    if not probs and not any(f"Cworst_{t}" in files for t in ("125C", "m40C")):
        return Result.skip("no Cworst/Cbest pair")
    if probs:
        return Result.fail("; ".join(probs), measured=len(probs), tolerance=">= 99% of nets")
    return Result.ok("Cworst >= Cbest on >= 99% of nets in both temperatures", tolerance=">= 99% of nets")
