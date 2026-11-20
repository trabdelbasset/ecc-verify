# ecc-verify: verification harness for ECC over the CI design suite

## Goal

Detect **flow, integration and physics bugs** (timing, placement, CTS, routing, RC, power, GDS, functional) in ECC by checking every artifact of every CI design against **independent recomputation and invariants**, not against ECC's own self-reported metrics.

The harness must produce:
- a pass/fail matrix;
- a **coverage model** (step × category);
- machine-readable output (JSON + JUnit);
- a human report (Markdown + HTML).

## Inputs

- **Finished workspaces:** `<work>/<design>/default/`. There are 14 CI designs, all completed with the `rtl2gds` preset. Ignore `fi_*` dirs. Each project root has `ecc.toml`, `project.json` and `default/`.
- **Step dirs** inside a workspace: `Synthesis_yosys`, `preFloorplan_ecc`, `macroPlacement_dreamplace`, `postFloorplan_ecc`, `preplace_sizer`, `place_dreamplace`, `diff_sizing_dreamplace`, `CTS_ecc`, `legalization_dreamplace`, `timing_optimization_sizer`, `route_ecc`, `filler_ecc`, `lvs_ecc`, `drc_ecc`, `antennaCheck_ecc`, `postRouteLec_kepler_formal`, `RCX_ecc`, `sta_ecc`, `powerAnalysis_ecc`, `emir_ecc`, `Harden_ecc` (plus numbered snake-case equivalents).
- **Contents of each step dir:**
  - `output/` (`*.def.gz`, `*.v.gz`, `*.spef`, `*.gds`, `*.lef`, `*.lib`, `*.png`, `geometry/`)
  - `feature/` (`*.step.json`, `*.db.json`, maps)
  - `analysis/` (`qor_metrics.json`, `qor_summary.json`)
  - `report/`
  - `log/`
- **Workspace files:** `home/flow.json`, `home/qor_report.json`, `home/checklist.json`, `home/params.toml`, `config/*.json` (e.g. `db_ecc.json` lists the tech LEF, cell LEFs and Liberty paths), `origin/` (SDC etc.).
- **STA outputs:** `sta_ecc/feature/<corner>/<rc>/qor_summary.json` (`summary.{setup,hold}.{wns,tns,nvp}`, `path_groups[]`, `design_statistics.{tran,cap,fanout}`) and `sta_ecc/report/<corner>/<rc>/{max_transition,max_capacitance,max_fanout,timing_*}.rpt`.
- **PDK:** `<pdk_root>` containing LEF/LIB/CDL/Verilog. The tech LEF is `prtech/techLEF/N551P6M_ecos.lef`; the std cells sit under `IP/STD_cell/`.
- **CI designs (RTL, SDC, ecc.toml):** `<ecc-ci-designs>/`.
- **ecc binary:** `ecc` on `PATH` (or `--ecc PATH`).
- **Independent tools:**
  - KLayout Python module (`klayout.db`) or `uv run --with klayout`.
  - Yosys, eqy, sby, iverilog and verilator (`<oss-cad-suite>/bin` or on `PATH`).
  - Optional `L3.oss.*` external open-source cross-tool oracles (`l3_oss_oracles.py`) supporting OpenSTA (`sta`), OpenROAD (`check_antennas` and OpenRCX), and KLayout batch CLI (`klayout -b`), locally or via `--oss-docker-image` (`hpretl/iic-osic-tools:latest`), alongside optional unofficial community KLayout/OpenRCX decks (`ckdur/icsprout55-openpdk`) while keeping official `icsprout55-pdk` (`pv/DRC/`) as the sole foundry authority.

## Architecture

