"""Structural (gate-level) Verilog parser."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ._io import norm, read_text

_COMMENT = re.compile(r"/\*.*?\*/|//[^\n]*", re.S)
_PORTCONN = re.compile(r"\.\s*(\\\S+\s|[\w$]+)\s*\(\s*([^()]*?)\s*\)")


@dataclass
class Inst:
    cell: str
    name: str
    conns: dict  # pin -> net (str, normalised); unconnected pins map to ""


@dataclass
class Module:
    name: str
    ports: dict = field(default_factory=dict)    # port -> direction (bit-blasted names when declared as bus)
    wires: set = field(default_factory=set)
    insts: dict = field(default_factory=dict)    # name -> Inst
    assigns: list = field(default_factory=list)  # (lhs, rhs)


def _expand(rng, name):
    if not rng:
        return [name]
    hi, lo = [int(v) for v in re.findall(r"-?\d+", rng)[:2]]
    step = -1 if hi >= lo else 1
    return [f"{name}[{i}]" for i in range(hi, lo + step, step)]


def parse_verilog_text(text: str) -> dict:
    text = _COMMENT.sub(" ", text)
    mods: dict[str, Module] = {}
    for mm in re.finditer(r"\bmodule\s+(\\\S+\s|[\w$]+)\s*(\(.*?\))?\s*;(.*?)\bendmodule\b", text, re.S):
        mod = Module(norm(mm.group(1)))
        body = mm.group(3)
        for stmt in body.split(";"):
            s = stmt.strip()
            if not s:
                continue
            m = re.match(r"(input|output|inout)\s*(\[[^\]]*\])?\s*(.*)", s, re.S)
            if m:
                for nm in re.split(r",\s*", m.group(3)):
                    nm = nm.strip()
                    if nm:
                        for b in _expand(m.group(2), norm(nm)):
                            mod.ports[b] = m.group(1)
                continue
            m = re.match(r"wire\s*(\[[^\]]*\])?\s*(.*)", s, re.S)
            if m:
                for nm in re.split(r",\s*", m.group(2)):
                    nm = nm.strip()
                    if nm:
                        mod.wires.update(_expand(m.group(1), norm(nm)))
                continue
            m = re.match(r"assign\s+(.*?)=(.*)", s, re.S)
            if m:
                mod.assigns.append((norm(m.group(1)), norm(m.group(2))))
                continue
            m = re.match(r"(\\\S+\s|[\w$]+)\s+(\\\S+\s|[\w$]+)\s*\((.*)\)\s*$", s, re.S)
            if m and m.group(1) not in ("wire", "reg", "supply0", "supply1", "tri"):
                conns = {}
                for pm in _PORTCONN.finditer(m.group(3)):
                    conns[norm(pm.group(1))] = norm(pm.group(2))
                inst = Inst(norm(m.group(1)), norm(m.group(2)), conns)
                mod.insts[inst.name] = inst
        mods[mod.name] = mod
    return mods


def parse_verilog(path) -> dict:
    return parse_verilog_text(read_text(path))
