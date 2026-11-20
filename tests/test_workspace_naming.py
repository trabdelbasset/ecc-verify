from __future__ import annotations

import json
from pathlib import Path

from ecc_verify.checks import l1_flow, l3_power, l3_signoff
from ecc_verify.model import Status
from ecc_verify.workspace import STEP_DIRS, STEP_ROW, Workspace, canonical_step_row, flow_step_dir

_SNAKE_STEPS = [
    ("synthesis", "yosys", "01_synthesis_yosys", [".v.gz"]),
    ("pre_floorplan", "ecc", "02_pre_floorplan_ecc", [".def.gz", ".v.gz"]),
    ("macro_placement", "dreamplace", "03_macro_placement_dreamplace", [".def.gz", ".v.gz"]),
    ("post_floorplan", "ecc", "04_post_floorplan_ecc", [".def.gz", ".v.gz"]),
    ("preplace", "sizer", "05_preplace_sizer", [".def.gz", ".v.gz"]),
    ("placement", "dreamplace", "06_placement_dreamplace", [".def.gz", ".v.gz"]),
    ("diff_sizing", "dreamplace", "07_diff_sizing_dreamplace", [".def.gz", ".v.gz"]),
    ("cts", "ecc", "08_cts_ecc", [".def.gz", ".v.gz"]),
    ("legalization", "dreamplace", "09_legalization_dreamplace", [".def.gz", ".v.gz"]),
    ("timing_optimization", "sizer", "10_timing_optimization_sizer", [".def.gz", ".v.gz"]),
    ("routing", "ecc", "11_routing_ecc", [".def.gz", ".v.gz"]),
    ("filler", "ecc", "12_filler_ecc", [".def.gz", ".v.gz", ".gds"]),
    ("lvs", "ecc", "13_lvs_ecc", [".def.gz", ".v.gz"]),
    ("drc", "ecc", "14_drc_ecc", [".def.gz", ".v.gz"]),
    ("antenna_check", "ecc", "15_antenna_check_ecc", [".def.gz", ".v.gz"]),
    ("post_route_lec", "kepler_formal", "16_post_route_lec_kepler_formal", ["_result.json"]),
    ("rcx", "ecc", "17_rcx_ecc", [".def.gz", ".v.gz"]),
    ("sta", "ecc", "18_sta_ecc", []),
    ("power_analysis", "ecc", "19_power_analysis_ecc", [".def.gz", ".v.gz"]),
    ("emir", "ecc", "20_emir_ecc", [".def.gz", ".v.gz"]),
    ("harden", "ecc", "21_harden_ecc", [".gds", ".lef", ".lib"]),
]