```
ecc_verify/
  ecc_verify/
    __main__.py          # CLI: python -m ecc_verify run --root <work> [--designs a,b] [--layers L0..L5] [--slow] --out <dir>
    model.py             # Check, Result(status PASS|FAIL|WARN|SKIP|ERROR, measured, expected, tolerance, evidence[], message)
    registry.py          # @check(id, layer, step, category, severity, needs=[...]) decorator; coverage matrix builder
    workspace.py         # locate artifacts per step; lazy cached parsed objects
    parsers/
      def_parser.py      # streaming DEF: UNITS, DIEAREA, ROWS, TRACKS, COMPONENTS, PINS, NETS (+ROUTED/NEW segments, vias), SPECIALNETS (gz ok)
      lef_parser.py      # tech: layers (type, direction, pitch, width), sites, vias; cells: SIZE, CLASS, PINS(direction, use, rects)
      liberty_parser.py  # light: cell area, pin direction, is_sequential (ff/latch), max_transition/max_capacitance, default_max_*, leakage
      verilog_parser.py  # structural netlist: module ports, instances (cell, name, pin->net)
      spef_parser.py     # *D_NET total cap per net, *RES count; units from header
      sdc_parser.py      # create_clock period/port, set_input/output_delay presence
    checks/
      l0_cli.py  l1_flow.py  l2_integration.py
      l3_floorplan.py  l3_placement.py  l3_cts.py  l3_routing.py  l3_filler.py
      l3_timing.py  l3_rc.py  l3_power.py  l3_gds.py  l3_signoff.py  l3_functional.py
      l4_metamorphic.py  l5_robustness.py
    report.py            # JSON, JUnit XML, Markdown + standalone HTML (ECOS orange), coverage heatmap
  tests/test_ecc_verify.py  # pytest entry: parametrize over (design, check) so `pytest -k` works; also unit tests for parsers on tiny fixtures
  README.md
```

Rules:
- Each check is a pure function `(ws: Workspace) -> Result`.
- A crash inside a check is reported as `ERROR` with the traceback; it never aborts the run.
- Checks declare `needs` (artifacts). If one is missing, the result is `SKIP` with a reason. A missing required artifact is `FAIL` in L1.
- Parallelize across designs with `concurrent.futures` (process pool, at most 6 workers). Parsing a DEF can be heavy: aes is about 23k cells. Cache parsed objects per workspace.
- Tolerances are explicit in each check and printed in the report.

## Check catalog

Implement **at least** these; add more where cheap. The ID prefix is the layer.

### L0: CLI contract (once, not per design)

- **L0.help**: every command and subcommand `--help` exits 0. Discover commands by parsing `ecc --help` recursively.
- **L0.json**: commands documenting `--json` emit parseable JSON on a finished workspace: `status`, `report qor`, `doctor`, `version`, `param list --all`, `signoff check`, `report project`, `diagnose run`. This also records exit codes. Known oddity: `signoff check` returns rc 7 with valid JSON; report it as WARN, "nonzero rc with success payload".
- **L0.flags_consistency**: `--project`/`--workspace` acceptance per command. Report the inconsistent ones as WARN.
- **L0.version_identity**: `ecc version --json` `build_commit` and version string. WARN if a nightly reports the stable version.

### L1: Flow integrity (per design)

- **L1.flow_all_success**: every step in `flow.json` is Success, and none is stuck in Ongoing.
- **L1.outputs_exist**: each step's expected outputs exist and are non-empty: DEF for physical steps, a netlist `.v` for netlist-changing steps, SPEF for each RC corner, GDS/LEF/LIB for Harden. Build the expectation table from the actual layout of the 14 workspaces plus step semantics.
- **L1.step_json_valid**: every `feature/*.step.json` and `analysis/*.json` parses, has the correct `step`/`design` fields, and `schema_version` is known.
- **L1.logs_clean**: no `Traceback`, `Segmentation fault`, `core dumped`, `ERROR`, or `[1;31m` error-level lines in step logs. Report the matched lines and allow a documented allowlist.
- **L1.timestamps_monotonic**: step output mtimes follow flow order. Out-of-order artifacts mean stale data.
- **L1.runtime_recorded**: every Success step has a runtime.
- **L1.lock_state**: no stale `runtime_processes` entry in `project.json` for a finished workspace.
- **L1.qor_report_fresh**: `home/qor_report.json` is newer than the last step outputs, and its `flow_steps` match `flow.json`.

### L2: Integration and cross-step consistency

