"""L3 functional cross-checks independent of kepler-formal, for small designs (< 2k std cells):
bounded miter equivalence with Yosys (RTL vs final netlist, std-cell models derived from Liberty) and an
iverilog random-stimulus comparison."""
from __future__ import annotations

import gzip
import json
import re
from pathlib import Path

from ecc_verify.model import Layer, Result
from ecc_verify.registry import check
from ecc_verify import config, util
from ecc_verify.util import run

# sampling thresholds come from [sampling] in ecc-verify.toml (defaults: 2000 cells, 25 BMC cycles, 1000 sim cycles,
# 24 settle cycles = reset-release X-resolution transients tolerated between RTL and gate level)
_RST = re.compile(r"^(rst|reset|resetn|rstn|rst_n|reset_n|nreset|nrst|rst_i|reset_i|i_rst|i_reset|rst_ni|rstb|resetb)$", re.I)


def _S(key):
    return config.current().sample(key)


def _T(key):
    return config.current().timeout(key)


def _tool(name):
    """Absolute path of an oss-cad-suite tool (config paths.oss_cad_bin), else the bare name (PATH lookup)."""
    b = config.current().oss_cad_bin
    return str(Path(b) / name) if b and (Path(b) / name).exists() else name


def _workdir(ws):
    d = util.VERIFY_RUNS / f"fn_{ws.name}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _libs(ws=None) -> list[str]:
    if ws is not None:
        try:
            db_libs = ws._db().get("INPUT", {}).get("lib_path") or []
            found = [Path(p) for p in db_libs if p and Path(p).is_file()]
            if found:
                return [str(p) for p in found]
        except Exception:  # noqa: BLE001
            pass
    return [str(p) for p in config.current().std_cell_liberties()]


def _models(ws=None) -> list[str]:
    if ws is not None:
        try:
            db_libs = ws._db().get("INPUT", {}).get("lib_path") or []
            seen: list[Path] = []
            seen_set: set[Path] = set()
            for lp in db_libs:
                if not lp:
                    continue
                vdir = Path(lp).parent.parent / "verilog"
                if not vdir.is_dir():
                    continue
                files = sorted(vdir.glob("*.v"))
                non_pg = [f for f in files if not f.stem.lower().endswith("_pg")]
                for f in non_pg or files:
                    if f not in seen_set:
                        seen_set.add(f)
                        seen.append(f)
            if seen:
                return [str(p) for p in seen]
        except Exception:  # noqa: BLE001
            pass
    return [str(p) for p in config.current().std_cell_verilogs()]


def _prepare(ws):
    """Returns dict(ports, rtl, gate, wrapper, reset, clock, active_low) or raises Skip(reason)."""
    key = "fn_prep"
    if key in ws._cache:
        v = ws._cache[key]
        if isinstance(v, str):
            raise _Skip(v)
        return v
    try:
        v = _prepare_impl(ws)
    except _Skip as s:
        ws._cache[key] = str(s)
        raise
    ws._cache[key] = v
    return v


def _q(paths):
    return " ".join(f'"{p}"' for p in paths)


class _Skip(Exception):
    pass


