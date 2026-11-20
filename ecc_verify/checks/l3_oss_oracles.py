"""L3 optional open-source cross-tool oracles (OpenSTA, OpenROAD/OpenRCX, KLayout).

Every check here delegates to an external open-source EDA tool configured via
``ecc-verify.toml``, ``ECC_VERIFY_*``, or CLI flags (supporting both local executables
and containerized toolchains such as ``hpretl/iic-osic-tools:latest`` via
``paths.oss_docker_image``).

The official ICS55 PDK (``paths.pdk_root`` / ``pv/DRC/``) remains the sole
authoritative foundry reference. Optional ``L3.oss.openroad_rcx`` and
``L3.oss.klayout_*`` checks additionally accept an optional, unofficial community
KLayout/OpenRCX deck directory (``paths.openpdk_root``); when a binary or deck is
unavailable on the host, the check returns ``Result.skip(...)`` with an explicit
reason instead of passing vacuously.
"""
from __future__ import annotations

import gzip
import json
import os
import re
import shutil
import statistics
import tempfile
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path

from ecc_verify import config, parsers as P
from ecc_verify.model import Layer, Result
from ecc_verify.registry import check
from ecc_verify.util import run

_DOCKER_PATH = "/foss/tools/bin:/foss/tools/klayout:/usr/local/bin:/usr/bin:/bin"
_STA_SDC_SHIM = """if {[info commands remove_from_collection] eq ""} {
  proc remove_from_collection {col rm} {
    set rlist {}
    foreach r $rm { lappend rlist [get_full_name $r] }
    set res {}
    foreach c $col {
      if {[lsearch -exact $rlist [get_full_name $c]] < 0} {
        lappend res $c
      }
    }
    return $res
  }
}"""


# --------------------------------------------------------------------------- tool, docker & staging helpers
def _resolve_bin(configured: str | None) -> str | None:
    """Return an executable path when configured and invokable, else None."""
    if not configured:
        return None
    p = Path(configured)
    if p.is_file() and os.access(p, os.X_OK):
        return str(p)
    return shutil.which(configured)


def _tool_runner(cfg: config.Config, configured_bin: str | None, tool_name: str) -> tuple[str | None, str | None]:
    """Select ``('local', exe)``, ``('docker', image)``, or ``(None, None)``."""
    exe = _resolve_bin(configured_bin)
    if exe is not None:
        return "local", exe
    if cfg.oss_docker_image and shutil.which("docker"):
        return "docker", cfg.oss_docker_image
    return None, None


def _exec_tool(cfg: config.Config, mode: str, runner: str, tool_name: str, args: list[str],
               cwd: Path, timeout: int) -> tuple[int, str, str]:
    """Run an open-source tool locally or inside the configured Docker image."""
    if mode == "local":
        return run([runner, *args], cwd=str(cwd), timeout=timeout, pin=True)
    home = str(Path.home().resolve())
    cmd = [
        "docker", "run", "--rm", "--entrypoint", "",
        "--user", f"{os.getuid()}:{os.getgid()}",
        "-e", f"PATH={_DOCKER_PATH}",
        "-v", f"{home}:{home}",
        "-w", str(cwd.resolve()),
        runner, tool_name, *args,
    ]
    return run(cmd, cwd=str(cwd), timeout=timeout, pin=False)


def _in_home(path: Path) -> bool:
    try:
        return path.resolve().is_relative_to(Path.home().resolve())
    except OSError:
        return False


def _work_tmpdir(cfg: config.Config, is_docker: bool, prefix: str) -> tempfile.TemporaryDirectory:
    if cfg.scratch_dir and (not is_docker or _in_home(cfg.runs_dir)):
        base = cfg.runs_dir
        base.mkdir(parents=True, exist_ok=True)
        return tempfile.TemporaryDirectory(prefix=prefix, dir=str(base))
    if is_docker:
        base = Path.home() / ".cache/ecc-verify/docker_tmp"
        base.mkdir(parents=True, exist_ok=True)
        return tempfile.TemporaryDirectory(prefix=prefix, dir=str(base))
    return tempfile.TemporaryDirectory(prefix=prefix)


def _stage_file(path: Path, tmpdir: Path, force_stage: bool = False) -> Path:
    """Decompress ``.gz`` files into ``tmpdir`` and copy external paths when Docker mode is active."""
    tag = f"{zlib.crc32(str(path).encode()):08x}"
    if path.suffix == ".gz":
        dst = tmpdir / f"{tag}_{path.stem}"
        dst.write_bytes(gzip.decompress(path.read_bytes()))
        return dst
    if force_stage and not _in_home(path):
        dst = tmpdir / f"{tag}_{path.name}"
        shutil.copyfile(path.resolve(), dst)
        return dst
    return path.resolve()


