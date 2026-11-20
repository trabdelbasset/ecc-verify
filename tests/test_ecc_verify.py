"""pytest entry. `pytest -k xtea` / `-k L3.pl.no_overlap` selects (design, check) pairs; parser unit tests use tiny fixtures."""
import copy
import gzip
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ecc_verify import parsers as P  # noqa: E402
from ecc_verify.model import Status  # noqa: E402
from ecc_verify.registry import REGISTRY, load_all_checks, run_check  # noqa: E402
from ecc_verify.workspace import discover  # noqa: E402

DEF = """VERSION 5.8 ;
DESIGN tiny ;
UNITS DISTANCE MICRONS 1000 ;
DIEAREA ( 0 0 ) ( 10000 10000 ) ;
ROW ROW_0 core7 1000 1000 FS DO 40 BY 1 STEP 200 0 ;
ROW ROW_1 core7 1000 2400 N DO 40 BY 1 STEP 200 0 ;
TRACKS X 300 DO 30 STEP 300 LAYER MET2 ;
VIAS 1 ;
- VIA1_X + VIARULE R + CUTSIZE 90 90 + LAYERS MET1 VIA1 MET2 ;
END VIAS
COMPONENTS 3 ;
    - u1 BUFX2H7R + PLACED ( 1000 1000 ) FS ;
    - u2 INVX1H7R + PLACED ( 3000 2400 ) N ;
    - u3 BUFX2H7R + FIXED ( 5000 1000 ) FS ;
END COMPONENTS
PINS 2 ;
 - a + NET a + DIRECTION INPUT + USE SIGNAL + LAYER MET4 ( -50 -50 ) ( 50 50 ) + PLACED ( 0 5000 ) N ;
 - z + NET z + DIRECTION OUTPUT + USE SIGNAL + LAYER MET4 ( -50 -50 ) ( 50 50 ) + PLACED ( 10000 5000 ) N ;
END PINS
NETS 2 ;
- n1 ( u1 Y ) ( u2 A )
  + USE SIGNAL
  + ROUTED MET2 ( 2050 1850 ) ( * 2800 )
    NEW MET3 ( 2050 2800 ) ( 3150 * )
    NEW MET3 ( 2050 2800 ) MET3_MET2_VIA2_6
    NEW MET3 ( 3150 2800 ) MET3_MET2_VIA2_6 ;
- n2 ( u2 Y ) ( u3 A ) ( PIN z ) + USE SIGNAL ;
END NETS
"""
LEF = """VERSION 5.7 ;
UNITS
  DATABASE MICRONS 1000 ;
END UNITS
LAYER MET2
  TYPE ROUTING ;
  DIRECTION VERTICAL ;
  PITCH 0.2 0.2 ;
  WIDTH 0.1 ;
END MET2
SITE core7
    SIZE 0.200 BY 1.400 ;
END core7
MACRO BUFX2H7R
  CLASS CORE ;
  SIZE 1.2 BY 1.4 ;
  SITE core7 ;
  PIN A
    DIRECTION INPUT ;
    USE SIGNAL ;
    PORT
      LAYER MET2 ;
        RECT 0.1 0.2 0.2 0.9 ;
    END
  END A
  PIN Y
    DIRECTION OUTPUT ;
    USE SIGNAL ;
    PORT
      LAYER MET2 ;
        RECT 1.0 0.2 1.1 0.9 ;
    END
  END Y
END BUFX2H7R
"""
LIB = """library (t) {
  time_unit : "1ns";
  default_max_transition : 0.8;
  cell (BUFX2H7R) {
    area : 2.8;
    cell_leakage_power : 1.5;
    pin (A) {
      direction : input;
      capacitance : 0.001;
      max_transition : 0.7;
    }
    pin (Y) {
      direction : output;
      function : "A";
      max_capacitance : 0.05;
    }
  }
  cell (DFFQX1H7R) {
    area : 6.16;
    ff (IQ, IQN) {
      clocked_on : "CK";
      next_state : "D";
    }
    pin (CK) {
      direction : input;
      clock : true;
    }
    pin (D) {
      direction : input;
    }
    pin (Q) {
      direction : output;
      function : "IQ";
    }
  }
}
"""
SPEF = """*SPEF "IEEE 1481-1998"
*DESIGN "tiny"
*T_UNIT 1.0 NS
*C_UNIT 1.0 PF
*R_UNIT 1.0 OHM
*NAME_MAP
*1 n1
*2 n2
*D_NET *1 0.002
*CONN
*CAP
1 *1:1 0.001
*RES
1 *1:1 *1:2 10
2 *1:2 *1:3 11
*END
*D_NET *2 0.5
*RES
1 *2:1 *2:2 1
*END
"""
VLOG = """module tiny (a, z);
  input a; output z;
  wire n1;
  BUFX2H7R u1 (.A(a), .Y(n1));
  BUFX2H7R \\u2/x  (.A(n1), .Y(z));
endmodule
"""


