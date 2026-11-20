"""Config loading: defaults, TOML, environment, CLI precedence, auto-detection, worker round trip."""
from pathlib import Path

import pytest

from ecc_verify import config, util
from ecc_verify.config import Config, ConfigError


def _toml(tmp_path, text):
    p = tmp_path / "ecc-verify.toml"
    p.write_text(text)
    return p


def test_defaults_without_autodetect(tmp_path):
    c = config.load(env={}, cwd=tmp_path, autodetect=False)
    assert c.ecc == "ecc" and c.pdk_root is None and c.oss_cad_bin is None
    assert c.workers == 6 and c.fault_target == "APU" and c.fault_path is None
    assert c.timeouts["miter_solver"] == 300 and c.sampling["rt_sample_min_cells"] == 10000
    assert c.tolerances["hpwl_rel"] == 0.02 and c.tolerances["runtime_rel"] == 0.25
    assert c.cpus and c.pin_cpus and c.cpus != c.pin_cpus or (config.os.cpu_count() or 1) < 2


def test_toml_values_and_relative_paths(tmp_path):
    f = _toml(tmp_path, '''
[ecc]
bin = "tools/ecc"
[paths]
pdk_root = "pdk"
designs_dir = "~/designs"
scratch_dir = "scratch"
baseline = "../base"
[run]
cpus = "4-7"
pin_cpus = "0-3"
workers = 3
[fault_injection]
target = "xtea"
path = "fi.py"
[timeouts]
miter_solver = 42
[sampling]
fn_max_cells = 777
[compare.tolerances]
hpwl_rel = 0.5
[compare]
ignore_metrics = ["runtime.*"]
''')
    c = config.load(str(f), env={}, autodetect=False)
    assert c.ecc == str(tmp_path / "tools/ecc")
    assert c.pdk_root == str(tmp_path / "pdk")
    assert c.designs_dir == str(Path.home() / "designs")
    assert c.scratch_dir == str(tmp_path / "scratch")
    assert c.baseline == str(tmp_path / "../base")
    assert (c.cpus, c.pin_cpus, c.workers) == ("4-7", "0-3", 3)
    assert c.fault_target == "xtea" and c.fault_path == str(tmp_path / "fi.py")
    assert c.fault_scratch == str(tmp_path / "scratch" / "fi_scratch")
    assert c.timeout("miter_solver") == 42 and c.timeout("cli_help") == 60      # untouched keys keep defaults
    assert c.sample("fn_max_cells") == 777 and c.sample("fn_bmc_cycles") == 25
    assert c.tolerances["hpwl_rel"] == 0.5 and c.tolerances["power_rel"] == 0.03
    assert c.compare["ignore_metrics"] == ["runtime.*"]


def test_precedence_cli_over_env_over_file_over_default(tmp_path):
    f = _toml(tmp_path, '[run]\ncpus = "1-2"\nworkers = 2\n[paths]\ndesigns_dir = "/from/file"\n')
    env = {"ECC_VERIFY_CPUS": "3-4", "ECC_VERIFY_WORKERS": "5", "ECC_VERIFY_DESIGNS_DIR": "/from/env"}
    c = config.load(str(f), env=env, autodetect=False)
    assert (c.cpus, c.workers, c.designs_dir) == ("3-4", 5, "/from/env")
    c = config.load(str(f), {"run": {"cpus": "9-10", "workers": None}, "paths": {"designs_dir": "/from/cli"}},
                    env=env, autodetect=False)
    assert (c.cpus, c.workers, c.designs_dir) == ("9-10", 5, "/from/cli")
    c = config.load(str(f), env={}, autodetect=False)
    assert (c.cpus, c.workers, c.designs_dir) == ("1-2", 2, "/from/file")