def _openpdk_file(cfg: config.Config, rel: str) -> Path | None:
    """Resolve an optional unofficial community KLayout/OpenRCX file under ``paths.openpdk_root``."""
    root = cfg.openpdk
    if root is None:
        return None
    for cand in (root / "icsprout55" / rel, root / rel):
        if cand.is_file():
            return cand
    return None


def _tcl_quote(path: Path | str) -> str:
    s = str(path).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


def _antenna_step_json(ws) -> Path | None:
    """Locate ECC's antenna-check step JSON across camelCase and snake_case layouts."""
    step_candidates: list[Path] = []
    for step_pre in ("antennaCheck", "antenna_check"):
        d = ws.step_dir(step_pre)
        if d is not None:
            step_candidates.append(d)
    for pat in ("antennaCheck_*", "antenna_check_*"):
        step_candidates.extend(sorted(p for p in ws.path.glob(pat) if p.is_dir()))
    seen: set[Path] = set()
    for d in step_candidates:
        if d in seen:
            continue
        seen.add(d)
        for fname in ("antennaCheck.step.json", "antenna_check.step.json"):
            cand = d / "feature" / fname
            if cand.is_file():
                return cand
    return None


def _drc_count(ws) -> int | None:
    d = ws.step_dir("drc")
    if d is None:
        return None
    p = d / "feature/drc.step.json"
    if p.is_file():
        try:
            return int(json.loads(p.read_text()).get("drc", {}).get("number", 0))
        except (OSError, ValueError, TypeError):
            return None
    return None


def _lvs_diff_count(ws) -> int | None:
    d = ws.step_dir("lvs")
    if d is None:
        return None
    p = d / "feature/lvs.step.json"
    if p.is_file():
        try:
            js = json.loads(p.read_text())
            return sum(abs(int(e.get("difference", 0))) for e in js.get("entity", []))
        except (OSError, ValueError, TypeError):
            return None
    return None


def _lefs_and_def(ws) -> tuple[list[Path], Path] | tuple[None, str]:
    try:
        db = ws._db()["INPUT"]
    except (OSError, KeyError, json.JSONDecodeError) as e:
        return None, f"cannot read db_ecc.json: {e}"
    tech = db.get("tech_lef_path")
    cells = list(db.get("lef_paths") or [])
    if not tech:
        return None, "db_ecc.json missing tech_lef_path"
    lefs = [Path(tech), *(Path(p) for p in cells)]
    missing = [str(p) for p in lefs if not p.is_file()]
    if missing:
        return None, f"missing LEF file(s) on disk: {missing[0]}"
    def_file = (
        ws.output_file("route", ".def.gz")
        or ws.output_file("route", ".def")
        or ws.output_file("filler", ".def.gz")
        or ws.output_file("filler", ".def")
    )
    if def_file is None or not def_file.is_file():
        return None, "no routed DEF available"
    return lefs, def_file


def _select_sta_corner(ws) -> dict | str:
    """Pick a matched (corner, libs, sdc, spef, netlist, qor_summary) tuple, or return skip reason."""
    corners = ws.sta_corners()
    if not corners:
        return "no ECC STA corner summaries"
    spefs = ws.spef_files()
    if not spefs:
        return "no SPEF files"
    sta_cfg_path = ws.path / "config/sta_ecc.json"
    lib_by_corner: dict[str, list[Path]] = {}
    if sta_cfg_path.is_file():
        try:
            cfg_sta = json.loads(sta_cfg_path.read_text())
            for entry in cfg_sta.get("liberty", []):
                cname = entry.get("corner")
                temp = entry.get("temperature")
                t_tag = f"m{abs(int(temp))}" if isinstance(temp, int) and temp < 0 else str(temp)
                if cname and entry.get("path"):
                    lib_by_corner[f"{cname}_{t_tag}"] = [Path(p) for p in entry["path"]]
        except (OSError, ValueError, TypeError):
            pass

    netlist = (
        ws.output_file("filler", ".v.gz")
        or ws.output_file("filler", ".v")
        or ws.output_file("route", ".v.gz")
        or ws.output_file("route", ".v")
    )
    if netlist is None or not netlist.is_file():
        return "no post-route/filler Verilog netlist"

    preferred = ["MAX_125/Cworst", "MAX_125/RCworst", "TYP_25/TYPICAL"]
    ordered_keys = [k for k in preferred if k in corners] + [k for k in sorted(corners) if k not in preferred]

    fallback_sdcs = sorted((ws.path / "origin").glob("*.sdc")) + sorted((ws.path / "origin/post_CTS").glob("*.tcl"))
    db_libs = []
    try:
        db_libs = [Path(p) for p in (ws._db()["INPUT"].get("lib_path") or [])]
    except Exception:  # noqa: BLE001
        pass

    last_reason = "no matched STA corner with Liberty/SDC/SPEF on disk"
    for key in ordered_keys:
        if "/" not in key or "_" not in key.split("/", 1)[0]:
            continue
        ctag, rc = key.split("/", 1)
        temp_part = ctag.split("_", 1)[1]
        spef_key = f"{rc}_{temp_part}C"
        spef_path = spefs.get(spef_key)
        if not spef_path or not spef_path.is_file():
            last_reason = f"no matching SPEF for {key} (expected {spef_key})"
            continue
        libs = lib_by_corner.get(ctag) or db_libs
        if not libs or any(not p.is_file() for p in libs):
            last_reason = f"Liberty file(s) missing on disk for {key}"
            continue
        sdc_cand = ws.path / f"origin/post_CTS/{ctag}_{rc}.tcl"
        sdc_path = sdc_cand if sdc_cand.is_file() else (fallback_sdcs[0] if fallback_sdcs else None)
        if sdc_path is None or not sdc_path.is_file():
            last_reason = f"no SDC constraint file for {key}"
            continue
        return {
            "corner_key": key,
            "spef_key": spef_key,
            "libs": libs,
            "sdc": sdc_path,
            "spef": spef_path,
            "netlist": netlist,
            "ecc_summary": corners[key].get("summary", {}),
        }
    return last_reason


