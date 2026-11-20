"""L2: integration and cross-step consistency per design."""
from __future__ import annotations

import json
import re
from collections import defaultdict

from ecc_verify import netgraph, phys
from ecc_verify.model import Layer, Result
from ecc_verify.registry import check

NETLIST_STEPS = ("Synthesis",) + phys.DEF_STEPS


def _short(items, n=5):
    items = list(items)
    return ", ".join(map(str, items[:n])) + (f" (+{len(items) - n} more)" if len(items) > n else "")


@check("L2.netlist_def_consistency", Layer.L2, steps=phys.DEF_STEPS, category="cross-step", severity="critical",
       title="DEF COMPONENTS == netlist instances (physical-only cells excluded)")
def netlist_def(ws):
    phys_only = phys.physical_only(ws)
    probs, checked = [], 0
    for step in phys.DEF_STEPS:
        if ws.step_dir(step) is None:
            continue
        d = ws.def_(step)
        mods = ws.verilog(step)
        top = ws.top(step)
        dset = {n: c.master for n, c in d.comps.items() if c.master not in phys_only}
        vset = {n: i.cell for n, i in top.insts.items() if i.cell not in phys_only}
        checked += 1
        only_d = sorted(set(dset) - set(vset))
        only_v = sorted(set(vset) - set(dset))
        mism = [n for n in dset if n in vset and dset[n] != vset[n]]
        if only_d or only_v or mism:
            probs.append(f"{step}: only-in-DEF={len(only_d)} {_short(only_d, 3)}, only-in-netlist={len(only_v)} "
                         f"{_short(only_v, 3)}, master-mismatch={len(mism)} {_short(mism, 3)}")
    if probs:
        return Result.fail("; ".join(probs[:4]), measured=len(probs), expected=0,
                           evidence=[ws.rel(ws.path)], details={"problems": probs})
    return Result.ok(f"{checked} steps: DEF == netlist", measured=0, expected=0)


def _seq_names(top, lib):
    return {n for n, i in top.insts.items() if lib.cells.get(i.cell) and lib.cells[i.cell].seq}


def _collapse(g, top, lib, net, depth=0):
    """Follow a net back through buffers/inverters. Returns (source(inst,pin)|port, parity)."""
    parity = 0
    seen = 0
    while seen < 200:
        drv = g.drivers.get(net, [])
        if len(drv) != 1:
            return (("MULTI" if drv else "NONE", net), parity)
        inst, pin = drv[0]
        if inst == "ASSIGN":
            net = pin
            seen += 1
            continue
        if inst == "PORT":
            return ((inst, pin), parity)
        cell = lib.cells[top.insts[inst].cell]
        if netgraph.is_buf(cell) or netgraph.is_inv(cell):
            if netgraph.is_inv(cell):
                parity ^= 1
            ip = next(p for p, v in cell.pins.items() if v["dir"] == "input")
            net = top.insts[inst].conns.get(ip, "")
            seen += 1
            continue
        return ((inst, pin), parity)
    return (("LOOP", net), parity)


def _signatures(top, lib, g):
    """For every sequential non-clock input pin and output port: (source, parity) after buffer collapse."""
    sig = {}
    for n, i in top.insts.items():
        c = lib.cells.get(i.cell)
        if not c or not c.seq:
            continue
        for p, net in i.conns.items():
            pc = c.pins.get(p, {})
            if pc.get("dir") != "input" or pc.get("clock"):
                continue
            if netgraph.is_const(net):
                sig[(n, p)] = (("CONST", net), 0)
            else:
                sig[(n, p)] = _collapse(g, top, lib, net)
    for p, d in top.ports.items():
        if d == "output":
            sig[("PORT", p)] = _collapse(g, top, lib, p)
    return sig


@check("L2.connectivity_preserved", Layer.L2, steps=("Synthesis", "Timing optimization", "route", "filler"),
       category="cross-step", severity="critical",
       title="sequential elements/ports preserved; register/port input sources identical to synthesis "
             "after buffer/inverter-pair collapse")
