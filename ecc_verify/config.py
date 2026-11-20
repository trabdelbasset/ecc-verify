"""Configuration: ``ecc-verify.toml`` + environment variables + CLI overrides + auto-detection.

Precedence (highest first):  CLI flag  >  ``ECC_VERIFY_*`` environment variable  >  ``ecc-verify.toml``  >
auto-detection  >  built-in default.

The resolved :class:`Config` is process-global (``current()``) because checks run in spawned worker processes; the
parent passes ``cfg.to_dict()`` to every worker which calls ``set_current(Config.from_dict(...))``.

Config file discovery: ``--config FILE``, else ``$ECC_VERIFY_CONFIG``, else ``./ecc-verify.toml``.
Relative paths inside the file are resolved against the directory of the file.
"""
from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------- defaults
DEFAULT_TIMEOUTS = {          # seconds
    "cli_help": 60,           # L0: `ecc <cmd> --help`
    "cli_json": 120,          # L0: `ecc <cmd> --json`, `ecc status`
    "gds_probe": 600,         # L3.gds: KLayout probe
    "yosys_ports": 300,       # L3.fn: port extraction
    "yosys_miter": 900,       # L3.fn: outer wall limit of the bounded miter
    "miter_solver": 300,      # L3.fn: `sat -timeout` inside the miter (expiry -> SKIP)
    "sim": 600,               # L3.fn: iverilog compile and vvp run
    "full_flow": 1500,        # L4/L5: one complete `ecc run`
    "step": 600,              # L4: one `ecc run --only <step>`
    "opensta": 600,           # L3.oss.opensta_timing
    "openroad": 600,          # L3.oss.openroad_antenna / openroad_rcx
    "klayout_drc": 1200,      # L3.oss.klayout_drc / klayout_antenna
    "klayout_lvs": 1200,      # L3.oss.klayout_lvs
}
DEFAULT_SAMPLING = {
    "rt_sample_min_cells": 10000,   # L3.rt.connectivity samples nets at/above this many components
    "rt_sample_stride": 3,          # ... every Nth net
    "fn_max_cells": 2000,           # L3.fn only runs below this std-cell count
    "fn_bmc_cycles": 25,
    "fn_sim_cycles": 1000,
    "fn_settle_cycles": 24,
}
# Compare tolerances (see compare.py).  Keys are documented in docs/guide.md.
DEFAULT_TOLERANCES = {
    "ws_abs_ns": 0.05,        # setup/hold worst slack: allowed drift = max(ws_abs_ns, ws_rel_t * T)
    "ws_rel_t": 0.05,
    "tns_abs_ns": 0.05,       # TNS: allowed = max(tns_abs_ns, tns_rel * |baseline|)
    "tns_rel": 0.10,
    "nvp_increase": 0,        # violating-path count may grow by at most this
    "hpwl_rel": 0.02,
    "route_wl_rel": 0.02,
    "via_rel": 0.02,
    "area_rel": 0.01,
    "cells_rel": 0.01,
    "util_abs": 0.01,         # core utilization (absolute ratio)
    "power_rel": 0.03,
    "drv_increase": 0,        # max_transition / max_cap / max_fanout / DRC / LVS counts: no increase
    "runtime_rel": 0.25,
    "runtime_min_s": 5.0,     # ignore runtime drift when the absolute change is below this
    "memory_rel": 0.25,
    "memory_min_mb": 50.0,
}
ENV_PREFIX = "ECC_VERIFY_"
SECTION_ENV = {  # env var suffix -> (section, key)
    "ECC": ("ecc", "bin"),
    "PDK_ROOT": ("paths", "pdk_root"),
    "OSS_CAD_BIN": ("paths", "oss_cad_bin"),
    "OPENSTA_BIN": ("paths", "opensta_bin"),
    "OPENROAD_BIN": ("paths", "openroad_bin"),
    "KLAYOUT_BIN": ("paths", "klayout_bin"),
    "OPENPDK_ROOT": ("paths", "openpdk_root"),
    "OSS_DOCKER_IMAGE": ("paths", "oss_docker_image"),
    "DESIGNS_DIR": ("paths", "designs_dir"),
    "SCRATCH_DIR": ("paths", "scratch_dir"),
    "KNOWN_ISSUES": ("paths", "known_issues"),
    "BASELINE": ("paths", "baseline"),
    "CPUS": ("run", "cpus"),
    "PIN_CPUS": ("run", "pin_cpus"),
    "WORKERS": ("run", "workers"),
    "FAULT_TARGET": ("fault_injection", "target"),
    "FAULT_SCRIPT": ("fault_injection", "path"),
}


