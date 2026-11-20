"""Pure-python unit tests of the flowrun helpers (no ecc runs)."""
import gzip
from pathlib import Path

from ecc_verify import flowrun as F

DEF = """VERSION 5.8 ;
DESIGN t ;
COMPONENTS 3 ;
- u1 NAND2 + PLACED ( 1000 2000 ) N ;
- u2 INV + PLACED ( 3000 2000 ) FS ;
- u3 TAPCELL + FIXED ( 0 0 ) N ;
END COMPONENTS
NETS 2 ;
- n1 ( u1 Z ) ( u2 A )
  + ROUTED MET2 ( 1000 2000 ) ( 3000 * )
    NEW MET2 ( 3000 2000 ) ( * 5000 )
    NEW MET3 ( 3000 5000 ) VIA2_1
 ;
- n2 ( u2 Z ) ( PIN o )
  + ROUTED MET3 ( 0 0 ) ( 100 * )
 ;
END NETS
END DESIGN
"""


def _w(tmp_path: Path) -> Path:
    p = tmp_path / "t.def.gz"
    with gzip.open(p, "wt") as f:
        f.write(DEF)
    return p


def test_placements(tmp_path):
    p = F.placements(_w(tmp_path))
    assert p["u1"] == ("NAND2", 1000, 2000, "N") and len(p) == 3


def test_delete_segment(tmp_path):
    p = _w(tmp_path)
    info = F.edit_def(p, F.delete_route_segment)
    assert info["net"] == "n1" and "5000" in info["removed"]
    assert "( * 5000 )" not in F.def_text(p)


def test_overlap(tmp_path):
    p = _w(tmp_path)
    info = F.edit_def(p, F.overlap_two_cells)
    assert info["moved"] == "u2" and F.placements(p)["u2"][1:3] == (1000, 2000)


def test_placement_diff(tmp_path):
    a = _w(tmp_path)
    b = tmp_path / "b.def.gz"
    b.write_bytes(a.read_bytes())
    assert F.placement_diff(a, b)["moved"] == 0
    F.edit_def(b, F.overlap_two_cells)
    assert F.placement_diff(a, b)["moved"] == 1


def test_edit_toml(tmp_path):
    t = tmp_path / "ecc.toml"
    t.write_text('[design]\nfrequency_mhz = 38.4\n[params.place]\ntarget_density = 0.5\n')
    F.edit_toml(t, {"frequency_mhz": 50.0, "params.place.target_density": 0.3})
    s = t.read_text()
    assert "frequency_mhz = 50.0" in s and "target_density = 0.3" in s


def test_numeric_diffs():
    d = F.numeric_diffs({"a": 1.0, "h": "x"}, {"a": 1.1, "h": "y"})
    assert abs(d["a"]["rel"] - 0.1) < 1e-9 and d["h"] == {"a": "x", "b": "y"}


def test_step_resolution_across_workspace_layouts(tmp_path):
    import json

    ws_named = tmp_path / "named" / "default"
    ws_snake = tmp_path / "snake" / "default"
    for ws, mapping in (
        (ws_named, {
            "route": "route_ecc", "place": "place_dreamplace",
            "sta": "sta_ecc", "power": "powerAnalysis_ecc", "drc": "drc_ecc",
        }),
        (ws_snake, {
            "route": "11_routing_ecc", "place": "06_placement_dreamplace",
            "sta": "18_sta_ecc", "power": "19_power_analysis_ecc", "drc": "14_drc_ecc",
        }),
    ):
        for k, dname in mapping.items():
            (ws / dname / "output").mkdir(parents=True)
            (ws / dname / "analysis").mkdir(parents=True)
            (ws / dname / "feature").mkdir(parents=True)
            (ws / dname / "log").mkdir(parents=True)
        (ws / mapping["route"] / "output" / "demo.def.gz").write_bytes(b"def\n")
        (ws / mapping["place"] / "log" / "place.log").write_text("placed ok\n")
        (ws / mapping["place"] / "analysis" / "qor_metrics.json").write_text(
            json.dumps({"metrics": [{"id": "place_hpwl", "value": 42.0}]})
        )
        (ws / mapping["sta"] / "feature" / "TYP_25" / "TYPICAL").mkdir(parents=True)
        (ws / mapping["sta"] / "feature" / "TYP_25" / "TYPICAL" / "qor_summary.json").write_text(
            json.dumps({"summary": {"setup": {"wns": 1.5, "tns": 0.0, "nvp": 0}, "hold": {"wns": 0.4}}})
        )

        assert F.step_dir(ws, "route_ecc") == ws / mapping["route"]
        assert F.final_def(ws) == ws / mapping["route"] / "output" / "demo.def.gz"
        assert "placed ok" in F.step_log(ws, "place_dreamplace")
        assert F.step_metric(ws, "place_dreamplace", ("place_hpwl",)) == {"place_hpwl": 42.0}
        assert F.sta_summary(ws)["setup_wns"] == 1.5
        assert F.wait_for_step_dir(ws, "place_dreamplace", timeout=0.1) is True