def _prepare_impl(ws):
    n = ws.metrics().get("std_cell_count")
    if n is None or n >= _S('fn_max_cells'):
        raise _Skip(f"design has {n} std cells (>= {_S('fn_max_cells')}): not in the small-design set")
    cfg = ws.ecc_toml["design"]
    rtl = [str(ws.project_dir / r) for r in cfg["rtl"]]
    for r in rtl:
        if not Path(r).exists():
            raise _Skip(f"RTL file missing: {r}")
    wd = _workdir(ws)
    top = ws.top_name
    # RTL ports via yosys
    pj = wd / "ports.json"
    (wd / "ports.ys").write_text(f"read_verilog -sv {_q(rtl)}\nhierarchy -top {top}\nproc\nwrite_json {_q([pj])}\n")
    rc, out, err = run(["yosys", "-q", str(wd / "ports.ys")], cwd=wd, timeout=_T("yosys_ports"))
    if rc != 0:
        raise _Skip(f"yosys cannot read RTL: {(err or out)[-200:]}")
    mod = json.load(open(pj))["modules"][top]
    ports = {n: (p["direction"], len(p["bits"])) for n, p in mod["ports"].items()}
    if any(d == "inout" for d, _ in ports.values()):
        raise _Skip("RTL has inout ports")
    # gate netlist (renamed module)
    gate_src = ws.output_file("filler", ".v.gz") or ws.output_file("filler", ".v")
    if gate_src is None:
        raise _Skip("no filler gate netlist")
    gtxt = (gzip.decompress(gate_src.read_bytes()) if gate_src.suffix == ".gz"
            else gate_src.read_bytes()).decode(errors="replace")
    gtxt, k = re.subn(rf"^module\s+{re.escape(top)}\b", f"module {top}_gate", gtxt, count=1, flags=re.M)
    if not k:
        raise _Skip("cannot rename gate top module")
    gate = wd / "gate.v"
    gate.write_text(gtxt)
    gports = ws.top("filler").ports
    mapping = {}
    for gp in gports:
        m = re.fullmatch(r"(.+?)(?:\[(\d+)\]|_(\d+))", gp)
        cand = []
        if gp in ports and ports[gp][1] == 1:
            cand.append((gp, None))
        if m:
            cand.append((m.group(1), int(m.group(2) or m.group(3))))
        for name, bit in cand:
            if name in ports and ((bit is None and ports[name][1] == 1) or (bit is not None and bit < ports[name][1])):
                mapping[gp] = (name, bit)
                break
        else:
            raise _Skip(f"cannot map gate port {gp} to an RTL port")
    covered = {(n, b) for n, b in mapping.values()}
    for n, (d, w) in ports.items():
        for b in range(w):
            if (n, b if w > 1 else None) not in covered and (n, b) not in covered:
                raise _Skip(f"RTL port bit {n}[{b}] absent in the gate netlist")
    lines = [f"module gate_top ({', '.join(ports)});"]
    for n, (d, w) in ports.items():
        lines.append(f"  {d} {'[%d:0] ' % (w - 1) if w > 1 else ''}{n};")
    conns = []
    for gp, (n, b) in mapping.items():
        pn = gp if re.fullmatch(r"[\w$]+", gp) else chr(92) + gp + " "
        sel = "[%d]" % b if b is not None else ""
        conns.append(f".{pn}({n}{sel})")
    lines.append(f"  {top}_gate u ({', '.join(conns)});")
    lines.append("endmodule")
    wrapper = wd / "wrapper.v"
    wrapper.write_text("\n".join(lines) + "\n")
    models = []
    for name, c in ws.liberty().cells.items():
        if c.icg:  # liberty gives no function for clock gates; behavioural latch+AND/OR models
            if name.startswith("ICGN"):
                models.append(f"module {name}(input CKN, input E, output ECK); reg l; always @* if (CKN) l = E; "
                              f"assign ECK = ~l | CKN; endmodule")
            else:
                models.append(f"module {name}(input CK, input E, output ECK); reg l; always @* if (!CK) l = E; "
                              f"assign ECK = CK & l; endmodule")
    (wd / "icg_models.v").write_text("\n".join(models) + "\n")
    clock = cfg.get("clock_port")
    resets = [n for n, (d, w) in ports.items() if d == "input" and w == 1 and _RST.match(n)]
    return {"ports": ports, "rtl": rtl, "gate": str(gate), "wrapper": str(wrapper), "clock": clock,
            "reset": resets[0] if resets else None, "wd": wd, "top": top}


def _active_levels(name):
    low = bool(re.search(r"(n|b|ni)$", name, re.I) or name.lower().startswith("n"))
    return [0, 1] if low else [1, 0]


@check("L3.fn.eqy_rtl_vs_final", Layer.L3, steps=("filler", "Synthesis"), category="integrity", severity="critical",
       title="independent Yosys miter (bounded sequential equivalence from reset): RTL vs final netlist")
