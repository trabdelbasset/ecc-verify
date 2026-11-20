"""Exit codes of the CLI: 0 = no new FAIL, 1 = new FAIL / regression, 2 = usage or infrastructure error.
Also: supported --root layouts and per-design verify.toml."""
import json

import pytest

from ecc_verify.__main__ import main
from ecc_verify.workspace import STEP_ROW, discover

from helpers import base_doc, doc, row, write


def fake_ws(root, name, state="Success", verify_toml=None):
    """Minimal finished workspace: only home/flow.json (enough for discover + L1.flow_all_success)."""
    h = root / name / "default" / "home"
    h.mkdir(parents=True)
    steps = []
    for d, nm in STEP_ROW.items():
        pre = nm if d.startswith(nm + "_") else nm.lower().replace(" ", "_")
        steps.append({"name": nm, "tool": d[len(pre) + 1:], "state": "Success", "runtime": "0:0:5",
                      "peak memory (mb)": 10.0})
    steps[0]["state"] = state
    (h / "flow.json").write_text(json.dumps({"steps": steps}))
    if verify_toml:
        (root / name / "verify.toml").write_text(verify_toml)


# ---------------------------------------------------------------- compare / baseline
def test_compare_exit_0_and_1(tmp_path):
    r = write(tmp_path / "r.json", base_doc())
    assert main(["baseline", "export", "--results", str(r), "--out", str(tmp_path / "bl")]) == 0
    assert main(["compare", "--baseline", str(tmp_path / "bl"), "--results", str(r), "--no-known-issues"]) == 0
    assert (tmp_path / "compare.md").exists() and json.loads((tmp_path / "compare.json").read_text())["exit_code"] == 0
    d = base_doc()
    d["results"][3] = row("d2", "L3.b", "FAIL")
    bad = write(tmp_path / "bad.json", d)
    assert main(["compare", "--baseline", str(tmp_path / "bl"), "--results", str(bad), "--out", str(tmp_path / "o")]) == 1
    j = json.loads((tmp_path / "o/compare.json").read_text())
    assert j["exit_code"] == 1 and j["counts"]["new_fail"] == 1
    d = base_doc()
    d["metrics"]["d1"]["hpwl"] *= 1.5
    reg = write(tmp_path / "reg.json", d)
    assert main(["compare", "--baseline", str(tmp_path / "bl"), "--results", str(reg), "--out", str(tmp_path / "o2")]) == 1


def test_compare_known_issues_file_makes_exit_0(tmp_path):
    d = base_doc()
    d["results"].append(row("d1", "L3.novel", "FAIL"))
    bad = write(tmp_path / "bad.json", d)
    r = write(tmp_path / "r.json", base_doc())
    main(["baseline", "export", "--results", str(r), "--out", str(tmp_path / "bl")])
    ki = tmp_path / "known.toml"
    ki.write_text('[[known]]\ncheck = "L3.novel"\nbugs = ["BUG-1"]\nnote = "x"\n')
    assert main(["compare", "--baseline", str(tmp_path / "bl"), "--results", str(bad), "--known-issues", str(ki),
                 "--out", str(tmp_path / "o")]) == 0
    assert "BUG-1" in (tmp_path / "o/compare.md").read_text()


def test_infra_errors_exit_2(tmp_path, capsys):
    r = write(tmp_path / "r.json", base_doc())
    assert main(["compare", "--baseline", str(tmp_path / "missing"), "--results", str(r)]) == 2
    assert main(["compare", "--baseline", str(tmp_path), "--results", str(tmp_path / "nope.json")]) == 2
    (tmp_path / "junk.json").write_text("nonsense")
    assert main(["compare", "--baseline", str(tmp_path), "--results", str(tmp_path / "junk.json")]) == 2
    assert main(["compare", "--results", str(r)]) == 2                                  # no baseline given
    assert main(["baseline", "export", "--results", str(tmp_path / "nope.json"), "--out", str(tmp_path / "b")]) == 2
    assert main(["run", "--root", str(tmp_path / "does-not-exist"), "--out", str(tmp_path / "o")]) == 2
    (tmp_path / "empty").mkdir()
    assert main(["run", "--root", str(tmp_path / "empty"), "--out", str(tmp_path / "o")]) == 2   # no workspaces
    fake_ws(tmp_path / "w", "x")
    assert main(["run", "--root", str(tmp_path / "w"), "--out", str(tmp_path / "o"), "--layers", "L9"]) == 2
    assert main(["run", "--root", str(tmp_path / "w"), "--out", str(tmp_path / "o"), "--config",
                 str(tmp_path / "nonexistent.toml")]) == 2
    err = capsys.readouterr().err
    assert "error:" in err