- **L2.netlist_def_consistency**: for each physical step, the DEF `COMPONENTS` instance set equals the `.v` instance set, after excluding physical-only cells (FILL, TAP, ENDCAP, ANT, DCAP; derive these from the LEF CLASS or the liberty absence).
- **L2.connectivity_preserved**: logical connectivity from the post-synthesis netlist is preserved through every later step, modulo buffer and inverter-pair insertion and sizing. Method: collapse buffer trees and compare register-to-register and port-to-register connectivity at the level of sequential elements and primary ports. If a full collapse is too hard, at least check that the sets of sequential instances and primary ports are identical across steps (renames allowed only with a reported mapping) and that no net becomes driverless or multi-driven.
- **L2.no_floating_or_multidriven**: in every netlist, every net has exactly one driver (output or inout pin, or an input port) and at least one load, except for documented unused nets.
- **L2.cell_masters_exist**: every instance master exists in the LEF and in the Liberty (or is physical-only).
- **L2.metrics_vs_artifacts**: ECC-reported metrics must agree with independent recomputation:
  - instance count (`instance_count`) vs DEF;
  - `core_area` and `die_area` vs DEF `DIEAREA` and rows;
  - `core_utilization` vs Σ cell area / core area;
  - `place_hpwl` vs recomputed HPWL (DBU→µm);
  - `route_wirelength` vs recomputed routed length;
  - `route_via_count` vs counted vias;
  - `cts_buffer_count` vs clock-tree buffers counted in the netlist;
  - `drc_count` vs the DRC report.
  - Tolerances are 1% for exact quantities and 5% for HPWL-type ones.
- **L2.reports_vs_json**: STA `qor_summary.rpt` vs `qor_summary.json`; the worst slack in `timing_max_*.rpt` vs `summary.setup.wns`; the NVP count vs the number of negative-slack endpoints in the reports.
- **L2.qor_vs_raw**: `home/qor_report.json` gate inputs vs the raw artifacts. Setup WS must equal the minimum over all per-corner `qor_summary.json`; this catches the known T01 bug class. DRC and LVS counts must equal the reports.
- **L2.harden_views_consistent**: Harden LEF `SIZE` equals the DEF die; LEF pins equal the top-level ports equal the LIB pins; LIB pin directions match.
- **L2.sdc_consistency**: the SDC clock period equals 1000 / `frequency_mhz` from `ecc.toml`; the clock port exists in the netlist.

### L3: Physics

**Floorplan**
- **L3.fp.core_in_die**: the core lies inside the die.
- **L3.fp.rows_tile_core**: rows tile the core with the site height, no overlaps, alternating orientation N/FS.
- **L3.fp.io_pins_on_boundary**: IO pins sit on the die edge, on allowed layers, with no two pins overlapping, on tracks.
- **L3.fp.pdn_present**: VDD and VSS special nets exist with stripes or rings on the expected layers, and every row has follow-pin rails.

**Placement** (on the place, legalization and final DEFs)
- **L3.pl.all_placed**: every component is PLACED or FIXED.
- **L3.pl.on_site_grid**: the x coordinate is on the site grid and the y coordinate on a row origin.
- **L3.pl.orientation_legal**: the orientation is legal for the row.
- **L3.pl.no_overlap**: no two cells overlap (sweep line over bboxes from LEF `SIZE`). Report the count and examples.
- **L3.pl.in_core**: every standard cell lies inside the core.
- **L3.pl.density_bins**: bin density is at most 1.0, and peak density is reported vs `target_density`.
- **L3.pl.hpwl_recomputed**: recomputed HPWL against the reported value.

**CTS**
- **L3.cts.all_sinks_clocked**: every sequential cell's clock pin is reached from the clock port through buffers and inverters only (or ICGs).
- **L3.cts.fanout_limit**: no clock buffer output exceeds `cts.max_fanout` from the params.
- **L3.cts.depth_balance**: the min and max buffer depth to sinks are recomputed and compared with the `clock_path_min/max_buffer` metrics.

