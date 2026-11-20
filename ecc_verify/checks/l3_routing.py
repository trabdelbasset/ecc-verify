"""L3 routing checks on the route DEF: routed nets, connectivity (opens), shorts, layer direction/range, counts."""
from __future__ import annotations

import json
import re
from collections import defaultdict

from ecc_verify import config, phys
from ecc_verify.model import Layer, Result
from ecc_verify.registry import check

G = 2000  # bucket size (DBU) for spatial hashing
# sampling: [sampling] rt_sample_min_cells / rt_sample_stride in ecc-verify.toml (defaults 10000 / 3)


class Geo:
    """Per-DEF geometry helper (layer widths, via pads, pin rects)."""

    def __init__(self, d, lef):
        self.d, self.lef = d, lef
        self.dbu = d.dbu
        self.half = {n: max(1, round(L.width * d.dbu / 2)) for n, L in lef.layers.items()}
        self.viapads = {}
        self._pin_cache = {}

    def viapad(self, name, layer_hint=None):
        if name not in self.viapads:
            v = self.lef.vias.get(name)
            pads = []
            if v:
                for (l, x0, y0, x1, y1) in v["rects"]:
                    if l.startswith(("MET", "T4M", "RDL")):
                        pads.append((l, round(x0 * self.dbu), round(y0 * self.dbu), round(x1 * self.dbu),
                                     round(y1 * self.dbu)))
            else:
                m = phys.via_metals(name, self.d)
                if m:
                    for l in m:
                        pads.append((l, -45, -45, 45, 45))
            self.viapads[name] = pads
        return self.viapads[name]

    def pin_rects(self, inst, pin):
        key = (inst, pin)
        if key in self._pin_cache:
            return self._pin_cache[key]
        out = []
        if inst == "PIN":
            p = self.d.pins.get(pin)
            if p:
                r = p.rect
                out.append((p.layer, p.x + r[0], p.y + r[1], p.x + r[2], p.y + r[3]))
        else:
            c = self.d.comps.get(inst)
            if c is not None:
                M = self.lef.macros.get(c.master)
                if M and pin in M.pins:
                    for (l, x0, y0, x1, y1) in M.pins[pin]["rects"]:
                        if not l.startswith(("MET", "T4M", "RDL")):
                            continue
                        r = phys.orient_rect((x0, y0, x1, y1), M.w, M.h, c.orient)
                        d = self.dbu
                        out.append((l, c.x + round(r[0] * d), c.y + round(r[1] * d), c.x + round(r[2] * d),
                                    c.y + round(r[3] * d)))
        self._pin_cache[key] = out
        return out

    def seg_rect(self, layer, x1, y1, x2, y2, half=None):
        h = half if half is not None else self.half.get(layer, 50)
        return (layer, min(x1, x2) - h, min(y1, y2) - h, max(x1, x2) + h, max(y1, y2) + h)


def net_items(geo, net):
    """Return (items, pin_nodes): items=[(layer,x0,y0,x1,y1,node)]."""
    items = []
    nid = 0
    pin_nodes = []
    for inst, pin in net.conns:
        rs = geo.pin_rects(inst, pin)
        if not rs:
            continue
        pin_nodes.append(nid)
        for r in rs:
            items.append((*r, nid))
        nid += 1
    for (layer, x1, y1, x2, y2) in net.segs:
        items.append((*geo.seg_rect(layer, x1, y1, x2, y2), nid))
        nid += 1
    for (layer, x0, y0, x1, y1) in net.patches:
        items.append((layer, x0, y0, x1, y1, nid))
        nid += 1
    for (vname, x, y, _l) in net.vias:
        for (l, a, b, c, e) in geo.viapad(vname):
            items.append((l, x + a, y + b, x + c, y + e, nid))
        nid += 1
    return items, pin_nodes, nid


