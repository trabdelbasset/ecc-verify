"""Minimal SDC/ECC-generated-Tcl parser (clocks and IO-delay presence)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ._io import read_text


@dataclass
class SdcData:
    clocks: list = field(default_factory=list)   # (name, period_ns or None, port or None)
    vars: dict = field(default_factory=dict)
    has_input_delay: bool = False
    has_output_delay: bool = False


def parse_sdc(path) -> SdcData:
    text = read_text(path)
    d = SdcData()
    for m in re.finditer(r"^\s*set\s+(\w+)\s+(?:\"([^\"]*)\"|(\S+))\s*$", text, re.M):
        d.vars[m.group(1)] = m.group(2) if m.group(2) is not None else m.group(3)

    for m in re.finditer(r"^\s*set\s+(\w+)\s+\[expr\s*\{\s*([\d.]+)\s*/\s*\$(\w+)\s*\}\]", text, re.M):
        if d.vars.get(m.group(3)):
            d.vars[m.group(1)] = str(float(m.group(2)) / float(d.vars[m.group(3)]))

    def subst(v: str):
        v = v.strip()
        if v.startswith("$"):
            return d.vars.get(v[1:])
        return v

    joined = re.sub(r"\\\n", " ", text)
    for m in re.finditer(r"^\s*create_clock\b(.*)$", joined, re.M):
        a = m.group(1)
        name = re.search(r"-name\s+(\S+)", a)
        per = re.search(r"-period\s+(\S+)", a)
        port = re.search(r"\[get_ports\s+\{?\s*([^\s\}\]]+)", a)
        p = None
        if per:
            v = subst(per.group(1))
            try:
                p = float(v)
            except (TypeError, ValueError):
                # "[expr {1000.0 / $clk_freq_mhz}]"
                mm = re.search(r"-period\s+\[expr\s*\{\s*([\d.]+)\s*/\s*\$(\w+)", a)
                if mm and d.vars.get(mm.group(2)):
                    p = float(mm.group(1)) / float(d.vars[mm.group(2)])
        d.clocks.append((subst(name.group(1)) if name else None, p,
                         subst(port.group(1)) if port else None))
    d.has_input_delay = "set_input_delay" in text
    d.has_output_delay = "set_output_delay" in text
    return d