def _w(tmp_path, name, text, gz=False):
    p = tmp_path / name
    if gz:
        with gzip.open(p, "wt") as f:
            f.write(text)
    else:
        p.write_text(text)
    return p


def test_def_parser(tmp_path):
    for gz in (False, True):
        d = P.parse_def(_w(tmp_path, "t.def.gz" if gz else "t.def", DEF, gz))
        assert d.design == "tiny" and d.dbu == 1000 and d.die == (0, 0, 10000, 10000)
        assert len(d.rows) == 2 and d.rows[0].orient == "FS" and d.rows[0].sx == 200 and d.rows[0].nx == 40
        assert d.comps["u3"].status == "FIXED" and d.comps["u1"].orient == "FS"
        assert set(d.pins) == {"a", "z"} and d.pins["a"].rect == (-50, -50, 50, 50) and d.pins["z"].x == 10000
        n1 = d.nets["n1"]
        assert n1.routed and n1.conns == [("u1", "Y"), ("u2", "A")]
        assert n1.segs == [("MET2", 2050, 1850, 2050, 2800), ("MET3", 2050, 2800, 3150, 2800)]
        assert n1.vias == [("MET3_MET2_VIA2_6", 2050, 2800, "MET3"), ("MET3_MET2_VIA2_6", 3150, 2800, "MET3")]
        assert not d.nets["n2"].routed and len(d.nets["n2"].conns) == 3
        assert d.vias["VIA1_X"] == ("MET1", "VIA1", "MET2")


def test_lef_parser(tmp_path):
    l = P.parse_lef(_w(tmp_path, "t.lef", LEF))
    assert l.dbu == 1000 and l.layers["MET2"].direction == "VERTICAL" and l.layers["MET2"].width == 0.1
    assert l.sites["core7"] == (0.2, 1.4)
    m = l.macros["BUFX2H7R"]
    assert (m.w, m.h) == (1.2, 1.4) and m.pins["Y"]["dir"] == "OUTPUT" and m.pins["A"]["rects"] == [("MET2", 0.1, 0.2, 0.2, 0.9)]


def test_liberty_parser(tmp_path):
    lib = P.parse_liberty(_w(tmp_path, "t.lib", LIB))
    assert lib.default_max_tran == 0.8
    b, f = lib.cells["BUFX2H7R"], lib.cells["DFFQX1H7R"]
    assert b.area == 2.8 and not b.seq and b.pins["A"]["max_tran"] == 0.7 and b.pins["Y"]["max_cap"] == 0.05
    assert b.pins["Y"]["dir"] == "output" and b.leakage == 1.5
    assert f.seq and f.pins["CK"]["clock"]


def test_spef_parser(tmp_path):
    s = P.parse_spef(_w(tmp_path, "t.spef", SPEF))
    assert s.design == "tiny" and s.c_unit_ff == 1000.0
    assert s.nets["n1"] == pytest.approx(2.0) and s.nets["n2"] == pytest.approx(500.0)
    assert s.res_count == {"n1": 2, "n2": 1}


def test_verilog_parser(tmp_path):
    m = P.parse_verilog(_w(tmp_path, "t.v", VLOG))["tiny"]
    assert m.ports == {"a": "input", "z": "output"}
    assert m.insts["u1"].cell == "BUFX2H7R" and m.insts["u1"].conns == {"A": "a", "Y": "n1"}
    assert "u2/x" in m.insts


def test_sdc_parser(tmp_path):
    s = P.parse_sdc(_w(tmp_path, "t.sdc", "create_clock -name clk -period 10 -waveform {0 5} [get_ports {clk}]\n"
                       "set_input_delay 0 -clock clk [get_ports a]\n"))
    assert s.clocks == [("clk", 10.0, "clk")] and s.has_input_delay and not s.has_output_delay


# ---- mutation sanity: the checks must detect injected defects --------------------------------------------------
def _geo(d, lef):
    from ecc_verify.checks.l3_routing import Geo, components, net_items
    return Geo, components, net_items


def test_connectivity_detects_open(tmp_path):
    from ecc_verify.checks.l3_routing import Geo, components, net_items
    d = P.parse_def(_w(tmp_path, "t.def", DEF))
    lef = P.parse_lef(_w(tmp_path, "t.lef", LEF))
    lef.macros["INVX1H7R"] = lef.macros["BUFX2H7R"]
    lef.vias["MET3_MET2_VIA2_6"] = {"rects": [("MET2", -0.05, -0.085, 0.05, 0.085), ("MET3", -0.085, -0.05, 0.085, 0.05)]}
    geo = Geo(d, lef)

    def n_roots(net):
        items, pins, cnt = net_items(geo, net)
        find = components(items, cnt)
        return len({find(p) for p in pins})

    net = d.nets["n1"]
    assert n_roots(net) == 1                   # intact net: one component
    broken = copy.deepcopy(net)
    broken.segs = [broken.segs[1]]            # delete the vertical MET2 wire -> u1.Y becomes an OPEN
    assert n_roots(broken) == 2


