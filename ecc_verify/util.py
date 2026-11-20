"""Small helpers shared by check modules. Paths come from the active config (``ecc_verify.config``)."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from ecc_verify import config

# Dynamic module attributes (PEP 562): `util.PDK`, `util.STD_CELL`, ... always reflect the active config.
_DYNAMIC = {
    "PDK": lambda c: c.pdk,
    "STD_CELL": lambda c: c.std_cell,
    "LAYERMAP": lambda c: c.layermap,
    "OSS_BIN": lambda c: Path(c.oss_cad_bin) if c.oss_cad_bin else Path(),
    "VERIFY_RUNS": lambda c: c.runs_dir,
}


def __getattr__(name):
    if name in _DYNAMIC:
        return _DYNAMIC[name](config.current())
    raise AttributeError(name)


def run(cmd, cwd=None, timeout=600, env=None, pin=False):
    """Run a subprocess; returns (rc, stdout, stderr). pin=True prefixes `taskset -c <run.pin_cpus>`."""
    cfg = config.current()
    cmd = list(cmd)
    if pin:
        cmd = cfg.pin(cmd)
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=cfg.tool_env(env))
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired as ex:
        return 124, ex.stdout or "", f"timeout after {timeout}s"
    except FileNotFoundError as ex:
        return 127, "", str(ex)


def scratch_copy(src_project: Path, name: str, rewrite_paths: bool = True) -> Path:
    """Copy a finished project under <scratch_dir>/<name> (idempotent).

    With rewrite_paths the project.json root_path/workspace_path are rewritten so `ecc` accepts the copy
    (an unmodified copy is rejected: known issue O-2)."""
    runs = config.current().runs_dir
    dst = runs / name
    if (dst / "project.json").exists():
        return dst
    runs.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src_project, dst, symlinks=True)
    if rewrite_paths:
        pj = dst / "project.json"
        txt = pj.read_text().replace(str(src_project), str(dst))
        pj.write_text(txt)
    return dst


def jload(path):
    with open(path) as f:
        return json.load(f)


def rect_area(r):
    return max(0, r[2] - r[0]) * max(0, r[3] - r[1])
