# ecc-verify: independent verification harness for ECC

Guide for running `ecc-verify` (v2) locally and in CI, configuring it, gating on a baseline, and extending it. Stdlib only for the checks. The numbers quoted in the results sections were measured with v1 on ECC nightly 2026-11-07 and are unchanged by v2. Bug IDs (BUG-nn) refer to the ECC evaluation's bug register; the mapping from checks to bugs is in `ecc_verify/data/known_issues.toml`.

## 1. Purpose and principle

ECC reports its own metrics (`qor_metrics.json`, `qor_summary.json`, `home/qor_report.json`, `home/checklist.json`). A bug in the producer is then invisible to any consumer that trusts those numbers. `ecc-verify` therefore **recomputes** quantities from the raw artifacts (DEF, Verilog, LEF, Liberty, SPEF, SDC, GDS, STA reports) with its own parsers, and checks invariants that must hold regardless of what ECC claims. It also drives real `ecc` runs (L4/L5) to test behaviour, not just artifacts.

Design rules:

- Every check is a pure function `(ws) -> Result` (global/slow checks take `ctx`). A crash becomes `ERROR`, never aborts the run.
- Tolerances are explicit in the check and printed in the report.
- FAIL must mean a real defect. Missing preconditions give SKIP with a reason, never a vacuous PASS.

**"Full coverage" means the step x category coverage model** (section 8): 23 rows (21 flow steps plus `CLI` and `workspace`) x 7 categories (existence, integrity, cross-step, physics, timing, metamorphic, fault) = 161 cells. A cell is covered when at least one registered check targets it. It measures breadth of the check suite, not defect-detection power. **Current coverage: 95/161 = 59.0% with all layers registered, 89/161 = 55.3% for the static run (L0-L3, L5)** (expanding the historical 79-check baseline at 93/161 = 57.8% overall and 87/161 = 54.0% static via the 6 `L3.oss.*` oracles, which newly cover `drc x physics` and `antennaCheck x physics`). The `metamorphic` and `fault` columns are only meaningful for flow-level rows, so the practical ceiling is below 100%.

## 2. Quick start

### 2.1 Inputs

`--root` is a directory of finished projects. Each `<root>/<design>/default/` is a workspace produced by `ecc run` with the `rtl2gds` preset, containing the 21 step directories (`Synthesis_yosys`, `preFloorplan_ecc`, ... `Harden_ecc`), `home/{flow.json,qor_report.json,checklist.json,params.toml}`, `config/db_ecc.json` (tech LEF, cell LEFs, Liberty paths), `origin/` (SDC) and `<design>/ecc.toml` + `project.json`. `fi_*` directories are not designs; `discover()` skips directories without a workspace.

Paths and tools are not hard-coded any more: see section 3 (configuration). Auto-detected defaults: `ecc` on `PATH`, PDK from `$CHIPCOMPILER_ICS55_PDK_ROOT` or `ecc pdk show --json`, Yosys/iverilog from `yosys` on `PATH` (or the newest `~/.local/share/ecc/tools/oss-cad-suite/*/bin`), KLayout from the running interpreter (`pip install ecc-verify[gds]`) or `uv run --with klayout`.

`--root` layouts: `<root>/<design>/default/home/flow.json` must exist. That is the layout of ecc's `nix/scripts/run-designs.sh` output and of an `ecc-ci-designs` checkout in which `ecc run --project <design>` was executed in place (section 5).

### 2.2 Static run (L0-L3 + L5 fault injection)

```bash
ecc-verify run --root <work> --out <out> [--baseline <baseline_dir>] \
    [--designs xtea,usb] [--layers L0,L1,L2,L3,L5] [--only 'L3\.rt\.'] [--workers 6] [--pin-cpus 0-7]
ecc-verify list          # registered checks (id, layer, category, severity, scope, title)
ecc-verify config show   # the resolved configuration
```

Measured (historical 79-check nightly baseline): **444 s wall** for 14 designs, 79 checks of which 71 ran (65 per design plus 6 global), 6 worker processes. Sum of per-design check time ranges from 15 s (usb) to 326 s (zipdiv, dominated by the 300 s eqy timeout); the global `L5.fault_injection` takes 315 s and runs in a parallel thread. Output: 916 results = 800 PASS, 65 FAIL, 16 WARN, 35 SKIP, 0 ERROR. (When `opensta_bin`/`openroad_bin`/`klayout_bin`/`openpdk_root` are unset on the host, the 6 additional `L3.oss.*` checks return `SKIP`.)

Exit code (v2): `0` no new FAIL vs the baseline (with no baseline: no FAIL that a known issue does not cover), `1` new FAILs or gating metric regressions, `2` usage or infrastructure error. `--no-gate` skips the comparison and exits 0. Every `run` writes `compare.md` and `compare.json` next to `results.json`.

### 2.3 Slow run (L4 metamorphic + L5 chaos/relocate; real ecc runs)

```bash
# Option A: through the CLI. Slow checks run sequentially in the global thread (about 52 min of per-check time)
ecc-verify run --root <work> --out <out> --slow --layers L4,L5 --designs-dir <ecc-ci-designs> --cpus 8-15

# Option B: standalone runner with two CPU lanes (1,456 s wall in v1)
python3 -m ecc_verify.checks.l4_metamorphic --lanes 8-11,12-15 --designs-dir <ecc-ci-designs> [--only L4.determinism,L5.relocate]
```

The standalone runner puts the base-dependent chain (determinism, resume_equivalence, relocate, signoff_truth) in lane A and the independent cases (set_override, density, freq sweep, chaos) in lane B, then writes `results.json` after each check. Cases live in `<scratch_dir>/slow_<case>/` and are deleted after their metrics are extracted. Each `ecc` call is wrapped in `taskset -c <lane>` with `OMP_NUM_THREADS=ECC_THREADS=4`. Per-check wall seconds (from `verify_slow/results.json`): determinism 600, resume_equivalence 725, freq_monotonic 940, chaos_kill_resume 366, signoff_truth 217, density_monotonic 213, set_override 51, relocate 1.3. The JSON summaries (`L4_*.json`, `L5_*.json`, `base_xtea.json`) go to `<scratch_dir>/slow_summaries/` (standalone runner: `--out`).

