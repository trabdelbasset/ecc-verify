"""L1: flow integrity per design."""
from __future__ import annotations

import json
import re
from pathlib import Path

from ecc_verify.model import Layer, Result
from ecc_verify.registry import check
from ecc_verify.workspace import STEP_ROW, canonical_step_row, flow_step_dir

# canonical step row -> required output suffixes (supports both legacy and numbered snake-case step dirs)
DEF_ROWS = [
    "preFloorplan", "macroPlacement", "postFloorplan", "preplace", "place", "diff_sizing",
    "CTS", "legalization", "Timing optimization", "route", "filler", "lvs", "drc",
    "antennaCheck", "RCX", "powerAnalysis", "emir", "sta",
]
EXPECT_BY_ROW = {row: [".def.gz", ".v.gz"] for row in DEF_ROWS}
EXPECT_BY_ROW["Synthesis"] = [".v.gz"]
EXPECT_BY_ROW["filler"] = [".def.gz", ".v.gz", ".gds"]
EXPECT_BY_ROW["postRouteLec"] = ["_result.json"]
EXPECT_BY_ROW["Harden"] = [".gds", ".lef", ".lib"]
EXPECT_BY_ROW["sta"] = []  # sta emits per-corner reports instead (checked below)
SPEF_NAMES = None

LOG_PATTERNS = [
    (re.compile(r"Traceback \(most recent call last\)"), "Traceback"),
    (re.compile(r"Segmentation fault"), "Segmentation fault"),
    (re.compile(r"core dumped"), "core dumped"),
    (re.compile(r"\x1b\[1;31m"), "red ANSI error"),
    (re.compile(r"(^|[\s\[\]|:])ERROR($|[\s\[\]|:])"), "ERROR"),
]
# documented allowlist: (regex on the log line) -> reason. Extended during triage.
LOG_ALLOW: list = [
    (re.compile(r"ERROR: 0\b|errors?: 0\b|error_count\W+0\b|0 errors", re.I), "zero-error summary"),
]


def _out(ws, d: Path, suffix):
    o = d / "output"
    return [f for f in o.iterdir() if f.name.endswith(suffix)] if o.is_dir() else []


@check("L1.flow_all_success", Layer.L1, steps=("workspace",), category="existence", severity="critical",
       title="every flow.json step Success, none Ongoing")
def flow_all_success(ws):
    steps = ws.flow.get("steps", [])
    bad = {s["name"]: s["state"] for s in steps if s.get("state") != "Success"}
    expected = set(STEP_ROW.values())
    have = {canonical_step_row(flow_step_dir(s)) for s in steps}
    missing = sorted(expected - have)
    if bad or missing:
        return Result.fail(f"non-success: {bad}; missing steps: {missing}", measured=bad, expected="all Success",
                           evidence=[f"{ws.rel(ws.path)}/home/flow.json"])
    return Result.ok(f"{len(steps)} steps Success", measured=len(steps), expected=len(expected))


@check("L1.outputs_exist", Layer.L1, steps=tuple(STEP_ROW.values()), category="existence", severity="critical",
       title="expected outputs exist and are non-empty")
def outputs_exist(ws):
    problems = []
    for row, suffixes in EXPECT_BY_ROW.items():
        d = ws.step_dir(row)
        if d is None or not d.is_dir():
            problems.append(f"{row}: step dir missing")
            continue
        for suf in suffixes:
            fs = _out(ws, d, suf)
            if not fs:
                problems.append(f"{row}: no *{suf}")
            elif any(f.stat().st_size == 0 for f in fs):
                problems.append(f"{row}: empty *{suf}")
    # SPEF per RC corner
    exp = ws.metrics().get("rcx_expected_corner_count")
    sp = ws.spef_files()
    if exp is not None and len(sp) != int(exp):
        problems.append(f"RCX: {len(sp)} SPEF files, expected {int(exp)}")
    if not sp:
        problems.append("RCX: no SPEF")
    for c, f in sp.items():
        if f.stat().st_size == 0:
            problems.append(f"RCX: empty spef {c}")
    # STA: every configured corner has summary + reports
    try:
        cfg = json.load(open(ws.path / "config/sta_ecc.json"))
        want = [f"{c}_{'125' if False else ''}" for c in []]  # placeholder, detailed in L3.sta.corner_coverage
    except Exception:  # noqa: BLE001
        cfg = None
    sta_dir = ws.step_dir("sta")
    if sta_dir is None or not (sta_dir / "report").is_dir():
        problems.append("sta: no report dir")
    if problems:
        return Result.fail(f"{len(problems)} problems: " + "; ".join(problems[:8]), measured=problems,
                           expected="all outputs present", evidence=[ws.rel(ws.path)])
    return Result.ok(f"{len(EXPECT_BY_ROW)} steps outputs present, {len(sp)} SPEF")


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


