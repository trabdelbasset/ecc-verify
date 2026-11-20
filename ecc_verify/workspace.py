"""Workspace: locates artifacts of a finished ECC project and lazily caches parsed objects.

Public API (relied upon by other checks / agents):
  ws.name                 design (project directory) name
  ws.project_dir          Path to <root>/<design>
  ws.path                 Path to <project_dir>/default
  ws.top_name             top module name (project_json base_design.top_module; may differ from ws.name)
  ws.flow                 parsed home/flow.json (dict)
  ws.params               parsed home/params.toml (dict)
  ws.param(key, default)  look a key up at top level, then in nested tables
  ws.ecc_toml             parsed project ecc.toml (dict)
  ws.step_dir(prefix)     Path of the step dir whose name starts with prefix ("route", "CTS", "Timing optimization"...)
  ws.step_dirs            {dir name: Path} for existing step dirs in flow order
  ws.metrics()            merged qor_metrics {id: value} (final > gate > trend; later step wins on ties)
  ws.step_metrics(step)   {id: value} of a single step
  ws.def_(step)           DefData of <step>/output/*.def.gz (cached)       -> parsers.def_parser
  ws.verilog(step)        {module: Module} of <step>/output/*.v.gz (cached) -> parsers.verilog_parser
  ws.top(step)            the top Module (named like the design) of verilog(step)
  ws.lef()                LefData (tech + all cell LEFs, from config/db_ecc.json)
  ws.liberty()            LibData (cell liberty of the first corner in db_ecc.json lib_path)
  ws.spef(corner)         SpefData of RCX_ecc/output/<design>_<corner>.spef, e.g. "Cworst_125C"
  ws.spef_files()         {corner: Path}
  ws.sta_corners()        {"MAX_125/Cworst": qor_summary dict}
  ws.sdc()                SdcData of the first origin/post_CTS tcl
  ws.output_file(step, suffix)  first file in <step>/output ending with suffix, or None
"""
from __future__ import annotations

import json
import re
import tomllib
from functools import lru_cache
from pathlib import Path

from ecc_verify import parsers as P

STEP_DIRS = (
    "Synthesis_yosys", "preFloorplan_ecc", "macroPlacement_dreamplace", "postFloorplan_ecc", "preplace_sizer",
    "place_dreamplace", "diff_sizing_dreamplace", "CTS_ecc", "legalization_dreamplace",
    "timing_optimization_sizer", "route_ecc", "filler_ecc", "lvs_ecc", "drc_ecc", "antennaCheck_ecc",
    "postRouteLec_kepler_formal", "RCX_ecc", "sta_ecc", "powerAnalysis_ecc", "emir_ecc", "Harden_ecc",
)
# coverage-row name (model.STEPS) per step dir
STEP_ROW = {
    "Synthesis_yosys": "Synthesis", "preFloorplan_ecc": "preFloorplan", "macroPlacement_dreamplace": "macroPlacement",
    "postFloorplan_ecc": "postFloorplan", "preplace_sizer": "preplace", "place_dreamplace": "place",
    "diff_sizing_dreamplace": "diff_sizing", "CTS_ecc": "CTS", "legalization_dreamplace": "legalization",
    "timing_optimization_sizer": "Timing optimization", "route_ecc": "route", "filler_ecc": "filler",
    "lvs_ecc": "lvs", "drc_ecc": "drc", "antennaCheck_ecc": "antennaCheck",
    "postRouteLec_kepler_formal": "postRouteLec", "RCX_ecc": "RCX", "sta_ecc": "sta",
    "powerAnalysis_ecc": "powerAnalysis", "emir_ecc": "emir", "Harden_ecc": "Harden",
}
_STEP_ALIASES = {
    "synthesis": "Synthesis",
    "pre_floorplan": "preFloorplan",
    "prefloorplan": "preFloorplan",
    "macro_placement": "macroPlacement",
    "macroplacement": "macroPlacement",
    "post_floorplan": "postFloorplan",
    "postfloorplan": "postFloorplan",
    "preplace": "preplace",
    "place": "place",
    "placement": "place",
    "diff_sizing": "diff_sizing",
    "cts": "CTS",
    "legalization": "legalization",
    "timing_optimization": "Timing optimization",
    "route": "route",
    "routing": "route",
    "filler": "filler",
    "lvs": "lvs",
    "drc": "drc",
    "antennacheck": "antennaCheck",
    "antenna_check": "antennaCheck",
    "postroutelec": "postRouteLec",
    "post_route_lec": "postRouteLec",
    "rcx": "RCX",
    "sta": "sta",
    "poweranalysis": "powerAnalysis",
    "power_analysis": "powerAnalysis",
    "emir": "emir",
    "harden": "Harden",
}
_TOOL_SUFFIXES = ("_kepler_formal", "_dreamplace", "_yosys", "_sizer", "_ecc")
ROLE_RANK = {"final": 3, "gate": 2, "trend": 1}