def components(items, n):
    parent = list(range(n))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    buckets = defaultdict(list)
    for idx, it in enumerate(items):
        l, x0, y0, x1, y1, node = it
        for gx in range(x0 // G, x1 // G + 1):
            for gy in range(y0 // G, y1 // G + 1):
                buckets[(l, gx, gy)].append(idx)
    for lst in buckets.values():
        if len(lst) < 2:
            continue
        for i in range(len(lst)):
            a = items[lst[i]]
            for j in range(i + 1, len(lst)):
                b = items[lst[j]]
                if a[5] == b[5]:
                    continue
                if a[1] <= b[3] and b[1] <= a[3] and a[2] <= b[4] and b[2] <= a[4]:
                    ra, rb = find(a[5]), find(b[5])
                    if ra != rb:
                        parent[ra] = rb
    return find


def _route_def(ws):
    return ws.def_("route")


def _signal_nets(d):
    return [n for n in d.nets.values() if n.use in ("SIGNAL", "CLOCK", "") and len(n.conns) >= 2]


@check("L3.rt.all_nets_routed", Layer.L3, steps=("route",), category="physics", severity="critical",
       title="every multi-pin signal net has routing")
def all_routed(ws):
    d = _route_def(ws)
    geo = Geo(d, ws.lef())
    unrouted = []
    for n in _signal_nets(d):
        if not n.segs and not n.vias and not n.patches:
            # nets whose pins already touch need no wire: accept if pin shapes of all conns intersect
            unrouted.append(n.name)
    if unrouted:
        # discount nets whose pin shapes overlap (abutment, legitimately unrouted)
        real = []
        nets = d.nets
        for nm in unrouted:
            n = nets[nm]
            items, pins, cnt = net_items(geo, n)
            find = components(items, cnt)
            roots = {find(p) for p in pins}
            if len(roots) > 1 or len(pins) < len(n.conns):
                real.append(nm)
        if real:
            return Result.fail(f"{len(real)} of {len(_signal_nets(d))} multi-pin nets have no routing (e.g. {real[:3]})",
                               measured=len(real), expected=0, evidence=[ws.rel(ws.output_file("route", ".def.gz"))],
                               details={"nets": real[:30]})
    return Result.ok(f"all {len(_signal_nets(d))} multi-pin nets routed", measured=0)


@check("L3.rt.connectivity", Layer.L3, steps=("route",), category="physics", severity="critical",
       title="union-find over wires/vias/pin shapes: each multi-pin net is one component (no OPEN)")
def connectivity(ws):
    d = _route_def(ws)
    geo = Geo(d, ws.lef())
    nets = sorted(_signal_nets(d), key=lambda n: n.name)
    min_cells, stride = config.current().sample("rt_sample_min_cells"), int(config.current().sample("rt_sample_stride"))
    sampled = len(d.comps) >= min_cells
    if sampled:
        nets = nets[::stride]
    opens, dangling, missing_pins = [], 0, 0
    for n in nets:
        items, pins, cnt = net_items(geo, n)
        if len(pins) < len(n.conns):
            missing_pins += 1
        if len(pins) < 2:
            continue
        find = components(items, cnt)
        roots = {find(p) for p in pins}
        if len(roots) > 1:
            opens.append((n.name, len(roots), len(pins)))
        all_roots = {find(i) for i in range(cnt)}
        dangling += len(all_roots - roots)
    msg = f"{len(nets)} nets checked" + (f" (every {stride}th net: design >= {min_cells} cells)" if sampled else "")
    if opens:
        return Result.fail(f"{len(opens)} OPEN nets of {len(nets)} (net, components, pins): {opens[:3]}",
                           measured=len(opens), expected=0, details={"opens": opens[:30], "dangling": dangling},
                           evidence=[ws.rel(ws.output_file("route", ".def.gz"))])
    if dangling:
        return Result.warn(f"{msg}: no opens but {dangling} floating wire components", measured=dangling)
    return Result.ok(msg + ", no opens", measured=0, expected=0)


@check("L3.rt.shorts", Layer.L3, steps=("route",), category="physics", severity="critical",
       title="same-layer wire/pin/PDN rectangles of different nets must not overlap")
def shorts(ws):
    d = _route_def(ws)
    lef = ws.lef()
    geo = Geo(d, lef)
    buckets = defaultdict(list)
    count = 0

    def add(layer, x0, y0, x1, y1, nid):
        nonlocal count
        count += 1
        r = (x0, y0, x1, y1, nid)
        for gx in range(x0 // G, x1 // G + 1):
            for gy in range(y0 // G, y1 // G + 1):
                buckets[(layer, gx, gy)].append(r)

    ids = {}
    for n in d.nets.values():
        nid = ids.setdefault(n.name, len(ids) + 1)
        for (layer, x1, y1, x2, y2) in n.segs:
            l, a, b, c, e = geo.seg_rect(layer, x1, y1, x2, y2)
            add(l, a, b, c, e, nid)
        for (layer, x0, y0, x1, y1) in n.patches:
            add(layer, x0, y0, x1, y1, nid)
        for (vname, x, y, _l) in n.vias:
            for (l, a, b, c, e) in geo.viapad(vname):
                add(l, x + a, y + b, x + c, y + e, nid)
        for inst, pin in n.conns:
            for (l, a, b, c, e) in geo.pin_rects(inst, pin):
                if l != "MET1":
                    add(l, a, b, c, e, nid)
    for k, sn in enumerate(d.snets.values()):
        nid = -(k + 1)
        for (layer, w, shape, x1, y1, x2, y2) in sn.shapes:
            if shape.startswith("VIA:") or not w:
                continue
            h = w // 2
            if y1 == y2:
                add(layer, min(x1, x2), y1 - h, max(x1, x2), y1 + h, nid)
            else:
                add(layer, x1 - h, min(y1, y2), x1 + h, max(y1, y2), nid)
    # cell obstructions above MET1
    for c in d.comps.values():
        M = lef.macros.get(c.master)
        if not M or phys.is_filler(c.master):
            continue
        for (l, x0, y0, x1, y1) in M.obs:
            if l == "MET1" or not l.startswith("MET"):
                continue
            r = phys.orient_rect((x0, y0, x1, y1), M.w, M.h, c.orient)
            add(l, c.x + round(r[0] * d.dbu), c.y + round(r[1] * d.dbu), c.x + round(r[2] * d.dbu),
                c.y + round(r[3] * d.dbu), -1000000 - hash(c.name) % 1000000)
    found = {}
    names = {v: k for k, v in ids.items()}
    for (layer, gx, gy), lst in buckets.items():
        if len(lst) < 2:
            continue
        for i in range(len(lst)):
            a = lst[i]
            for j in range(i + 1, len(lst)):
                b = lst[j]
                if a[4] == b[4]:
                    continue
                ox = min(a[2], b[2]) - max(a[0], b[0])
                oy = min(a[3], b[3]) - max(a[1], b[1])
                if ox > 0 and oy > 0:
                    key = (layer, min(a[4], b[4]), max(a[4], b[4]))
                    if key not in found:
                        found[key] = (max(a[0], b[0]), max(a[1], b[1]), ox, oy)
    if found:
        ex = [(k[0], names.get(k[1], k[1]), names.get(k[2], k[2]), v) for k, v in list(found.items())[:3]]
        return Result.fail(f"{len(found)} overlapping net pairs among {count} rects (layer, netA, netB, "
                           f"(x, y, dx, dy) dbu): {ex}", measured=len(found), expected=0,
                           details={"examples": [str(e) for e in ex]},
                           evidence=[ws.rel(ws.output_file("route", ".def.gz"))])
    return Result.ok(f"{count} rects, no overlaps between different nets", measured=0, expected=0)


@check("L3.rt.layer_direction", Layer.L3, steps=("route",), category="physics", severity="minor",
       title="fraction of routed length on the non-preferred direction (WARN above 10%)")
def layer_direction(ws):
    d = _route_def(ws)
    lef = ws.lef()
    tot = defaultdict(float)
    wrong = defaultdict(float)
    for n in d.nets.values():
        for (layer, x1, y1, x2, y2) in n.segs:
            L = lef.layers.get(layer)
            if not L:
                continue
            ln = abs(x2 - x1) + abs(y2 - y1)
            horiz = y1 == y2
            tot[layer] += ln
            if (L.direction == "HORIZONTAL") != horiz:
                wrong[layer] += ln
    fr = {l: wrong[l] / tot[l] for l in tot if tot[l] > 0}
    worst = max(fr.items(), key=lambda kv: kv[1], default=("", 0))
    s = ", ".join(f"{l}:{v:.1%}" for l, v in sorted(fr.items()))
    if worst[1] > 0.10:
        return Result.warn(f"non-preferred-direction length {s}", measured=fr, expected="<= 10% per layer",
                           tolerance="10%")
    return Result.ok(f"non-preferred-direction length {s}", measured=fr, tolerance="10%")


@check("L3.rt.wirelength_recomputed", Layer.L3, steps=("route",), category="physics", severity="major",
       title="per-layer routed wirelength vs route.step.json final DR iteration")
def wl(ws):
    d = _route_def(ws)
    lef = ws.lef()
    order = phys.layer_index(lef)
    route_json = ws.step_feature_json("route")
    raw = json.load(open(route_json)) if route_json else {}
    js = raw.get("route") or raw.get("routing") or {}
    it = js.get("DR", [])
    if not it:
        return Result.skip("no DR iterations in route.step.json")
    last = it[-1]["routing_wire_length_map"]
    mine = defaultdict(float)
    for n in d.nets.values():
        for (layer, x1, y1, x2, y2) in n.segs:
            mine[layer] += (abs(x2 - x1) + abs(y2 - y1)) / d.dbu
    probs = []
    for layer, i in order.items():
        rep = last.get(str(i), 0.0)
        m = mine.get(layer, 0.0)
        if abs(rep - m) > max(0.01 * max(rep, m), 1.0):
            probs.append(f"{layer}: reported {rep:.1f} vs recomputed {m:.1f} um")
    if probs:
        return Result.fail("; ".join(probs), measured=dict(mine), expected=last, tolerance="1% (>=1um)")
    return Result.ok(f"per-layer wirelength matches ({sum(mine.values()):.0f} um)", tolerance="1% (>=1um)")


@check("L3.rt.via_count", Layer.L3, steps=("route",), category="physics", severity="major",
       title="via count per cut layer vs route.step.json; via layers adjacent to wire layer")
def via_count(ws):
    d = _route_def(ws)
    lef = ws.lef()
    order = phys.layer_index(lef)
    route_json = ws.step_feature_json("route")
    raw = json.load(open(route_json)) if route_json else {}
    js = raw.get("route") or raw.get("routing") or {}
    it = js.get("DR", [])
    cnt = defaultdict(int)
    probs = []
    for n in d.nets.values():
        for (vname, x, y, layer) in n.vias:
            m = phys.via_metals(vname, d)
            if not m:
                probs.append(f"{n.name}: unknown via {vname}")
                continue
            if abs(order.get(m[0], -9) - order.get(m[1], -99)) != 1:
                probs.append(f"{n.name}: via {vname} joins non-adjacent layers {m}")
            mm = re.search(r"VIA(\d+)", vname)
            cnt[int(mm.group(1)) if mm else -1] += 1
    if it:
        rep = {int(k): v for k, v in it[-1]["cut_via_num_map"].items() if v}
        for k in set(rep) | set(cnt):
            if abs(rep.get(k, 0) - cnt.get(k, 0)) > max(1, 0.01 * rep.get(k, 0)):
                probs.append(f"VIA{k}: reported {rep.get(k, 0)} vs counted {cnt.get(k, 0)}")
    if probs:
        return Result.fail("; ".join(probs[:4]), measured=dict(cnt), tolerance="1%")
    return Result.ok(f"via counts per cut layer {dict(sorted(cnt.items()))}", tolerance="1%")


@check("L3.rt.layer_range", Layer.L3, steps=("route",), category="physics", severity="major",
       title="no routing outside route bottom_layer..top_layer")
def layer_range(ws):
    d = _route_def(ws)
    lef = ws.lef()
    order = phys.layer_index(lef)
    lo, hi = ws.param("bottom_layer", "MET2"), ws.param("top_layer", "MET5")
    if lo not in order or hi not in order:
        return Result.skip(f"unknown layer range {lo}..{hi}")
    a, b = order[lo], order[hi]
    bad = defaultdict(int)
    for n in d.nets.values():
        for seg in n.segs:
            if not a <= order.get(seg[0], -1) <= b:
                bad[seg[0]] += 1
        for (vn, x, y, l) in n.vias:
            m = phys.via_metals(vn, d)
            if m and not (a <= order.get(m[0], -1) <= b or a <= order.get(m[1], -1) <= b):
                bad[vn] += 1
    if bad:
        return Result.fail(f"routing outside {lo}..{hi}: {dict(bad)}", measured=dict(bad), expected=0)
    return Result.ok(f"all routing within {lo}..{hi}", measured=0)
