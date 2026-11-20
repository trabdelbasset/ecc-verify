"""LEF parser: tech layers/sites/vias and cell macros (sizes, pins, rects)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ._io import open_text


@dataclass
class Layer:
    name: str
    type: str = ""
    direction: str = ""
    pitch: float = 0.0
    width: float = 0.0
    offset: float = 0.0
    spacing: float = 0.0


@dataclass
class Macro:
    name: str
    cls: str = ""
    w: float = 0.0
    h: float = 0.0
    site: str = ""
    pins: dict = field(default_factory=dict)  # name -> {"dir","use","rects":[(layer,x0,y0,x1,y1)]}
    obs: list = field(default_factory=list)


@dataclass
class LefData:
    dbu: int = 1000
    layers: dict = field(default_factory=dict)
    sites: dict = field(default_factory=dict)    # name -> (w, h)
    vias: dict = field(default_factory=dict)     # name -> {"rects": [(layer,x0,y0,x1,y1)]}
    macros: dict = field(default_factory=dict)

    def merge(self, other: "LefData"):
        self.layers.update(other.layers)
        self.sites.update(other.sites)
        self.vias.update(other.vias)
        self.macros.update(other.macros)
        if other.dbu != 1000 or not self.dbu:
            self.dbu = other.dbu


def parse_lef(path) -> LefData:
    d = LefData()
    ctx = None       # ("LAYER", obj) | ("MACRO", obj) | ("SITE", name) | ("VIA", name)
    pin = None
    portlayer = None
    inobs = False
    with open_text(path) as f:
        for line in f:
            t = line.strip().rstrip(";").split()
            if not t:
                continue
            k = t[0]
            if ctx is None:
                if line[0] in " \t" and k != "DATABASE":
                    continue
                if k == "DATABASE" and t[1] == "MICRONS":
                    d.dbu = int(t[2])
                elif k == "LAYER" and len(t) == 2:
                    ctx = ("LAYER", Layer(t[1]))
                elif k == "SITE":
                    ctx = ("SITE", t[1], [0.0, 0.0])
                elif k == "MACRO":
                    ctx = ("MACRO", Macro(t[1]))
                    pin = None
                    inobs = False
                elif k == "VIA":
                    ctx = ("VIA", t[1], [])
                    portlayer = None
                continue
            kind = ctx[0]
            if kind == "LAYER":
                L = ctx[1]
                if k == "TYPE":
                    L.type = t[1]
                elif k == "DIRECTION":
                    L.direction = t[1]
                elif k == "PITCH":
                    L.pitch = float(t[1])
                elif k == "WIDTH":
                    L.width = float(t[1])
                elif k == "OFFSET":
                    L.offset = float(t[1])
                elif k == "SPACING" and not L.spacing:
                    L.spacing = float(t[1])
                elif k == "END" and len(t) > 1 and t[1] == L.name:
                    d.layers[L.name] = L
                    ctx = None
            elif kind == "SITE":
                if k == "SIZE":
                    ctx[2][:] = [float(t[1]), float(t[3])]
                elif k == "END" and t[1] == ctx[1]:
                    d.sites[ctx[1]] = tuple(ctx[2])
                    ctx = None
            elif kind == "VIA":
                if k == "LAYER":
                    portlayer = t[1]
                elif k == "RECT":
                    ctx[2].append((portlayer, *map(float, t[1:5])))
                elif k == "END" and t[1] == ctx[1]:
                    d.vias[ctx[1]] = {"rects": ctx[2]}
                    ctx = None
            elif kind == "MACRO":
                M = ctx[1]
                if k == "CLASS":
                    M.cls = " ".join(t[1:])
                elif k == "SIZE":
                    M.w, M.h = float(t[1]), float(t[3])
                elif k == "SITE":
                    M.site = t[1]
                elif k == "PIN":
                    pin = {"dir": "", "use": "", "rects": []}
                    M.pins[t[1]] = pin
                    inobs = False
                    pname = t[1]
                elif k == "OBS":
                    inobs = True
                    pin = None
                elif k == "DIRECTION" and pin is not None:
                    pin["dir"] = t[1]
                elif k == "USE" and pin is not None:
                    pin["use"] = t[1]
                elif k == "LAYER":
                    portlayer = t[1]
                elif k == "RECT":
                    r = (portlayer, *map(float, t[1:5]))
                    if inobs:
                        M.obs.append(r)
                    elif pin is not None:
                        pin["rects"].append(r)
                elif k == "END":
                    if len(t) > 1 and t[1] == M.name:
                        d.macros[M.name] = M
                        ctx = None
                    elif len(t) > 1 and pin is not None and t[1] in M.pins:
                        pin = None
                    elif len(t) == 1:
                        inobs = False
    return d
