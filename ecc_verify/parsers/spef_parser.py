"""SPEF parser: per-net total capacitance, resistor counts, units."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ._io import norm, open_text


@dataclass
class SpefData:
    design: str = ""
    c_unit_ff: float = 1.0   # multiplier to femtofarad
    r_unit_ohm: float = 1.0
    t_unit_ns: float = 1.0
    nets: dict = field(default_factory=dict)       # name -> total cap in fF
    res_count: dict = field(default_factory=dict)  # name -> number of *RES entries

    @property
    def total_cap_ff(self):
        return sum(self.nets.values())


_CU = {"FF": 1.0, "PF": 1e3, "NF": 1e6, "F": 1e15}
_RU = {"OHM": 1.0, "KOHM": 1e3, "MOHM": 1e6}


def parse_spef(path) -> SpefData:
    d = SpefData()
    name_map: dict[str, str] = {}
    in_map = False
    cur = None
    in_res = False
    with open_text(path) as f:
        for line in f:
            s = line.strip()
            if not s:
                continue
            if s.startswith("*DESIGN "):
                d.design = s.split('"')[1] if '"' in s else s.split()[1]
            elif s.startswith("*C_UNIT"):
                t = s.split()
                d.c_unit_ff = float(t[1]) * _CU.get(t[2].upper(), 1.0)
            elif s.startswith("*R_UNIT"):
                t = s.split()
                d.r_unit_ohm = float(t[1]) * _RU.get(t[2].upper(), 1.0)
            elif s.startswith("*T_UNIT"):
                t = s.split()
                d.t_unit_ns = float(t[1]) * {"NS": 1.0, "PS": 1e-3, "US": 1e3}.get(t[2].upper(), 1.0)
            elif s.startswith("*NAME_MAP"):
                in_map = True
            elif in_map and s.startswith("*") and s[1:2].isdigit():
                t = s.split(None, 1)
                if len(t) == 2 and ":" not in t[0]:
                    name_map[t[0]] = norm(t[1])
                    continue
                in_map = False
            elif s.startswith("*D_NET"):
                in_map = False
                t = s.split()
                key = t[1]
                nm = name_map.get(key, norm(key))
                cur = nm
                d.nets[nm] = float(t[2]) * d.c_unit_ff
                d.res_count[nm] = 0
                in_res = False
            elif s.startswith("*RES"):
                in_res = True
            elif s.startswith("*END"):
                cur = None
                in_res = False
            elif s.startswith("*CAP"):
                in_res = False
            elif in_res and cur and s[0].isdigit():
                d.res_count[cur] += 1
    return d