def connectivity(ws):
    lib = ws.liberty()
    base_top = ws.top("Synthesis")
    gb = netgraph.build(base_top, lib)
    base_seq = _seq_names(base_top, lib)
    base_sig = _signatures(base_top, lib, gb)
    probs = []
    steps = ["CTS", "Timing optimization", "filler"]
    for step in steps:
        top = ws.top(step)
        seq = _seq_names(top, lib)
        if base_seq != seq:
            probs.append(f"{step}: sequential set differs: -{len(base_seq - seq)} +{len(seq - base_seq)} "
                         f"e.g. {_short(sorted(base_seq - seq)[:3] + sorted(seq - base_seq)[:3])}")
        if set(base_top.ports) != set(top.ports):
            probs.append(f"{step}: port set differs: {_short(set(base_top.ports) ^ set(top.ports))}")
        g = netgraph.build(top, lib)
        sig = _signatures(top, lib, g)
        bad = []
        for k, (s0, p0) in base_sig.items():
            if k not in sig:
                continue
            s1, p1 = sig[k]
            # sources that are non-buffer cells keep their instance name through sizing; compare name+pin
            if s0 != s1 or p0 != p1:
                bad.append((k, s0, s1, p0, p1))
        if bad:
            probs.append(f"{step}: {len(bad)}/{len(base_sig)} register/port inputs have a different logical "
                         f"source, e.g. {bad[0]}")
    if probs:
        return Result.fail("; ".join(probs[:3]), measured=len(probs), expected=0, details={"problems": probs})
    return Result.ok(f"{len(base_seq)} sequential cells, {len(base_top.ports)} ports, {len(base_sig)} sinks match "
                     f"synthesis in CTS/timing-opt/filler")


@check("L2.no_floating_or_multidriven", Layer.L2, steps=("Synthesis", "CTS", "Timing optimization", "filler"),
       category="integrity", severity="critical", title="every net has exactly one driver and a load")
def drivers(ws):
    lib = ws.liberty()
    probs, unused_total = [], 0
    for step in ("Synthesis", "CTS", "Timing optimization", "filler"):
        top = ws.top(step)
        g = netgraph.build(top, lib)
        nets = set(g.drivers) | set(g.loads)
        driverless = sorted(n for n in nets if n in g.loads and n not in g.drivers)
        multi = sorted(n for n, d in g.drivers.items() if len([x for x in d if x[0] != "ASSIGN"]) > 1)
        unused = [n for n in g.drivers if n not in g.loads]
        unused_total += len(unused)
        if driverless or multi:
            probs.append(f"{step}: {len(driverless)} driverless nets {_short(driverless, 3)}; "
                         f"{len(multi)} multi-driven {_short(multi, 3)}")
        if g.unknown:
            probs.append(f"{step}: {len(g.unknown)} instances w/o liberty cell: "
                         f"{_short({top.insts[n].cell for n in g.unknown if n in top.insts}, 4)}")
    if probs:
        return Result.fail("; ".join(probs[:3]), measured=len(probs), expected=0, details={"problems": probs})
    return Result.ok(f"single driver on all nets ({unused_total} unloaded driver nets tolerated)")


@check("L2.cell_masters_exist", Layer.L2, steps=("Synthesis", "filler", "Harden"), category="integrity",
       severity="major", title="every instance master exists in LEF and Liberty (or is physical-only)")
def masters(ws):
    lef, lib = ws.lef(), ws.liberty()
    miss_lef, miss_lib = set(), set()
    for step in ("Synthesis", "CTS", "Timing optimization", "filler"):
        for i in ws.top(step).insts.values():
            if i.cell not in lef.macros:
                miss_lef.add(i.cell)
            elif i.cell not in lib.cells and not phys.is_filler(i.cell) and not i.cell.startswith("ANT"):
                miss_lib.add(i.cell)
    d = ws.def_("filler")
    for c in d.comps.values():
        if c.master not in lef.macros:
            miss_lef.add(c.master)
    if miss_lef or miss_lib:
        return Result.fail(f"missing in LEF: {_short(sorted(miss_lef))}; missing in LIB: {_short(sorted(miss_lib))}")
    return Result.ok("all masters resolved")


