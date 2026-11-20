"""Unit and mock-executable integration tests for optional L3.oss.* open-source cross-tool oracles."""
from __future__ import annotations

import gzip
import json
import stat
from pathlib import Path

from ecc_verify import __main__ as cli_main, config
from ecc_verify.checks import l3_oss_oracles as oss
from ecc_verify.model import Layer, Status
from ecc_verify.registry import REGISTRY, coverage_matrix, load_all_checks, run_check
from ecc_verify.workspace import Workspace


def _make_exec(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/usr/bin/env python3\n{body}\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _make_openpdk(root: Path, nested: bool = True) -> Path:
    base = root / "icsprout55" if nested else root
    for rel in (
        "libs.tech/klayout/tech/ics55.drc",
        "libs.tech/klayout/tech/ics55_antenna.drc",
        "libs.tech/klayout/tech/ics55.lvs",
        "libs.tech/librelane/rcx/ics55_typ.rules",
        "libs.tech/librelane/rcx/ics55_rcworst.rules",
        "libs.tech/librelane/rcx/ics55_rcbest.rules",
    ):
        p = base / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("# stub deck/rules\n")
    return root


def _make_workspace(tmp_path: Path, *, snake_ant: bool = False) -> Workspace:
    proj = tmp_path / "work" / "tiny"
    ws_dir = proj / "default"
    (proj).mkdir(parents=True, exist_ok=True)
    (proj / "project.json").write_text(json.dumps({"base_design": {"top_module": "tiny_top"}}))
    (proj / "ecc.toml").write_text('[design]\nname = "tiny_top"\nfrequency_mhz = 100.0\n')

    # Pointers in a mock pdk folder
    pdk = tmp_path / "pdk"
    tech_lef = pdk / "tech.lef"
    cell_lef = pdk / "cell.lef"
    lib_file = pdk / "typ.lib"
    pdk.mkdir(parents=True, exist_ok=True)
    tech_lef.write_text("VERSION 5.7 ;\n")
    cell_lef.write_text("VERSION 5.7 ;\n")
    lib_file.write_text('library (t) { time_unit : "1ns"; }\n')

    (ws_dir / "home").mkdir(parents=True, exist_ok=True)
    (ws_dir / "home/flow.json").write_text(json.dumps({"steps": []}))
    (ws_dir / "config").mkdir(parents=True, exist_ok=True)
    (ws_dir / "config/db_ecc.json").write_text(json.dumps({
        "INPUT": {
            "tech_lef_path": str(tech_lef),
            "lef_paths": [str(cell_lef)],
            "lib_path": [str(lib_file)],
            "sdc_path": str(ws_dir / "origin/post_CTS/MAX_125_Cworst.tcl"),
        }
    }))
    (ws_dir / "config/sta_ecc.json").write_text(json.dumps({
        "liberty": [{"corner": "MAX", "temperature": 125, "path": [str(lib_file)]}],
        "signoff": [{"MAX": ["Cworst"]}],
    }))

    (ws_dir / "origin/post_CTS").mkdir(parents=True, exist_ok=True)
    (ws_dir / "origin/post_CTS/MAX_125_Cworst.tcl").write_text("create_clock -period 10 [get_ports clk]\n")

    (ws_dir / "route_ecc/output").mkdir(parents=True, exist_ok=True)
    (ws_dir / "route_ecc/output/tiny.def").write_text("VERSION 5.8 ;\nDESIGN tiny_top ;\nEND DESIGN\n")
    (ws_dir / "filler_ecc/output").mkdir(parents=True, exist_ok=True)
    (ws_dir / "filler_ecc/output/tiny.v.gz").write_bytes(gzip.compress(b"module tiny_top(); endmodule\n"))

    (ws_dir / "Harden_ecc/output").mkdir(parents=True, exist_ok=True)
    (ws_dir / "Harden_ecc/output/tiny.gds").write_bytes(b"GDS")
    (ws_dir / "Harden_ecc/output/tiny.cdl").write_text(".SUBCKT tiny_top\n.ENDS\n")

    (ws_dir / "RCX_ecc/output").mkdir(parents=True, exist_ok=True)
    (ws_dir / "RCX_ecc/output/tiny_Cworst_125C.spef").write_text(
        '*SPEF "IEEE 1481-1998"\n*DESIGN "tiny_top"\n*C_UNIT 1.0 FF\n*D_NET n1 10.0\n*END\n*D_NET n2 20.0\n*END\n'
    )

    (ws_dir / "sta_ecc/feature/MAX_125/Cworst").mkdir(parents=True, exist_ok=True)
    (ws_dir / "sta_ecc/feature/MAX_125/Cworst/qor_summary.json").write_text(json.dumps({
        "summary": {
            "setup": {"wns": 1.25, "tns": 0.0, "nvp": 0},
            "hold": {"wns": 0.15, "tns": 0.0, "nvp": 0},
        }
    }))

    ant_dir = "antenna_check_ecc" if snake_ant else "antennaCheck_ecc"
    ant_file = "antenna_check.step.json" if snake_ant else "antennaCheck.step.json"
    (ws_dir / ant_dir / "feature").mkdir(parents=True, exist_ok=True)
    (ws_dir / ant_dir / "feature" / ant_file).write_text(json.dumps({"antenna_check": {"violation_count": 0}}))

    (ws_dir / "drc_ecc/feature").mkdir(parents=True, exist_ok=True)
    (ws_dir / "drc_ecc/feature/drc.step.json").write_text(json.dumps({"drc": {"number": 0}}))

    (ws_dir / "lvs_ecc/feature").mkdir(parents=True, exist_ok=True)
    (ws_dir / "lvs_ecc/feature/lvs.step.json").write_text(json.dumps({
        "entity": [{"entity": "Net", "def": 2, "netlist": 2, "difference": 0}]
    }))
    return Workspace(proj)


# ---------------------------------------------------------------- registry & coverage
def test_oss_checks_registered_and_coverage():
    load_all_checks()
    expected_ids = {
        "L3.oss.opensta_timing",
        "L3.oss.openroad_antenna",
        "L3.oss.openroad_rcx",
        "L3.oss.klayout_drc",
        "L3.oss.klayout_antenna",
        "L3.oss.klayout_lvs",
    }
    assert expected_ids.issubset(REGISTRY)
    assert len(REGISTRY) == 85
    m_all = coverage_matrix()
    m_static = coverage_matrix({Layer.L0, Layer.L1, Layer.L2, Layer.L3, Layer.L5})
    assert sum(1 for r in m_all.values() for v in r.values() if v > 0) == 95
    assert sum(1 for r in m_static.values() for v in r.values() if v > 0) == 89


# ---------------------------------------------------------------- config & CLI
def test_oss_config_precedence_roundtrip_and_cli(tmp_path):
    cfg_file = tmp_path / "ecc-verify.toml"
    cfg_file.write_text(
        '[paths]\nopensta_bin = "bin/sta"\nopenroad_bin = "bin/or"\n'
        'klayout_bin = "bin/kl"\nopenpdk_root = "openpdk"\n'
        'oss_docker_image = "img:toml"\n'
        '[timeouts]\nopensta = 111\nopenroad = 222\nklayout_drc = 333\nklayout_lvs = 444\n'
    )
    c_file = config.load(str(cfg_file), env={}, autodetect=False)
    assert c_file.opensta_bin == str(tmp_path / "bin/sta")
    assert c_file.openroad_bin == str(tmp_path / "bin/or")
    assert c_file.klayout_bin == str(tmp_path / "bin/kl")
    assert c_file.openpdk_root == str(tmp_path / "openpdk")
    assert c_file.oss_docker_image == "img:toml"
    assert (c_file.timeouts["opensta"], c_file.timeouts["openroad"],
            c_file.timeouts["klayout_drc"], c_file.timeouts["klayout_lvs"]) == (111, 222, 333, 444)

    env = {
        "ECC_VERIFY_OPENSTA_BIN": "/env/sta",
        "ECC_VERIFY_OPENROAD_BIN": "/env/openroad",
        "ECC_VERIFY_KLAYOUT_BIN": "/env/klayout",
        "ECC_VERIFY_OPENPDK_ROOT": "/env/openpdk",
        "ECC_VERIFY_OSS_DOCKER_IMAGE": "img:env",
    }
    c_env = config.load(str(cfg_file), env=env, autodetect=False)
    assert (c_env.opensta_bin, c_env.openroad_bin, c_env.klayout_bin, c_env.openpdk_root, c_env.oss_docker_image) == (
        "/env/sta", "/env/openroad", "/env/klayout", "/env/openpdk", "img:env"
    )

    c_cli = config.load(
        str(cfg_file),
        {"paths": {"opensta_bin": "/cli/sta", "openpdk_root": "/cli/openpdk", "oss_docker_image": "img:cli"}},
        env=env,
        autodetect=False,
    )
    assert c_cli.opensta_bin == "/cli/sta"
    assert c_cli.openpdk_root == "/cli/openpdk"
    assert c_cli.oss_docker_image == "img:cli"
    assert config.Config.from_dict(c_cli.to_dict()).to_dict() == c_cli.to_dict()

    args = cli_main.build_parser().parse_args([
        "config", "show",
        "--opensta-bin", "/p/sta",
        "--openroad-bin", "/p/or",
        "--klayout-bin", "/p/kl",
        "--openpdk-root", "/p/pdk",
        "--oss-docker-image", "hpretl/iic-osic-tools:latest",
    ])
    ov = cli_main._cli_overrides(args)["paths"]
    assert ov["opensta_bin"] == "/p/sta" and ov["openpdk_root"] == "/p/pdk"
    assert ov["oss_docker_image"] == "hpretl/iic-osic-tools:latest"


def test_oss_autodetect(tmp_path):
    bin_dir = tmp_path / "bins"
    _make_exec(bin_dir / "sta", "print('ok')")
    _make_exec(bin_dir / "openroad", "print('ok')")
    _make_exec(bin_dir / "klayout", "print('ok')")
    opdk = _make_openpdk(tmp_path / "openpdk")
    env = {"PATH": str(bin_dir), "ICS55_OPENPDK_ROOT": str(opdk), "CHIPCOMPILER_ICS55_PDK_ROOT": str(tmp_path)}
    c = config.load(env=env, cwd=tmp_path, autodetect=True)
    assert c.opensta_bin == str(bin_dir / "sta")
    assert c.openroad_bin == str(bin_dir / "openroad")
    assert c.klayout_bin == str(bin_dir / "klayout")
    assert c.openpdk_root == str(opdk)


# ---------------------------------------------------------------- parsers & comparators
def test_parsers_and_spef_compare(tmp_path):
    sta = oss.parse_opensta_output(
        "ECC_VERIFY_SETUP_WNS -0.25\nECC_VERIFY_SETUP_TNS -1.5\n"
        "ECC_VERIFY_HOLD_WNS 0.12\nECC_VERIFY_HOLD_TNS 0.0\n"
    )
    assert sta == {"setup_wns": -0.25, "setup_tns": -1.5, "hold_wns": 0.12, "hold_tns": 0.0}
    assert oss.parse_opensta_output("wns max 0.44\ntns max 0.0\n")["setup_wns"] == 0.44

    ant = oss.parse_openroad_antenna("[INFO ANT] Found 7 pin violations.\n[INFO ANT] Found 3 net violations.\n")
    assert ant == {"pin_violations": 7, "net_violations": 3}

    lyrdb = tmp_path / "r.lyrdb"
    lyrdb.write_text(
        '<?xml version="1.0"?><report-database><items>'
        "<item><category>'AR_5_10_M1'</category></item>"
        "<item><category>'AR_5_10_M1'</category></item>"
        "<item><category>'M1.W.1'</category></item>"
        "</items></report-database>"
    )
    db = oss.parse_klayout_lyrdb(lyrdb)
    assert db["total"] == 3 and db["by_category"] == {"AR_5_10_M1": 2, "M1.W.1": 1}

    assert oss.parse_klayout_lvs_output("INFO : Congratulations! Netlists match.")["matched"] is True
    assert oss.parse_klayout_lvs_output("ERROR : Netlists don't match")["matched"] is False
    assert oss.parse_klayout_lvs_output("something else")["matched"] is None

    e_sp = tmp_path / "e.spef"
    o_sp = tmp_path / "o.spef"
    e_sp.write_text('*SPEF "IEEE 1481-1998"\n*C_UNIT 1.0 FF\n*D_NET a 10.0\n*END\n*D_NET b 20.0\n*END\n')
    o_sp.write_text('*SPEF "IEEE 1481-1998"\n*C_UNIT 1.0 FF\n*D_NET a 11.0\n*END\n*D_NET b 20.0\n*END\n')
    cmp_ = oss.compare_spef_caps(e_sp, o_sp)
    assert cmp_["shared_nets"] == 2 and round(cmp_["max_rel_diff"], 3) == round(1.0 / 11.0, 3)


# ---------------------------------------------------------------- skip & mock-oracle pass/fail
def test_all_oss_checks_skip_when_unconfigured(tmp_path):
    ws = _make_workspace(tmp_path)
    old = config.current()
    try:
        config.set_current(config.Config())
        for cid in (
            "L3.oss.opensta_timing",
            "L3.oss.openroad_antenna",
            "L3.oss.openroad_rcx",
            "L3.oss.klayout_drc",
            "L3.oss.klayout_antenna",
            "L3.oss.klayout_lvs",
        ):
            r = run_check(REGISTRY[cid][1], ws)
            assert r.status == Status.SKIP, (cid, r)
    finally:
        config.set_current(old)


def test_oss_checks_pass_and_fail_with_mock_oracles(tmp_path):
    ws = _make_workspace(tmp_path, snake_ant=True)
    openpdk = _make_openpdk(tmp_path / "ics55-openpdk")

    # Passing stubs (also asserts OpenSTA Tcl uses read_spef and remove_from_collection shim)
    sta_ok = _make_exec(tmp_path / "ok/sta", """
import sys
tcl = open(sys.argv[2]).read()
assert "read_spef " in tcl and "remove_from_collection" in tcl
print("ECC_VERIFY_SETUP_WNS 1.24")
print("ECC_VERIFY_SETUP_TNS 0.0")
print("ECC_VERIFY_HOLD_WNS 0.14")
print("ECC_VERIFY_HOLD_TNS 0.0")
""")
    or_ok = _make_exec(tmp_path / "ok/openroad", """
import sys, re
tcl = open(sys.argv[2]).read()
if "check_antennas" in tcl:
    print("Found 0 pin violations.")
    print("Found 0 net violations.")
elif "write_spef" in tcl:
    m = re.search(r'write_spef\\s+"([^"]+)"', tcl)
    open(m.group(1), "w").write('*SPEF "IEEE 1481-1998"\\n*C_UNIT 1.0 FF\\n*D_NET n1 10.2\\n*END\\n*D_NET n2 19.8\\n*END\\n')
""")
    kl_ok = _make_exec(tmp_path / "ok/klayout", """
import sys
args = dict(a.split("=", 1) for a in sys.argv if "=" in a)
rep = args["report"]
if rep.endswith(".lyrdb"):
    open(rep, "w").write('<?xml version="1.0"?><report-database><items/></report-database>')
else:
    open(rep, "w").write("# LVSDB\\n")
    print("INFO : Congratulations! Netlists match.")
""")

    old = config.current()
    try:
        config.set_current(config.Config(
            opensta_bin=str(sta_ok),
            openroad_bin=str(or_ok),
            klayout_bin=str(kl_ok),
            openpdk_root=str(openpdk),
        ))
        assert run_check(oss.opensta_timing, ws).status == Status.PASS
        assert run_check(oss.openroad_antenna, ws).status == Status.PASS
        assert run_check(oss.openroad_rcx, ws).status == Status.PASS
        assert run_check(oss.klayout_drc, ws).status == Status.PASS
        assert run_check(oss.klayout_antenna, ws).status == Status.PASS
        assert run_check(oss.klayout_lvs, ws).status == Status.PASS

        # Failing stubs catching divergences & hidden violations
        sta_bad = _make_exec(tmp_path / "bad/sta", """
print("ECC_VERIFY_SETUP_WNS -0.80")
print("ECC_VERIFY_SETUP_TNS -4.2")
print("ECC_VERIFY_HOLD_WNS 0.14")
print("ECC_VERIFY_HOLD_TNS 0.0")
""")
        or_bad = _make_exec(tmp_path / "bad/openroad", """
import sys, re
tcl = open(sys.argv[2]).read()
if "check_antennas" in tcl:
    print("Found 3 pin violations.")
    print("Found 2 net violations.")
elif "write_spef" in tcl:
    m = re.search(r'write_spef\\s+"([^"]+)"', tcl)
    open(m.group(1), "w").write('*SPEF "IEEE 1481-1998"\\n*C_UNIT 1.0 FF\\n*D_NET n1 30.0\\n*END\\n*D_NET n2 50.0\\n*END\\n')
""")
        kl_bad = _make_exec(tmp_path / "bad/klayout", """
import sys
args = dict(a.split("=", 1) for a in sys.argv if "=" in a)
rep = args["report"]
if rep.endswith(".lyrdb"):
    open(rep, "w").write('<?xml version="1.0"?><report-database><items><item><category>RULE1</category></item></items></report-database>')
else:
    open(rep, "w").write("# LVSDB\\n")
    print("ERROR : Netlists don't match")
""")
        config.set_current(config.Config(
            opensta_bin=str(sta_bad),
            openroad_bin=str(or_bad),
            klayout_bin=str(kl_bad),
            openpdk_root=str(openpdk),
        ))
        r_sta = run_check(oss.opensta_timing, ws)
        assert r_sta.status == Status.FAIL and "sign flip" in r_sta.message
        r_ant = run_check(oss.openroad_antenna, ws)
        assert r_ant.status == Status.FAIL and "2 net / 3 pin" in r_ant.message
        r_rcx = run_check(oss.openroad_rcx, ws)
        assert r_rcx.status == Status.FAIL and "exceeds 25%" in r_rcx.message
        r_kdrc = run_check(oss.klayout_drc, ws)
        assert r_kdrc.status == Status.FAIL and r_kdrc.measured == 1
        r_kant = run_check(oss.klayout_antenna, ws)
        assert r_kant.status == Status.FAIL and r_kant.measured == 1
        r_klvs = run_check(oss.klayout_lvs, ws)
        assert r_klvs.status == Status.FAIL and "do not match" in r_klvs.message

        # Also test non-snake-case OpenPDK layout without `icsprout55/` nesting and camelCase antennaCheck
        flat_pdk = _make_openpdk(tmp_path / "flat-openpdk", nested=False)
        ws_camel = _make_workspace(tmp_path / "camel_ws", snake_ant=False)
        config.set_current(config.Config(openroad_bin=str(or_ok), klayout_bin=str(kl_ok), openpdk_root=str(flat_pdk)))
        assert run_check(oss.klayout_drc, ws).status == Status.PASS
        assert run_check(oss.openroad_antenna, ws_camel).status == Status.PASS

        # Stage helper decompresses .gz and copies external paths when force_stage=True
        stage_dir = tmp_path / "staged"
        stage_dir.mkdir()
        gz_src = ws.output_file("filler", ".v.gz")
        staged_gz = oss._stage_file(gz_src, stage_dir, force_stage=True)
        assert staged_gz.parent == stage_dir and staged_gz.suffix == ".v"
        ext_src = tmp_path / "plain.spef"
        ext_src.write_text("*D_NET n 1\n")
        staged_ext = oss._stage_file(ext_src, stage_dir, force_stage=True)
        assert staged_ext.parent == stage_dir

        # IEEE 1481 unescaped '$' SPEF defect: raw run fails with syntax error, retry with '\\$' succeeds -> FAIL
        spef_path = ws.path / "RCX_ecc/output/tiny_Cworst_125C.spef"
        spef_path.write_text(
            '*SPEF "IEEE 1481-1998"\n*NAME_MAP\n*10 state_8__$_OR__A_Y\n*D_NET *10 10.0\n*END\n'
        )
        sta_spef = _make_exec(tmp_path / "spef/sta", r"""
import re, sys
tcl = open(sys.argv[2]).read()
m = re.search(r'read_spef\s+"([^"]+)"', tcl)
spef = open(m.group(1)).read()
if re.search(r'(?<!\\)\$', spef):
    sys.stderr.write("Error: 1670 x.spef line 3, syntax error\n")
    sys.exit(1)
print("ECC_VERIFY_SETUP_WNS 1.24")
print("ECC_VERIFY_SETUP_TNS 0.0")
print("ECC_VERIFY_HOLD_WNS 0.14")
print("ECC_VERIFY_HOLD_TNS 0.0")
""")
        config.set_current(config.Config(opensta_bin=str(sta_spef)))
        r_spef = run_check(oss.opensta_timing, ws)
        assert r_spef.status == Status.FAIL
        assert r_spef.details.get("spef_syntax_defect") is True
        assert r_spef.details["opensta"]["setup_wns"] == 1.24
    finally:
        config.set_current(old)