def canonical_step_row(name: str) -> str:
    if name in STEP_ROW:
        return STEP_ROW[name]
    token = re.sub(r"^\d+_", "", name.strip()).lower().replace(" ", "_")
    for suffix in _TOOL_SUFFIXES:
        if token.endswith(suffix):
            token = token[: -len(suffix)]
            break
    if token in _STEP_ALIASES:
        return _STEP_ALIASES[token]
    compact = re.sub(r"[^a-z0-9]", "", token)
    if compact in _STEP_ALIASES:
        return _STEP_ALIASES[compact]
    return name


def flow_step_dir(step: dict) -> str:
    if step.get("directory"):
        return str(step["directory"])
    return f"{step['name'].lower().replace(' ', '_')}_{step['tool']}" if step["name"] == "Timing optimization" \
        else f"{step['name']}_{step['tool']}"


def _norm_prefix(p: str) -> str:
    return p.lower().replace(" ", "_")


def _json(path: Path):
    with open(path) as f:
        return json.load(f)


@lru_cache(maxsize=8)
def _lef_cached(paths: tuple):
    d = None
    for p in paths:
        x = P.parse_lef(p)
        if d is None:
            d = x
        else:
            d.merge(x)
    return d


@lru_cache(maxsize=8)
def _lib_cached(paths: tuple):
    d = None
    for p in paths:
        x = P.parse_liberty(p)
        if d is None:
            d = x
        else:
            d.merge(x)
    return d


