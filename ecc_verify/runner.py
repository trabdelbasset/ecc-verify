"""Process-pool worker (kept out of __main__ so it can be pickled under the spawn start method)."""
from __future__ import annotations

import sys
import time
from pathlib import Path

from ecc_verify import config, metrics
from ecc_verify.model import Status
from ecc_verify.registry import REGISTRY, load_all_checks, run_check
from ecc_verify.workspace import Workspace


def _row(design, cid, r, seconds):
    return {"design": design, "check": cid, "status": r.status.value, "message": r.message, "measured": _j(r.measured),
            "expected": _j(r.expected), "tolerance": _j(r.tolerance), "evidence": list(r.evidence),
            "details": _j(r.details), "seconds": round(seconds, 2)}


def _j(v):
    import json
    try:
        json.dumps(v)
        return v
    except TypeError:
        return json.loads(json.dumps(v, default=str))


def _worker(args):
    """args: (root, design, check ids, config dict). Returns (design, rows, metrics, verify.toml dict)."""
    root, name, ids, cfg = args
    config.set_current(config.Config.from_dict(cfg))
    load_all_checks()
    ws = Workspace(Path(root) / name)
    rows = []
    for cid in ids:
        spec, fn = REGISTRY[cid]
        t = time.time()
        r = run_check(fn, ws)
        rows.append(_row(name, cid, r, time.time() - t))
        if r.status == Status.ERROR:
            print(f"  ERROR {name} {cid}: {r.message}", file=sys.stderr, flush=True)
    return name, rows, metrics.extract(ws), metrics.design_config(ws.project_dir)