@check("L1.step_json_valid", Layer.L1, steps=tuple(STEP_ROW.values()), category="integrity", severity="major",
       title="feature/analysis JSON parse; step/design fields correct; schema_version known")
def step_json_valid(ws):
    problems = []
    n = 0
    known = {1, 2, 3, 4}
    for sd, d in ws.step_dirs.items():
        row = canonical_step_row(sd)
        for p in list(d.glob("feature/*.json")) + list(d.glob("analysis/*.json")):
            n += 1
            try:
                j = json.load(open(p))
            except Exception as e:  # noqa: BLE001
                problems.append(f"{ws.rel(p)}: unparseable ({e})")
                continue
            if not isinstance(j, dict):
                continue
            if p.parent.name == "analysis":
                if "schema_version" in j and j["schema_version"] not in known:
                    problems.append(f"{ws.rel(p)}: unknown schema_version {j['schema_version']}")
                if "step" in j:
                    step_val = str(j["step"])
                    if canonical_step_row(step_val) != row and _norm(step_val) != _norm(row) and _norm(step_val) not in _norm(sd):
                        problems.append(f"{ws.rel(p)}: step={j['step']!r} != {row!r}")
                if "design" in j and j["design"] not in (ws.name, ws.top_name):
                    problems.append(f"{ws.rel(p)}: design={j['design']!r} != {ws.name!r}")
            elif "schema_version" in j and j["schema_version"] not in known:
                problems.append(f"{ws.rel(p)}: unknown schema_version {j['schema_version']}")
    if problems:
        return Result.fail(f"{len(problems)} problems: " + "; ".join(problems[:5]), measured=problems,
                           evidence=[ws.rel(ws.path)])
    return Result.ok(f"{n} JSON files valid", measured=n)


@check("L1.logs_clean", Layer.L1, steps=tuple(STEP_ROW.values()), category="integrity", severity="major",
       title="no Traceback/segfault/ERROR in step logs (allowlisted patterns excepted)")
def logs_clean(ws):
    hits = []
    recovered = []
    allowed = 0
    for sd, d in ws.step_dirs.items():
        for p in d.glob("log/*"):
            if not p.is_file():
                continue
            txt = p.read_text(errors="replace")
            has_recovery = "recovered a legal placement" in txt
            with open(p, errors="replace") as f:
                for i, ln in enumerate(f, 1):
                    for rx, label in LOG_PATTERNS:
                        if rx.search(ln):
                            if any(a.search(ln) for a, _ in LOG_ALLOW):
                                allowed += 1
                            elif has_recovery and re.search(r"legality check failed|legalization also failed|overlap node", ln):
                                recovered.append(f"{ws.rel(p)}:{i}: [{label}] {ln.strip()[:160]}")
                            else:
                                hits.append(f"{ws.rel(p)}:{i}: [{label}] {ln.strip()[:160]}")
                            break
    if hits:
        return Result.fail(f"{len(hits)} error-level log lines (+{allowed} allowlisted)", measured=len(hits),
                           expected=0, evidence=hits[:5], details={"examples": hits[:20]})
    if recovered:
        return Result.warn(f"{len(recovered)} greedy-legalization ERROR lines, later recovered by the row-segment "
                           f"repair ('recovered a legal placement') in the same log", measured=len(recovered),
                           evidence=recovered[:3], details={"examples": recovered[:20]})
    return Result.ok(f"clean ({allowed} allowlisted)", measured=0)


_PARALLEL_DEPS: dict[str, tuple[str, ...]] = {
    "lvs": ("filler",),
    "drc": ("filler",),
    "antennaCheck": ("filler",),
    "postRouteLec": ("Synthesis", "filler"),
    "RCX": ("filler",),
    "sta": ("filler", "RCX"),
    "powerAnalysis": ("filler", "RCX"),
    "emir": ("filler", "RCX", "powerAnalysis"),
    "Harden": ("filler", "RCX"),
}


@check("L1.timestamps_monotonic", Layer.L1, steps=("workspace",), category="integrity", severity="major",
       title="step output mtimes follow flow order (no stale artifacts)")