### 2.4 pytest

```bash
cd ~ && uv run -q --with pytest pytest <repo>/tests -q                    # unit tests, no workspaces needed
ECC_VERIFY_ROOT=<work> pytest tests -k "xtea and L3.pl"                   # (design x check) matrix on real workspaces
```

`tests/test_ecc_verify.py` has parser unit tests on tiny inline fixtures (DEF with 3 cells and 2 nets, LEF, Liberty, SPEF, Verilog, SDC), synthetic open/overlap detection, mutation tests on the real xtea workspace (delete a route segment must create an OPEN; move a cell must create an overlap; rewire a flop input must change the connectivity signature), and a parametrized `test_check` over 58 design checks x 14 designs (L3.fn and L3.gds excluded as slow). Known real findings are `xfail` there. `tests/test_flowrun.py` unit-tests the L4 helpers. `tests/test_config.py`, `test_compare.py`, `test_baseline.py` and `test_cli_exit_codes.py` cover config precedence and auto-detection, compare verdicts and tolerances, baseline export, exit codes and `--root` layouts.

### 2.5 Outputs (`--out`)

| File | Content |
|---|---|
| `results.json` | `meta` (incl. `provenance` and the effective `config`), `metrics` (per design), `design_config` (per-design `verify.toml`), `checks` (spec per ID), `results` (one row per design x check: status, message, measured, expected, tolerance, evidence, details, seconds), `findings` (deduplicated FAILs with component), `coverage` (matrix, covered, cells, percent) |
| `junit.xml` | one `<testsuite>` per design (plus `<global>`); FAIL becomes `<failure>`, ERROR `<error>`, SKIP `<skipped>`; WARN goes to `<system-out>` |
| `REPORT.md` | findings, coverage matrix, design x check matrix, WARN and SKIP reasons |
| `report.html` | self-contained (no external assets), ECOS orange `#EF6C00` on `#121011` |
| `compare.md`, `compare.json` | verdict of the gate: NEW FAIL, FIXED, STILL-FAILING, metric drift, notes, stale known issues |
| `fault_injection.json` | per-fault verdicts from the 44-fault suite (written by `L5.fault_injection`) |

## 3. Configuration

`ecc-verify.toml` (found via `--config`, `$ECC_VERIFY_CONFIG`, or `./ecc-verify.toml`; `ecc-verify config example` prints a commented template). Relative paths resolve against the file. Precedence: **CLI flag > `ECC_VERIFY_*` env var > file > auto-detection > default**.

| Key | CLI / env | Default / auto-detection |
|---|---|---|
| `[ecc] bin` | `--ecc` / `ECC_VERIFY_ECC` | `ecc` on `PATH`, else `~/.local/bin/ecc` |
| `[paths] pdk_root` | `--pdk-root` / `ECC_VERIFY_PDK_ROOT` | `$CHIPCOMPILER_ICS55_PDK_ROOT`, else `ecc pdk show --json` |
| `[paths] oss_cad_bin` | `--oss-cad-bin` / `ECC_VERIFY_OSS_CAD_BIN` | directory of `yosys` on `PATH`, else newest `~/.local/share/ecc/tools/oss-cad-suite/*/bin` |
| `[paths] opensta_bin` | `--opensta-bin` / `ECC_VERIFY_OPENSTA_BIN` | `sta` on `PATH` (or `oss_docker_image`; `L3.oss.opensta_timing` SKIPs if neither set) |
| `[paths] openroad_bin` | `--openroad-bin` / `ECC_VERIFY_OPENROAD_BIN` | `openroad` on `PATH` (or `oss_docker_image`; `L3.oss.openroad_*` SKIPs if neither set) |
| `[paths] klayout_bin` | `--klayout-bin` / `ECC_VERIFY_KLAYOUT_BIN` | `klayout` on `PATH` (or `oss_docker_image`; `L3.oss.klayout_*` SKIPs if neither set) |
| `[paths] openpdk_root` | `--openpdk-root` / `ECC_VERIFY_OPENPDK_ROOT` | `$ICS55_OPENPDK_ROOT` or `$OPENPDK_ROOT`; **optional unofficial community** KLayout/OpenRCX decks (not official PDK) |
| `[paths] oss_docker_image` | `--oss-docker-image` / `ECC_VERIFY_OSS_DOCKER_IMAGE` | none; container image (e.g. `hpretl/iic-osic-tools:latest`) providing OpenSTA/OpenROAD/KLayout |
| `[paths] designs_dir` | `--designs-dir` / `ECC_VERIFY_DESIGNS_DIR` | none; required by the slow L4/L5 checks |
| `[paths] scratch_dir` | `--scratch-dir` / `ECC_VERIFY_SCRATCH_DIR` | `~/.cache/ecc-verify/runs` |
| `[paths] known_issues`, `baseline` | `--known-issues`, `--baseline` | `./known_issues.toml` else packaged list; no baseline |
| `[run] cpus` | `--cpus` / `ECC_VERIFY_CPUS` | upper half of the machine (taskset list for real `ecc run`) |
| `[run] pin_cpus` | `--pin-cpus` | lower half (taskset list for the Yosys miter) |
| `[run] workers` | `--workers` | 6 |
| `[fault_injection] target, path, scratch` | `--fault-target`, `--fault-script` | `APU`, vendored script, `<scratch_dir>/fi_scratch` |
| `[timeouts]` | | `cli_help 60`, `cli_json 120`, `gds_probe 600`, `yosys_ports 300`, `yosys_miter 900`, `miter_solver 300`, `sim 600`, `full_flow 1500`, `step 600`, `opensta 600`, `openroad 600`, `klayout_drc 1200`, `klayout_lvs 1200` (seconds) |
| `[sampling]` | | `rt_sample_min_cells 10000`, `rt_sample_stride 3`, `fn_max_cells 2000`, `fn_bmc_cycles 25`, `fn_sim_cycles 1000`, `fn_settle_cycles 24` |
| `[compare.tolerances]`, `[compare]` | | section 4 |

