"""L3 CTS checks: clock-tree reachability, fanout, depth balance (from the post-CTS netlist)."""
from __future__ import annotations

import json
import re
from collections import defaultdict

from ecc_verify import netgraph
from ecc_verify.model import Layer, Result
from ecc_verify.registry import check


def clock_tree(ws, step="CTS"):
    """Walk from the clock port through buffers/inverters/ICGs.

    Returns dict(sinks={(inst,pin):depth}, nets={net:(fanout,driver_inst)}, unreached=[(inst,pin,why)])."""
    lib = ws.liberty()
    top = ws.top(step)
    g = netgraph.build(top, lib)
    clk = ws.ecc_toml["design"].get("clock_port")
    if clk not in top.ports:
        raise RuntimeError(f"clock port {clk} not in netlist")
    sinks, nets = {}, {}
    frontier = [(clk, 0)]
    seen = {clk}
    while frontier:
        net, depth = frontier.pop()
        loads = [l for l in g.loads.get(net, []) if l[0] != "PORT"]
        nets[net] = (len(loads), (g.drivers.get(net) or [("?", "")])[0][0], depth)
        for inst, pin in loads:
            cell = lib.cells[top.insts[inst].cell]
            if netgraph.is_buf(cell) or netgraph.is_inv(cell) or cell.icg:
                out = next(p for p, v in cell.pins.items() if v["dir"] == "output")
                on = top.insts[inst].conns.get(out)
                if on and on not in seen:
                    seen.add(on)
                    frontier.append((on, depth + (0 if cell.icg else 1)))
            elif cell.seq and cell.pins.get(pin, {}).get("clock"):
                sinks[(inst, pin)] = depth
    unreached = []
    for n, i in top.insts.items():
        c = lib.cells.get(i.cell)
        if not c or not c.seq:
            continue
        for p, net in i.conns.items():
            if c.pins.get(p, {}).get("clock") and (n, p) not in sinks:
                drv = g.drivers.get(net, [("?", "")])[0]
                dc = top.insts[drv[0]].cell if drv[0] in top.insts else drv[0]
                unreached.append((n, p, f"driven by {dc}"))
    return {"sinks": sinks, "nets": nets, "unreached": unreached}


@check("L3.cts.all_sinks_clocked", Layer.L3, steps=("CTS",), category="physics", severity="critical",
       title="every sequential clock pin reached from the clock port via buffers/inverters/ICGs")
def sinks(ws):
    ct = clock_tree(ws)
    n_seq = len(ct["sinks"]) + len(ct["unreached"])
    if ct["unreached"]:
        return Result.fail(f"{len(ct['unreached'])}/{n_seq} clock pins not reached from the clock port "
                           f"(e.g. {ct['unreached'][:3]})", measured=len(ct["unreached"]), expected=0,
                           details={"examples": ct["unreached"][:20]})
    return Result.ok(f"all {n_seq} sequential clock pins reached", measured=n_seq)


@check("L3.cts.fanout_limit", Layer.L3, steps=("CTS",), category="physics", severity="major",
       title="no clock buffer output exceeds cts max_fanout")
def fanout(ws):
    cfg = json.load(open(ws.path / "config/cts_ecc.json"))
    lim = int(cfg.get("max_fanout") or ws.param("max_fanout", 0))
    ct = clock_tree(ws)
    bad = {n: v[0] for n, v in ct["nets"].items() if v[0] > lim}
    worst = max((v[0] for v in ct["nets"].values()), default=0)
    if bad:
        why, ev = "", [ws.rel(ws.path / "config/cts_ecc.json")]
        log = ws.step_dir("CTS") / "log/CTS.log"
        if not log.exists():
            log = ws.step_dir("CTS") / "log/cts.log"
        if log.exists():
            for i, ln in enumerate(log.read_text(errors="replace").splitlines(), 1):
                if "replaces the configured value" in ln:
                    why = " CTS log: " + re.sub(r"^.*applyDesignRules\]\s*", "", re.sub(r"\x1b\[[0-9;]*m", "", ln)).strip()
                    ev.append(f"{ws.rel(log)}:{i}")
                    break
        return Result.fail(f"{len(bad)} clock nets exceed max_fanout={lim} (worst {worst}, e.g. "
                           f"{list(bad.items())[:2]}).{why}", measured=worst, expected=f"<= {lim}",
                           evidence=ev, details={"nets": dict(list(bad.items())[:20])})
    return Result.ok(f"max clock fanout {worst} <= {lim}", measured=worst, expected=f"<= {lim}")


@check("L3.cts.depth_balance", Layer.L3, steps=("CTS",), category="physics", severity="major",
       title="min/max buffer depth recomputed vs clock_path_min/max_buffer metrics")
def depth(ws):
    m = ws.step_metrics("CTS")
    if "clock_path_max_buffer" not in m:
        return Result.skip("no clock_path_* metrics")
    ct = clock_tree(ws)
    ds = list(ct["sinks"].values())
    if not ds:
        return Result.fail("no sinks")
    mine = (min(ds), max(ds))
    rep = (m["clock_path_min_buffer"], m["clock_path_max_buffer"])
    if mine != rep:
        return Result.fail(f"depth min/max recomputed {mine} vs reported {rep}", measured=mine, expected=rep)
    return Result.ok(f"depth min/max {mine} matches reported", measured=mine, expected=rep)