def _std_area(d, lef):
    a = n = 0
    for c in d.comps.values():
        if phys.is_filler(c.master):
            continue
        M = lef.macros[c.master]
        a += M.w * M.h
        n += 1
    return a, n


def _all_area(d, lef):
    return sum(lef.macros[c.master].w * lef.macros[c.master].h for c in d.comps.values())


@check("L2.metrics_vs_artifacts", Layer.L2,
       steps=("place", "CTS", "legalization", "Timing optimization", "route", "filler", "drc"),
       category="cross-step", severity="critical",
       title="ECC-reported metrics agree with independent recomputation from DEF/netlist/reports")
def metrics_vs_artifacts(ws):
    lef = ws.lef()
    tol_exact, tol_hpwl = 0.01, 0.05
    probs, notes = [], []

    def cmp(label, rep, mine, tol, absolute=0.0):
        if rep is None:
            return
        denom = max(abs(rep), abs(mine), 1e-9)
        if abs(rep - mine) > max(tol * denom, absolute):
            probs.append(f"{label}: reported {rep} vs recomputed {mine:.4g} (tol {tol:.0%})")

    for step in ("place", "diff_sizing", "CTS", "legalization", "Timing optimization", "route", "filler", "drc"):
        if ws.step_dir(step) is None:
            continue
        m = ws.step_metrics(step)
        if not m:
            continue
        d = ws.def_(step)
        dbu = d.dbu
        tag = step
        cmp(f"{tag}.instance_count", m.get("instance_count"), len(d.comps), tol_exact)
        cmp(f"{tag}.instance_area", m.get("instance_area"), _all_area(d, lef), tol_exact)
        sa, sn = _std_area(d, lef)
        cmp(f"{tag}.std_cell_count", m.get("std_cell_count"), sn, tol_exact)
        cmp(f"{tag}.std_cell_area", m.get("std_cell_area"), sa, tol_exact)
        die = (d.die[2] - d.die[0]) * (d.die[3] - d.die[1]) / dbu ** 2
        cmp(f"{tag}.die_area", m.get("die_area"), die, tol_exact)
        cr = phys.core_rect(d)
        core = (cr[2] - cr[0]) * (cr[3] - cr[1]) / dbu ** 2
        cmp(f"{tag}.core_area", m.get("core_area"), core, tol_exact)
        cmp(f"{tag}.core_utilization", m.get("core_utilization"), sa / core, 0.0, absolute=0.011)
        cmp(f"{tag}.net_count", m.get("net_count"), len(d.nets), tol_exact)
        cmp(f"{tag}.io_pin_count", m.get("io_pin_count"), len(d.pins), tol_exact)
        if "place_hpwl" in m and step == "place":
            h, _ = phys.hpwl_um(d, lef)
            cmp("place.place_hpwl", m["place_hpwl"], h, tol_hpwl)
        if step == "route":
            wl = sum(abs(s[3] - s[1]) + abs(s[4] - s[2]) for n in d.nets.values() for s in n.segs) / dbu
            vias = sum(1 for n in d.nets.values() for v in n.vias)
            cmp("route.route_wirelength", m.get("route_wirelength"), wl, tol_exact)
            cmp("route.route_via_count", m.get("route_via_count"), vias, tol_exact)
        if step == "CTS":
            top = ws.top("CTS")
            lib = ws.liberty()
            nbuf = sum(1 for n, i in top.insts.items()
                       if n.startswith("cts_flow") and lib.cells.get(i.cell) and
                       (netgraph.is_buf(lib.cells[i.cell]) or netgraph.is_inv(lib.cells[i.cell])))
            cmp("CTS.cts_buffer_count", m.get("cts_buffer_count"), nbuf, tol_exact)
    # drc_count vs report/feature
    dm = ws.step_metrics("drc")
    p = ws.step_dir("drc")
    if p and "drc_count" in dm:
        js = json.load(open(p / "feature/drc.step.json"))
        cmp("drc.drc_count(step.json)", dm["drc_count"], js.get("drc", {}).get("number", 0), 0.0)
        rpt = p / "report/drc.db.rpt"
        if rpt.exists():
            txt = rpt.read_text(errors="replace")
            mm = re.search(r"(?i)(?:total|number of)[^\n]*?violations?\D*(\d+)", txt)
            if mm:
                cmp("drc.drc_count(report)", dm["drc_count"], int(mm.group(1)), 0.0)
    if probs:
        return Result.fail("; ".join(probs[:4]), measured=len(probs), expected=0, tolerance=f"{tol_exact:.0%} "
                           f"exact / {tol_hpwl:.0%} HPWL", details={"problems": probs})
    return Result.ok("metrics match recomputation", tolerance=f"{tol_exact:.0%} exact / {tol_hpwl:.0%} HPWL")


