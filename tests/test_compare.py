"""Compare logic: NEW FAIL / FIXED / STILL-FAILING / known issues / metric drift / provenance notes."""
import copy

import pytest

from ecc_verify import baseline as B
from ecc_verify import compare as C
from ecc_verify.known_issues import KnownIssue, KnownIssues

from helpers import BASE_METRICS, base_doc, doc, row, write

NONE = KnownIssues([])


@pytest.fixture
def base(tmp_path):
    p = write(tmp_path / "r.json", base_doc())
    B.export(p, tmp_path / "bl")
    return C.load_baseline(tmp_path / "bl")


def cmp_(cur, base, known=NONE, **kw):
    return C.compare(cur, base, known, **kw)


def test_identical_run_is_clean_but_untracked_failures_are_listed(base):
    c = cmp_(base_doc(), base)
    assert c.exit_code == 0 and c.counts["new_fail"] == 0 and c.counts["drift_regressions"] == 0
    assert c.counts["still_failing_untracked"] == 2 and c.counts["still_failing_known"] == 0


def test_new_fail_gates(base):
    d = base_doc()
    d["results"][2] = row("d2", "L1.a", "FAIL", "broke")
    c = cmp_(d, base)
    assert c.exit_code == 1
    nf = c.of("new_fail")
    assert [(e["design"], e["check"]) for e in nf] == [("d2", "L1.a")]
    assert "d2" in C.render_markdown(c) and "NEW FAIL (1)" in C.render_markdown(c)


def test_error_counts_as_failure(base):
    d = base_doc()
    d["results"][3] = row("d2", "L3.b", "ERROR", "crash")
    assert cmp_(d, base).exit_code == 1


def test_fixed_and_unverified(base):
    d = base_doc()
    d["results"][1] = row("d1", "L3.b", "PASS")       # was FAIL
    d["results"][4] = row("<global>", "L5.g", "SKIP")  # was FAIL, now not run
    c = cmp_(d, base)
    assert c.exit_code == 0
    assert [(e["design"], e["check"]) for e in c.of("fixed")] == [("d1", "L3.b")]
    assert [(e["design"], e["check"]) for e in c.of("unverified")] == [("<global>", "L5.g")]


def test_new_design_or_check_not_in_baseline(base):
    d = base_doc()
    d["results"].append(row("d3", "L1.a", "FAIL"))
    assert cmp_(d, base).exit_code == 1


def test_known_issue_makes_fail_known_not_new(base):
    d = base_doc()
    d["results"].append(row("d2", "L3.new", "FAIL"))
    k = KnownIssues([KnownIssue(("L3.new",), ("d2",), ("BUG-77",), "n")])
    c = cmp_(d, base, k)
    assert c.exit_code == 0
    e = next(e for e in c.entries if e["check"] == "L3.new")
    assert e["kind"] == "still_failing" and e["known"] and e["bugs"] == ["BUG-77"]
    # restricted to d2: the same check failing on d1 is NEW
    d["results"].append(row("d1", "L3.new", "FAIL"))
    c = cmp_(d, base, KnownIssues([KnownIssue(("L3.new",), ("d2",), ("BUG-77",), "n")]))
    assert c.exit_code == 1 and [e["design"] for e in c.of("new_fail")] == ["d1"]


def test_known_glob_and_all_designs(base):
    d = base_doc()
    d["results"].append(row("<global>", "L5.zzz", "FAIL"))
    k = KnownIssues([KnownIssue(("L5.*",), None, ("BUG-1",))])
    assert cmp_(d, base, k).exit_code == 0


def test_stale_known_issue_reported(base):
    # L3.b still fails (entry used); L1.a ran and passes everywhere (entry stale: the bug may be fixed)
    k = KnownIssues([KnownIssue(("L3.b",), None, ("BUG-1",)), KnownIssue(("L1.a",), None, ("BUG-9",))])
    c = cmp_(base_doc(), base, k)
    assert [s["bugs"] for s in c.stale_known] == [["BUG-9"]]
    assert "candidates to close" in C.render_markdown(c)


def test_no_baseline_means_no_fail_unless_known():
    d = base_doc()
    assert cmp_(d, None).exit_code == 1
    k = KnownIssues([KnownIssue(("L3.b", "L5.g"), None, ("BUG-1",))])
    assert cmp_(d, None, k).exit_code == 0
    assert cmp_(doc([row("d", "a", "PASS"), row("d", "b", "WARN"), row("d", "c", "SKIP")]), None).exit_code == 0


def test_verify_toml_expected_fail_is_honoured():
    d = doc([row("d1", "L3.cts.fanout_limit", "FAIL"), row("d2", "L3.cts.fanout_limit", "FAIL")],
            design_config={"d1": {"expected_fail": [{"check": "L3.cts.*", "bug": "BUG-10", "note": "x"}]}})
    c = cmp_(d, None)
    assert [e["design"] for e in c.of("new_fail")] == ["d2"]
    e = next(e for e in c.entries if e["design"] == "d1")
    assert e["known"] and e["bugs"] == ["BUG-10"]


# ---------------------------------------------------------------- metric drift
def drift(metric, cur, tol=None, base=None, tclk=20.0):
    t = dict(C.DEFAULT_TOLERANCES)
    t.update(tol or {})
    return C.metric_drift(metric, BASE_METRICS[metric] if base is None else base, cur, t, tclk)