class ConfigError(Exception):
    """Unusable configuration (exit code 2)."""


# --------------------------------------------------------------------------- dataclass
@dataclass
class Config:
    ecc: str = "ecc"
    pdk_root: str | None = None
    oss_cad_bin: str | None = None
    opensta_bin: str | None = None
    openroad_bin: str | None = None
    klayout_bin: str | None = None
    openpdk_root: str | None = None
    oss_docker_image: str | None = None
    designs_dir: str | None = None
    scratch_dir: str = ""
    baseline: str | None = None          # baseline directory used by `run` (and `compare` when --baseline is omitted)
    known_issues: str | None = None      # None -> ./known_issues.toml if present, else the packaged default
    cpus: str = ""                       # taskset list for real ecc runs (L4/L5)
    pin_cpus: str = ""                   # taskset list for pinned tool calls in the static run
    workers: int = 6
    fault_target: str = "APU"
    fault_path: str | None = None        # None -> vendored ecc_verify/vendor/fault_inject.py
    fault_scratch: str = ""
    timeouts: dict = field(default_factory=lambda: dict(DEFAULT_TIMEOUTS))
    sampling: dict = field(default_factory=lambda: dict(DEFAULT_SAMPLING))
    tolerances: dict = field(default_factory=lambda: dict(DEFAULT_TOLERANCES))
    compare: dict = field(default_factory=lambda: {"ignore_metrics": [], "warn_only_metrics": []})
    source: str = ""                     # human readable description of where the values came from
    config_file: str | None = None

    # ---- (de)serialisation for worker processes
    def to_dict(self) -> dict:
        return copy.deepcopy(self.__dict__)

    @classmethod
    def from_dict(cls, d: dict) -> "Config":
        c = cls()
        for k, v in d.items():
            if hasattr(c, k):
                setattr(c, k, copy.deepcopy(v))
        return c

    # ---- derived paths
    @property
    def pdk(self) -> Path | None:
        return Path(self.pdk_root) if self.pdk_root else None

    @property
    def openpdk(self) -> Path | None:
        return Path(self.openpdk_root) if self.openpdk_root else None

    @property
    def std_cell(self) -> Path:
        """Std-cell library directory inside the PDK (single version dir or multi-flavor IP/STD_cell)."""
        p = self.pdk
        if p is None:
            raise ConfigError("PDK root not configured (set paths.pdk_root, $CHIPCOMPILER_ICS55_PDK_ROOT or `ecc pdk set-root`)")
        base = p / "IP" / "STD_cell"
        cands = sorted(x for x in base.glob("*") if x.is_dir()) if base.is_dir() else []
        if len(cands) == 1:
            return cands[0]
        if len(cands) > 1:
            return base
        return base / "ics55_LLSC_H7C_V1p10C100"

    def std_cell_dirs(self) -> list[Path]:
        """Return per-flavor standard-cell directories across flat or nested PDK layouts."""
        p = self.pdk
        if p is None:
            raise ConfigError("PDK root not configured (set paths.pdk_root, $CHIPCOMPILER_ICS55_PDK_ROOT or `ecc pdk set-root`)")
        base = p / "IP" / "STD_cell"
        if not base.is_dir():
            return []
        out: list[Path] = []
        subdirs = ("liberty", "verilog", "lef")
        for child in sorted(x for x in base.iterdir() if x.is_dir()):
            if any((child / sub).is_dir() for sub in subdirs):
                out.append(child)
                continue
            for gc in sorted(x for x in child.iterdir() if x.is_dir()):
                if any((gc / sub).is_dir() for sub in subdirs):
                    out.append(gc)
        return out

    def std_cell_liberties(self) -> list[Path]:
        """Select one typical NLDM Liberty file per standard-cell flavor directory."""
        out: list[Path] = []
        for d in self.std_cell_dirs():
            libdir = d / "liberty"
            if not libdir.is_dir():
                continue
            files = sorted(libdir.glob("*.lib"))
            if not files:
                continue
            pick = next(
                (f for f in files if all(k in f.name.lower() for k in ("tt", "1p2", "25", "nldm"))),
                None,
            ) or next(
                (f for f in files if all(k in f.name.lower() for k in ("tt", "25", "nldm"))),
                None,
            ) or next(
                (f for f in files if "tt" in f.name.lower()),
                files[0],
            )
            out.append(pick)
        return out

    def std_cell_verilogs(self) -> list[Path]:
        """Select behavioral Verilog cell models per standard-cell flavor directory."""
        out: list[Path] = []
        for d in self.std_cell_dirs():
            vdir = d / "verilog"
            if not vdir.is_dir():
                continue
            files = sorted(vdir.glob("*.v"))
            if not files:
                continue
            non_pg = [f for f in files if not f.stem.lower().endswith("_pg")]
            out.extend(non_pg or files)
        return out

    @property
    def layermap(self) -> Path:
        p = self.pdk
        if p is None:
            raise ConfigError("PDK root not configured")
        return p / "techfile" / "icsprout55.layermap"

    @property
    def runs_dir(self) -> Path:
        return Path(self.scratch_dir)

    def timeout(self, key: str) -> int:
        return int(self.timeouts.get(key, DEFAULT_TIMEOUTS[key]))

    def sample(self, key: str):
        return self.sampling.get(key, DEFAULT_SAMPLING[key])

    def tool_env(self, extra: dict | None = None) -> dict:
        """Environment for subprocesses: oss-cad-suite on PATH, PDK root exported."""
        e = dict(os.environ)
        if self.oss_cad_bin:
            e["PATH"] = f"{self.oss_cad_bin}{os.pathsep}{e.get('PATH', '')}"
        if self.pdk_root:
            e["CHIPCOMPILER_ICS55_PDK_ROOT"] = str(self.pdk_root)
        if extra:
            e.update(extra)
        return e

    def pin(self, cmd: list[str], cpus: str | None = None) -> list[str]:
        """Prefix `taskset -c <cpus>` when taskset exists and a cpu list is set."""
        cpus = cpus or self.pin_cpus
        if cpus and shutil.which("taskset"):
            return ["taskset", "-c", cpus, *cmd]
        return list(cmd)