def test_config_file_discovery(tmp_path, monkeypatch):
    _toml(tmp_path, "[run]\nworkers = 11\n")
    assert config.load(env={}, cwd=tmp_path, autodetect=False).workers == 11          # ./ecc-verify.toml
    other = tmp_path / "other.toml"
    other.write_text("[run]\nworkers = 12\n")
    assert config.load(env={"ECC_VERIFY_CONFIG": str(other)}, cwd=tmp_path, autodetect=False).workers == 12
    assert config.load(str(other), env={"ECC_VERIFY_CONFIG": "/nonexistent"}, cwd=tmp_path,
                       autodetect=False).workers == 12                                  # explicit wins


def test_errors_are_config_errors(tmp_path):
    with pytest.raises(ConfigError):
        config.load(str(tmp_path / "missing.toml"), env={}, autodetect=False)
    bad = _toml(tmp_path, "[run\nworkers = ")
    with pytest.raises(ConfigError):
        config.load(str(bad), env={}, autodetect=False)
    with pytest.raises(ConfigError):
        config.load(None, {"run": {"workers": "many"}}, env={}, cwd=tmp_path, autodetect=False)
    with pytest.raises(ConfigError):
        config.load(env={"ECC_VERIFY_CONFIG": str(tmp_path / "nope.toml")}, cwd=tmp_path, autodetect=False)


def test_pdk_autodetect_from_env_var(tmp_path, fake_ecc):
    ecc, _ = fake_ecc
    pdk = tmp_path / "envpdk"
    pdk.mkdir()
    c = config.load(None, {"ecc": {"bin": str(ecc)}}, env={"CHIPCOMPILER_ICS55_PDK_ROOT": str(pdk)}, cwd=tmp_path)
    assert c.pdk_root == str(pdk)
    assert "CHIPCOMPILER_ICS55_PDK_ROOT" in c.source


def test_pdk_autodetect_from_ecc_pdk_show(tmp_path, fake_ecc):
    ecc, root = fake_ecc
    c = config.load(None, {"ecc": {"bin": str(ecc)}}, env={}, cwd=tmp_path)
    assert c.pdk_root == str(root) and "ecc pdk show" in c.source
    assert c.std_cell == root / "IP/STD_cell/libA"


def test_explicit_pdk_beats_autodetect(tmp_path, fake_ecc):
    ecc, _ = fake_ecc
    c = config.load(None, {"ecc": {"bin": str(ecc)}, "paths": {"pdk_root": "/explicit"}}, env={}, cwd=tmp_path)
    assert c.pdk_root == "/explicit"


def test_ecc_bare_name_resolved_on_path(tmp_path, fake_ecc, monkeypatch):
    ecc, _ = fake_ecc
    monkeypatch.setenv("PATH", f"{ecc.parent}:/usr/bin:/bin")
    c = config.load(env={}, cwd=tmp_path, autodetect=True)
    assert c.ecc == str(ecc)


def test_to_dict_roundtrip_and_util_follow_config(tmp_path):
    c = config.load(None, {"paths": {"pdk_root": str(tmp_path / "p"), "scratch_dir": str(tmp_path / "s"),
                                     "oss_cad_bin": str(tmp_path / "bin")}}, env={}, cwd=tmp_path, autodetect=False)
    c2 = Config.from_dict(c.to_dict())
    assert c2.to_dict() == c.to_dict()
    config.set_current(c2)
    assert util.PDK == tmp_path / "p" and util.VERIFY_RUNS == tmp_path / "s" and util.OSS_BIN == tmp_path / "bin"
    assert util.LAYERMAP == tmp_path / "p/techfile/icsprout55.layermap"
    env = c2.tool_env()
    assert env["PATH"].startswith(str(tmp_path / "bin")) and env["CHIPCOMPILER_ICS55_PDK_ROOT"] == str(tmp_path / "p")


def test_pin_prefix(tmp_path, monkeypatch):
    c = Config(pin_cpus="0-3")
    monkeypatch.setattr(config.shutil, "which", lambda n: "/usr/bin/taskset")
    assert c.pin(["x", "y"]) == ["taskset", "-c", "0-3", "x", "y"]
    assert c.pin(["x"], "5") == ["taskset", "-c", "5", "x"]
    monkeypatch.setattr(config.shutil, "which", lambda n: None)
    assert c.pin(["x"]) == ["x"]