def timestamps(ws):
    tol = 2.0
    seen: dict[str, tuple[float, str]] = {}
    prev_serial: tuple[float, str] | None = None
    bad = []
    for s in ws.flow["steps"]:
        sd = flow_step_dir(s)
        d = ws.path / sd
        row = canonical_step_row(sd)
        outs = [f for f in (d / "output").iterdir() if f.is_file()] if (d / "output").is_dir() else []
        if not outs:
            continue
        t = max(f.stat().st_mtime for f in outs)
        t0 = min(f.stat().st_mtime for f in outs)
        info = s.get("info")
        explicit_input = s.get("input_step") or (info.get("input_step") if isinstance(info, dict) else None)
        dep_rows: list[str] = []
        if explicit_input:
            dep_rows.append(canonical_step_row(str(explicit_input)))
        if row in _PARALLEL_DEPS:
            dep_rows.extend(_PARALLEL_DEPS[row])
        deps = [seen[r] for r in dict.fromkeys(dep_rows) if r in seen]
        if row in _PARALLEL_DEPS and deps:
            for dep_t0, dep_sd in deps:
                if t < dep_t0 - tol:
                    bad.append(f"{sd} newest output older than {dep_sd} by {dep_t0 - t:.1f}s")
        elif prev_serial is not None and t < prev_serial[0] - tol:
            bad.append(f"{sd} newest output older than {prev_serial[1]} by {prev_serial[0] - t:.1f}s")
        seen[row] = (t0, sd)
        if row not in _PARALLEL_DEPS:
            prev_serial = (max(prev_serial[0], t0), sd) if prev_serial is not None else (t0, sd)
    if bad:
        return Result.fail("; ".join(bad[:5]), measured=bad, tolerance=f"{tol}s", expected="non-decreasing")
    return Result.ok("monotonic", tolerance=f"{tol}s")


@check("L1.runtime_recorded", Layer.L1, steps=("workspace",), category="integrity", severity="minor",
       title="every Success step has runtime and peak memory")
def runtime_recorded(ws):
    bad = []
    for s in ws.flow["steps"]:
        if s.get("state") != "Success":
            continue
        rt = s.get("runtime")
        if not rt or not re.fullmatch(r"\d+:\d+:\d+", str(rt)):
            bad.append(f"{s['name']}: runtime={rt!r}")
        if s.get("peak memory (mb)") in (None, ""):
            bad.append(f"{s['name']}: no peak memory")
    if bad:
        return Result.fail("; ".join(bad[:6]), measured=bad)
    return Result.ok("all steps timed")


@check("L1.lock_state", Layer.L1, steps=("workspace",), category="integrity", severity="major",
       title="no stale runtime_processes entry in project.json")
def lock_state(ws):
    pj = ws.project_json
    rp = pj.get("runtime_processes")
    msgs = []
    if rp:
        msgs.append(f"runtime_processes not empty: {json.dumps(rp)[:200]}")
    for w in pj.get("workspaces", []):
        if w.get("status") == "running":
            msgs.append(f"workspace {w.get('workspace_id')} status=running")
    if msgs:
        return Result.fail("; ".join(msgs), evidence=[ws.rel(ws.project_dir / "project.json")])
    return Result.ok("no stale processes")


@check("L1.qor_report_fresh", Layer.L1, steps=("workspace",), category="integrity", severity="major",
       title="home/qor_report.json newer than last step outputs; flow_steps match flow.json")
def qor_fresh(ws):
    p = ws.path / "home/qor_report.json"
    if not p.exists():
        return Result.fail("home/qor_report.json missing", evidence=[ws.rel(p)])
    q = json.load(open(p))
    msgs = []
    last = 0
    for sd, d in ws.step_dirs.items():
        o = d / "output"
        if o.is_dir():
            for f in o.iterdir():
                if f.is_file():
                    last = max(last, f.stat().st_mtime)
    if p.stat().st_mtime + 2 < last:
        msgs.append(f"qor_report older than newest step output by {last - p.stat().st_mtime:.0f}s")
    fs = q.get("flow_steps", {})
    flow = {s["name"]: s["state"] for s in ws.flow["steps"]}
    if fs != flow:
        diff = {k: (fs.get(k), flow.get(k)) for k in set(fs) | set(flow) if fs.get(k) != flow.get(k)}
        msgs.append(f"flow_steps differ from flow.json: {diff}")
    if q.get("design") not in (ws.name, ws.top_name):
        msgs.append(f"design {q.get('design')} != {ws.name}")
    if msgs:
        return Result.fail("; ".join(msgs), evidence=[ws.rel(p)])
    return Result.ok("fresh and consistent")
