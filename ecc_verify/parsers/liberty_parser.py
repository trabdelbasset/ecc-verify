"""Light Liberty parser: per-cell area/leakage/sequential flag and per-pin direction/limits."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ._io import open_text


@dataclass
class LibCell:
    name: str
    area: float = 0.0
    leakage: float = 0.0
    seq: bool = False
    icg: bool = False   # integrated clock gate (clock_gating_integrated_cell)
    pins: dict = field(default_factory=dict)  # name -> {"dir","cap","max_tran","max_cap","clock","func"}


@dataclass
class LibData:
    cells: dict = field(default_factory=dict)
    default_max_tran: float | None = None
    default_max_cap: float | None = None
    time_unit: str = ""
    cap_unit: str = ""

    def merge(self, o: "LibData"):
        self.cells.update(o.cells)
        self.default_max_tran = self.default_max_tran if self.default_max_tran is not None else o.default_max_tran
        self.default_max_cap = self.default_max_cap if self.default_max_cap is not None else o.default_max_cap


_CELL = re.compile(r'cell\s*\(\s*"?([^")\s]+)"?\s*\)\s*\{')
_PIN = re.compile(r'pin\s*\(\s*"?([^")\s]+)"?\s*\)\s*\{')
_ATTR = re.compile(r'^\s*([A-Za-z_]+)\s*:\s*"?([^";]*)"?\s*;')


def parse_liberty(path) -> LibData:
    d = LibData()
    depth = 0
    cell = None
    pin = None
    with open_text(path) as f:
        for line in f:
            if depth == 1:
                m = _ATTR.match(line)
                if m:
                    k, v = m.groups()
                    if k == "default_max_transition":
                        d.default_max_tran = float(v)
                    elif k == "default_max_capacitance":
                        d.default_max_cap = float(v)
                    elif k == "time_unit":
                        d.time_unit = v
                m = _CELL.search(line)
                if m:
                    cell = LibCell(m.group(1))
                    d.cells[cell.name] = cell
            elif depth == 2 and cell is not None:
                m = _ATTR.match(line)
                if m:
                    k, v = m.groups()
                    if k == "area":
                        cell.area = float(v)
                    elif k == "cell_leakage_power":
                        cell.leakage = float(v)
                    elif k == "clock_gating_integrated_cell":
                        cell.icg = True
                        cell.seq = False
                elif line.lstrip().startswith(("ff ", "ff(", "latch ", "latch(", "ff_bank", "latch_bank")):
                    cell.seq = not cell.icg
                else:
                    m = _PIN.search(line)
                    if m:
                        pin = {"dir": "", "cap": None, "max_tran": None, "max_cap": None, "clock": False,
                               "func": ""}
                        cell.pins[m.group(1)] = pin
            elif depth == 3 and pin is not None:
                m = _ATTR.match(line)
                if m:
                    k, v = m.groups()
                    if k == "direction":
                        pin["dir"] = v.strip()
                    elif k == "capacitance":
                        pin["cap"] = float(v)
                    elif k == "max_transition":
                        pin["max_tran"] = float(v)
                    elif k == "max_capacitance":
                        pin["max_cap"] = float(v)
                    elif k == "clock":
                        pin["clock"] = v.strip() == "true"
                    elif k == "function":
                        pin["func"] = v
            depth += line.count("{") - line.count("}")
            if depth <= 2 and pin is not None and depth < 3:
                pin = None
            if depth <= 1:
                cell = None if depth < 2 else cell
    return d