**Routing** (on the route DEF)
- **L3.rt.all_nets_routed**: every signal net with two or more pins has routing.
- **L3.rt.connectivity**: build a graph from wire segments (endpoints and overlaps), vias and pin shapes (from the LEF pin rects transformed by component placement and orientation), and union-find per net. Each multi-pin net must form one component; anything else is an **OPEN**. Sample if too slow, but run fully on designs under 10k cells.
- **L3.rt.shorts**: same-layer wire rectangles of different nets must not overlap (sweep line).
- **L3.rt.layer_direction**: compute the fraction of length on each layer's non-preferred direction and WARN above 10%.
- **L3.rt.wirelength_recomputed** and **L3.rt.via_count**.
- **L3.rt.layer_range**: no routing outside `route.bottom_layer`..`top_layer` from the params (e.g. MET2..MET5) except pin access.

**Filler**
- **L3.fill.row_coverage**: after filler, every row is fully covered: Σ widths of cells in the row equals the row length (gaps are FAIL and overlaps already covered).

**Timing**
- **L3.sta.corner_coverage**: every expected corner and RC combination has a summary. That is 13 combinations; derive the expectation from `config/sta*.json`.
- **L3.sta.setup_ordering**: the slow corner (MAX/WCL, 125 °C) setup WS ≤ the fast corner's (MIN) setup WS. FAIL if inverted by more than 5% of T.
- **L3.sta.hold_ordering**: hold is typically worst at the fast corner; WARN if not.
- **L3.sta.ws_nvp_consistency**: WS < 0 ⇔ NVP > 0, per corner and per path group.
- **L3.sta.frequency_formula**: `frequency_mhz` = 1000 / (T − WS) within 1%.
- **L3.sta.drv_clean**: `design_statistics.tran/cap/fanout` == 0, and the counts of `VIOLATED` rows in `max_*.rpt` match. This is expected to FAIL on 7 designs; that is a real finding.
- **L3.sta.unconstrained**: no unconstrained endpoints, if reported.
- **L3.sta.hold_margin**: WARN if hold WS < 50 ps at any corner.
- **L3.sta.independent_path_check**: take the worst setup path from `timing_max_reg2reg.rpt`. Re-sum its incremental delays and compare with the arrival time; check that required − arrival = slack. This is a report-arithmetic sanity check.

**RC**
- **L3.rc.spef_coverage**: every routed signal net appears in every corner's SPEF.
- **L3.rc.cap_per_um**: per-net total cap / routed length lies within a plausible band (0.05–0.5 fF/µm). Report outliers.
- **L3.rc.corner_ordering**: the Cworst total cap is at least the Cbest total cap, per net, for 99% or more of nets.

**Power**
- **L3.pw.sum**: total = internal + switching + leakage, within 0.5%.
- **L3.pw.positive**: every component is positive.
- **L3.pw.leakage_corner**: if several corners are present, leakage at 125 °C is above leakage at 25 °C.

**GDS** (KLayout)
- **L3.gds.loads**: the GDS loads, and its top cell name equals the design.
- **L3.gds.bbox_matches_die**: the bbox equals the DEF die.
- **L3.gds.std_cells_resolved**: every referenced cell has geometry, so no empty or ghost cells. This checks whether std-cell GDS is merged.
- **L3.gds.no_zero_area**: no zero-area shapes.
- **L3.gds.layers_expected**: the layer and datatype set is a subset of the PDK layermap (`techfile/*.layermap`).

**Signoff integrity**
- **L3.so.antenna_effective**: the antenna check actually ran: `signal_net_count` > 0 and `pins_with_gate_area` > 0. FAIL when vacuous, which is the known finding.
- **L3.so.lec_passed**: post-route LEC reports equivalence.
- **L3.so.checklist_vs_reality**: no checklist item is `pass` while the underlying artifact check fails.

**Functional (independent)**
- **L3.fn.eqy_rtl_vs_final**: for designs under 2k cells (xtea, gcd_icg, Fstage-scpu, usb, zipdiv), run Yosys `eqy` (or a `yosys equiv_*` flow) between the RTL and the final Harden/route netlist, using the PDK std-cell verilog models from `IP/STD_cell/*/verilog`. This independently cross-checks kepler-formal.
- **L3.fn.sim_smoke**: optional. Simulate with iverilog a random-stimulus comparison of RTL vs gate netlist for the same small designs, for 1000 cycles after reset. Generate the testbench from the port list; drive inputs randomly, compare outputs after reset. Mark it SKIP if the module has inout or complex interfaces.