def _rcx_rules_and_spef(ws, cfg: config.Config) -> tuple[str, Path, Path] | str:
    spefs = ws.spef_files()
    if not spefs:
        return "no ECC SPEF files in RCX output"
    candidates = [
        ("TYPICAL_25C", "libs.tech/librelane/rcx/ics55_typ.rules"),
        ("Cworst_125C", "libs.tech/librelane/rcx/ics55_rcworst.rules"),
        ("RCworst_125C", "libs.tech/librelane/rcx/ics55_rcworst.rules"),
        ("Cbest_m40C", "libs.tech/librelane/rcx/ics55_rcbest.rules"),
        ("RCbest_m40C", "libs.tech/librelane/rcx/ics55_rcbest.rules"),
    ]
    for corner, rel_rule in candidates:
        sp = spefs.get(corner)
        rules = _openpdk_file(cfg, rel_rule)
        if sp and sp.is_file() and rules is not None:
            return corner, sp, rules
    if cfg.openpdk is None:
        return "paths.openpdk_root not configured (optional unofficial community OpenRCX rules)"
    return "no matching unofficial OpenRCX rules / ECC SPEF corner found"


def _find_schematic(ws) -> Path | None:
    exts = (".cdl", ".spice", ".sp")
    search_dirs: list[Path] = []
    for step_name, subs in (("Harden", ("output",)), ("lvs", ("output", "data/lvs"))):
        d = ws.step_dir(step_name)
        if d is not None:
            for sub in subs:
                cand_dir = d / sub
                if cand_dir.is_dir():
                    search_dirs.append(cand_dir)
    for sd in search_dirs:
        for f in sorted(sd.rglob("*")):
            if f.is_file() and f.suffix.lower() in exts:
                return f
    return None


# --------------------------------------------------------------------------- parsers & comparators
_NUM_RE = r"([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)"


def parse_opensta_output(text: str) -> dict[str, float | None]:
    """Parse OpenSTA marker output (or fallback ``wns``/``tns`` lines)."""
    out: dict[str, float | None] = {
        "setup_wns": None,
        "setup_tns": None,
        "hold_wns": None,
        "hold_tns": None,
    }
    for key, tag in (
        ("setup_wns", "ECC_VERIFY_SETUP_WNS"),
        ("setup_tns", "ECC_VERIFY_SETUP_TNS"),
        ("hold_wns", "ECC_VERIFY_HOLD_WNS"),
        ("hold_tns", "ECC_VERIFY_HOLD_TNS"),
    ):
        m = re.search(rf"^{tag}\s+{_NUM_RE}\b", text, re.M)
        if m:
            out[key] = float(m.group(1))
    if out["setup_wns"] is None:
        m = re.search(rf"^\s*wns(?:\s+max)?\s+{_NUM_RE}\b", text, re.M | re.I)
        if m:
            out["setup_wns"] = float(m.group(1))
    if out["setup_tns"] is None:
        m = re.search(rf"^\s*tns(?:\s+max)?\s+{_NUM_RE}\b", text, re.M | re.I)
        if m:
            out["setup_tns"] = float(m.group(1))
    return out


def parse_openroad_antenna(text: str) -> dict[str, int | None]:
    """Parse OpenROAD ``check_antennas -verbose`` net and pin violation totals."""
    pin_m = re.search(r"Found\s+(\d+)\s+pin\s+violations", text, re.I) or re.search(
        r"pin\s+violations?\s*[:=]\s*(\d+)", text, re.I
    )
    net_m = re.search(r"Found\s+(\d+)\s+net\s+violations", text, re.I) or re.search(
        r"net\s+violations?\s*[:=]\s*(\d+)", text, re.I
    )
    return {
        "pin_violations": int(pin_m.group(1)) if pin_m else None,
        "net_violations": int(net_m.group(1)) if net_m else None,
    }