def test_usage_error_exit_2():
    with pytest.raises(SystemExit) as e:
        main(["run"])               # --root/--out missing
    assert e.value.code == 2


# ---------------------------------------------------------------- run end to end on synthetic workspaces
RUN = ["--layers", "L1", "--only", r"L1\.flow_all_success", "--workers", "2"]


@pytest.fixture
def run_args(tmp_path, fake_ecc):
    ecc, _ = fake_ecc
    return ["--ecc", str(ecc), "--scratch-dir", str(tmp_path / "scratch")]


def test_run_exit_codes_end_to_end(tmp_path, run_args):
    root = tmp_path / "work"
    fake_ws(root, "ok1")
    fake_ws(root, "bad1", state="Failed")
    out = tmp_path / "out"
    base = ["run", "--root", str(root), "--out", str(out), *RUN, *run_args]
    # no baseline: a FAIL is a failure
    assert main(base + ["--no-known-issues"]) == 1
    doc_ = json.loads((out / "results.json").read_text())
    assert doc_["metrics"]["ok1"]["runtime.Synthesis"] == 5.0 and doc_["meta"]["provenance"]["ecc"]["version"] == "9.9.9"
    assert any(r["design"] == "bad1" and r["status"] == "FAIL" for r in doc_["results"])
    assert main(base + ["--no-gate"]) == 0
    # known issue covers it
    ki = tmp_path / "k.toml"
    ki.write_text('[[known]]\ncheck = "L1.flow_all_success"\ndesigns = ["bad1"]\nbugs = ["BUG-X"]\n')
    assert main(base + ["--known-issues", str(ki)]) == 0
    # baseline taken from this run: still failing but not new
    assert main(["baseline", "export", "--results", str(out / "results.json"), "--out", str(tmp_path / "bl")]) == 0
    assert main(base + ["--baseline", str(tmp_path / "bl"), "--no-known-issues"]) == 0
    # regression: ok1 breaks
    fake_ws_state = root / "ok1/default/home/flow.json"
    fake_ws_state.write_text(fake_ws_state.read_text().replace("Success", "Failed"))
    assert main(base + ["--baseline", str(tmp_path / "bl"), "--no-known-issues"]) == 1
    c = json.loads((out / "compare.json").read_text())
    assert [(e["design"], e["check"]) for e in c["entries"] if e["kind"] == "new_fail"] == [("ok1", "L1.flow_all_success")]


def test_run_honours_verify_toml(tmp_path, run_args):
    root = tmp_path / "work"
    fake_ws(root, "bad1", state="Failed",
            verify_toml='[[expected_fail]]\ncheck = "L1.flow_all_success"\nbug = "BUG-Y"\nnote = "expected"\n'
                        '[tolerances]\nhpwl_rel = 0.3\n')
    out = tmp_path / "out"
    assert main(["run", "--root", str(root), "--out", str(out), *RUN, *run_args, "--no-known-issues"]) == 0
    d = json.loads((out / "results.json").read_text())
    assert d["design_config"]["bad1"]["tolerances"]["hpwl_rel"] == 0.3
    c = json.loads((out / "compare.json").read_text())
    assert c["entries"][0]["known"] and c["entries"][0]["bugs"] == ["BUG-Y"]


# ---------------------------------------------------------------- --root layouts
def test_root_layouts(tmp_path):
    """run-designs.sh output: <out>/<design>/default (+ <design>.log, summary.txt);
    ecc-ci-designs checkout run in place: <checkout>/<design>/default next to profiles/, Readme.md, .git/."""
    out_root = tmp_path / "ci-artifacts"
    fake_ws(out_root, "xtea")
    fake_ws(out_root, "gcd_icg")
    (out_root / "xtea.log").write_text("log")
    (out_root / "summary.txt").write_text("PASS xtea")
    assert [w.name for w in discover(out_root)] == ["gcd_icg", "xtea"]

    checkout = tmp_path / "ecc-ci-designs"
    fake_ws(checkout, "xtea")
    (checkout / "profiles").mkdir()
    (checkout / ".git").mkdir()
    (checkout / "Readme.md").write_text("x")
    (checkout / "unrun-design").mkdir()
    (checkout / "unrun-design" / "ecc.toml").write_text("")     # never run: no default/home/flow.json
    assert [w.name for w in discover(checkout)] == ["xtea"]
    assert [w.name for w in discover(out_root, ["xtea"])] == ["xtea"]
