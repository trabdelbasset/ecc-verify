"""Netlist connectivity graph built from a parsed structural module + Liberty directions."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_CONST = re.compile(r"^\d*'[bBhHdD][0-9a-fA-FxXzZ_]+$")


def is_const(net: str) -> bool:
    return bool(_CONST.match(net)) or net == ""


@dataclass
class Graph:
    drivers: dict = field(default_factory=dict)  # net -> [(inst|"PORT", pin)]
    loads: dict = field(default_factory=dict)    # net -> [(inst|"PORT", pin)]
    unknown: list = field(default_factory=list)  # instances whose master is not in the lib


def build(mod, lib) -> Graph:
    g = Graph()
    for p, d in mod.ports.items():
        if d == "input":
            g.drivers.setdefault(p, []).append(("PORT", p))
        elif d == "output":
            g.loads.setdefault(p, []).append(("PORT", p))
        else:
            g.drivers.setdefault(p, []).append(("PORT", p))
            g.loads.setdefault(p, []).append(("PORT", p))
    for a, b in mod.assigns:   # treat 'assign lhs = rhs' as a wire alias driver
        g.loads.setdefault(b, []).append(("ASSIGN", a))
        g.drivers.setdefault(a, []).append(("ASSIGN", b))
    for inst in mod.insts.values():
        cell = lib.cells.get(inst.cell)
        if cell is None:
            if not inst.cell.startswith(("FILL", "ANT")):
                g.unknown.append(inst.name)
            continue
        for pin, net in inst.conns.items():
            if is_const(net):
                continue
            pd = cell.pins.get(pin, {}).get("dir", "")
            if pd == "output":
                g.drivers.setdefault(net, []).append((inst.name, pin))
            elif pd == "inout":
                g.drivers.setdefault(net, []).append((inst.name, pin))
                g.loads.setdefault(net, []).append((inst.name, pin))
            else:
                g.loads.setdefault(net, []).append((inst.name, pin))
    return g


def is_buf(cell) -> bool:
    """single-input single-output cell with function == input (buffer)."""
    ins = [p for p, v in cell.pins.items() if v["dir"] == "input"]
    outs = [(p, v) for p, v in cell.pins.items() if v["dir"] == "output"]
    return len(ins) == 1 and len(outs) == 1 and outs[0][1]["func"].strip("() ") == ins[0] and not cell.seq


def is_inv(cell) -> bool:
    ins = [p for p, v in cell.pins.items() if v["dir"] == "input"]
    outs = [(p, v) for p, v in cell.pins.items() if v["dir"] == "output"]
    if len(ins) != 1 or len(outs) != 1 or cell.seq:
        return False
    f = outs[0][1]["func"].replace(" ", "")
    return f in (f"!{ins[0]}", f"(!{ins[0]})", f"{ins[0]}'")