_CURRENT: Config | None = None


def current() -> Config:
    """The process-wide config; built from defaults/env on first use (so library use and tests work unconfigured)."""
    global _CURRENT
    if _CURRENT is None:
        _CURRENT = load()
    return _CURRENT


def set_current(cfg: Config | None) -> None:
    global _CURRENT
    _CURRENT = cfg


# --------------------------------------------------------------------------- auto-detection
def _half_split(n: int) -> tuple[str, str]:
    """(low half, high half) of the machine as taskset lists; the high half is for real ecc runs."""
    if n < 2:
        return "0", "0"
    h = n // 2
    return f"0-{h - 1}", f"{h}-{n - 1}"


def detect_ecc() -> str:
    found = shutil.which("ecc")
    if found:
        return found
    p = Path.home() / ".local/bin/ecc"
    return str(p) if p.exists() else "ecc"


def _run_json(cmd: list[str], timeout: int = 30) -> Any:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return json.loads(p.stdout)
    except Exception:  # noqa: BLE001
        return None


def detect_pdk(ecc: str, env=os.environ) -> tuple[str | None, str]:
    v = env.get("CHIPCOMPILER_ICS55_PDK_ROOT")
    if v and Path(v).is_dir():
        return v, "CHIPCOMPILER_ICS55_PDK_ROOT"
    j = _run_json([ecc, "pdk", "show", "--json"])
    if isinstance(j, list):
        for rec in j:
            if isinstance(rec, dict) and rec.get("root") and Path(rec["root"]).is_dir():
                return rec["root"], "ecc pdk show --json"
    return None, "not found"


def detect_oss_bin(env=os.environ) -> tuple[str | None, str]:
    y = shutil.which("yosys")
    if y:
        return str(Path(y).parent), "yosys on PATH"
    base = Path.home() / ".local/share/ecc/tools/oss-cad-suite"
    cands = sorted(base.glob("*/bin")) if base.is_dir() else []
    cands = [c for c in cands if (c / "yosys").exists()]
    if cands:
        return str(cands[-1]), "~/.local/share/ecc/tools/oss-cad-suite"
    return None, "not found"


def detect_binary(name: str, env=os.environ) -> tuple[str | None, str]:
    found = shutil.which(name, path=env["PATH"]) if "PATH" in env else shutil.which(name)
    return (found, f"{name} on PATH") if found else (None, "not found")