def _make_workspace(tmp_path: Path, *, numbered: bool) -> Workspace:
    proj = tmp_path / ("proj_snake" if numbered else "proj_legacy")
    ws_dir = proj / "default"
    (ws_dir / "home").mkdir(parents=True)
    steps = []

    if numbered:
        for name, tool, dirname, suffixes in _SNAKE_STEPS:
            steps.append({"name": name, "tool": tool, "directory": dirname, "state": "Success"})
            sdir = ws_dir / dirname
            (sdir / "output").mkdir(parents=True)
            (sdir / "feature").mkdir(parents=True)
            (sdir / "analysis").mkdir(parents=True)
            for suf in suffixes:
                fname = "demo_post_route_lec_result.json" if suf == "_result.json" else f"demo{suf}"
                if suf == "_result.json":
                    (sdir / "output" / fname).write_text(json.dumps({"status": "proven"}))
                else:
                    (sdir / "output" / fname).write_text("x\n")
            if name == "rcx":
                (sdir / "output/demo_TYPICAL_25C.spef").write_text("*SPEF \"IEEE 1481-1998\"\n")
            elif name == "sta":
                (sdir / "report").mkdir(parents=True)
            elif name == "antenna_check":
                (sdir / "feature/antenna_check.step.json").write_text(
                    json.dumps({"antenna_check": {"signal_net_count": 5, "pins_with_gate_area": 3, "violation_count": 0}})
                )
            elif name == "power_analysis":
                (sdir / "report").mkdir(parents=True)
                (sdir / "feature/power_summary.json").write_text(
                    json.dumps({"internal_uw": 10.0, "switching_uw": 5.0, "dynamic_uw": 15.0, "leakage_uw": 1.0})
                )
                (sdir / "report/power_analysis.rpt").write_text("Total 0.010 mW 0.005 mW 1000 nW 0.016 mW\n")
            (sdir / "analysis/qor_metrics.json").write_text(
                json.dumps({"schema_version": 1, "step": name, "design": proj.name, "metrics": []})
            )
    else:
        for dirname in STEP_DIRS:
            row = STEP_ROW[dirname]
            tool = dirname.rsplit("_", 1)[-1] if "_" in dirname else "ecc"
            if dirname == "postRouteLec_kepler_formal":
                tool = "kepler_formal"
            steps.append({"name": row, "tool": tool, "state": "Success"})
            sdir = ws_dir / dirname
            (sdir / "output").mkdir(parents=True)
            (sdir / "feature").mkdir(parents=True)
            (sdir / "analysis").mkdir(parents=True)
            if dirname == "Synthesis_yosys":
                (sdir / "output/demo.v.gz").write_text("x\n")
            elif dirname == "postRouteLec_kepler_formal":
                (sdir / "output/demo_postRouteLec_result.json").write_text(json.dumps({"status": "proven"}))
            elif dirname == "Harden_ecc":
                for suf in (".gds", ".lef", ".lib"):
                    (sdir / "output" / f"demo{suf}").write_text("x\n")
            elif dirname == "sta_ecc":
                (sdir / "report").mkdir(parents=True)
            else:
                (sdir / "output/demo.def.gz").write_text("x\n")
                (sdir / "output/demo.v.gz").write_text("x\n")
                if dirname == "filler_ecc":
                    (sdir / "output/demo.gds").write_text("x\n")
                elif dirname == "RCX_ecc":
                    (sdir / "output/demo_TYPICAL_25C.spef").write_text("*SPEF \"IEEE 1481-1998\"\n")
                elif dirname == "antennaCheck_ecc":
                    (sdir / "feature/antennaCheck.step.json").write_text(
                        json.dumps({"antenna_check": {"signal_net_count": 5, "pins_with_gate_area": 3, "violation_count": 0}})
                    )
                elif dirname == "powerAnalysis_ecc":
                    (sdir / "report").mkdir(parents=True)
                    (sdir / "feature/power_summary.json").write_text(
                        json.dumps({"internal_uw": 10.0, "switching_uw": 5.0, "dynamic_uw": 15.0, "leakage_uw": 1.0})
                    )
                    (sdir / "report/powerAnalysis.rpt").write_text("Total 0.010 mW 0.005 mW 1000 nW 0.016 mW\n")
            (sdir / "analysis/qor_metrics.json").write_text(
                json.dumps({"schema_version": 1, "step": row, "design": proj.name, "metrics": []})
            )

    (proj / "project.json").write_text(json.dumps({"base_design": {"top_module": "demo"}}))
    (ws_dir / "home/flow.json").write_text(json.dumps({"steps": steps}))
    (ws_dir / "home/qor_report.json").write_text(json.dumps({"design": proj.name, "power": {"total_uw": 16.0}}))
    return Workspace(proj)


def test_canonical_step_row_and_flow_step_dir():
    assert canonical_step_row("01_synthesis_yosys") == "Synthesis"
    assert canonical_step_row("10_timing_optimization_sizer") == "Timing optimization"
    assert canonical_step_row("11_routing_ecc") == "route"
    assert canonical_step_row("15_antenna_check_ecc") == "antennaCheck"
    assert canonical_step_row("16_post_route_lec_kepler_formal") == "postRouteLec"
    assert canonical_step_row("19_power_analysis_ecc") == "powerAnalysis"
    assert canonical_step_row("21_harden_ecc") == "Harden"
    assert canonical_step_row("Timing optimization") == "Timing optimization"
    assert flow_step_dir({"name": "routing", "tool": "ecc", "directory": "11_routing_ecc"}) == "11_routing_ecc"
    assert flow_step_dir({"name": "Timing optimization", "tool": "sizer"}) == "timing_optimization_sizer"