def parse_klayout_lyrdb(path: Path) -> dict:
    """Parse a KLayout ``.lyrdb`` XML database and count items per rule category."""
    tree = ET.parse(path)
    root = tree.getroot()
    items = root.findall("./items/item")
    by_category: dict[str, int] = {}
    for item in items:
        cat_el = item.find("category")
        cat = (cat_el.text or "<unknown>").strip().strip("'\"") if cat_el is not None else "<unknown>"
        by_category[cat] = by_category.get(cat, 0) + 1
    top = sorted(by_category.items(), key=lambda kv: (-kv[1], kv[0]))[:10]
    return {"total": len(items), "by_category": by_category, "top_categories": top}


def parse_klayout_lvs_output(stdout: str, stderr: str = "") -> dict:
    """Extract match verdict and summary info from KLayout LVS deck output."""
    text = f"{stdout}\n{stderr}"
    matched: bool | None = None
    if re.search(r"Congratulations!\s*Netlists match", text, re.I):
        matched = True
    elif re.search(r"ERROR\s*:\s*Netlists don't match|Netlists don't match", text, re.I):
        matched = False
    summaries = [ln.strip() for ln in text.splitlines() if "SVRF LVS:" in ln and ("nets" in ln or "devices" in ln)]
    return {"matched": matched, "summaries": summaries}


def compare_spef_caps(ecc_spef: Path, or_spef: Path) -> dict:
    """Compare per-net total capacitance (fF) between ECC and OpenRCX SPEF files."""
    e = P.parse_spef(ecc_spef)
    o = P.parse_spef(or_spef)
    common = [n for n in e.nets if n in o.nets]
    diffs = []
    worst: list[tuple[str, float, float, float]] = []
    for n in common:
        ec, oc = e.nets[n], o.nets[n]
        denom = max(abs(ec), abs(oc), 1e-6)
        rel = abs(oc - ec) / denom
        diffs.append(rel)
        worst.append((n, round(ec, 4), round(oc, 4), round(rel, 4)))
    if not diffs:
        return {"shared_nets": 0, "ecc_nets": len(e.nets), "or_nets": len(o.nets),
                "median_rel_diff": None, "p95_rel_diff": None, "max_rel_diff": None, "worst": []}
    s = sorted(diffs)
    p95_idx = min(len(s) - 1, int(0.95 * (len(s) - 1)))
    worst.sort(key=lambda t: -t[3])
    return {
        "shared_nets": len(common),
        "ecc_nets": len(e.nets),
        "or_nets": len(o.nets),
        "median_rel_diff": statistics.median(s),
        "p95_rel_diff": s[p95_idx],
        "max_rel_diff": s[-1],
        "worst": worst[:5],
    }


def _write_opensta_tcl(tcl: Path, libs: list[Path], netlist: Path, top_name: str, sdc: Path, spef: Path) -> None:
    lines = [_STA_SDC_SHIM]
    lines += [f"read_liberty {_tcl_quote(p)}" for p in libs]
    lines += [
        f"read_verilog {_tcl_quote(netlist)}",
        f"link_design {_tcl_quote(top_name)}",
        f"read_sdc {_tcl_quote(sdc)}",
        f"read_spef {_tcl_quote(spef)}",
        'puts [format "ECC_VERIFY_SETUP_WNS %s" [worst_slack -max]]',
        'puts [format "ECC_VERIFY_SETUP_TNS %s" [total_negative_slack -max]]',
        'puts [format "ECC_VERIFY_HOLD_WNS %s" [worst_slack -min]]',
        'puts [format "ECC_VERIFY_HOLD_TNS %s" [total_negative_slack -min]]',
        "exit 0",
    ]
    tcl.write_text("\n".join(lines) + "\n")


def _timing_divergences(ecc_sum: dict, parsed: dict[str, float | None], tol: float) -> list[str]:
    probs: list[str] = []
    ecc_setup = ecc_sum.get("setup", {}).get("wns")
    ecc_hold = ecc_sum.get("hold", {}).get("wns")
    oss_setup = parsed.get("setup_wns")
    oss_hold = parsed.get("hold_wns")
    if ecc_setup is not None and oss_setup is not None:
        if ecc_setup >= 0 and oss_setup < -0.05:
            probs.append(f"setup sign flip: ECC WNS={ecc_setup:.3f} ns >= 0 vs OpenSTA {oss_setup:.3f} ns")
        elif abs(oss_setup - ecc_setup) > tol:
            probs.append(f"setup WNS divergence: OpenSTA {oss_setup:.3f} vs ECC {ecc_setup:.3f} ns (> {tol:.3f} ns)")
    if ecc_hold is not None and oss_hold is not None:
        if ecc_hold >= 0 and oss_hold < -0.05:
            probs.append(f"hold sign flip: ECC WNS={ecc_hold:.3f} ns >= 0 vs OpenSTA {oss_hold:.3f} ns")
        elif abs(oss_hold - ecc_hold) > tol:
            probs.append(f"hold WNS divergence: OpenSTA {oss_hold:.3f} vs ECC {ecc_hold:.3f} ns (> {tol:.3f} ns)")
    return probs