def detect_openpdk(env=os.environ) -> tuple[str | None, str]:
    for key in ("ICS55_OPENPDK_ROOT", "OPENPDK_ROOT"):
        v = env.get(key)
        if v and Path(v).is_dir():
            return v, key
    return None, "not found"


# --------------------------------------------------------------------------- loading
def _deep_merge(dst: dict, src: dict) -> dict:
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _deep_merge(dst[k], v)
        else:
            dst[k] = v
    return dst


def find_config_file(explicit: str | None, env=os.environ, cwd: Path | None = None) -> Path | None:
    if explicit:
        p = Path(explicit)
        if not p.is_file():
            raise ConfigError(f"config file not found: {p}")
        return p
    e = env.get("ECC_VERIFY_CONFIG")
    if e:
        p = Path(e)
        if not p.is_file():
            raise ConfigError(f"$ECC_VERIFY_CONFIG points to a missing file: {p}")
        return p
    p = (cwd or Path.cwd()) / "ecc-verify.toml"
    return p if p.is_file() else None


def _resolve(base: Path | None, v: str | None) -> str | None:
    if v in (None, ""):
        return None
    p = Path(os.path.expanduser(str(v)))
    if not p.is_absolute() and base is not None:
        p = base / p
    return str(p)


def load(config_file: str | None = None, cli: dict | None = None, env=None, cwd: Path | None = None,
         autodetect: bool = True) -> Config:
    """Build the effective config.

    ``cli`` uses the same ``{section: {key: value}}`` shape as the TOML file (None values are ignored).
    ``autodetect=False`` skips the subprocess probes (used by unit tests)."""
    env = os.environ if env is None else env
    raw: dict = {}
    src: dict[str, str] = {}
    cfg_path = find_config_file(config_file, env, cwd)
    base = cfg_path.parent if cfg_path else None
    if cfg_path:
        try:
            with open(cfg_path, "rb") as f:
                raw = tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            raise ConfigError(f"{cfg_path}: invalid TOML: {e}") from e
    # environment overrides (below CLI, above file)
    envd: dict = {}
    for suf, (sec, key) in SECTION_ENV.items():
        v = env.get(ENV_PREFIX + suf)
        if v not in (None, ""):
            envd.setdefault(sec, {})[key] = v
    clid = {s: {k: v for k, v in d.items() if v is not None} for s, d in (cli or {}).items()}

    def pick(sec: str, key: str, default=None):
        for name, d in (("cli", clid), ("env", envd), ("file", raw)):
            v = d.get(sec, {}).get(key) if isinstance(d.get(sec), dict) else None
            if v not in (None, ""):
                src[f"{sec}.{key}"] = name
                return v, name
        return default, "default"

    cfg = Config(config_file=str(cfg_path) if cfg_path else None)

    def path_val(sec, key):
        v, name = pick(sec, key)
        if v is None:
            return None
        return _resolve(base if name == "file" else None, v)

    def bin_val(sec: str, key: str, default_cmd: str) -> tuple[str | None, str]:
        v, name = pick(sec, key)
        if v:
            s = str(v)
            if os.sep in s or s.startswith("~"):
                s = _resolve(base if name == "file" else None, s) or s
            resolved = shutil.which(s, path=env["PATH"]) if "PATH" in env else shutil.which(s)
            return resolved or s, name
        if autodetect:
            return detect_binary(default_cmd, env)
        return None, "not found"

    # ecc (a bare command name is looked up on PATH; anything with a slash is a path relative to its source)
    v, name = pick("ecc", "bin")
    ecc_from = name
    if v:
        v = str(v)
        if os.sep in v or v.startswith("~"):
            v = _resolve(base if name == "file" else None, v)
        cfg.ecc = shutil.which(v) or v
    else:
        ecc_from = "auto"
        cfg.ecc = detect_ecc() if autodetect else "ecc"
    # pdk
    cfg.pdk_root = path_val("paths", "pdk_root")
    pdk_from = src.get("paths.pdk_root", "")
    if not cfg.pdk_root and autodetect:
        cfg.pdk_root, pdk_from = detect_pdk(cfg.ecc, env)
    # oss-cad-suite & optional open-source external oracle tools
    cfg.oss_cad_bin = path_val("paths", "oss_cad_bin")
    oss_from = src.get("paths.oss_cad_bin", "")
    if not cfg.oss_cad_bin and autodetect:
        cfg.oss_cad_bin, oss_from = detect_oss_bin(env)
    cfg.opensta_bin, _ = bin_val("paths", "opensta_bin", "sta")
    cfg.openroad_bin, _ = bin_val("paths", "openroad_bin", "openroad")
    cfg.klayout_bin, _ = bin_val("paths", "klayout_bin", "klayout")
    cfg.openpdk_root = path_val("paths", "openpdk_root")
    openpdk_from = src.get("paths.openpdk_root", "")
    if not cfg.openpdk_root and autodetect:
        cfg.openpdk_root, openpdk_from = detect_openpdk(env)
    v, _ = pick("paths", "oss_docker_image")
    cfg.oss_docker_image = str(v).strip() if v else None
    # directories
    cfg.designs_dir = path_val("paths", "designs_dir")
    cfg.known_issues = path_val("paths", "known_issues")
    cfg.baseline = path_val("paths", "baseline")
    sd = path_val("paths", "scratch_dir")
    if not sd:
        xdg = env.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
        sd = str(Path(xdg) / "ecc-verify" / "runs")
    cfg.scratch_dir = sd
    # cpus
    n = os.cpu_count() or 1
    low, high = _half_split(n)
    v, _ = pick("run", "cpus")
    cfg.cpus = str(v) if v else high
    v, _ = pick("run", "pin_cpus")
    cfg.pin_cpus = str(v) if v else low
    v, _ = pick("run", "workers")
    try:
        cfg.workers = int(v) if v is not None else 6
    except (TypeError, ValueError) as e:
        raise ConfigError(f"run.workers must be an integer, got {v!r}") from e
    # fault injection
    v, _ = pick("fault_injection", "target")
    cfg.fault_target = str(v) if v else "APU"
    cfg.fault_path = path_val("fault_injection", "path")
    fs = path_val("fault_injection", "scratch")
    cfg.fault_scratch = fs or str(Path(cfg.scratch_dir) / "fi_scratch")
    # dict sections: defaults <- file <- cli
    for attr, sec in (("timeouts", "timeouts"), ("sampling", "sampling")):
        d = dict(getattr(cfg, attr))
        for layer in (raw, clid):
            if isinstance(layer.get(sec), dict):
                d.update(layer[sec])
        setattr(cfg, attr, d)
    tol = dict(DEFAULT_TOLERANCES)
    for layer in (raw, clid):
        cmp_ = layer.get("compare", {})
        if isinstance(cmp_, dict) and isinstance(cmp_.get("tolerances"), dict):
            tol.update(cmp_["tolerances"])
    cfg.tolerances = tol
    cc = {"ignore_metrics": [], "warn_only_metrics": []}
    for layer in (raw, clid):
        cmp_ = layer.get("compare", {})
        if isinstance(cmp_, dict):
            for k in cc:
                if k in cmp_:
                    cc[k] = list(cmp_[k])
    cfg.compare = cc
    for k, dflt in DEFAULT_TIMEOUTS.items():
        try:
            cfg.timeouts[k] = int(cfg.timeouts.get(k, dflt))
        except (TypeError, ValueError) as e:
            raise ConfigError(f"timeouts.{k} must be an integer") from e
    cfg.source = (
        f"file={cfg_path or '-'} ecc={ecc_from} pdk={pdk_from or 'unset'} "
        f"oss-cad={oss_from or 'unset'} openpdk={openpdk_from or 'unset'}"
    )
    return cfg