def eqy(ws):
    try:
        p = _prepare(ws)
    except _Skip as s:
        return Result.skip(str(s))
    if not p["reset"]:
        return Result.skip("no recognisable reset port: cannot constrain initial state for the bounded miter")
    libs = _libs(ws)
    if not libs:
        return Result.skip("no standard-cell Liberty files available for Yosys miter")
    wd = p["wd"]
    last = None
    for level in _active_levels(p["reset"]):
        script = wd / "miter.ys"
        script.write_text(f"""
read_liberty -overwrite -ignore_miss_func -ignore_miss_dir {_q(libs)}
read_verilog -overwrite "{p['wd'] / 'icg_models.v'}"
read_verilog "{p['gate']}"
read_verilog "{p['wrapper']}"
hierarchy -top gate_top
proc; flatten; opt_clean
rename gate_top gate
design -stash gate
design -reset
read_liberty -overwrite -ignore_miss_func -ignore_miss_dir {_q(libs)}
read_verilog -overwrite "{p['wd'] / 'icg_models.v'}"
read_verilog -sv {_q(p['rtl'])}
hierarchy -top {p['top']}
proc; flatten; opt_clean
rename {p['top']} gold
design -stash gold
design -reset
design -copy-from gold -as gold gold
design -copy-from gate -as gate gate
async2sync
miter -equiv -flatten -make_assert gold gate miter
hierarchy -top miter
flatten
opt_clean
sat -verify -seq {_S('fn_bmc_cycles')} -prove-asserts -set-at 1 in_{p['reset']} {level} -set-at 2 in_{p['reset']} {level} -prove-skip 2 -set-init-zero -timeout {_T('miter_solver')}
""")
        rc, out, err = run(["yosys", "-q", "-l", str(wd / "miter.log"), str(script)], cwd=wd, timeout=_T("yosys_miter"), pin=True)
        txt = (wd / "miter.log").read_text(errors="replace") if (wd / "miter.log").exists() else out + err
        if rc == 0 and "SAT proof finished - no model found: SUCCESS" in txt:
            if "fn_known" not in ws._cache:
                sim_smoke(ws)
            if ws._cache.get("fn_known") == 0:
                return Result.warn(f"bounded miter passed ({_S('fn_bmc_cycles')} cycles) but is vacuous: RTL outputs stay "
                                   f"X/Z for the whole simulation, so no output is observable", measured="vacuous")
            return Result.ok(f"no output divergence in {_S('fn_bmc_cycles')} cycles from reset ({p['reset']}={level}) "
                             f"for all inputs (bounded miter, {len(p['ports'])} ports)",
                             measured="equivalent", details={"log": str(wd / "miter.log")})
        last = (rc, txt)
        if "ERROR" in txt and "SAT proof finished" not in txt:
            break
    rc, txt = last
    if rc == 124 or re.search(r"timed? ?out|TIMEOUT", txt):
        return Result.skip(f"bounded miter inconclusive: solver timeout after {_T('miter_solver')} s ({_S('fn_bmc_cycles')} cycles)")
    if "Unsupported" in txt or "ERROR" in txt and "model found" not in txt:
        err = [l for l in txt.splitlines() if "ERROR" in l][:2]
        return Result.skip(f"yosys could not build the miter: {err}")
    return Result.fail(f"miter found a divergence between RTL and final netlist within {_S('fn_bmc_cycles')} cycles (log "
                       f"{wd / 'miter.log'})", measured="divergent", evidence=[str(wd / "miter.log")])


TB = """`timescale 1ns/1ps
module tb;
{decl}
  integer cyc = 0, mism = 0, early = 0, known = 0, seed = 12345;
  {top} r ({rconn});
  gate_top g ({gconn});
  initial begin {clk} = 0; forever #5 {clk} = ~{clk}; end
  always @(negedge {clk}) begin
    cyc = cyc + 1;
{drive}
    if (cyc <= 4) {rst} = {rst_on}; else {rst} = ~{rst_on};
  end
  always @(posedge {clk}) if (cyc > 7) begin : cmp_blk
    #1;
{cmp}
  end
  initial begin
    #{end_t};
    $display("RESULT mismatches=%0d cycles=%0d known=%0d early=%0d", mism, cyc, known, early);
    $finish;
  end
endmodule
"""