# --------------------------------------------------------------------------- registered checks
@check("L3.oss.opensta_timing", Layer.L3, steps=("sta",), category="timing", severity="major",
       title="OpenSTA independent NLDM/Elmore STA matches ECC iSTA within tolerance")
def opensta_timing(ws):
    cfg = config.current()
    mode, runner = _tool_runner(cfg, cfg.opensta_bin, "sta")
    if mode is None:
        return Result.skip("OpenSTA not configured (set paths.opensta_bin, `sta` on PATH, or paths.oss_docker_image)")
    sel = _select_sta_corner(ws)
    if isinstance(sel, str):
        return Result.skip(sel)

    is_docker = mode == "docker"
    try:
        tclk = 1000.0 / float(ws.ecc_toml["design"]["frequency_mhz"])
    except Exception:  # noqa: BLE001
        tclk = 10.0
    tol = max(0.10, 0.05 * tclk)
    ecc_sum = sel["ecc_summary"]

    with _work_tmpdir(cfg, is_docker, "ecc_verify_sta_") as td:
        tmp = Path(td)
        libs = [_stage_file(p, tmp, force_stage=is_docker) for p in sel["libs"]]
        netlist = _stage_file(sel["netlist"], tmp, force_stage=is_docker)
        sdc = _stage_file(sel["sdc"], tmp, force_stage=is_docker)
        spef = _stage_file(sel["spef"], tmp, force_stage=is_docker)
        tcl = tmp / "run_sta.tcl"
        _write_opensta_tcl(tcl, libs, netlist, ws.top_name, sdc, spef)
        rc, out, err = _exec_tool(cfg, mode, runner, "sta", ["-exit", str(tcl)], tmp, cfg.timeout("opensta"))

        if rc != 0:
            raw_tail = (err or out).strip().splitlines()[-1] if (err or out).strip() else f"rc={rc}"
            combined = f"{out}\n{err}"
            if "syntax error" in combined.lower():
                spef_text = spef.read_text(errors="replace")
                if re.search(r"(?<!\\)\$", spef_text):
                    escaped_spef = tmp / f"{spef.stem}.escaped.spef"
                    escaped_spef.write_text(re.sub(r"(?<!\\)\$", r"\\$", spef_text))
                    _write_opensta_tcl(tcl, libs, netlist, ws.top_name, sdc, escaped_spef)
                    rc2, out2, err2 = _exec_tool(
                        cfg, mode, runner, "sta", ["-exit", str(tcl)], tmp, cfg.timeout("opensta")
                    )
                    sanitized = parse_opensta_output(f"{out2}\n{err2}") if rc2 == 0 else {}
                    tprobs = _timing_divergences(ecc_sum, sanitized, tol) if sanitized.get("setup_wns") is not None else []
                    hold_val = sanitized.get("hold_wns")
                    hold_txt = f"{hold_val:.3f}" if isinstance(hold_val, float) else str(hold_val)
                    san_summary = (
                        f"after escaping '$': OpenSTA setup={sanitized.get('setup_wns'):.3f} ns "
                        f"vs ECC {ecc_sum.get('setup', {}).get('wns')}, "
                        f"hold={hold_txt} ns vs ECC {ecc_sum.get('hold', {}).get('wns')}"
                        if sanitized.get("setup_wns") is not None
                        else f"escaped retry rc={rc2}"
                    )
                    extra = f"; {'; '.join(tprobs)}" if tprobs else ""
                    return Result.fail(
                        f"IEEE 1481 SPEF syntax defect in {sel['spef'].name} (unescaped '$' in identifier): "
                        f"{raw_tail} ({san_summary}{extra})",
                        measured=sanitized or raw_tail,
                        expected=ecc_sum,
                        tolerance=f"{tol:.3f} ns",
                        evidence=[ws.rel(sel["spef"])],
                        details={
                            "corner": sel["corner_key"],
                            "spef_syntax_defect": True,
                            "raw_error": raw_tail,
                            "ecc": ecc_sum,
                            "opensta": sanitized,
                        },
                    )
            return Result.fail(f"OpenSTA failed (rc={rc}) on {sel['corner_key']}: {(err or out)[-300:]}",
                               measured=rc, expected=0, evidence=[ws.rel(sel["spef"])])

    parsed = parse_opensta_output(out + "\n" + err)
    oss_setup = parsed["setup_wns"]
    if oss_setup is None:
        return Result.fail("could not parse OpenSTA worst_slack output",
                           evidence=[ws.rel(sel["spef"])], details={"stdout_tail": out[-400:]})

    probs = _timing_divergences(ecc_sum, parsed, tol)
    details = {"corner": sel["corner_key"], "ecc": ecc_sum, "opensta": parsed}
    if probs:
        return Result.fail("; ".join(probs), measured=parsed, expected=ecc_sum,
                           tolerance=f"{tol:.3f} ns", evidence=[ws.rel(sel["spef"])], details=details)
    return Result.ok(f"{sel['corner_key']}: OpenSTA setup={oss_setup:.3f} ns vs ECC {ecc_sum.get('setup', {}).get('wns')}",
                     measured=parsed, expected=ecc_sum, tolerance=f"{tol:.3f} ns", details=details)