_ROW = re.compile(r"^(\S+)\s+(-?[\d.]+)\s+(-?[\d.]+)\s+(\d+)\s+(\d+)MHz\s+(-?[\d.]+)\s+(-?[\d.]+)\s+(\d+)")


@check("L2.reports_vs_json", Layer.L2, steps=("sta",), category="cross-step", severity="major",
       title="STA qor_summary.rpt vs qor_summary.json vs timing_paths.json / timing reports")
def reports_vs_json(ws):
    d = ws.step_dir("sta")
    probs, n = [], 0
    for key, js in ws.sta_corners().items():
        corner, rc = key.split("/")
        rpt = d / "report" / corner / rc / "qor_summary.rpt"
        if not rpt.exists():
            probs.append(f"{key}: qor_summary.rpt missing")
            continue
        rows = {}
        for ln in rpt.read_text().splitlines():
            m = _ROW.match(ln.strip())
            if m:
                rows[m.group(1)] = tuple(float(m.group(i)) for i in range(2, 9))
        s = js["summary"]
        got = rows.get("Summary")
        if not got:
            probs.append(f"{key}: no Summary row")
            continue
        n += 1
        exp = (s["setup"]["wns"], s["setup"]["tns"], s["setup"]["nvp"], s["setup"].get("frequency_mhz", 0),
               s["hold"]["wns"], s["hold"]["tns"], s["hold"]["nvp"])
        for lbl, a, b in zip(("setup wns", "setup tns", "setup nvp", "freq", "hold wns", "hold tns", "hold nvp"),
                             got, exp):
            if abs(a - b) > 1e-3 + 0.01 * abs(b):
                probs.append(f"{key}: {lbl} rpt={a} json={b}")
        for g in js.get("path_groups", []):
            r = rows.get(g["name"])
            if r and abs(r[0] - g["setup"]["wns"]) > 1e-3:
                probs.append(f"{key}/{g['name']}: group setup wns rpt={r[0]} json={g['setup']['wns']}")
        # timing_paths.json: worst setup slack must equal summary wns; NVP cross-check with path count
        tp = d / "feature" / corner / rc / "timing_paths.json"
        if tp.exists():
            paths = json.load(open(tp)).get("paths", [])
            for kind in ("setup", "hold"):
                sl = [p["slack_ns"] for p in paths if p.get("analysis_type") == kind]
                if sl and abs(min(sl) - s[kind]["wns"]) > 2e-3:
                    probs.append(f"{key}: min {kind} slack in timing_paths.json {min(sl):.4f} != summary wns "
                                 f"{s[kind]['wns']}")
                neg = sum(1 for x in sl if x < 0)
                if kind == "setup" and s["setup"]["nvp"] and neg > s["setup"]["nvp"]:
                    probs.append(f"{key}: {neg} negative setup paths listed > nvp {s['setup']['nvp']}")
        # negative-slack endpoints in timing_max_*.rpt vs nvp
        neg_ep = set()
        for f in (d / "report" / corner / rc).glob("timing_max_*.rpt"):
            txt = f.read_text(errors="replace")
            for mm in re.finditer(r"slack \((VIOLATED|MET)\)\s+(-?[\d.]+)", txt):
                if mm.group(1) == "VIOLATED":
                    neg_ep.add(mm.group(2))
        if s["setup"]["nvp"] == 0 and neg_ep:
            probs.append(f"{key}: nvp=0 but {len(neg_ep)} VIOLATED setup slacks in timing_max_*.rpt")
    if probs:
        return Result.fail("; ".join(probs[:4]), measured=len(probs), expected=0, details={"problems": probs})
    return Result.ok(f"{n} corner summaries consistent")