The resolved config is stored in `results.json` (`meta.config`) and handed to every worker process. The fault-injection check mutates a copy of the target design's workspace; when the target is not among the selected designs the check SKIPs.

## 4. Baseline, compare and known issues

**Baseline** (`ecc-verify baseline export --results results.json --out <dir>`): `<dir>/<design>.json` (every check verdict and the key metrics), `_global.json` (global checks) and `provenance.json` (ecc `version`/`build_commit` from `ecc version --json`, ecc-ci-designs commit when the root or the designs dir is a git checkout, PDK version and technology-LEF fingerprint, host, cpus, date). Re-exporting a subset of designs refreshes those files and keeps the others. Files are sorted-key JSON, so the directory diffs cleanly in git. `baseline show <dir>` prints it.

**Key metrics** (`metrics.py`): `setup_ws hold_ws setup_tns hold_tns setup_nvp hold_nvp tclk_ns hpwl route_wl via_count core_util die_area std_cell_area instance_area cell_count power_total_uw power_dynamic_uw power_leakage_uw drv_max_tran drv_max_cap drv_max_fanout drc_count lvs_count route_violations runtime.<step> runtime.total memory.<step>`. Unavailable metrics are omitted, never invented; `memory.<step>` is the peak RSS ECC records in `flow.json`.

**Compare** (`ecc-verify compare --baseline <dir> --results results.json`, or `run --baseline`):

| Class | Meaning | Exit |
|---|---|---|
| NEW FAIL | FAIL/ERROR now; not failing in the baseline, not covered by a known issue (new designs and checks included) | 1 |
| FIXED | failing in the baseline, PASS/WARN now | 0 |
| STILL-FAILING | FAIL/ERROR now and in the baseline, or covered by a known issue. Tagged `known` (with BUG ids) or `untracked` | 0 |
| not verified | failed in the baseline, SKIP or not run now | 0 |
| metric regression | a metric moved the wrong way beyond tolerance | 1 |
| metric improvement / non-gating drift | better beyond tolerance; or worse but warn-only | 0 |

Tolerances (`[compare.tolerances]`, defaults; per design in `verify.toml [tolerances]`):

| Metric | Rule |
|---|---|
| `setup_ws`, `hold_ws` | allowed drift `max(ws_abs_ns = 0.05, ws_rel_t = 0.05 x clock period)`; lower is worse |
| `setup_tns`, `hold_tns` | `max(tns_abs_ns = 0.05, tns_rel = 10% of baseline)` |
| `*_nvp`, `drv_*`, `drc_count`, `lvs_count`, `route_violations` | no increase (`nvp_increase`, `drv_increase` = 0) |
| `hpwl`, `route_wl`, `via_count` | +-2 % (`hpwl_rel`, `route_wl_rel`, `via_rel`) |
| `std_cell_area`, `instance_area`, `cell_count` | +-1 % (`area_rel`, `cells_rel`) |
| `die_area`, `core_util` | any move beyond 1 % / 0.01 abs (`util_abs`) is flagged `changed` (these are inputs) |
| `power_*_uw` | +-3 % (`power_rel`) |
| `runtime.*`, `memory.*` | +-25 % **and** more than `runtime_min_s = 5` s / `memory_min_mb = 50` MB |

`ignore_metrics` (glob list) are never compared; `warn_only_metrics` are reported but do not gate. When the baseline was produced on a different host or CPU set, runtime and memory drift is automatically non-gating (a note says so). `tclk_ns` changes are noted because slack drift is then not comparable. Notes also flag a changed ecc build, designs commit or PDK.

**Known issues** (`known_issues.toml`, stdlib `tomllib`; default list shipped in `ecc_verify/data/`):

```toml
[[known]]
check   = "L3.sta.drv_clean"            # id, glob, or list
designs = ["aes", "BM64"]              # optional; global checks use "<global>"
bugs    = ["BUG-02"]
note    = "max_transition / max_cap violations are not gated"
```

A FAIL covered by an entry is `STILL-FAILING (known)`. Entries that matched no failing result are listed as candidates to close. `--no-known-issues` treats every FAIL as new. A design directory may carry `verify.toml` with `[[expected_fail]]` (`check`, `bug`, `note`) and `[tolerances]`; the file is read at run time and stored in `results.json`, so `compare` works without the design tree.

Operating the baseline: after triage of a nightly, `baseline export` into the baselines repo and commit. Fixed bugs show as FIXED and their known-issue entries as stale; remove them. Track `coverage.percent` from `results.json`.

## 5. ecc-ci-designs integration

`--root` is any directory whose children are `<design>/default/home/flow.json`:

- **`nix/scripts/run-designs.sh --out-root <out>`** writes `<out>/<design>/default`, `<out>/<design>.log`, `<out>/summary.txt`. Use `--root <out>`.
- **An ecc-ci-designs checkout run in place** (`ecc run --project <checkout>/<design>`): `--root <checkout>`. Non-design entries (`profiles`, `Readme.md`, designs never run) are skipped; the commit of the checkout is recorded in the provenance.
- **`scripts/run_ci_designs.sh`** does what `run-designs.sh` does (copy each design, run `ecc run --plain`) but pins job `k` to CPUs `[first + k*cpj, first + (k+1)*cpj)` with `taskset` and `OMP_NUM_THREADS=ECC_THREADS=cpj` (ecc collapses under oversubscription), then runs `ecc-verify run --baseline` on the remaining CPUs. It exits 1 when a flow run failed or ecc-verify found a new FAIL, 2 on a usage error.

## 6. Architecture

### 6.1 Package map (lines of code)