@check("L3.oss.openroad_antenna", Layer.L3, steps=("antennaCheck",), category="physics", severity="major",
       title="OpenROAD check_antennas agrees with ECC antennaCheck report")
def openroad_antenna(ws):
    cfg = config.current()
    mode, runner = _tool_runner(cfg, cfg.openroad_bin, "openroad")
    if mode is None:
        return Result.skip(
            "OpenROAD not configured (set paths.openroad_bin, `openroad` on PATH, or paths.oss_docker_image)"
        )
    ant_json = _antenna_step_json(ws)
    if ant_json is None:
        return Result.skip("no antennaCheck step JSON in workspace")
    lefs, def_or_msg = _lefs_and_def(ws)
    if lefs is None:
        return Result.skip(def_or_msg)
    ecc_ant = json.loads(ant_json.read_text()).get("antenna_check", {})
    ecc_viols = int(ecc_ant.get("violation_count", 0))

    is_docker = mode == "docker"
    with _work_tmpdir(cfg, is_docker, "ecc_verify_orant_") as td:
        tmp = Path(td)
        staged_lefs = [_stage_file(p, tmp, force_stage=is_docker) for p in lefs]
        def_path = _stage_file(def_or_msg, tmp, force_stage=is_docker)
        tcl = tmp / "run_ant.tcl"
        lines = [f"read_lef {_tcl_quote(p)}" for p in staged_lefs] + [
            f"read_def {_tcl_quote(def_path)}",
            "check_antennas -verbose",
            "exit 0",
        ]
        tcl.write_text("\n".join(lines) + "\n")
        rc, out, err = _exec_tool(cfg, mode, runner, "openroad", ["-exit", str(tcl)], tmp, cfg.timeout("openroad"))

    if rc != 0:
        return Result.fail(f"OpenROAD check_antennas failed (rc={rc}): {(err or out)[-300:]}",
                           measured=rc, expected=0, evidence=[ws.rel(def_or_msg)])

    parsed = parse_openroad_antenna(out + "\n" + err)
    if parsed["net_violations"] is None and parsed["pin_violations"] is None:
        return Result.fail("could not parse OpenROAD check_antennas violation counts",
                           evidence=[ws.rel(ant_json)], details={"stdout_tail": out[-400:]})

    or_nets = parsed["net_violations"] or 0
    or_pins = parsed["pin_violations"] or 0
    details = {"ecc_violation_count": ecc_viols, "openroad": parsed}
    if ecc_viols == 0 and (or_nets > 0 or or_pins > 0):
        return Result.fail(f"OpenROAD found {or_nets} net / {or_pins} pin antenna violations while ECC reported 0",
                           measured=parsed, expected=0, evidence=[ws.rel(ant_json)], details=details)
    if ecc_viols > 0 and ecc_viols not in (or_nets, or_pins):
        return Result.fail(f"antenna count mismatch: ECC={ecc_viols} vs OpenROAD {or_nets} nets / {or_pins} pins",
                           measured=parsed, expected=ecc_viols, evidence=[ws.rel(ant_json)], details=details)
    return Result.ok(f"OpenROAD {or_nets} nets / {or_pins} pins match ECC ({ecc_viols} violations)",
                     measured=parsed, expected=ecc_viols, details=details)


@check("L3.oss.openroad_rcx", Layer.L3, steps=("RCX",), category="physics", severity="major",
       title="OpenRCX extraction (optional unofficial rules) matches ECC SPEF total capacitance")