def test_checks_support_legacy_and_numbered_snake_layouts(tmp_path):
    for numbered in (False, True):
        ws = _make_workspace(tmp_path, numbered=numbered)
        assert ws.step_dir("route").name.endswith("routing_ecc" if numbered else "route_ecc")
        assert ws.step_dir("Harden").name.endswith("harden_ecc" if numbered else "Harden_ecc")
        assert ws.step_feature_json("antennaCheck") is not None
        assert l1_flow.flow_all_success(ws).status == Status.PASS
        assert l1_flow.outputs_exist(ws).status == Status.PASS
        assert l1_flow.step_json_valid(ws).status == Status.PASS
        assert l3_signoff.antenna(ws).status == Status.PASS
        assert l3_signoff.lec(ws).status == Status.PASS
        assert l3_power.power_sum(ws).status == Status.PASS


def _set_step_mtimes(ws: Workspace, mtimes: dict[str, float]) -> None:
    import os

    for step_dir_name, ts in mtimes.items():
        for f in (ws.path / step_dir_name / "output").iterdir():
            if f.is_file():
                os.utime(f, (ts, ts))


def test_timestamps_monotonic_respects_parallel_waves_and_catches_stale_deps(tmp_path):
    base = 1_700_000_000.0
    ws = _make_workspace(tmp_path, numbered=True)
    # Assign strictly increasing timestamps for serial PNR steps, then realistic
    # out-of-order finish times across parallel checker waves.
    mtimes = {}
    t = base
    for _, _, dirname, suffixes in _SNAKE_STEPS:
        if not suffixes:
            continue
        t += 10.0
        mtimes[dirname] = t
    # Checker wave 1: rcx finishes quickly; post_route_lec takes longer (later mtime)
    mtimes["13_lvs_ecc"] = base + 125.0
    mtimes["14_drc_ecc"] = base + 123.0
    mtimes["15_antenna_check_ecc"] = base + 122.0
    mtimes["16_post_route_lec_kepler_formal"] = base + 160.0
    mtimes["17_rcx_ecc"] = base + 124.0
    # Checker wave 2: power_analysis finishes before sta
    mtimes["19_power_analysis_ecc"] = base + 135.0
    mtimes["20_emir_ecc"] = base + 150.0
    mtimes["21_harden_ecc"] = base + 155.0
    _set_step_mtimes(ws, mtimes)
    assert l1_flow.timestamps(ws).status == Status.PASS

    # Stale dependency violations must fail
    bad_rcx = dict(mtimes)
    bad_rcx["17_rcx_ecc"] = mtimes["12_filler_ecc"] - 10.0
    _set_step_mtimes(ws, bad_rcx)
    assert l1_flow.timestamps(ws).status == Status.FAIL

    bad_power = dict(mtimes)
    bad_power["19_power_analysis_ecc"] = mtimes["17_rcx_ecc"] - 10.0
    _set_step_mtimes(ws, bad_power)
    assert l1_flow.timestamps(ws).status == Status.FAIL

    bad_emir = dict(mtimes)
    bad_emir["20_emir_ecc"] = mtimes["19_power_analysis_ecc"] - 10.0
    _set_step_mtimes(ws, bad_emir)
    assert l1_flow.timestamps(ws).status == Status.FAIL

    bad_serial = dict(mtimes)
    bad_serial["08_cts_ecc"] = mtimes["06_placement_dreamplace"] - 10.0
    _set_step_mtimes(ws, bad_serial)
    assert l1_flow.timestamps(ws).status == Status.FAIL