| Path | Lines | Role |
|---|---|---|
| `__main__.py` | | CLI (`run`, `compare`, `baseline`, `list`, `config`, `known-issues`), process pool, gate and exit codes |
| `config.py` | | `ecc-verify.toml` + env + CLI + auto-detection (`Config`, `current()`) |
| `metrics.py`, `provenance.py` | | key metrics per design; ecc/designs/PDK/host provenance |
| `baseline.py`, `compare.py`, `known_issues.py` | | baseline export/load; verdicts, drift rules, markdown/JSON; known-issue matching |
| `vendor/fault_inject.py` | | vendored fault-injection harness (44 faults) |
| `runner.py` | 42 | pool worker, kept outside `__main__` so it pickles under spawn |
| `model.py` | 72 | `Status`, `Layer`, `Result`, `CheckSpec`, `STEPS`, `CATEGORIES` |
| `registry.py` | 75 | `@check`, `REGISTRY`, `run_check`, `coverage_matrix`, `load_all_checks` |
| `workspace.py` | 306 | `Workspace` (artifact lookup and cached parsed objects), `discover` |
| `phys.py`, `netgraph.py`, `util.py` | 146, 67, 58 | geometry (core rect, pin centres, HPWL, orientation); driver/load graph, buffer/inverter detection; subprocess, scratch copy, PDK paths |
| `parsers/` | 255 (DEF), 146 (LEF), 100 (Liberty), 54 (SDC), 76 (SPEF), 78 (Verilog), 21 (`_io`) | independent parsers (gz transparent) |
| `checks/l0_cli.py` ... `l3_*.py` | 145 / 247 / 449 / 1,752 across 11 L3 modules | static checks |
| `checks/l4_metamorphic.py`, `l5_chaos.py`, `l5_fault.py` | 466, 102, 82 | slow and fault checks |
| `flowrun.py` | 329 | helpers to drive real `ecc` (cases, taskset, DEF edits, metric extraction) |
| `report.py` | 233 | findings dedup, JUnit, Markdown, HTML, component mapping |
| `tools/gds_probe.py` | 46 | KLayout probe run in a subprocess (`uv run --with klayout`) |
| `tests/` | 317 + 72 | pytest |

### 6.2 Data model (`model.py`)

- `Status`: `PASS`, `FAIL` (real defect in ECC or its artifacts), `WARN` (suspicious or policy), `SKIP` (precondition missing), `ERROR` (the checker crashed; traceback in `details`).
- `Layer`: `L0-cli`, `L1-flow`, `L2-integration`, `L3-physics`, `L4-metamorphic`, `L5-robustness`.
- `Result(status, message, measured, expected, tolerance, evidence[], details{})` with constructors `Result.ok/fail/warn/skip`.
- `CheckSpec(id, layer, steps, category, severity, title, scope)`: `severity` is `critical|major|minor`; `scope` is `design` (once per workspace), `global` (once, receives `ctx`) or `slow` (only with `--slow`, receives `ctx`).

### 6.3 Registry

`@check(id, layer, *, steps, category, severity, title, scope="design")` validates `category in CATEGORIES` and every step in `STEPS`, rejects duplicate IDs, and stores `(CheckSpec, fn)` in `REGISTRY`. `load_all_checks()` imports every module under `ecc_verify.checks`, so dropping a file there is enough. `coverage_matrix(layers=None)` counts checks per (step, category). `run_check` converts exceptions and non-`Result` returns into `ERROR`.

### 6.4 Workspace API

| Member | Meaning |
|---|---|
| `name`, `top_name` | project name; top module (differs for NPC, gcd and picorv32 projects) |
| `project_dir`, `path` | `<root>/<design>` and `<root>/<design>/default` |
| `flow`, `params`, `ecc_toml`, `project_json` | parsed `home/flow.json`, `home/params.toml`, `ecc.toml`, `project.json` |
| `param(key, default)` | top-level then nested-table lookup |
| `step_dir(prefix)`, `step_dirs` | e.g. `step_dir("route")`, `step_dir("Timing optimization")` |
| `output_file(step, suffix)` | first file in `<step>/output` with that suffix |
| `metrics()`, `step_metrics(step)` | merged `qor_metrics.json` (final > gate > trend, later step wins) / one step |
| `def_(step)` | parsed DEF, cached, at most 3 resident per workspace |
| `verilog(step)`, `top(step)` | `{module: Module}` / top module (`ports`, `insts`, `assigns`) |
| `lef()`, `liberty()` | `LefData` (tech + cell LEFs) and `LibData`; process-wide `lru_cache` |
| `spef(corner)`, `spef_files()` | `SpefData` (per-net total cap in fF, resistor count) |
| `sta_corners()` | `{"MAX_125/Cworst": qor_summary.json dict}` |
| `sdc()` | `SdcData(clocks=[(name, period_ns, port)])` |
| `rel(path)` | path relative to the projects root, for `evidence` |

### 6.5 `ctx` (global and slow checks)

`ecc` (binary path), `root`, `workspaces` (list), `designs_dir` (Path or None), `out` (Path), `slow` (bool), `cpus` (taskset list for real ecc runs), `cfg` (the resolved `Config`). Checks read paths and thresholds through `ecc_verify.config.current()` (`std_cell`, `layermap`, `oss_cad_bin`, `timeout(key)`, `sample(key)`, `tool_env()`, `pin(cmd)`) and `ecc_verify.util` (`PDK`, `STD_CELL`, `LAYERMAP`, `OSS_BIN`, `VERIFY_RUNS` resolve lazily).

### 6.6 Parsers

| Parser | Extracts |
|---|---|
| `def_parser` | UNITS, DIEAREA, ROWs, TRACKS, COMPONENTS (master, status, xy, orient), PINS, NETS with ROUTED/NEW segments, vias and patches, SPECIALNETS. Streaming, coordinates in DBU |
| `lef_parser` | tech layers (type, direction, pitch, width), sites, vias; macros with SIZE, CLASS, pins (direction, use, rects) |
| `liberty_parser` | per-cell area, leakage, sequential/ICG flags; per-pin direction, clock flag, max_transition/capacitance |
| `verilog_parser` | structural netlist: module ports, instances (cell, name, pin to net), assigns |
| `spef_parser` | `*D_NET` total cap per net, `*RES` count, units from header |
| `sdc_parser` | `create_clock` period/port, IO-delay presence (also reads ECC-generated Tcl) |

### 6.7 Parallelism

`ProcessPoolExecutor` with `spawn`, at most 6 workers, designs submitted largest first (by route DEF size) for better packing. A single-thread executor runs the global checks concurrently. The worker module is separate from `__main__` so spawned processes can unpickle it; spawn also avoids forking a parent that already runs a thread. Each worker holds its own caches (DEF bounded to 3, LEF/Liberty via `lru_cache`) because an aes-sized DEF (about 23k cells) is heavy.