def openroad_rcx(ws):
    cfg = config.current()
    mode, runner = _tool_runner(cfg, cfg.openroad_bin, "openroad")
    if mode is None:
        return Result.skip(
            "OpenROAD not configured (set paths.openroad_bin, `openroad` on PATH, or paths.oss_docker_image)"
        )
    sel = _rcx_rules_and_spef(ws, cfg)
    if isinstance(sel, str):
        return Result.skip(sel)
    corner, ecc_spef, rules_file = sel
    lefs, def_or_msg = _lefs_and_def(ws)
    if lefs is None:
        return Result.skip(def_or_msg)

    is_docker = mode == "docker"
    with _work_tmpdir(cfg, is_docker, "ecc_verify_rcx_") as td:
        tmp = Path(td)
        staged_lefs = [_stage_file(p, tmp, force_stage=is_docker) for p in lefs]
        def_path = _stage_file(def_or_msg, tmp, force_stage=is_docker)
        staged_rules = _stage_file(rules_file, tmp, force_stage=is_docker)
        out_spef = tmp / f"{ws.top_name}_openrcx.spef"
        tcl = tmp / "run_rcx.tcl"
        lines = [f"read_lef {_tcl_quote(p)}" for p in staged_lefs] + [
            f"read_def {_tcl_quote(def_path)}",
            "define_process_corner -ext_model_index 0 X",
            f"extract_parasitics -ext_model_file {_tcl_quote(staged_rules)} -lef_res",
            f"write_spef {_tcl_quote(out_spef)}",
            "exit 0",
        ]
        tcl.write_text("\n".join(lines) + "\n")
        rc, out, err = _exec_tool(cfg, mode, runner, "openroad", ["-exit", str(tcl)], tmp, cfg.timeout("openroad"))
        if rc != 0 or not out_spef.is_file():
            return Result.fail(f"OpenRCX failed (rc={rc}) for {corner}: {(err or out)[-300:]}",
                               measured=rc, expected=0, evidence=[ws.rel(ecc_spef)])
        cmp_ = compare_spef_caps(ecc_spef, out_spef)

    if cmp_["shared_nets"] == 0:
        return Result.fail(f"OpenRCX produced no matching nets for {corner}",
                           measured=cmp_, expected="shared_nets > 0", evidence=[ws.rel(ecc_spef)])
    med = cmp_["median_rel_diff"]
    if med is not None and med > 0.25:
        return Result.fail(
            f"{corner}: median SPEF capacitance diff {med:.1%} exceeds 25% across {cmp_['shared_nets']} nets",
            measured=round(med, 4), expected="<= 0.25", tolerance="25% median",
            evidence=[ws.rel(ecc_spef)], details=cmp_,
        )
    return Result.ok(f"{corner}: {cmp_['shared_nets']} nets, median cap diff {med:.2%}",
                     measured=round(med, 4), tolerance="25% median", details=cmp_)


@check("L3.oss.klayout_drc", Layer.L3, steps=("drc", "Harden"), category="physics", severity="critical",
       title="KLayout batch DRC (optional unofficial deck) reports clean merged GDS")
def klayout_drc(ws):
    cfg = config.current()
    mode, runner = _tool_runner(cfg, cfg.klayout_bin, "klayout")
    if mode is None:
        return Result.skip("KLayout CLI not configured (set paths.klayout_bin, `klayout` on PATH, or paths.oss_docker_image)")
    deck = _openpdk_file(cfg, "libs.tech/klayout/tech/ics55.drc")
    if deck is None:
        return Result.skip("optional unofficial ics55.drc not configured (set paths.openpdk_root)")
    gds = ws.output_file("Harden", ".gds") or ws.output_file("filler", ".gds")
    if gds is None:
        return Result.skip("no GDS output for DRC oracle")

    is_docker = mode == "docker"
    with _work_tmpdir(cfg, is_docker, "ecc_verify_kdrc_") as td:
        tmp = Path(td)
        staged_deck = _stage_file(deck, tmp, force_stage=is_docker)
        staged_gds = _stage_file(gds, tmp, force_stage=is_docker)
        lyrdb = tmp / "drc.lyrdb"
        args = [
            "-b", "-r", str(staged_deck),
            "-rd", f"input={staged_gds}",
            "-rd", f"topcell={ws.top_name}",
            "-rd", f"report={lyrdb}",
            "-rd", "densityRules=False",
            "-rd", "flat=1",
        ]
        rc, out, err = _exec_tool(cfg, mode, runner, "klayout", args, tmp, cfg.timeout("klayout_drc"))
        if rc != 0 or not lyrdb.is_file():
            return Result.fail(f"KLayout DRC failed (rc={rc}): {(err or out)[-300:]}",
                               measured=rc, expected=0, evidence=[ws.rel(gds)])
        parsed = parse_klayout_lyrdb(lyrdb)

    ecc_drc = _drc_count(ws)
    details = {"ecc_drc_number": ecc_drc, **parsed}
    if parsed["total"] > 0:
        return Result.fail(
            f"KLayout DRC found {parsed['total']} violations (top: {parsed['top_categories'][:3]}; ECC iDRC={ecc_drc})",
            measured=parsed["total"], expected=0, evidence=[ws.rel(gds)], details=details,
        )
    return Result.ok(f"0 KLayout DRC violations (ECC iDRC={ecc_drc})", measured=0, expected=0, details=details)


@check("L3.oss.klayout_antenna", Layer.L3, steps=("antennaCheck", "Harden"), category="physics", severity="major",
       title="KLayout batch antenna DRC (optional unofficial deck) finds 0 GDS violations")