class Workspace:
    def __init__(self, project_dir, ws_id: str = "default"):
        self.project_dir = Path(project_dir)
        self.name = self.project_dir.name
        self.path = self.project_dir / ws_id
        self._cache: dict = {}

    def __repr__(self):
        return f"Workspace({self.name})"

    # ---- plain files -------------------------------------------------
    def _memo_lru(self, key, fn, keep=3):
        """Memo with a bound on how many entries of the same kind (key[0]) stay resident."""
        if key in self._cache:
            return self._cache[key]
        kind = key[0]
        same = [k for k in self._cache if isinstance(k, tuple) and k[0] == kind]
        while len(same) >= keep:
            self._cache.pop(same.pop(0))
        self._cache[key] = fn()
        return self._cache[key]

    def _memo(self, key, fn):
        if key not in self._cache:
            self._cache[key] = fn()
        return self._cache[key]

    @property
    def flow(self) -> dict:
        return self._memo("flow", lambda: _json(self.path / "home/flow.json"))

    @property
    def params(self) -> dict:
        def load():
            p = self.path / "home/params.toml"
            with open(p, "rb") as f:
                return tomllib.load(f)
        return self._memo("params", load)

    @property
    def ecc_toml(self) -> dict:
        def load():
            with open(self.project_dir / "ecc.toml", "rb") as f:
                return tomllib.load(f)
        return self._memo("ecc_toml", load)

    @property
    def project_json(self) -> dict:
        return self._memo("project_json", lambda: _json(self.project_dir / "project.json"))

    @property
    def top_name(self) -> str:
        """Top module name (differs from the project name for NPC/gcd/picorv32 projects)."""
        return self.project_json.get("base_design", {}).get("top_module") or self.name

    def param(self, key, default=None):
        def walk(d):
            if key in d:
                return d[key]
            for v in d.values():
                if isinstance(v, dict):
                    r = walk(v)
                    if r is not None:
                        return r
            return None
        r = walk(self.params)
        return default if r is None else r

    # ---- steps -------------------------------------------------------
    @property
    def step_dirs(self) -> dict:
        def build():
            out = {}
            names = [flow_step_dir(s) for s in self.flow.get("steps", [])] or list(STEP_DIRS)
            for n in list(names) + [n for n in STEP_DIRS if n not in names]:
                p = self.path / n
                if p.is_dir():
                    out[n] = p
            return out
        return self._memo("step_dirs", build)

    def step_dir(self, prefix: str):
        pre = _norm_prefix(prefix)
        target = canonical_step_row(prefix)
        for n, p in self.step_dirs.items():
            if canonical_step_row(n) == target:
                return p
            stem = re.sub(r"^\d+_", "", n).lower()
            if stem == pre or stem.startswith(pre + "_"):
                return p
        return None

    def step_feature_json(self, step: str) -> Path | None:
        d = self.step_dir(step) if isinstance(step, str) else step
        if d is None or not (d / "feature").is_dir():
            return None
        matches = sorted((d / "feature").glob("*.step.json"))
        return matches[0] if matches else None

    def output_file(self, step, suffix: str):
        d = self.step_dir(step) if isinstance(step, str) else step
        if d is None or not (d / "output").is_dir():
            return None
        for f in sorted((d / "output").iterdir()):
            if f.is_file() and f.name.endswith(suffix):
                return f
        return None

    # ---- metrics -----------------------------------------------------
    def step_metrics(self, step) -> dict:
        d = self.step_dir(step)
        if d is None:
            return {}
        p = d / "analysis/qor_metrics.json"
        if not p.exists():
            return {}
        return {m["id"]: m.get("value") for m in _json(p).get("metrics", [])}

    def metrics(self) -> dict:
        def build():
            best: dict = {}
            for n, d in self.step_dirs.items():
                p = d / "analysis/qor_metrics.json"
                if not p.exists():
                    continue
                try:
                    ms = _json(p).get("metrics", [])
                except Exception:  # noqa: BLE001
                    continue
                for m in ms:
                    r = ROLE_RANK.get(m.get("project_role"), 0)
                    cur = best.get(m["id"])
                    if cur is None or r >= cur[0]:
                        best[m["id"]] = (r, m.get("value"))
            return {k: v[1] for k, v in best.items()}
        return self._memo("metrics", build)

    # ---- parsed artifacts -------------------------------------------
    def _db(self) -> dict:
        return self._memo("db", lambda: _json(self.path / "config/db_ecc.json"))

    def lef(self):
        def build():
            db = self._db()["INPUT"]
            return _lef_cached(tuple([db["tech_lef_path"]] + list(db["lef_paths"])))
        return self._memo("lef", build)

    def liberty(self):
        def build():
            db = self._db()["INPUT"]
            return _lib_cached(tuple(db["lib_path"]))
        return self._memo("lib", build)

    def def_(self, step):
        def build():
            f = self.output_file(step, ".def.gz") or self.output_file(step, ".def")
            if f is None:
                raise FileNotFoundError(f"no DEF in step {step}")
            return P.parse_def(f)
        return self._memo_lru(("def", str(step)), build)

    def verilog(self, step):
        def build():
            f = self.output_file(step, ".v.gz") or self.output_file(step, ".v")
            if f is None:
                raise FileNotFoundError(f"no netlist in step {step}")
            return P.parse_verilog(f)
        return self._memo_lru(("v", str(step)), build, keep=4)

    def top(self, step):
        mods = self.verilog(step)
        return mods.get(self.top_name) or mods.get(self.name) or next(iter(mods.values()))

    def spef_files(self) -> dict:
        d = self.step_dir("RCX")
        out = {}
        if d and (d / "output").is_dir():
            for f in sorted((d / "output").glob("*.spef")):
                m = re.fullmatch(r"(.+?)_((?:C|RC)(?:best|worst)_(?:125|m40)C|TYPICAL_25C)\.spef", f.name)
                if m:
                    out[m.group(2)] = f
        return out

    def spef(self, corner):
        def build():
            return P.parse_spef(self.spef_files()[corner])
        return self._memo(("spef", corner), build)

    def sta_corners(self) -> dict:
        def build():
            out = {}
            d = self.step_dir("sta")
            if d:
                for p in sorted((d / "feature").glob("*/*/qor_summary.json")):
                    out[f"{p.parent.parent.name}/{p.parent.name}"] = _json(p)
            return out
        return self._memo("sta", build)

    def sdc(self):
        def build():
            files = sorted((self.path / "origin").glob("*.sdc")) or \
                sorted((self.path / "origin/post_CTS").glob("*.tcl"))
            if not files:
                raise FileNotFoundError("no SDC under origin/post_CTS")
            return P.parse_sdc(files[0])
        return self._memo("sdc", build)

    def rel(self, p) -> str:
        try:
            return str(Path(p).relative_to(self.project_dir.parent))
        except ValueError:
            return str(p)


def discover(root, designs=None) -> list:
    root = Path(root)
    out = []
    for d in sorted(root.iterdir()):
        if d.name.startswith("fi_") or not (d / "default/home/flow.json").exists():
            continue
        if designs and d.name not in designs:
            continue
        out.append(Workspace(d))
    return out
