"""Baseline export / load."""
import json

import pytest

from ecc_verify import baseline as B
from ecc_verify import compare as C

from helpers import base_doc, doc, row, write


def test_export_layout_and_content(tmp_path):
    r = write(tmp_path / "r.json", base_doc())
    info = B.export(r, tmp_path / "bl")
    assert sorted(p.name for p in (tmp_path / "bl").iterdir()) == ["_global.json", "d1.json", "d2.json", "provenance.json"]
    d1 = json.loads((tmp_path / "bl/d1.json").read_text())
    assert d1["checks"]["L3.b"]["status"] == "FAIL" and d1["metrics"]["hpwl"] == 1000.0
    assert d1["design"] == "d1" and d1["provenance"]["ecc_build_commit"] == "aaaa1111bbbb"
    p = json.loads((tmp_path / "bl/provenance.json").read_text())
    assert p["ecc"]["version"] == "1.0" and p["host"]["name"] == "h1" and p["designs"]["commit"] == "d" * 40
    assert p["pdk"]["fingerprint"] == "f1" and p["exported_designs"] == ["<global>", "d1", "d2"]
    assert info["designs"] == ["<global>", "d1", "d2"]


def test_export_is_deterministic(tmp_path):
    r = write(tmp_path / "r.json", base_doc())
    B.export(r, tmp_path / "a")
    B.export(r, tmp_path / "b")
    assert (tmp_path / "a/d1.json").read_text() == (tmp_path / "b/d1.json").read_text()


def test_partial_export_keeps_other_designs(tmp_path):
    r = write(tmp_path / "r.json", base_doc())
    B.export(r, tmp_path / "bl")
    r2 = write(tmp_path / "r2.json", doc([row("d1", "L3.b", "PASS")], {"d1": {"hpwl": 5.0}}))
    B.export(r2, tmp_path / "bl")
    assert json.loads((tmp_path / "bl/d1.json").read_text())["checks"]["L3.b"]["status"] == "PASS"
    assert json.loads((tmp_path / "bl/d2.json").read_text())["checks"]["L1.a"]["status"] == "PASS"
    assert C.load_baseline(tmp_path / "bl").designs.keys() == {"d1", "d2", "<global>"}


def test_export_design_filter(tmp_path):
    r = write(tmp_path / "r.json", base_doc())
    B.export(r, tmp_path / "bl", ["d2"])
    assert not (tmp_path / "bl/d1.json").exists() and (tmp_path / "bl/d2.json").exists()


def test_show(tmp_path):
    B.export(write(tmp_path / "r.json", base_doc()), tmp_path / "bl")
    s = B.show(tmp_path / "bl")
    assert "d1" in s and "3 designs" in s


def test_bad_inputs(tmp_path):
    with pytest.raises(C.CompareError):
        C.load_baseline(tmp_path / "nope")
    (tmp_path / "empty").mkdir()
    with pytest.raises(C.CompareError):
        C.load_baseline(tmp_path / "empty")
    (tmp_path / "bad.json").write_text("{not json")
    with pytest.raises(C.CompareError):
        C.load_results(tmp_path / "bad.json")
    (tmp_path / "other.json").write_text("{}")
    with pytest.raises(C.CompareError):
        C.load_results(tmp_path / "other.json")