### 6.8 Tolerance philosophy

Exact quantities (counts, areas, via counts) use 1%; HPWL-type quantities 5%; timing arithmetic 1e-3 ns to 2 ps; the SDC period 1e-6 relative. Orderings that depend on ECC's corners get slack (setup ordering allows 5% of T). Anything that differs by method, not by defect (HPWL from LEF pin-shape centres, integer MHz in reports) is absorbed in the tolerance and stated in the result. Where a legitimate transient exists it is bounded and documented (24 settle cycles in `sim_smoke`).

## 7. Check catalog

Generated from the registry (85 checks). Severity: C critical, M major, m minor. Tolerances are those in the code.

### L0 CLI contract (global)

| ID | Sev | What / method | Tolerance |
|---|---|---|---|
| `L0.help` | M | walks the command tree via `--help`; every (sub)command exits 0 | rc 0 |
| `L0.json` | M | `--json` commands (`status`, `report qor`, `doctor`, `version`, `param list --all`, `signoff check`, `report project`, `diagnose run`) emit parseable JSON on a scratch xtea copy; nonzero rc with a success payload is WARN | parse OK |
| `L0.flags_consistency` | m | `--project`/`--workspace` acceptance per command; inconsistency is WARN | none |
| `L0.version_identity` | m | `ecc version --json` has a commit; a nightly must not report the stable version | none |

### L1 flow integrity (per design)

| ID | Sev | What / method | Tolerance |
|---|---|---|---|
| `L1.flow_all_success` | C | every `flow.json` step Success, none Ongoing | exact |
| `L1.outputs_exist` | C | per-step expectation table (DEF, `.v`, SPEF per corner, GDS/LEF/LIB for Harden) exists and is non-empty | non-empty |
| `L1.step_json_valid` | M | `feature/*.step.json` and `analysis/*.json` parse; `step`/`design` fields right; `schema_version` known | exact |
| `L1.logs_clean` | M | regex scan for Traceback, segfault, core dumped, red ANSI, `ERROR`; allowlist `LOG_ALLOW` (zero-error summaries) | allowlist |
| `L1.timestamps_monotonic` | M | step output mtimes non-decreasing in flow order | 2 s |
| `L1.runtime_recorded` | m | each Success step has runtime and peak memory | exact |
| `L1.lock_state` | M | `project.json` has no `runtime_processes` and no workspace `running` | exact |
| `L1.qor_report_fresh` | M | `qor_report.json` not older than newest step output; `flow_steps` equals `flow.json`; design name matches | 2 s |

### L2 integration and cross-step (per design)

| ID | Sev | What / method | Tolerance |
|---|---|---|---|
| `L2.netlist_def_consistency` | C | DEF COMPONENTS set equals netlist instances at each of 17 physical steps; physical-only cells excluded | exact |
| `L2.connectivity_preserved` | C | per-step signatures: for every sequential input pin and output port, the driving source after buffer/inverter-pair collapse must equal synthesis | exact |
| `L2.no_floating_or_multidriven` | C | exactly one driver per net, at least one load (unloaded driver nets tallied) | exact |
| `L2.cell_masters_exist` | M | every master in LEF and Liberty, or physical-only | exact |
| `L2.metrics_vs_artifacts` | C | ECC metrics vs recomputation from DEF/netlist: instance/std-cell count and area, die/core area, utilization, net and IO counts, `place_hpwl`, `route_wirelength`, `route_via_count`, `cts_buffer_count` | 1% exact, 5% HPWL, 0.011 abs utilization |
| `L2.reports_vs_json` | M | STA `qor_summary.rpt` vs JSON; worst slack in `timing_max_*.rpt` vs `setup.wns` | 1 ps + 1% |
| `L2.qor_vs_raw` | C | `qor_report.json` gate inputs vs min over per-corner `qor_summary.json`, DRC and LVS counts | 1e-3 ns |
| `L2.harden_views_consistent` | M | Harden LEF SIZE equals DEF die; LEF pins equal DEF ports equal LIB pins, directions match | 5 nm |
| `L2.sdc_consistency` | M | SDC period equals 1000/`frequency_mhz`; clock port exists in netlist; QoR `tclk_ns` agrees | 1e-6 rel |

### L3 physics (per design)

