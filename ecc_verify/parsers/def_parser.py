"""Streaming DEF parser (plain or .gz). Coordinates stay in DBU; see DefData.dbu."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ._io import norm, open_text

_PT = re.compile(r"\(\s*(\*|-?\d+)\s+(\*|-?\d+)(?:\s+-?\d+)?\s*\)")
_CONN = re.compile(r"\(\s*(\S+)\s+(\S+)\s*\)")


@dataclass
class Row:
    name: str
    site: str
    x: int
    y: int
    orient: str
    nx: int
    ny: int
    sx: int
    sy: int


@dataclass
class Comp:
    name: str
    master: str
    status: str
    x: int
    y: int
    orient: str
    source: str = ""


@dataclass
class Pin:
    name: str
    net: str
    direction: str
    use: str
    layer: str
    rect: tuple  # relative to (x,y)
    x: int
    y: int
    orient: str
    status: str


@dataclass
class Net:
    name: str
    use: str = "SIGNAL"
    conns: list = field(default_factory=list)   # (inst, pin); inst == "PIN" for top ports
    segs: list = field(default_factory=list)    # (layer, x1, y1, x2, y2)
    vias: list = field(default_factory=list)    # (via_name, x, y, layer_of_wire_statement)
    patches: list = field(default_factory=list) # (layer, x1, y1, x2, y2)
    routed: bool = False


@dataclass
class SNet:
    name: str
    use: str
    shapes: list = field(default_factory=list)  # (layer, width, shape, x1, y1, x2, y2)


@dataclass
class DefData:
    design: str = ""
    dbu: int = 1000
    die: tuple = (0, 0, 0, 0)
    rows: list = field(default_factory=list)
    tracks: list = field(default_factory=list)  # (dir, start, num, step, layer)
    comps: dict = field(default_factory=dict)
    pins: dict = field(default_factory=dict)
    nets: dict = field(default_factory=dict)
    snets: dict = field(default_factory=dict)
    vias: dict = field(default_factory=dict)    # via name -> (layers tuple)
    declared: dict = field(default_factory=dict)  # section -> declared count


def parse_routing(text: str, net: Net, special: bool = False, snet: SNet | None = None):
    """Parse the text after '+ ROUTED' of a net statement."""
    pieces = re.split(r"\bNEW\b", text)
    for piece in pieces:
        toks = piece.replace("(", " ( ").replace(")", " ) ").split()
        if not toks:
            continue
        i = 0
        layer = None
        width = None
        shape = ""
        # leading: layer [width] [+ SHAPE x]
        layer = toks[0]
        i = 1
        cx = cy = None
        while i < len(toks):
            t = toks[i]
            if t == "(":
                # point: ( x y [ext] )
                j = toks.index(")", i)
                vals = toks[i + 1:j]
                if i > 0 and toks[i - 1] == "RECT" and len(vals) == 4:
                    if cx is not None and not special:
                        net.patches.append((layer, cx + int(vals[0]), cy + int(vals[1]),
                                            cx + int(vals[2]), cy + int(vals[3])))
                    i = j + 1
                    continue
                x, y = vals[0], vals[1]
                nx = cx if x == "*" else int(x)
                ny = cy if y == "*" else int(y)
                if cx is not None and (nx != cx or ny != cy):
                    if special and snet is not None:
                        snet.shapes.append((layer, width, shape, cx, cy, nx, ny))
                    else:
                        net.segs.append((layer, cx, cy, nx, ny))
                elif cx is None and special and snet is not None:
                    pass
                cx, cy = nx, ny
                i = j + 1
            elif t == "+":
                if i + 2 < len(toks) and toks[i + 1] == "SHAPE":
                    shape = toks[i + 2]
                    i += 3
                else:
                    i += 1
            elif re.fullmatch(r"-?\d+", t):
                if width is None:
                    width = int(t)
                i += 1
            elif t == "RECT":
                i += 1
            elif t == "MASK":
                i += 2
            else:
                # via name at current point
                if cx is not None and not special:
                    net.vias.append((t, cx, cy, layer))
                elif cx is not None and special and snet is not None:
                    snet.shapes.append((layer, width, "VIA:" + t, cx, cy, cx, cy))
                i += 1


def parse_def(path) -> DefData:
    d = DefData()
    section = None
    buf: list[str] = []
    with open_text(path) as f:
        for line in f:
            s = line.strip()
            if not s:
                continue
            if section is None:
                if s.startswith("DESIGN "):
                    d.design = s.split()[1]
                elif s.startswith("UNITS DISTANCE"):
                    d.dbu = int(s.split()[3])
                elif s.startswith("DIEAREA"):
                    n = [int(v) for v in re.findall(r"-?\d+", s)]
                    d.die = (n[0], n[1], n[2], n[3]) if len(n) >= 4 else (n[0], n[1], n[-2], n[-1])
                elif s.startswith("ROW "):
                    t = s.rstrip(";").split()
                    d.rows.append(Row(t[1], t[2], int(t[3]), int(t[4]), t[5], int(t[7]), int(t[9]),
                                      int(t[11]), int(t[12])))
                elif s.startswith("TRACKS "):
                    t = s.rstrip(";").split()
                    d.tracks.append((t[1], int(t[2]), int(t[4]), int(t[6]), t[8]))
                else:
                    m = re.match(r"(COMPONENTS|PINS|NETS|SPECIALNETS|VIAS)\s+(\d+)", s)
                    if m:
                        section = m.group(1)
                        d.declared[section] = int(m.group(2))
                        buf = []
                continue
            if s.startswith("END " + section):
                section = None
                continue
            buf.append(s)
            if s.endswith(";"):
                stmt = " ".join(buf)
                buf = []
                _statement(d, section, stmt)
    return d


def _statement(d: DefData, section: str, stmt: str):
    stmt = stmt.rstrip(";").strip()
    if not stmt.startswith("-"):
        return
    body = stmt[1:].strip()
    if section == "COMPONENTS":
        t = body.split()
        name, master = norm(t[0]), t[1]
        status, x, y, orient = "UNPLACED", 0, 0, "N"
        source = ""
        m = re.search(r"\+\s+(PLACED|FIXED|COVER)\s*\(\s*(-?\d+)\s+(-?\d+)\s*\)\s*(\S+)", body)
        if m:
            status, x, y, orient = m.group(1), int(m.group(2)), int(m.group(3)), m.group(4)
        m = re.search(r"\+\s+SOURCE\s+(\S+)", body)
        if m:
            source = m.group(1)
        d.comps[name] = Comp(name, master, status, x, y, orient, source)
    elif section == "PINS":
        t = body.split()
        name = norm(t[0])
        net = name
        m = re.search(r"\+\s+NET\s+(\S+)", body)
        if m:
            net = norm(m.group(1))
        dirn = (re.search(r"\+\s+DIRECTION\s+(\S+)", body) or [None, ""])[1]
        use = (re.search(r"\+\s+USE\s+(\S+)", body) or [None, "SIGNAL"])[1]
        layer, rect = "", (0, 0, 0, 0)
        m = re.search(r"\+\s+LAYER\s+(\S+)\s*\(\s*(-?\d+)\s+(-?\d+)\s*\)\s*\(\s*(-?\d+)\s+(-?\d+)\s*\)", body)
        if m:
            layer, rect = m.group(1), tuple(int(m.group(i)) for i in range(2, 6))
        x = y = 0
        orient = "N"
        status = ""
        m = re.search(r"\+\s+(PLACED|FIXED|COVER)\s*\(\s*(-?\d+)\s+(-?\d+)\s*\)\s*(\S+)", body)
        if m:
            status, x, y, orient = m.group(1), int(m.group(2)), int(m.group(3)), m.group(4)
        d.pins[name] = Pin(name, net, dirn, use, layer, rect, x, y, orient, status)
    elif section == "NETS":
        m = re.match(r"(\S+)", body)
        name = norm(m.group(1))
        net = Net(name)
        rest = body[m.end():]
        k = rest.find("+")
        conn_txt = rest if k < 0 else rest[:k]
        for a, b in _CONN.findall(conn_txt):
            net.conns.append((norm(a), b))
        mu = re.search(r"\+\s+USE\s+(\S+)", rest)
        if mu:
            net.use = mu.group(1)
        mr = re.search(r"\+\s+(?:ROUTED|FIXED|COVER)\b", rest)
        if mr:
            net.routed = True
            parse_routing(rest[mr.end():], net)
        d.nets[name] = net
    elif section == "SPECIALNETS":
        m = re.match(r"(\S+)", body)
        name = norm(m.group(1))
        mu = re.search(r"\+\s+USE\s+(\S+)", body)
        sn = SNet(name, mu.group(1) if mu else "")
        mr = re.search(r"\+\s+(?:ROUTED|FIXED|COVER)\b", body)
        if mr:
            tmp = Net(name)
            parse_routing(body[mr.end():], tmp, special=True, snet=sn)
        d.snets[name] = sn
    elif section == "VIAS":
        t = body.split()
        m = re.search(r"\+\s+LAYERS\s+(\S+)\s+(\S+)\s+(\S+)", body)
        d.vias[t[0]] = m.groups() if m else ()