def test_example_toml_is_valid_and_loads(tmp_path):
    import tomllib
    tomllib.loads(config.EXAMPLE_TOML)
    f = _toml(tmp_path, config.EXAMPLE_TOML)
    assert config.load(str(f), env={}, autodetect=False).workers == 6


def test_std_cell_discovery_v1_and_v2_layouts(tmp_path):
    from ecc_verify.checks import l3_functional

    # Nested three-Vt layout (v1-style)
    pdk_v1 = tmp_path / "pdk_v1"
    for flavor in ("R", "L", "H"):
        d = pdk_v1 / f"IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7C{flavor}"
        (d / "liberty").mkdir(parents=True)
        (d / "verilog").mkdir(parents=True)
        (d / "liberty" / f"ics55_LLSC_H7C{flavor}_typ_tt_1p2_25_nldm.lib").write_text("library(x){}\n")
        (d / "liberty" / f"ics55_LLSC_H7C{flavor}_ss_1p08_125_nldm.lib").write_text("library(y){}\n")
        (d / "verilog" / f"ics55_LLSC_H7C{flavor}.v").write_text("module x(); endmodule\n")
    c1 = Config(pdk_root=str(pdk_v1))
    assert c1.std_cell == pdk_v1 / "IP/STD_cell/ics55_LLSC_H7C_V1p10C100"
    assert len(c1.std_cell_dirs()) == 3
    libs1 = c1.std_cell_liberties()
    assert len(libs1) == 3 and all("_typ_tt_1p2_25_nldm.lib" in p.name for p in libs1)
    vs1 = c1.std_cell_verilogs()
    assert len(vs1) == 3 and all(p.name.endswith(".v") and "_pg" not in p.name for p in vs1)

    # Flat per-flavor layout (v2-style)
    pdk_v2 = tmp_path / "pdk_v2"
    for vt in ("HVT", "LVT", "SVT"):
        d = pdk_v2 / f"IP/STD_cell/ICsprout55_SC9T_BASIC_{vt}"
        (d / "liberty").mkdir(parents=True)
        (d / "verilog").mkdir(parents=True)
        (d / "liberty" / f"ICsprout55_9T{vt}_ss_v1p08_125c_basic_nldm.lib").write_text("library(ss){}\n")
        (d / "liberty" / f"ICsprout55_9T{vt}_tt_v1p2_25c_basic_nldm.lib").write_text("library(tt){}\n")
        (d / "verilog" / f"ICsprout55_9T{vt}_basic.v").write_text("module c(); endmodule\n")
        (d / "verilog" / f"ICsprout55_9T{vt}_basic_pg.v").write_text("module c(); endmodule\n")
    c2 = Config(pdk_root=str(pdk_v2))
    assert c2.std_cell == pdk_v2 / "IP/STD_cell"
    assert len(c2.std_cell_dirs()) == 3
    libs2 = c2.std_cell_liberties()
    assert len(libs2) == 3 and all("tt_v1p2_25c_basic_nldm.lib" in p.name for p in libs2)
    vs2 = c2.std_cell_verilogs()
    assert [p.name for p in vs2] == [f"ICsprout55_9T{vt}_basic.v" for vt in ("HVT", "LVT", "SVT")]

    # Workspace db_ecc.json takes precedence over PDK fallback when present
    class _FakeWs:
        def __init__(self, libs):
            self._libs = libs

        def _db(self):
            return {"INPUT": {"lib_path": [str(p) for p in self._libs]}}

    config.set_current(c1)
    ws_libs = l3_functional._libs(_FakeWs(libs2))
    ws_mods = l3_functional._models(_FakeWs(libs2))
    assert ws_libs == [str(p) for p in libs2]
    assert ws_mods == [str(p) for p in vs2]
    # Falls back to PDK discovery when db_ecc.json is unavailable
    assert l3_functional._libs(None) == [str(p) for p in libs1]
    assert l3_functional._models(None) == [str(p) for p in vs1]