| ID | Sev | What / method | Tolerance |
|---|---|---|---|
| `L3.fp.core_in_die` | C | row bbox inside DIEAREA | exact |
| `L3.fp.rows_tile_core` | C | rows tile the core at site height, no overlap, alternating N/FS | exact |
| `L3.fp.io_pins_on_boundary` | M | IO pins on die edge, routing layers, no overlap, on tracks | exact |
| `L3.fp.pdn_present` | M | VDD/VSS special nets with stripes and follow-pin rails on every row | exact |
| `L3.pl.all_placed` | C | every component PLACED or FIXED (7 steps) | exact |
| `L3.pl.on_site_grid` | C | x on site grid, y on a row origin (legalized steps) | exact |
| `L3.pl.orientation_legal` | M | N/FN on N rows, FS/S on FS rows | exact |
| `L3.pl.no_overlap` | C | sweep over LEF bboxes, count and examples | 0 overlaps |
| `L3.pl.in_core` | C | std cells inside the core | exact |
| `L3.pl.density_bins` | M | bin density at most 1.0; WARN if legalization peak exceeds target + 0.35 | 1.0 / target+0.35 |
| `L3.pl.hpwl_recomputed` | M | HPWL from DEF pin-shape centres vs `place_hpwl`; legalization growth sane | 5% |
| `L3.pl.util_growth` | M | std-cell area and utilization across Timing optimization | x1.5, util 0.80 |
| `L3.cts.all_sinks_clocked` | C | BFS from the clock port through buffers/inverters/ICGs reaches every sequential clock pin | exact |
| `L3.cts.fanout_limit` | M | clock buffer fanout vs `cts.max_fanout` from params | exact |
| `L3.cts.depth_balance` | M | min/max buffer depth vs `clock_path_min/max_buffer` metrics | exact |
| `L3.rt.all_nets_routed` | C | every multi-pin signal net has routing | exact |
| `L3.rt.connectivity` | C | union-find over wires, vias, patches and transformed LEF pin shapes; more than one component is an OPEN; every 3rd net sampled at 10k cells or more | 0 opens |
| `L3.rt.shorts` | C | per-layer rectangle overlap of different nets (wire, via, pin, PDN, OBS) on a 2 um bucket grid | 0 |
| `L3.rt.layer_direction` | m | share of length in the non-preferred direction | WARN above 10% |
| `L3.rt.layer_range` | M | no routing outside `route.bottom_layer..top_layer` except pin access | exact |
| `L3.rt.wirelength_recomputed` | M | per-layer length vs last detailed-route iteration in `route.step.json` | 1% (at least 1 um) |
| `L3.rt.via_count` | M | vias per cut layer vs `route.step.json`; via layers adjacent to wire layer | 1% |
| `L3.fill.row_coverage` | M | sum of cell widths per row equals row length after filler | exact |
| `L3.sta.corner_coverage` | C | every configured corner x RC has a summary and reports (13) | exact |
| `L3.sta.setup_ordering` | M | MAX/WCL setup WS at most MIN/ML setup WS | 5% of T |
| `L3.sta.hold_ordering` | m | hold worst at the fast corner (WARN otherwise) | none |
| `L3.sta.ws_nvp_consistency` | M | WS < 0 iff NVP > 0, per corner and path group | exact |
| `L3.sta.frequency_formula` | m | `frequency_mhz` = 1000/(T-WS) | 1% or 1 MHz |
| `L3.sta.drv_clean` | M | `design_statistics` tran/cap/fanout equal 0 and `VIOLATED` rows in `max_*.rpt` match | 0 |
| `L3.sta.unconstrained` | M | no unconstrained endpoints | 0 |
| `L3.sta.hold_margin` | m | hold WS at least 50 ps at every corner (WARN otherwise) | 50 ps |
| `L3.sta.independent_path_check` | M | re-sum stage delays vs arrival; required - arrival equals slack, on `timing_paths.json` | 2 ps |
| `L3.rc.spef_coverage` | C | every routed signal net is in every corner's SPEF | exact |
| `L3.rc.cap_per_um` | M | per-net cap/length (nets of 10 um or more) | 0.05-0.5 fF/um, 2% outliers |
| `L3.rc.corner_ordering` | M | Cworst cap at least Cbest per net, both temperatures | 99% of nets |
| `L3.pw.sum` | M | total = internal + switching + leakage (JSON and report) | 0.5% |
| `L3.pw.positive` | M | every power component positive | exact |
| `L3.pw.leakage_corner` | m | leakage 125C above 25C when several corners exist | none |
| `L3.gds.loads` | C | GDS loads in KLayout; top cell equals the design (Harden and filler) | exact |
| `L3.gds.bbox_matches_die` | M | GDS bbox equals DEF die | 0.01 um |
| `L3.gds.std_cells_resolved` | M | every referenced std cell has geometry; FEOL layers (ACT/NW/POLY) merged | exact |
| `L3.gds.no_zero_area` | m | no zero-area shapes | exact |
| `L3.gds.layers_expected` | m | layer/datatype set is a subset of the PDK layermap | exact |
| `L3.so.antenna_effective` | M | antenna check analysed nets: `signal_net_count > 0` and `pins_with_gate_area > 0` | > 0 |
| `L3.so.lec_passed` | C | post-route LEC reports equivalence on the artifacts actually produced | exact |
| `L3.so.checklist_vs_reality` | M | no checklist item is `pass` while its artifact check fails | exact |
| `L3.fn.eqy_rtl_vs_final` | C | Yosys miter RTL vs final netlist, std cells imported from Liberty, constrained reset, `sat -seq 25 -timeout 300`; designs under 2,000 cells | 25 cycles |
| `L3.fn.sim_smoke` | M | iverilog random stimulus RTL vs gate, 1,000 cycles after reset; X/Z RTL outputs not compared | 24 settle cycles |
| `L3.oss.opensta_timing` | M | Optional OpenSTA (`sta -exit`, local or Docker) setup/hold NLDM+Elmore timing vs ECC `iSTA`; also flags IEEE 1481 unescaped-`$` SPEF syntax errors while cross-checking sanitized timing | `max(0.10 ns, 5% T)` |
| `L3.oss.openroad_antenna` | M | Optional OpenROAD `check_antennas -verbose` on tech/cell LEF + DEF vs ECC `antennaCheck` | exact violation count |
| `L3.oss.openroad_rcx` | M | Optional OpenROAD `extract_parasitics` (unofficial `openpdk_root` RCX rules) vs ECC SPEF total cap | 25% median |
| `L3.oss.klayout_drc` | C | Optional KLayout batch `ics55.drc` (unofficial `openpdk_root` deck) on merged GDS | 0 violations |
| `L3.oss.klayout_antenna` | M | Optional KLayout batch `ics55_antenna.drc` (unofficial `openpdk_root` deck) on merged GDS | 0 violations |
| `L3.oss.klayout_lvs` | C | Optional KLayout batch `ics55.lvs` (unofficial `openpdk_root` deck) GDS vs CDL/SPICE schematic | exact match |

### L4 metamorphic and L5 robustness (real runs, xtea)

| ID | Sev | What / method | Tolerance |
|---|---|---|---|
| `L4.determinism` | M | two identical runs: placement hash, WNS, wirelength, power | bit-identical, else noise band 1% |
| `L4.resume_equivalence` | M | `--to place` then `--resume` vs full run; resume from pending place vs full | metrics equal |
| `L4.freq_monotonic` | C | frequency x0.8/1.0/1.5/3.0: WNS strictly decreases and the setup gate trips at x3.0 | monotonic |
| `L4.density_monotonic` | m | `target_density` 0.3 vs 0.8: bbox and HPWL move as expected (WARN if only one does) | direction |
| `L4.set_override_effective` | M | `--set place.num_threads=2` reaches `config/dreamplace_ecc.json` | exact |
| `L4.signoff_truth` | C | inject (a) deleted route segment, (b) tight clock, (c) overlapping cells; LVS, STA and DRC must detect each | all detected |
| `L5.chaos_kill_resume` | C | SIGTERM during place, then plain `ecc run --resume`; then reconcile + resume must complete | rc 0 |
| `L5.relocate` | M | copied finished project still answers `ecc status` (slow variant on xtea copy) | rc 0 |
| `L5.fault_injection` | C | wraps the vendored `fault_inject.py` `run_suite` on the configured target, default APU (44 faults); diffs FAIL set against `REFERENCE_FAIL`; oracle false positive `C05` is excluded | no new FAIL |
| `L5.relocate_status` | M | static variant: unmodified copy of xtea, `ecc status --json` | rc 0 |