def klayout_antenna(ws):
    cfg = config.current()
    mode, runner = _tool_runner(cfg, cfg.klayout_bin, "klayout")
    if mode is None:
        return Result.skip("KLayout CLI not configured (set paths.klayout_bin, `klayout` on PATH, or paths.oss_docker_image)")
    deck = _openpdk_file(cfg, "libs.tech/klayout/tech/ics55_antenna.drc")
    if deck is None:
        return Result.skip("optional unofficial ics55_antenna.drc not configured (set paths.openpdk_root)")
    gds = ws.output_file("Harden", ".gds") or ws.output_file("filler", ".gds")
    if gds is None:
        return Result.skip("no GDS output for antenna DRC oracle")

    is_docker = mode == "docker"
    with _work_tmpdir(cfg, is_docker, "ecc_verify_kant_") as td:
        tmp = Path(td)
        staged_deck = _stage_file(deck, tmp, force_stage=is_docker)
        staged_gds = _stage_file(gds, tmp, force_stage=is_docker)
        lyrdb = tmp / "antenna.lyrdb"
        args = [
            "-b", "-r", str(staged_deck),
            "-rd", f"input={staged_gds}",
            "-rd", f"topcell={ws.top_name}",
            "-rd", f"report={lyrdb}",
            "-rd", "flat=1",
        ]
        rc, out, err = _exec_tool(cfg, mode, runner, "klayout", args, tmp, cfg.timeout("klayout_drc"))
        if rc != 0 or not lyrdb.is_file():
            return Result.fail(f"KLayout antenna DRC failed (rc={rc}): {(err or out)[-300:]}",
                               measured=rc, expected=0, evidence=[ws.rel(gds)])
        parsed = parse_klayout_lyrdb(lyrdb)

    ant_json = _antenna_step_json(ws)
    ecc_viols = json.loads(ant_json.read_text()).get("antenna_check", {}).get("violation_count") if ant_json else None
    details = {"ecc_violation_count": ecc_viols, **parsed}
    if parsed["total"] > 0:
        return Result.fail(
            f"KLayout antenna DRC found {parsed['total']} violations (top: {parsed['top_categories'][:3]}; ECC={ecc_viols})",
            measured=parsed["total"], expected=0, evidence=[ws.rel(gds)], details=details,
        )
    return Result.ok(f"0 KLayout antenna DRC violations (ECC={ecc_viols})", measured=0, expected=0, details=details)


@check("L3.oss.klayout_lvs", Layer.L3, steps=("lvs", "Harden"), category="integrity", severity="critical",
       title="KLayout batch LVS (optional unofficial deck) matches GDS layout against schematic")
def klayout_lvs(ws):
    cfg = config.current()
    mode, runner = _tool_runner(cfg, cfg.klayout_bin, "klayout")
    if mode is None:
        return Result.skip("KLayout CLI not configured (set paths.klayout_bin, `klayout` on PATH, or paths.oss_docker_image)")
    deck = _openpdk_file(cfg, "libs.tech/klayout/tech/ics55.lvs")
    if deck is None:
        return Result.skip("optional unofficial ics55.lvs not configured (set paths.openpdk_root)")
    gds = ws.output_file("Harden", ".gds") or ws.output_file("filler", ".gds")
    if gds is None:
        return Result.skip("no GDS output for LVS oracle")
    schem = _find_schematic(ws)
    if schem is None:
        return Result.skip("no CDL/SPICE schematic netlist in workspace")

    is_docker = mode == "docker"
    with _work_tmpdir(cfg, is_docker, "ecc_verify_klvs_") as td:
        tmp = Path(td)
        staged_deck = _stage_file(deck, tmp, force_stage=is_docker)
        staged_gds = _stage_file(gds, tmp, force_stage=is_docker)
        staged_schem = _stage_file(schem, tmp, force_stage=is_docker)
        lvsdb = tmp / "lvs.lvsdb"
        args = [
            "-b", "-r", str(staged_deck),
            "-rd", f"input={staged_gds}",
            "-rd", f"topcell={ws.top_name}",
            "-rd", f"schematic={staged_schem}",
            "-rd", f"report={lvsdb}",
            "-rd", "run_mode=deep",
        ]
        rc, out, err = _exec_tool(cfg, mode, runner, "klayout", args, tmp, cfg.timeout("klayout_lvs"))

    parsed = parse_klayout_lvs_output(out, err)
    ecc_diff = _lvs_diff_count(ws)
    details = {"ecc_lvs_diff": ecc_diff, "schematic": str(schem), **parsed}
    if rc != 0 or parsed["matched"] is not True:
        reason = "netlists do not match" if parsed["matched"] is False else f"unconfirmed LVS verdict (rc={rc})"
        return Result.fail(f"KLayout LVS: {reason} (ECC iLVS diff={ecc_diff})",
                           measured=parsed["matched"], expected=True,
                           evidence=[ws.rel(gds), ws.rel(schem)], details=details)
    return Result.ok(f"KLayout LVS matched (ECC iLVS diff={ecc_diff})", measured=True, expected=True, details=details)
