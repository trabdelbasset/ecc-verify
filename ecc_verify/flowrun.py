"""Helpers for the slow, real-flow layers (L4 metamorphic, L5 chaos).

Everything here shells out to the real ``ecc`` binary, pinned with ``taskset`` and
``OMP_NUM_THREADS=4``.  Cases live in ``<scratch_dir>/slow_<case>/`` and are
deleted after their metrics have been extracted; small JSON summaries are kept under
``<scratch_dir>/slow_summaries/`` (all paths come from ``ecc_verify.config``).
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ecc_verify import config
from ecc_verify.workspace import canonical_step_row


def runs_dir() -> Path:
    return config.current().runs_dir


def get_designs_dir() -> Path:
    d = config.current().designs_dir
    if not d:
        raise RuntimeError("designs_dir is not configured (paths.designs_dir / --designs-dir): needed by slow checks")
    return Path(d)


def summary_dir() -> Path:
    return runs_dir() / "slow_summaries"


def default_cpus() -> str:
    return config.current().cpus


WS = "default"


def full_timeout() -> int:
    return config.current().timeout("full_flow")


def step_timeout() -> int:
    return config.current().timeout("step")


# ---------------------------------------------------------------- running ecc
@dataclass
class Run:
    args: list[str]
    rc: int
    out: str
    secs: float
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.rc == 0 and not self.timed_out

    def tail(self, n: int = 600) -> str:
        return self.out[-n:]


def env_for(cpus_n: int = 4) -> dict:
    e = config.current().tool_env()
    e["OMP_NUM_THREADS"] = str(cpus_n)
    e["ECC_THREADS"] = str(cpus_n)
    return e


def cmd_for(ecc: str, cpus: str, project: Path, args: list[str]) -> list[str]:
    return config.current().pin([ecc, *args, "--project", str(project)] if args[0] in ("run", "status", "report")
                                else [ecc, *args], cpus)


def run_ecc(ecc: str, project: Path, args: list[str], cpus: str | None = None,
            timeout: int | None = None, plain: bool = True) -> Run:
    """Run `ecc <args> --project <project>` (pinned). `args` e.g. ["run", "--resume"]."""
    cpus = cpus or default_cpus()
    timeout = timeout or full_timeout()
    cmd = config.current().pin([ecc, *args, "--project", str(project)], cpus)
    if plain and args[0] == "run":
        cmd.append("--plain")
    t0 = time.time()
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, env=env_for(), timeout=timeout)
        return Run(cmd, p.returncode, (p.stdout or "") + (p.stderr or ""), time.time() - t0)
    except subprocess.TimeoutExpired as e:
        out = (e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or ""))
        return Run(cmd, 124, out, time.time() - t0, timed_out=True)


def ecc_json(ecc: str, project: Path, args: list[str], cpus: str | None = None, timeout: int = 120) -> Any:
    r = run_ecc(ecc, project, args + ["--json"], cpus, timeout, plain=False)
    try:
        return json.loads(r.out[r.out.index(next(c for c in r.out if c in "[{")):])
    except Exception:  # noqa: BLE001
        return {"_error": r.out[-400:], "_rc": r.rc}


def status_json(ecc: str, project: Path) -> dict:
    """{'workspace': <status str>, 'steps': {step: {...}}, 'raw': [...]} or {'error':...}."""
    raw = ecc_json(ecc, project, ["status"])
    if not isinstance(raw, list):
        return {"error": raw}
    ws = next((x for x in raw if "workspace_id" in x), {})
    return {"workspace": ws.get("status"), "steps": {x["step"]: x for x in raw if "step" in x}, "raw": raw}


def qor_json(ecc: str, project: Path) -> dict:
    raw = ecc_json(ecc, project, ["report", "qor"])
    return raw[0] if isinstance(raw, list) and raw else {"_error": raw}


# ---------------------------------------------------------------- case dirs
@dataclass
class Case:
    name: str
    root: Path
    design: str
    notes: dict = field(default_factory=dict)

    @property
    def ws(self) -> Path:
        return self.root / WS


def new_case(name: str, design: str, designs_dir: Path | None = None, toml_edits: dict | None = None) -> Case:
    """Copy a CI design to <scratch_dir>/slow_<name>/ and optionally edit ecc.toml keys (no other rewrites)."""
    designs_dir = Path(designs_dir) if designs_dir else get_designs_dir()
    root = runs_dir() / f"slow_{name}"
    if root.exists():
        shutil.rmtree(root)
    root.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(designs_dir / design, root, ignore=shutil.ignore_patterns("checks", "*.lit"))
    if toml_edits:
        edit_toml(root / "ecc.toml", toml_edits)
    return Case(name, root, design)


def edit_toml(path: Path, edits: dict) -> None:
    """edits: {"frequency_mhz": 50.0} or {"params.place.target_density": 0.3} (dotted = table.key)."""
    txt = path.read_text()
    for key, val in edits.items():
        *tbl, k = key.split(".")
        header = "[" + ".".join(tbl) + "]" if tbl else None
        lines = txt.split("\n")
        cur = None
        done = False
        for i, ln in enumerate(lines):
            m = re.match(r"\s*\[([^\]]+)\]", ln)
            if m:
                cur = m.group(1)
                continue
            want = header[1:-1] if header else "design"
            if cur == want and re.match(rf"\s*{re.escape(k)}\s*=", ln):
                lines[i] = f"{k} = {val}"
                done = True
                break
        if not done:
            raise KeyError(f"{key} not found in {path}")
        txt = "\n".join(lines)
    path.write_text(txt)


def cleanup(case: Case | Path) -> None:
    p = case.root if isinstance(case, Case) else case
    shutil.rmtree(p, ignore_errors=True)


def cleanup_all() -> None:
    for p in runs_dir().glob("slow_*"):
        shutil.rmtree(p, ignore_errors=True)


def save_summary(name: str, data: dict) -> Path:
    summary_dir().mkdir(parents=True, exist_ok=True)
    p = summary_dir() / f"{name}.json"
    p.write_text(json.dumps(data, indent=2, default=str))
    return p


# ---------------------------------------------------------------- metrics
def _jload(p: Path):
    try:
        return json.loads(p.read_text())
    except Exception:  # noqa: BLE001
        return None


def step_dir(ws: Path, name: str) -> Path | None:
    """Locate a step directory across named and numbered snake-case workspace layouts."""
    direct = ws / name
    if direct.is_dir():
        return direct
    target = canonical_step_row(name)
    if ws.is_dir():
        for d in sorted(p for p in ws.iterdir() if p.is_dir()):
            if canonical_step_row(d.name) == target:
                return d
    return None


def step_log(ws: Path, name: str, max_bytes: int = 200000) -> str:
    """Return the head of the first log file for step ``name``, or ''."""
    d = step_dir(ws, name)
    if d is None or not (d / "log").is_dir():
        return ""
    logs = sorted(p for p in (d / "log").glob("*.log") if p.is_file())
    return read_text(logs[0], max_bytes) if logs else ""


def sta_summary(ws: Path) -> dict:
    """Worst setup/hold WNS/TNS over every corner/rc in STA feature output."""
    rows = {}
    d_sta = step_dir(ws, "sta_ecc")
    if d_sta is not None:
        for f in sorted((d_sta / "feature").glob("*/*/qor_summary.json")):
            d = _jload(f)
            if d and "summary" in d:
                rows[f"{f.parent.parent.name}/{f.parent.name}"] = d["summary"]
    if not rows:
        return {}
    sw = min(rows.items(), key=lambda kv: kv[1]["setup"]["wns"])
    hw = min(rows.items(), key=lambda kv: kv[1]["hold"]["wns"])
    return {
        "setup_wns": sw[1]["setup"]["wns"], "setup_wns_corner": sw[0],
        "setup_tns": sum(v["setup"]["tns"] for v in rows.values()),
        "setup_nvp": sum(v["setup"]["nvp"] for v in rows.values()),
        "hold_wns": hw[1]["hold"]["wns"], "hold_wns_corner": hw[0],
        "n_corners": len(rows),
    }


def step_metric(ws: Path, step_dir_name: str, ids: tuple[str, ...]) -> dict:
    d_step = step_dir(ws, step_dir_name)
    if d_step is None:
        return {}
    d = _jload(d_step / "analysis" / "qor_metrics.json") or {}
    return {m["id"]: m["value"] for m in d.get("metrics", []) if m["id"] in ids}


_COMP_RE = re.compile(r"^\s*-\s+(\S+)\s+(\S+).*?\+\s+(?:PLACED|FIXED|COVER)\s+\(\s*(-?\d+)\s+(-?\d+)\s*\)\s+(\S+)", re.M)


def def_text(path: Path) -> str:
    op = gzip.open if str(path).endswith(".gz") else open
    with op(path, "rt") as f:
        return f.read()


def placements(def_path: Path) -> dict[str, tuple]:
    txt = def_text(def_path)
    i = txt.find("\nCOMPONENTS")
    j = txt.find("END COMPONENTS", i)
    return {m.group(1): (m.group(2), int(m.group(3)), int(m.group(4)), m.group(5))
            for m in _COMP_RE.finditer(txt[i:j])}


def final_def(ws: Path, step_name: str = "route_ecc") -> Path | None:
    d_step = step_dir(ws, step_name)
    if d_step is None:
        return None
    out = sorted((d_step / "output").glob("*.def.gz")) or sorted((d_step / "output").glob("*.def"))
    return out[0] if out else None


def placement_diff(a: Path, b: Path) -> dict:
    pa, pb = placements(a), placements(b)
    moved = [k for k in pa.keys() & pb.keys() if pa[k] != pb[k]]
    return {"n_a": len(pa), "n_b": len(pb), "only_a": len(pa.keys() - pb.keys()),
            "only_b": len(pb.keys() - pa.keys()), "moved": len(moved), "examples": sorted(moved)[:5]}


def placement_hash(def_path: Path) -> str:
    p = placements(def_path)
    return hashlib.sha1(json.dumps(sorted(p.items())).encode()).hexdigest()[:12]


def flow_metrics(ws: Path) -> dict:
    """Compact metric vector of a (partially) finished workspace."""
    m: dict[str, Any] = {}
    m.update(sta_summary(ws))
    m.update({f"route_{k}": v for k, v in step_metric(
        ws, "route_ecc", ("route_wirelength", "route_via_count", "core_utilization", "instance_count",
                          "std_cell_area", "instance_area")).items()})
    pl = step_metric(ws, "place_dreamplace", ("place_hpwl", "core_utilization", "instance_count", "std_cell_area"))
    m.update({f"place_{k.replace('place_', '')}": v for k, v in pl.items()})
    d_pw = step_dir(ws, "powerAnalysis_ecc")
    pw = _jload(d_pw / "feature" / "power_summary.json") if d_pw is not None else None
    if pw:
        m["power_dynamic_uw"] = pw.get("dynamic_uw")
        m["power_leakage_uw"] = pw.get("leakage_uw")
        m["power_total_uw"] = (pw.get("dynamic_uw") or 0) + (pw.get("leakage_uw") or 0)
    m.update({k: v for k, v in step_metric(ws, "drc_ecc", ("drc_count",)).items()})
    m.update({k: v for k, v in step_metric(ws, "lvs_ecc", ("lvs_count",)).items()})
    d = final_def(ws)
    if d:
        m["final_def_hash"] = placement_hash(d)
    d = final_def(ws, "place_dreamplace")
    if d:
        m["place_def_hash"] = placement_hash(d)
    return m


def numeric_diffs(a: dict, b: dict) -> dict:
    out = {}
    for k in sorted(a.keys() & b.keys()):
        x, y = a[k], b[k]
        if isinstance(x, (int, float)) and isinstance(y, (int, float)) and not isinstance(x, bool):
            if x == y:
                continue
            out[k] = {"a": x, "b": y, "abs": y - x, "rel": (y - x) / abs(x) if x else (0.0 if y == x else float("inf"))}
        elif x != y:
            out[k] = {"a": x, "b": y}
    return out


def read_text(p: Path, n: int = 4000) -> str:
    try:
        return p.read_text(errors="replace")[:n]
    except Exception:  # noqa: BLE001
        return ""


# ---------------------------------------------------------------- DEF surgery
def edit_def(path: Path, fn) -> Any:
    """Rewrite a (gz) DEF text through fn(text) -> (new_text, info). Writes in place, returns info."""
    txt = def_text(path)
    new, info = fn(txt)
    op = gzip.open if str(path).endswith(".gz") else open
    with op(path, "wt") as f:
        f.write(new)
    return info


def delete_route_segment(txt: str):
    """Delete one planar wire segment (a `NEW METx ( x y ) ( x2 * )` line) from the NETS section."""
    i = txt.index("\nNETS")
    head, nets = txt[:i], txt[i:]
    lines = nets.split("\n")
    net = None
    for k, ln in enumerate(lines):
        if ln.startswith("- "):
            net = ln.split()[1]
        elif net and re.match(r"\s+NEW\s+MET[2-4]\s+\(\s*\d+ \d+\s*\)\s+\(", ln):
            removed = lines.pop(k)
            return head + "\n".join(lines), {"net": net, "removed": removed.strip(), "line": k}
    raise RuntimeError("no deletable segment found")


def overlap_two_cells(txt: str):
    """Move the second PLACED std cell onto the first one (same x y)."""
    i = txt.index("\nCOMPONENTS")
    j = txt.index("END COMPONENTS", i)
    comp = txt[i:j]
    ms = [m for m in _COMP_RE.finditer(comp)
          if "PLACED" in m.group(0) and "FILL" not in m.group(2).upper() and "TAP" not in m.group(2).upper()]
    a, b = ms[0], ms[1]
    seg = comp[b.start():b.end()]
    new = re.sub(r"\(\s*-?\d+\s+-?\d+\s*\)", f"( {a.group(3)} {a.group(4)} )", seg, count=1)
    comp2 = comp[:b.start()] + new + comp[b.end():]
    return txt[:i] + comp2 + txt[j:], {"moved": b.group(1), "onto": a.group(1), "cell": b.group(2),
                                        "to": [int(a.group(3)), int(a.group(4))]}


# ---------------------------------------------------------------- misc
def runtime_run_id(project: Path, ws: str = WS) -> str | None:
    d = _jload(project / "project.json") or {}
    return ((d.get("runtime_processes") or {}).get(ws) or {}).get("run_id")


def wait_for_step_dir(ws: Path, step_name: str, timeout: float = 600, poll: float = 0.5) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        d = step_dir(ws, step_name)
        if d is not None and any(d.glob("log/*")):
            return True
        time.sleep(poll)
    return False