@check("L2.qor_vs_raw", Layer.L2, steps=("workspace", "sta", "drc", "lvs"), category="cross-step",
       severity="critical", title="home/qor_report.json gate inputs equal raw per-corner/DRC/LVS artifacts")
def qor_vs_raw(ws):
    q = json.load(open(ws.path / "home/qor_report.json"))
    corners = ws.sta_corners()
    if not corners:
        return Result.skip("no STA corner summaries")
    set_min = min(c["summary"]["setup"]["wns"] for c in corners.values())
    hold_min = min(c["summary"]["hold"]["wns"] for c in corners.values())
    setup_nvp = sum(c["summary"]["setup"]["nvp"] for c in corners.values())
    probs = []
    gates = {g["id"]: g for g in q["feasibility"]["gates"]}
    tol = 1e-3
    g = gates.get("GATE_SETUP_SLACK")
    if g and g.get("timing_slack"):
        ws_ns = g["timing_slack"]["ws_ns"]
        if abs(ws_ns - set_min) > tol:
            probs.append(f"qor_report setup ws {ws_ns} != min over {len(corners)} corners {set_min}")
        if set_min < 0 and g["state"] == "passed":
            probs.append(f"setup gate PASSED although worst corner setup WS = {set_min}")
    g = gates.get("GATE_HOLD_SLACK")
    if g and g.get("timing_slack"):
        if abs(g["timing_slack"]["ws_ns"] - hold_min) > tol:
            probs.append(f"qor_report hold ws {g['timing_slack']['ws_ns']} != min {hold_min}")
        if hold_min < 0 and g["state"] == "passed":
            probs.append(f"hold gate PASSED although worst hold WS = {hold_min}")
    m = ws.metrics()
    if m.get("sta_setup_wns") is not None and abs(m["sta_setup_wns"] - set_min) > tol:
        probs.append(f"sta_setup_wns metric {m['sta_setup_wns']} != min over corners {set_min} "
                     f"(metric corner scoped to one corner)")
    if m.get("sta_setup_violation_count") is not None and m["sta_setup_violation_count"] != setup_nvp:
        probs.append(f"sta_setup_violation_count {m['sta_setup_violation_count']} != sum NVP {setup_nvp}")
    # DRC / LVS counts
    p = ws.step_dir("drc")
    if p:
        n = json.load(open(p / "feature/drc.step.json"))["drc"]["number"]
        if gates.get("GATE_DRC") and (n == 0) != (gates["GATE_DRC"]["state"] == "passed"):
            probs.append(f"DRC gate {gates['GATE_DRC']['state']} but drc.step.json number={n}")
        if m.get("drc_count") is not None and m["drc_count"] != n:
            probs.append(f"drc_count metric {m['drc_count']} != step json {n}")
    p = ws.step_dir("lvs")
    if p:
        lj = json.load(open(p / "feature/lvs.step.json"))
        diff = sum(abs(e.get("difference", 0)) for e in lj.get("entity", []))
        unmatched = 0
        for c in lj.get("connectivity", []):
            for v in (c.get("not_connected"), c.get("unmatched")):
                if isinstance(v, (int, float)):
                    unmatched += v
        if gates.get("GATE_LVS") and ((diff == 0) != (gates["GATE_LVS"]["state"] == "passed")):
            probs.append(f"LVS gate {gates['GATE_LVS']['state']} but entity differences={diff}")
        if m.get("lvs_count") is not None and m["lvs_count"] != 0 and diff == 0:
            probs.append(f"lvs_count metric {m['lvs_count']} but entity differences 0")
    if probs:
        return Result.fail("; ".join(probs[:4]), measured=len(probs), expected=0, tolerance=f"{tol} ns",
                           evidence=[ws.rel(ws.path / "home/qor_report.json")], details={"problems": probs})
    return Result.ok(f"gate inputs match raw (setup min {set_min}, hold min {hold_min})", tolerance=f"{tol} ns")


