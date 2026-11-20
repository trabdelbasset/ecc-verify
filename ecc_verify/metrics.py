"""Key metrics extracted from a finished workspace (stored in results.json and in baselines).

Metric names are flat strings; the compare rules in ``compare.py`` are keyed by them:

  setup_ws hold_ws setup_tns hold_tns setup_nvp hold_nvp      worst slack / TNS / violating paths (ns, ns, count)
  tclk_ns                                                     clock period (informational, scales the WS tolerance)
  hpwl route_wl via_count                                     placement HPWL, routed wirelength (um), via count
  core_util die_area std_cell_area instance_area cell_count   floorplan / area / std-cell count
  power_total_uw power_dynamic_uw power_leakage_uw            power analysis
  drv_max_tran drv_max_cap drv_max_fanout drc_count lvs_count route_violations   design-rule counters (worst corner)
  runtime.<step> runtime.total memory.<step>                  seconds / MB from home/flow.json (memory if available)
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

# metric name -> key in Workspace.metrics() (the merged qor_metrics.json values)
QOR_KEYS = {
    "setup_ws": "sta_setup_wns", "hold_ws": "sta_hold_wns",
    "setup_tns": "sta_setup_tns", "hold_tns": "sta_hold_tns",
    "setup_nvp": "sta_setup_violation_count", "hold_nvp": "sta_hold_violation_count",
    "hpwl": "place_hpwl", "route_wl": "route_wirelength", "via_count": "route_via_count",
    "core_util": "core_utilization", "die_area": "die_area", "std_cell_area": "std_cell_area",
    "instance_area": "instance_area", "cell_count": "std_cell_count",
    "drc_count": "drc_count", "lvs_count": "lvs_count", "route_violations": "route_dr_total_violation_count",
}


def _parse_hms(s) -> float | None:
    if isinstance(s, (int, float)):
        return float(s)
    m = re.fullmatch(r"\s*(\d+):(\d+):(\d+(?:\.\d+)?)\s*", str(s or ""))
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else None


def extract(ws) -> dict:
    """Return {metric: number}; metrics that are unavailable are omitted (never invented)."""
    out: dict = {}
    try:
        m = ws.metrics()
    except Exception:  # noqa: BLE001
        m = {}
    for name, key in QOR_KEYS.items():
        v = m.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            out[name] = v
    if "power_analysis_dynamic_uw" in m and "power_analysis_leakage_uw" in m:
        out["power_total_uw"] = round(m["power_analysis_dynamic_uw"] + m["power_analysis_leakage_uw"], 6)
        out["power_dynamic_uw"] = m["power_analysis_dynamic_uw"]
        out["power_leakage_uw"] = m["power_analysis_leakage_uw"]
    try:  # clock period
        import json
        q = json.loads((ws.path / "home/qor_report.json").read_text())
        if isinstance(q.get("tclk_ns"), (int, float)):
            out["tclk_ns"] = round(q["tclk_ns"], 6)
    except Exception:  # noqa: BLE001
        pass
    try:  # design-rule violation counters: worst over every STA corner
        corners = ws.sta_corners()
        for key, name in (("tran", "drv_max_tran"), ("cap", "drv_max_cap"), ("fanout", "drv_max_fanout")):
            vals = [c.get("design_statistics", {}).get(key) for c in corners.values()]
            vals = [v for v in vals if isinstance(v, (int, float))]
            if vals:
                out[name] = max(vals)
    except Exception:  # noqa: BLE001
        pass
    try:  # per-step runtime and peak memory
        total = 0.0
        for st in ws.flow.get("steps", []):
            nm = str(st.get("name", "")).replace(" ", "_")
            ro = st.get("resource_observation") or {}
            secs = ro.get("elapsed_seconds")
            secs = secs if isinstance(secs, (int, float)) else _parse_hms(st.get("runtime"))
            if secs is not None and nm:
                out[f"runtime.{nm}"] = round(secs, 3)
                total += secs
            mem = st.get("peak memory (mb)")
            if isinstance(mem, (int, float)) and nm:
                out[f"memory.{nm}"] = round(mem, 3)
        if total:
            out["runtime.total"] = round(total, 3)
    except Exception:  # noqa: BLE001
        pass
    return out


def design_config(project_dir: Path) -> dict:
    """Optional per-design ``verify.toml`` (expected-fail checks, tolerance overrides); {} when absent or invalid.

        [tolerances]                 # same keys as [compare.tolerances]
        hpwl_rel = 0.05

        [[expected_fail]]            # known failures on this design only (like known_issues.toml)
        check = "L3.cts.fanout_limit"   # id or glob
        bug = "BUG-10"
        note = "why"
    """
    p = Path(project_dir) / "verify.toml"
    if not p.is_file():
        return {}
    try:
        with open(p, "rb") as f:
            d = tomllib.load(f)
    except (tomllib.TOMLDecodeError, OSError) as e:
        return {"_error": f"{p}: {e}"}
    d["_path"] = str(p)
    return d