## 8. Coverage model

Counts of registered checks per cell (all layers). `.` = uncovered.

| Step | existence | integrity | cross-step | physics | timing | metamorphic | fault |
|---|---|---|---|---|---|---|---|
| CLI | 1 | 2 | 1 | . | . | . | 3 |
| workspace | 1 | 5 | 2 | . | . | . | 4 |
| Synthesis | 1 | 6 | 1 | . | . | . | . |
| preFloorplan | 1 | 2 | 1 | . | . | . | . |
| macroPlacement | 1 | 2 | 1 | . | . | . | . |
| postFloorplan | 1 | 2 | 1 | 4 | . | . | . |
| preplace | 1 | 2 | 1 | . | . | . | . |
| place | 1 | 2 | 2 | 2 | . | 4 | 1 |
| diff_sizing | 1 | 2 | 1 | 1 | . | . | . |
| CTS | 1 | 3 | 2 | 4 | . | . | . |
| legalization | 1 | 2 | 3 | 7 | . | . | . |
| Timing optimization | 1 | 3 | 4 | 6 | . | . | . |
| route | 1 | 2 | 3 | 12 | . | 2 | . |
| filler | 1 | 10 | 3 | 12 | . | . | . |
| lvs | 1 | 3 | 2 | . | . | 1 | 1 |
| drc | 1 | 2 | 3 | 1 | . | 1 | 1 |
| antennaCheck | 1 | 3 | 1 | 2 | . | . | . |
| postRouteLec | 1 | 3 | . | . | . | . | . |
| RCX | 1 | 2 | 1 | 4 | . | . | . |
| sta | 1 | 2 | 3 | . | 10 | 4 | 1 |
| powerAnalysis | 1 | 2 | 1 | 3 | . | 1 | 1 |
| emir | 1 | 2 | 1 | . | . | . | . |
| Harden | 1 | 8 | 1 | 3 | . | . | . |

**Covered: 95 of 161 cells = 59.0%.** Static run layers only (L0-L3, L5; this is what `REPORT.md` prints): 89 cells = 55.3%. The 6 extra cells come from L4 and are all in the `metamorphic` column: `place`, `route`, `lvs`, `drc`, `sta`, `powerAnalysis`. The `filler` row carries many checks because the final-DEF and GDS checks target it; the `L3.oss.*` oracles newly populate `drc x physics` and `antennaCheck x physics`.

Uncovered areas and what to add next, by expected value:

| Gap | Cells | Proposal |
|---|---|---|
| `physics` for `lvs`, `postRouteLec`, `emir` | 3 | Cross-check EM/IR drop reports and extracted LVS netlists (e.g., via Magic + Netgen alongside KLayout `ics55.lvs`) |
| `physics` for `Synthesis`, `preFloorplan`, `macroPlacement`, `preplace` | 4 | Netlist invariants (no tie-less constants, fanout/slew sanity), macro halo and overlap, IO placement legality before floorplan |
| `timing` for all but `sta` | 20 | Extend OpenSTA (`sta`) checks to intermediate steps (`place`, `CTS`, `Timing optimization`, `RCX`) using zero-wire-load/extracted timing |
| `metamorphic` outside xtea | 14+ | Run the L4 set on gcd_icg and Fstage-scpu; add a seed-perturbation test; rerun-with-same-seed per step |
| `fault` for `route`, `CTS`, `Harden`, `RCX`, `Synthesis` | 5+ | Fault-inject corrupt DEF/SPEF/LIB inputs per step and assert clean errors |
| `existence` for the pseudo rows | partial | none planned |

## 9. Extending

### 9.1 Add a check

Drop a module under `ecc_verify/checks/` (auto-imported). Example: every IO pin of the final DEF must be PLACED or FIXED.

```python
# ecc_verify/checks/l3_example.py
from ecc_verify.model import Layer, Result
from ecc_verify.registry import check

@check("L3.fp.io_pins_placed", Layer.L3, steps=("filler",), category="physics",
       severity="major", title="every IO pin is PLACED or FIXED in the final DEF")
def io_pins_placed(ws):
    d = ws.def_("filler")                           # cached, parsed once per workspace
    if not d.pins:
        return Result.skip("DEF has no PINS section")      # never PASS vacuously
    bad = [p.name for p in d.pins.values() if p.status not in ("PLACED", "FIXED")]
    ev = [f"{ws.rel(ws.step_dir('filler'))}/output"]
    if bad:
        return Result.fail(f"{len(bad)}/{len(d.pins)} pins unplaced (e.g. {bad[:3]})",
                           measured=len(bad), expected=0, tolerance="exact", evidence=ev,
                           details={"pins": bad[:50]})
    return Result.ok(f"{len(d.pins)} pins placed", measured=0, expected=0, tolerance="exact")
```

Then: add a unit test with a tiny inline fixture in `tests/test_ecc_verify.py`, run `python -m ecc_verify list | grep io_pins`, run `--only 'io_pins' --designs xtea`, and confirm the coverage cell count changed. If the check is a known real finding on the current nightly, add its ID to `KNOWN_REAL` in the tests and record the finding in `docs/03_bugs_and_issues.md`. Global and slow checks take `ctx` instead of `ws` and set `scope="global"` or `scope="slow"`.

The example was executed against the xtea workspace (PASS, 260 pins). Pitfall: `Workspace.def_()` keeps at most 3 DEFs per workspace, so do not hold references across many steps.

### 9.2 Add a parser