def describe(cfg: Config) -> str:
    rows = [("ecc", cfg.ecc), ("pdk_root", cfg.pdk_root), ("oss_cad_bin", cfg.oss_cad_bin),
            ("opensta_bin", cfg.opensta_bin), ("openroad_bin", cfg.openroad_bin),
            ("klayout_bin", cfg.klayout_bin), ("openpdk_root", cfg.openpdk_root),
            ("oss_docker_image", cfg.oss_docker_image),
            ("designs_dir", cfg.designs_dir), ("scratch_dir", cfg.scratch_dir), ("cpus (ecc runs)", cfg.cpus),
            ("pin_cpus (tools)", cfg.pin_cpus), ("workers", cfg.workers), ("fault target", cfg.fault_target),
            ("fault script", cfg.fault_path or "(vendored)"), ("config file", cfg.config_file or "(none)")]
    return "\n".join(f"{k:18s} {v}" for k, v in rows)


EXAMPLE_TOML = '''\
# ecc-verify.toml : every key is optional. Precedence: CLI flag > ECC_VERIFY_* env var > this file > auto-detection.
# Relative paths are resolved against the directory of this file.

[ecc]
# bin = "ecc"                    # default: `ecc` on PATH, else ~/.local/bin/ecc            (env ECC_VERIFY_ECC)

[paths]
# pdk_root         = "/path/to/icsprout55-pdk"  # official ICS55 PDK root; else `ecc pdk show`  (ECC_VERIFY_PDK_ROOT)
# oss_cad_bin      = "/opt/oss-cad-suite/bin"   # yosys, iverilog, vvp. default: yosys on PATH  (ECC_VERIFY_OSS_CAD_BIN)
# opensta_bin      = "sta"                      # OpenSTA binary for L3.oss.opensta_timing      (ECC_VERIFY_OPENSTA_BIN)
# openroad_bin     = "openroad"                 # OpenROAD binary for L3.oss.openroad_*         (ECC_VERIFY_OPENROAD_BIN)
# klayout_bin      = "klayout"                  # KLayout CLI for L3.oss.klayout_{drc,ant,lvs}  (ECC_VERIFY_KLAYOUT_BIN)
# openpdk_root     = "/path/to/icsprout55-openpdk"  # optional unofficial community KLayout/OpenRCX decks (ECC_VERIFY_OPENPDK_ROOT)
# oss_docker_image = "hpretl/iic-osic-tools:latest" # optional container fallback for sta/openroad/klayout (ECC_VERIFY_OSS_DOCKER_IMAGE)
# designs_dir      = "/path/to/ecc-ci-designs"  # pristine design sources for L4/L5 slow checks (ECC_VERIFY_DESIGNS_DIR)
# scratch_dir      = "~/.cache/ecc-verify/runs" # scratch copies, Yosys work dirs, slow cases   (ECC_VERIFY_SCRATCH_DIR)
# known_issues     = "known_issues.toml"        # default: ./known_issues.toml, else the packaged one
# baseline         = "../ecc-verify-baselines"  # compared against by `run` / `compare` when no --baseline is given

[run]
# cpus     = "8-15"   # taskset list for real `ecc run` calls (L4/L5). default: upper half of the machine
# pin_cpus = "0-7"    # taskset list for pinned tools (Yosys miter) in the static run. default: lower half
# workers  = 6        # design-check worker processes

[fault_injection]
# target  = "APU"     # design whose workspace the fault-injection suite mutates (copies only)
# path    = ""        # alternative fault_inject.py; default is the vendored ecc_verify/vendor/fault_inject.py
# scratch = ""        # default <scratch_dir>/fi_scratch

[timeouts]            # seconds
# cli_help = 60
# cli_json = 120
# gds_probe = 600
# yosys_ports = 300
# yosys_miter = 900
# miter_solver = 300  # `sat -timeout`; expiry makes L3.fn.eqy_rtl_vs_final SKIP
# sim = 600
# full_flow = 1500
# step = 600
# opensta = 600       # L3.oss.opensta_timing
# openroad = 600      # L3.oss.openroad_antenna / L3.oss.openroad_rcx
# klayout_drc = 1200  # L3.oss.klayout_drc / L3.oss.klayout_antenna
# klayout_lvs = 1200  # L3.oss.klayout_lvs

[sampling]
# rt_sample_min_cells = 10000   # L3.rt.connectivity checks every Nth net at/above this component count
# rt_sample_stride = 3
# fn_max_cells = 2000           # L3.fn checks only run below this std-cell count
# fn_bmc_cycles = 25
# fn_sim_cycles = 1000
# fn_settle_cycles = 24

[compare]
# ignore_metrics    = []        # glob patterns of metric names never compared, e.g. ["runtime.*"]
# warn_only_metrics = []        # drift is reported but never fails the gate

[compare.tolerances]            # defaults shown
# ws_abs_ns = 0.05      # setup/hold WS drift allowed = max(ws_abs_ns, ws_rel_t * clock period)
# ws_rel_t = 0.05
# tns_abs_ns = 0.05
# tns_rel = 0.10
# nvp_increase = 0
# hpwl_rel = 0.02
# route_wl_rel = 0.02
# via_rel = 0.02
# area_rel = 0.01
# cells_rel = 0.01
# util_abs = 0.01
# power_rel = 0.03
# drv_increase = 0      # max_transition / max_cap / max_fanout / DRC / LVS counts: no increase
# runtime_rel = 0.25
# runtime_min_s = 5.0   # ignore runtime drift below this absolute change
# memory_rel = 0.25
# memory_min_mb = 50.0
'''