@check("L2.harden_views_consistent", Layer.L2, steps=("Harden",), category="cross-step", severity="major",
       title="Harden LEF SIZE == DEF die; LEF pins == DEF ports == LIB pins with matching directions")
def harden_views(ws):
    from ecc_verify.parsers import parse_lef, parse_liberty
    h = ws.step_dir("Harden")
    lefp, libp = ws.output_file(h, ".lef"), ws.output_file(h, ".lib")
    if not lefp or not libp:
        return Result.fail("Harden LEF/LIB missing")
    lef = parse_lef(lefp)
    lib = parse_liberty(libp)
    d = ws.def_("filler")
    probs = []
    macro = lef.macros.get(ws.top_name) or next(iter(lef.macros.values()))
    dw, dh = (d.die[2] - d.die[0]) / d.dbu, (d.die[3] - d.die[1]) / d.dbu
    if abs(macro.w - dw) > 0.005 or abs(macro.h - dh) > 0.005:
        probs.append(f"LEF SIZE {macro.w}x{macro.h} != DEF die {dw}x{dh}")
    lef_pins = {p for p, v in macro.pins.items() if v["use"] not in ("POWER", "GROUND")}
    def_pins = {p for p, v in d.pins.items() if v.use not in ("POWER", "GROUND")}
    cell = lib.cells.get(ws.top_name) or next(iter(lib.cells.values()))
    lib_pins = {p for p, v in cell.pins.items() if v["dir"]}
    if lef_pins != def_pins:
        probs.append(f"LEF pins vs DEF ports differ: LEF-only {_short(sorted(lef_pins - def_pins))}, "
                     f"DEF-only {_short(sorted(def_pins - lef_pins))}")
    if lef_pins != lib_pins:
        probs.append(f"LEF pins vs LIB pins differ: LEF-only {_short(sorted(lef_pins - lib_pins))}, "
                     f"LIB-only {_short(sorted(lib_pins - lef_pins))}")
    dm = {"INPUT": "input", "OUTPUT": "output", "INOUT": "inout"}
    for p in lef_pins & lib_pins & def_pins:
        a, b, c = macro.pins[p]["dir"], cell.pins[p]["dir"], d.pins[p].direction
        if not (dm.get(a, a) == b == dm.get(c, c)):
            probs.append(f"pin {p}: direction LEF={a} LIB={b} DEF={c}")
            if len(probs) > 8:
                break
    if probs:
        return Result.fail("; ".join(probs[:4]), measured=len(probs), expected=0, evidence=[ws.rel(lefp), ws.rel(libp)])
    return Result.ok(f"{len(lef_pins)} pins consistent across LEF/DEF/LIB, size {macro.w}x{macro.h}")


@check("L2.sdc_consistency", Layer.L2, steps=("workspace", "sta"), category="cross-step", severity="major",
       title="SDC clock period == 1000/frequency_mhz; clock port exists in the netlist")
def sdc(ws):
    cfg = ws.ecc_toml["design"]
    f = cfg["frequency_mhz"]
    s = ws.sdc()
    probs = []
    main = [c for c in s.clocks if c[2]]
    if not main:
        return Result.fail("no clock with a port in SDC")
    name, per, port = main[0]
    want = 1000.0 / f
    if per is None or abs(per - want) > 1e-6 * want:
        probs.append(f"SDC period {per} != 1000/{f} = {want:.6f}")
    if port != cfg.get("clock_port", port):
        probs.append(f"SDC clock port {port} != ecc.toml clock_port {cfg.get('clock_port')}")
    top = ws.top("Synthesis")
    if port not in top.ports:
        probs.append(f"clock port {port} not in netlist ports")
    q = json.load(open(ws.path / "home/qor_report.json"))
    if abs(q["tclk_ns"] - want) > 1e-6 * want:
        probs.append(f"qor_report tclk_ns {q['tclk_ns']} != {want:.6f}")
    if probs:
        return Result.fail("; ".join(probs), evidence=[ws.rel(ws.project_dir / "ecc.toml")])
    return Result.ok(f"period {want:.4f} ns on port {port}", tolerance="1e-6 rel")