Create `parsers/<fmt>_parser.py` with a dataclass result and `parse_<fmt>(path)`; use `_io.open_text` / `read_text` (gzip transparent); export it in `parsers/__init__.py`; add an accessor on `Workspace` using `self._memo(key, fn)` (or `_memo_lru` for large objects); add a fixture test in `tests/test_ecc_verify.py`. Keep parsers streaming and independent of ECC code: reusing ECC's own readers would defeat the purpose.

### 9.3 Conventions

| Topic | Rule |
|---|---|
| ID | `L<layer>.<area>.<name>`; area is `fp pl cts rt fill sta rc pw gds so fn` for L3 |
| Evidence | paths relative to the projects root through `ws.rel()`; add `:line` or ` -> json/pointer` when known; one entry per artifact a human should open |
| FAIL | a real defect in ECC or its artifacts, with numbers and examples in the message |
| WARN | suspicious or policy (hold margin, nonzero rc with a success payload); does not fail CI |
| SKIP | a precondition is missing or the check is out of scope (size cap, solver timeout, single corner); always with a reason |
| ERROR | the checker crashed; fix the checker. Zero ERRORs is a CI requirement |
| Tolerance | declared in the check and in `Result.tolerance`; justify non-trivial values in a comment |
| Purity | a check is a pure function of the workspace; scratch output goes under `<scratch_dir>` only; never modify the workspaces |
| Severity | `critical` when a wrong result would invalidate a signoff claim; `minor` for hygiene |

## 10. CI integration recipe

Nightly job: build or install the nightly, run ECC on the CI designs with pinned CPUs (BUG-24: performance collapses under thread oversubscription), run ecc-verify against the stored baseline, publish JUnit and HTML, fail on exit code 1. `scripts/run_ci_designs.sh` does all of it:

```bash
scripts/run_ci_designs.sh --designs-dir ~/ecc-ci-designs --out-root out/e2e --jobs 3 --cpus-per-job 4 \
    --baseline ../ecc-verify-baselines            # exit 0 / 1 / 2 as described in section 4
```

The v1 `ci/gate.py` is obsolete: `compare` implements it (and metric drift, known issues, provenance). The GitHub Actions example is in `ci/github-actions/`. The slow set (L4/L5, about 25 min with two lanes) belongs in the nightly or a weekly job, not in PR gating.

## 11. Known limitations and roadmap

Limitations (from code and `SPEC.md`):

- **Optional OpenSTA / OpenROAD / KLayout OSS oracles (`L3.oss.*`).** When neither local `sta`/`openroad`/`klayout` binaries nor `--oss-docker-image` (e.g., `hpretl/iic-osic-tools:latest`) nor an optional unofficial community KLayout/OpenRCX deck (`--openpdk-root`, `ckdur/icsprout55-openpdk`) are configured, the affected `L3.oss.*` checks return `SKIP` cleanly, while official `icsprout55-pdk` (`--pdk-root`) always remains the sole authoritative foundry reference for `L1`–`L3` checks. When configured via `iic-osic-tools`, `L3.oss.opensta_timing` runs OpenSTA `3.1.0` directly on ECC Liberty/SDC/SPEF/Verilog (catching `iRCX`'s IEEE 1481 unescaped-`$` SPEF syntax defect on `*NAME_MAP` identifiers while verifying setup/hold WNS against `iSTA`) and `L3.oss.openroad_antenna` runs OpenROAD `check_antennas`.
- **Routing connectivity is sampled** (every third net) for designs of 10,000 cells or more (aes, about 23k cells). Shorts use rectangles per layer on a 2 um bucket grid.
- **Functional checks only under 2,000 cells** (`MAX_CELLS`); the miter is bounded to 25 cycles from reset, ICG models are behavioural. Solver timeout of 300 s makes the check SKIP (Fstage-scpu, usb, zipdiv). `sim_smoke` tolerates 24 post-reset X-resolution cycles and ignores X/Z RTL outputs (usb has 4 tolerated transients).
- **L4 uses xtea only**; findings on larger designs are untested. `L4.density_monotonic` is a WARN because core utilization is a floorplan input.
- **HPWL** uses LEF pin-shape centres; ECC's value differs by about 0.3%, hence the 5% tolerance.
- `L5.fault_injection` runs on one configured design (default APU) and SKIPs if it is not selected; `fault_injection` and `relocate_status` write under the scratch dir.
- Metric extraction relies on the `qor_metrics.json` ids of ECC `0.1.0a12`; a renamed id makes that metric disappear from the comparison (visible as fewer compared metrics), not fail it.
- Reference lists in `l5_fault.py` (`REFERENCE_FAIL`, `REFERENCE_GAPS`, oracle false positive `C05`) must be updated when ECC fixes a fault.

Known false-positive fixes that are encoded in the harness: the `L1.logs_clean` allowlist for zero-error summaries; `C05` excluded as an over-strict oracle (ECC prints a clean TOML error and exits 1); `sim_smoke` settle window and X/Z handling; eqy reset constraints and timeout-as-SKIP; `util_growth` threshold at x1.5 to separate xtea (x1.87) from the others (x1.05-1.16); L4 harness fixes after the first slow run (a `place_hpwl` KeyError in density, a `None` path in resume, signoff_truth injection (a) failing to find a deletable segment, determinism second-run failure) recorded in `verify_slow/runner.log` versus the merged `results.json`.

Roadmap, in priority order:

1. (done in v2) exit codes, baseline/compare, known issues, configuration.
2. Trend dashboard from baselines over time.
3. Extend OpenSTA (`L3.oss.opensta_timing`) to intermediate placement/CTS steps and add built-in Magic/Netgen layout extraction + LVS cross-checks alongside KLayout (`ics55.lvs`).
4. Expand built-in DEF-level geometry checks and OpenROAD/KLayout `ics55-openpdk` CI runner presets so `L3.oss.*` runs by default in CI containers.
5. Run L4 on three designs; add seed-perturbation and resume-at-every-step tests.
6. Full-design routing connectivity on large designs via a faster union-find (C or numpy), removing sampling.
7. Per-nightly trend dashboard (coverage, FAIL count, runtimes) built from `results.json`.