def test_overlap_detection(tmp_path):
    from ecc_verify.checks.l3_placement import find_overlaps
    d = P.parse_def(_w(tmp_path, "t.def", DEF))
    lef = P.parse_lef(_w(tmp_path, "t.lef", LEF))
    lef.macros["INVX1H7R"] = lef.macros["BUFX2H7R"]
    assert find_overlaps(d, lef) == []
    d.comps["u3"].x, d.comps["u3"].y = 1500, 1000  # overlaps u1 (1000..2200)
    assert len(find_overlaps(d, lef)) == 1


# ---- (design, check) matrix over the real workspaces (skipped when absent) -------------------------------------
# Set ECC_VERIFY_ROOT to a directory of finished projects (<root>/<design>/default) to run the matrix.
ROOT = Path(os.environ.get("ECC_VERIFY_ROOT", "/nonexistent-ecc-verify-root"))
load_all_checks()
_WS = discover(ROOT) if ROOT.exists() else []
_DESIGN_CHECKS = sorted(k for k, (s, _) in REGISTRY.items() if s.scope == "design"
                        and not k.startswith(("L3.fn", "L3.gds", "L3.oss")))
# real ECC findings: derived from the shipped known-issues list (plus the L1 log WARN class)
from ecc_verify.known_issues import KnownIssues  # noqa: E402

_KNOWN = KnownIssues.packaged()
KNOWN_REAL = {c for e in _KNOWN.entries for c in e.checks if "*" not in c} | {"L1.logs_clean"}


@pytest.mark.parametrize("cid", _DESIGN_CHECKS)
@pytest.mark.parametrize("ws", _WS, ids=[w.name for w in _WS])
def test_check(ws, cid):
    r = run_check(REGISTRY[cid][1], ws)
    assert r.status != Status.ERROR, r.details.get("traceback", r.message)
    if cid in KNOWN_REAL:
        pytest.xfail(f"known real finding: {r.status.value} {r.message[:100]}") if r.status == Status.FAIL else None
    else:
        assert r.status in (Status.PASS, Status.WARN, Status.SKIP), r.message


# ---- mutation tests on a real workspace: the physics checks must catch injected defects -----------------------
_XTEA = next((w for w in _WS if w.name == "xtea"), None)


@pytest.mark.skipif(_XTEA is None, reason="xtea workspace not available")
def test_real_open_short_overlap_detected():
    from ecc_verify.checks import l3_placement, l3_routing
    ws = _XTEA
    d = ws.def_("route")
    lef = ws.lef()
    geo = l3_routing.Geo(d, lef)

    def opens():
        n = 0
        for net in l3_routing._signal_nets(d):
            items, pins, cnt = l3_routing.net_items(geo, net)
            find = l3_routing.components(items, cnt)
            n += len({find(p) for p in pins}) > 1
        return n

    assert opens() == 0
    victim = max((n for n in l3_routing._signal_nets(d) if len(n.segs) > 3), key=lambda n: len(n.segs))
    saved = list(victim.segs)
    victim.segs = saved[:1] + saved[2:]            # delete one routed segment
    try:
        assert opens() >= 1
    finally:
        victim.segs = saved
    assert opens() == 0
    # overlap: move one cell onto its neighbour
    fd = copy.deepcopy(ws.def_("filler"))
    a, b = [c for c in fd.comps.values() if c.master.startswith("BUF")][:2]
    a.x, a.y, a.orient = b.x + 100, b.y, b.orient
    assert l3_placement.find_overlaps(fd, lef)


@pytest.mark.skipif(_XTEA is None, reason="xtea workspace not available")
def test_real_netlist_mutation_changes_connectivity_signature():
    from ecc_verify import netgraph
    from ecc_verify.checks.l2_integration import _signatures
    ws = _XTEA
    lib = ws.liberty()
    top = copy.deepcopy(ws.top("filler"))
    g = netgraph.build(top, lib)
    base = _signatures(top, lib, g)
    inst = next(i for i in top.insts.values() if lib.cells.get(i.cell) and lib.cells[i.cell].seq)
    pin = next(p for p, v in lib.cells[inst.cell].pins.items() if v["dir"] == "input" and not v["clock"])
    other = next(n for n in top.ports if top.ports[n] == "input" and n != inst.conns[pin])
    inst.conns[pin] = other
    mut = _signatures(top, lib, netgraph.build(top, lib))
    assert mut != base