### L4: Metamorphic and differential flow tests (`--slow`; real ecc runs)

Use small designs (xtea, gcd_icg, Fstage-scpu). Copy each to `~/ecos-work/verify_runs/<case>/`. Pin CPUs with `taskset` (use cores 8-15 only). Run with `ECC_THREADS` and `OMP_NUM_THREADS` at 4.

- **L4.determinism**: run the same project twice. Final DEF instance placements, routing length, WS and power must be identical, or differ only within a documented noise band. Report the diffs.
- **L4.resume_equivalence**: run to place, then `--resume` to the end; compare with a full run (same metrics within the noise band).
- **L4.freq_monotonic**: frequency × {0.8, 1.0, 1.5, 3.0}. WS must decrease monotonically, and at a high enough frequency the setup gate must FAIL. The latter checks that timing violations are detected.
- **L4.density_monotonic**: `target_density` low vs high. Utilization and density must move in the expected direction.
- **L4.set_override_effective**: `--set place.num_threads=2` must appear in `config/dreamplace_ecc.json`. This is known to fail (O-3).
- **L4.signoff_truth**: inject known errors and assert detection:
  - (a) edit the routed DEF to delete one routed segment of a net, then run `ecc run --only drc --force` and `--only lvs --force` (or the lvs step), and expect an open or LVS mismatch to be reported;
  - (b) shrink the SDC period on a finished run and re-run sta only, and expect setup FAIL;
  - (c) move one cell to overlap another, then run DRC, and expect a violation.
  - If a step cannot re-run on edited inputs through the CLI, report SKIP with the reason.

### L5: Robustness

- **L5.fault_injection**: import and run `ecc_verify/vendor/fault_inject.py` (`run_suite`) on one workspace. Fold its results in.
- **L5.chaos_kill_resume** (`--slow`): start `ecc run` on a fresh small design, SIGTERM it during placement, then call `ecc run --resume` without reconcile. The expected PASS is that resume works. It currently FAILs with `workspace_running`, the known O-1 bug. Then verify that `ecc process reconcile` followed by resume completes.
- **L5.relocate**: copy a finished project to a new path and run `ecc status`. Expected PASS; currently `manifest_invalid` (O-2).

## Coverage model

- **Rows:** the 21 flow steps plus "workspace/QoR" and "CLI".
- **Columns:** `existence`, `integrity`, `cross-step`, `physics`, `timing`, `metamorphic`, `fault`.
- A cell is covered if at least one implemented check targets it.
- The report prints this matrix as a colored grid with the count of checks per cell and the overall percentage.
- **Results matrix:** design × check, colored PASS/FAIL/WARN/SKIP/ERROR.
- The summary lists distinct FAIL findings, deduplicated across designs, with severity, affected designs, evidence (file path plus a line or values), and a probable ECC component.

## Output

`--out` writes:
- `results.json`
- `junit.xml`
- `REPORT.md`
- `report.html`: self-contained, ECOS orange `#EF6C00` on `#121011`, with tables and coverage heatmap; no external assets.

## Done means

1. Parser unit tests pass on tiny inline fixtures (DEF with 3 cells and 2 nets with routing; LEF snippet; Liberty snippet; SPEF snippet).
2. `ecc-verify run --root <work> --out <out>` completes over all 14 designs for L0–L3 and L5 fault injection in reasonable time (under ~30 min). Report the wall time.
3. Every FAIL is *real*: manually verify a sample of each distinct FAIL by inspecting the evidence, and fix false positives in the checker (tolerances, parsing). Document any remaining uncertainty.
4. The README documents how to add a check, the layers, the coverage model, and the known-real failures.

Do not modify the 14 workspaces. Write only inside the verification output and `<scratch_dir>` directories.