@pytest.mark.parametrize("metric,cur,expect", [
    ("setup_ws", 5.8 - 0.9, None),                 # T=20: allowed max(0.05, 1.0) = 1.0 ns
    ("setup_ws", 5.8 - 1.1, "worse"),
    ("setup_ws", 5.8 + 1.5, "better"),
    ("hpwl", 1019.0, None), ("hpwl", 1021.0, "worse"), ("hpwl", 970.0, "better"),
    ("route_wl", 5101.0, "worse"), ("via_count", 714, None), ("via_count", 730, "worse"),
    ("cell_count", 202, None), ("cell_count", 203, "worse"), ("std_cell_area", 809.0, "worse"),
    ("power_total_uw", 102.9, None), ("power_total_uw", 103.5, "worse"),
    ("core_util", 0.505, None), ("core_util", 0.52, "changed"), ("core_util", 0.48, "changed"),
    ("drv_max_cap", 1, "worse"), ("drc_count", 0, None),
    ("setup_nvp", 1, "worse"), ("hold_nvp", 0, None),
    ("runtime.route", 120.0, None), ("runtime.route", 126.0, "worse"), ("runtime.route", 70.0, "better"),
    ("tclk_ns", 40.0, None),
])
def test_metric_drift_defaults(metric, cur, expect):
    d = drift(metric, cur)
    assert (d["direction"] if d else None) == expect


def test_slack_uses_absolute_floor_when_period_small():
    assert drift("hold_ws", 0.06 - 0.04, tclk=None) is None
    assert drift("hold_ws", 0.06 - 0.06, tclk=None)["direction"] == "worse"


def test_drv_decrease_is_improvement():
    assert drift("drv_max_cap", 0, base=3)["direction"] == "better"


def test_runtime_absolute_floor():
    assert drift("runtime.route", 6.0, base=2.0) is None          # +200% but only 4 s < 5 s floor
    assert drift("runtime.route", 7.0, base=1.0)["direction"] == "worse"


def test_tolerance_overrides_from_config_and_verify_toml(base):
    d = base_doc()
    d["metrics"]["d1"]["hpwl"] = 1100.0       # +10 %
    assert cmp_(d, base).exit_code == 1
    assert cmp_(d, base, cfg_tolerances={"hpwl_rel": 0.2}).exit_code == 0
    d["design_config"] = {"d1": {"tolerances": {"hpwl_rel": 0.2}}}
    assert cmp_(d, base).exit_code == 0
    d["design_config"] = {"d2": {"tolerances": {"hpwl_rel": 0.2}}}
    assert cmp_(d, base).exit_code == 1


def test_regression_named_in_outputs(base):
    d = base_doc()
    d["metrics"]["d2"]["hpwl"] = 1200.0
    c = cmp_(d, base)
    assert c.exit_code == 1 and c.counts["drift_regressions"] == 1
    assert "d2" in C.render_console(c) and "hpwl" in C.render_console(c)
    md = C.render_markdown(c)
    assert "Metric regressions (1)" in md and "`hpwl`" in md


def test_improvement_does_not_gate(base):
    d = base_doc()
    d["metrics"]["d1"]["hpwl"] = 800.0
    c = cmp_(d, base)
    assert c.exit_code == 0 and c.counts["drift_improvements"] == 1


def test_ignore_and_warn_only_patterns(base):
    d = base_doc()
    d["metrics"]["d1"]["runtime.route"] = 300.0
    assert cmp_(d, base).exit_code == 1
    assert cmp_(d, base, ignore_metrics=["runtime.*"]).exit_code == 0
    c = cmp_(d, base, warn_only_metrics=["runtime.*"])
    assert c.exit_code == 0 and c.counts["drift_non_gating"] == 1


def test_different_host_downgrades_runtime_only(tmp_path, base):
    d = base_doc(host="other-host")
    d["metrics"]["d1"]["runtime.route"] = 300.0
    c = cmp_(d, base)
    assert c.exit_code == 0 and c.counts["drift_non_gating"] == 1
    assert any("different host" in n for n in c.notes)
    d["metrics"]["d1"]["hpwl"] = 2000.0
    assert cmp_(d, base).exit_code == 1


def test_provenance_notes_on_ecc_change(base):
    c = cmp_(base_doc(ecc_commit="ffff9999eeee"), base)
    assert any("ecc build changed" in n for n in c.notes)


def test_clock_period_change_noted(base):
    d = base_doc()
    d["metrics"]["d1"]["tclk_ns"] = 10.0
    assert any("clock period changed" in n for n in cmp_(d, base).notes)


def test_subset_run_only_compares_what_it_has(base):
    d = doc([row("d1", "L1.a", "PASS")], {"d1": dict(BASE_METRICS)})
    c = cmp_(d, base)
    assert c.exit_code == 0 and any("absent from the current results" in n for n in c.notes)


def test_compare_json_roundtrip(tmp_path, base):
    c = cmp_(base_doc(), base)
    C.write_outputs(c, tmp_path / "o")
    import json
    j = json.loads((tmp_path / "o/compare.json").read_text())
    assert j["exit_code"] == 0 and j["counts"]["new_fail"] == 0 and (tmp_path / "o/compare.md").exists()


def test_stale_known_ignores_checks_that_did_not_run(base):
    k = KnownIssues([KnownIssue(("L4.not_run",), None, ("BUG-8",)), KnownIssue(("L1.a",), None, ("BUG-1",))])
    c = cmp_(base_doc(), base, k)           # L1.a ran and passes -> stale; L4.not_run did not run -> not reported
    assert [s["bugs"] for s in c.stale_known] == [["BUG-1"]]