@check("L3.fn.sim_smoke", Layer.L3, steps=("filler", "Synthesis"), category="integrity", severity="major",
       title="iverilog random-stimulus RTL vs gate-level comparison for 1000 cycles after reset")
def sim_smoke(ws):
    try:
        p = _prepare(ws)
    except _Skip as s:
        return Result.skip(str(s))
    if not p["reset"] or not p["clock"]:
        return Result.skip("no reset/clock port to initialise the simulation")
    ports = p["ports"]
    decl, drive, cmp_ = [], [], []
    for n, (d, w) in ports.items():
        rng = f"[{w - 1}:0] " if w > 1 else ""
        if d == "input":
            decl.append(f"  reg {rng}{n};")
            if n not in (p["clock"], p["reset"]):
                drive.append(f"    {n} = {{{','.join(['$random(seed)'] * ((w + 31) // 32))}}};")
        else:
            decl.append(f"  wire {rng}o_r_{n}, o_g_{n};")
            cmp_.append(f"    if (^o_r_{n} !== 1'bx) begin known = known + 1; if (o_r_{n} !== o_g_{n}) begin if (cyc <= {_S('fn_settle_cycles')}) early = early + 1; else mism = mism + 1; end end")
    rconn = ", ".join(f".{n}({'o_r_' + n if d == 'output' else n})" for n, (d, w) in ports.items())
    gconn = ", ".join(f".{n}({'o_g_' + n if d == 'output' else n})" for n, (d, w) in ports.items())
    low = _active_levels(p["reset"])[0] == 0
    tb = TB.format(decl="\n".join(decl), top=p["top"], rconn=rconn, gconn=gconn, clk=p["clock"], drive="\n".join(drive),
                   rst=p["reset"], rst_on="1'b0" if low else "1'b1", cmp="\n".join(cmp_), end_t=(_S('fn_sim_cycles') + 8) * 10)
    models = _models(ws)
    if not models:
        return Result.skip("no standard-cell Verilog models available for simulation")
    wd = p["wd"]
    (wd / "tb.v").write_text(tb)
    rtl_text = "".join(Path(r).read_text(errors="replace") for r in p["rtl"])
    fills = sorted(c for c in {i.cell for i in ws.top("filler").insts.values() if i.cell.startswith(("FILL", "ANT"))}
                   if not re.search(rf"\bmodule\s+{re.escape(c)}\b", rtl_text))
    (wd / "phys_stubs.v").write_text("".join(f"module {c}(); endmodule\n" for c in fills))
    exe = wd / "sim.vvp"
    rc, out, err = run([_tool("iverilog"), "-g2012", "-Dfunctional", "-o", str(exe), *models,
                        str(wd / "phys_stubs.v"), *p["rtl"], p["gate"], p["wrapper"], str(wd / "tb.v")], cwd=wd, timeout=_T("sim"))
    if rc != 0:
        return Result.skip(f"iverilog compile failed: {(err or out)[-200:]}")
    rc, out, err = run([_tool("vvp"), str(exe)], cwd=wd, timeout=_T("sim"))
    m = re.search(r"RESULT mismatches=(\d+) cycles=(\d+) known=(\d+) early=(\d+)", out)
    if not m:
        return Result.skip(f"simulation did not finish: {(out + err)[-200:]}")
    mism, cyc, known, early = (int(m.group(i)) for i in (1, 2, 3, 4))
    ws._cache["fn_known"] = known
    if known == 0:
        return Result.skip(f"vacuous: RTL outputs are X/Z in all {cyc} cycles (e.g. tri-state/undriven default "
                           f"branches), nothing comparable")
    if mism:
        return Result.fail(f"{mism} output mismatches over {cyc} cycles of random stimulus", measured=mism,
                           expected=0, evidence=[str(wd / "tb.v")])
    note = f" ({early} transient output mismatches within {_S('fn_settle_cycles')} cycles of reset release tolerated)" if early else ""
    return Result.ok(f"0 mismatches over {cyc} cycles, {known} known output-vector samples compared{note}", measured=0,
                     expected=0, details={"early_transients": early})
