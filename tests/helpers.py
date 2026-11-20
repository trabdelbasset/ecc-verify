"""Synthetic results.json documents for compare / baseline / CLI tests."""
import json
from pathlib import Path


def row(design, check, status, msg=""):
    return {"design": design, "check": check, "status": status, "message": msg or status, "measured": None,
            "expected": None, "tolerance": None, "evidence": [], "details": {}, "seconds": 0.1}


BASE_METRICS = {"setup_ws": 5.8, "hold_ws": 0.06, "setup_tns": 0.0, "setup_nvp": 0, "hold_nvp": 0, "tclk_ns": 20.0,
                "hpwl": 1000.0, "route_wl": 5000.0, "via_count": 700, "core_util": 0.50, "cell_count": 200,
                "std_cell_area": 800.0, "power_total_uw": 100.0, "drv_max_cap": 0, "drc_count": 0,
                "runtime.route": 100.0, "runtime.total": 400.0}


def doc(rows, metrics=None, design_config=None, host="h1", ecc_commit="aaaa1111bbbb"):
    return {"meta": {"date": "2026-01-01", "root": "/r", "ecc_version": "1.0",
                     "provenance": {"date": "2026-01-01T00:00:00Z",
                                    "ecc": {"version": "1.0", "build_commit": ecc_commit},
                                    "designs": {"commit": "d" * 40}, "pdk": {"fingerprint": "f1"},
                                    "host": {"name": host, "cpus": 16, "pin_cpus": "0-7"}}},
            "results": rows, "metrics": metrics or {}, "design_config": design_config or {}}


def base_doc(**kw):
    rows = [row("d1", "L1.a", "PASS"), row("d1", "L3.b", "FAIL"), row("d2", "L1.a", "PASS"), row("d2", "L3.b", "PASS"),
            row("<global>", "L5.g", "FAIL")]
    return doc(rows, {"d1": dict(BASE_METRICS), "d2": dict(BASE_METRICS)}, **kw)


def write(path: Path, d) -> Path:
    path.write_text(json.dumps(d))
    return path
