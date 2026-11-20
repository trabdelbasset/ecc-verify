"""Provenance recorded with every run (results.json meta) and copied into baselines."""
from __future__ import annotations

import hashlib
import json
import platform
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from ecc_verify import __version__
from ecc_verify.config import Config


def _git(path: Path, *args: str, timeout: int = 15) -> str | None:
    try:
        p = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, timeout=timeout)
        return p.stdout.strip() if p.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def git_info(path: Path | str | None) -> dict | None:
    """{commit, dirty, remote} when `path` is inside a git work tree, else None."""
    if not path or not Path(path).is_dir():
        return None
    top = _git(Path(path), "rev-parse", "--show-toplevel")
    if not top:
        return None
    # the checkout must contain the path itself as (part of) the work tree, not an unrelated parent repo
    commit = _git(Path(path), "rev-parse", "HEAD")
    if not commit:
        return None
    dirty = bool(_git(Path(path), "status", "--porcelain", "--untracked-files=no"))
    return {"commit": commit, "dirty": dirty, "remote": _git(Path(path), "config", "--get", "remote.origin.url"),
            "toplevel": top}


def ecc_version(ecc: str) -> dict:
    """`ecc version --json`: version, build_commit, build_date, tools."""
    try:
        p = subprocess.run([ecc, "version", "--json"], capture_output=True, text=True, timeout=60)
        j = json.loads(p.stdout)
        if isinstance(j, list):
            j = j[0] if j else {}
        return {"version": j.get("ecc"), "build_commit": j.get("build_commit"), "build_date": j.get("build_date"),
                "ecc_tools": j.get("ecc_tools"), "dreamplace": j.get("dreamplace"), "tools": j.get("tools", {}),
                "binary": ecc}
    except Exception as e:  # noqa: BLE001
        return {"version": None, "build_commit": None, "error": f"{type(e).__name__}: {e}", "binary": ecc}


def pdk_info(pdk_root: str | None) -> dict:
    if not pdk_root or not Path(pdk_root).is_dir():
        return {"root": pdk_root, "version": None, "fingerprint": None}
    root = Path(pdk_root)
    version = None
    for part in root.parts:       # e.g. .../nightly/v0.1.0-nightly.20261107/pdk
        if part.startswith("v") and any(ch.isdigit() for ch in part) and "." in part:
            version = part
    g = git_info(root)
    fp = None
    for f in sorted((root / "prtech/techLEF").glob("*.lef"))[:1]:
        try:
            fp = hashlib.sha256(f.read_bytes()).hexdigest()[:16]
        except OSError:
            pass
    return {"root": str(root), "version": version or (g or {}).get("commit"), "git": g, "fingerprint": fp}


def collect(cfg: Config, root: Path | str, cpus_used: str | None = None) -> dict:
    """Everything needed to judge whether a baseline is comparable to this run."""
    designs = git_info(root)
    src = "root"
    if designs is None and cfg.designs_dir:
        designs, src = git_info(cfg.designs_dir), "designs_dir"
    return {
        "ecc_verify": __version__,
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ecc": ecc_version(cfg.ecc),
        "designs": ({**designs, "source": src} if designs else {"commit": None, "source": None}),
        "pdk": pdk_info(cfg.pdk_root),
        "host": {"name": platform.node(), "platform": platform.platform(), "python": platform.python_version(),
                 "cpus": __import__("os").cpu_count(), "pin_cpus": cpus_used or cfg.pin_cpus, "ecc_cpus": cfg.cpus},
    }
