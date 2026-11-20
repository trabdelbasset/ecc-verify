"""L3 signoff integrity: antenna effectiveness, LEC, checklist vs reality."""
from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path

from ecc_verify.model import Layer, Result
from ecc_verify.registry import check


@check("L3.so.antenna_effective", Layer.L3, steps=("antennaCheck",), category="integrity", severity="major",
       title="antenna check actually ran: signal_net_count > 0 and pins_with_gate_area > 0")
def antenna(ws):
    p = ws.step_feature_json("antennaCheck")
    if p is None or not p.exists():
        return Result.fail("antennaCheck step feature JSON missing")
    a = json.load(open(p))["antenna_check"]
    if a.get("signal_net_count", 0) > 0 and a.get("pins_with_gate_area", 0) > 0:
        return Result.ok(f"{a['signal_net_count']} nets, {a['pins_with_gate_area']} gate pins analysed", measured=a)
    return Result.fail(f"vacuous antenna check: signal_net_count={a.get('signal_net_count')}, "
                       f"pins_with_gate_area={a.get('pins_with_gate_area')}, violation_count="
                       f"{a.get('violation_count')} (0 violations reported without analysing any net)",
                       measured=a, expected="signal_net_count > 0 and pins_with_gate_area > 0",
                       evidence=[f"{ws.rel(p)} -> /antenna_check"])


def _sha(path: Path):
    raw = path.read_bytes()
    out = {hashlib.sha256(raw).hexdigest()}
    if path.name.endswith(".gz"):
        out.add(hashlib.sha256(gzip.decompress(raw)).hexdigest())
    return out


@check("L3.so.lec_passed", Layer.L3, steps=("postRouteLec",), category="integrity", severity="critical",
       title="post-route LEC reports equivalence, on the artifacts actually produced")
def lec(ws):
    d = ws.step_dir("postRouteLec")
    f = next((d / "output").glob("*_result.json"), None)
    if f is None:
        return Result.fail("no LEC result json")
    r = json.load(open(f))
    probs = []
    if r.get("status") != "proven":
        probs.append(f"LEC status {r.get('status')!r}")
    eq = d / "report/equiv_status.rpt"
    if eq.exists() and "IDENTICAL" not in eq.read_text(errors="replace"):
        probs.append("equiv_status.rpt does not say IDENTICAL")
    gold = ws.output_file("Synthesis", ".v.gz")
    gate = ws.output_file("filler", ".v.gz")
    if gold and r.get("golden_sha256") and r["golden_sha256"] not in _sha(gold):
        probs.append("golden sha256 does not match current Synthesis netlist (stale LEC input)")
    if gate and r.get("gate_sha256") and r["gate_sha256"] not in _sha(gate):
        probs.append("gate sha256 does not match current filler netlist (stale LEC input)")
    if probs:
        return Result.fail("; ".join(probs), evidence=[ws.rel(f)])
    return Result.ok("proven; hashes match current Synthesis/filler netlists", measured=r["status"])


@check("L3.so.checklist_vs_reality", Layer.L3, steps=("workspace",), category="integrity", severity="major",
       title="no checklist item is 'pass' while the underlying artifact check fails")
def checklist(ws):
    c = json.load(open(ws.path / "home/checklist.json"))
    flow = {s["name"].lower(): s["state"] for s in ws.flow["steps"]}
    corners = ws.sta_corners()
    drc = json.load(open(ws.step_dir("drc") / "feature/drc.step.json"))["drc"]["number"]
    lvs = json.load(open(ws.step_dir("lvs") / "feature/lvs.step.json"))
    lvs_diff = sum(abs(e.get("difference", 0)) for e in lvs.get("entity", []))
    ant_path = ws.step_feature_json("antennaCheck")
    ant = json.load(open(ant_path))["antenna_check"] if ant_path and ant_path.exists() else {}
    set_ws = min((v["summary"]["setup"]["wns"] for v in corners.values()), default=None)
    hold_ws = min((v["summary"]["hold"]["wns"] for v in corners.values()), default=None)
    probs = []
    for it in c["checklist"]:
        if it["state"] != "pass":
            continue
        iid = it["id"]
        src = it.get("source", {})
        if src.get("kind") in ("output", "report") and src.get("path"):
            p = ws.path / src["path"]
            if not p.exists() or (p.is_file() and p.stat().st_size == 0):
                probs.append(f"{iid}: pass but {src['path']} missing/empty")
        if iid == "quality.drc.clean" and drc != 0:
            probs.append(f"{iid}: pass but drc number={drc}")
        if iid == "quality.lvs.clean" and lvs_diff != 0:
            probs.append(f"{iid}: pass but LVS differences={lvs_diff}")
        if iid == "quality.sta.setup_closed" and set_ws is not None and set_ws < 0:
            probs.append(f"{iid}: pass but worst setup WS={set_ws}")
        if iid == "quality.sta.hold_closed" and hold_ws is not None and hold_ws < 0:
            probs.append(f"{iid}: pass but worst hold WS={hold_ws}")
        if iid == "quality.rcx.corner_coverage" and len(ws.spef_files()) < 9:
            probs.append(f"{iid}: pass but {len(ws.spef_files())} SPEF files")
        if iid.startswith("flow.") and iid.endswith(".completed"):
            nm = it.get("source", {}).get("step", "").lower()
            if nm and flow.get(nm) != "Success":
                probs.append(f"{iid}: pass but flow state {flow.get(nm)}")
        if iid in ("artifact.antennacheck.report", "artifact.antenna_check.report") and (
            ant.get("signal_net_count", 0) == 0 or ant.get("pins_with_gate_area", 0) == 0
        ):
            probs.append(f"{iid}: pass ('report present and non-empty') although the antenna run analysed "
                         f"{ant.get('signal_net_count')} nets / {ant.get('pins_with_gate_area')} gate pins")
    if probs:
        return Result.fail("; ".join(probs[:4]), measured=len(probs), expected=0,
                           evidence=[ws.rel(ws.path / "home/checklist.json")], details={"problems": probs})
    return Result.ok(f"{sum(1 for i in c['checklist'] if i['state'] == 'pass')} pass items verified")
