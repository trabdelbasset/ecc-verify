# Changelog

## 2.0.0

Standalone project, starting from the `ecc_eval/ecc_verify` v1 harness (79 checks unchanged).

### Added
- `ecc-verify.toml` configuration with CLI flags and `ECC_VERIFY_*` environment overrides and auto-detection (ecc on PATH,
  PDK from `$CHIPCOMPILER_ICS55_PDK_ROOT` or `ecc pdk show --json`, oss-cad-suite from `yosys` on PATH). Configurable:
  ecc binary, PDK root, oss-cad-suite bin, designs dir, scratch dir, cpus / pin_cpus, workers, fault-injection target and
  script, timeouts, sampling thresholds, compare tolerances. `ecc-verify config show|example`.
- `fault_inject.py` vendored into the package (`ecc_verify/vendor/`), so the fault-injection check is self-contained.
- Exit codes: 0 no new FAIL, 1 new FAIL or gating regression, 2 usage/infrastructure error (v1 always exited 0).
- Baseline: `ecc-verify baseline export|show`; `run` and `compare` write `compare.md` / `compare.json` with NEW FAIL, FIXED,
  STILL-FAILING and metric drift (per-metric tolerances); key metrics and provenance are stored in `results.json`.
- `known_issues.toml` (stdlib `tomllib`) shipped with the package, seeded from the ECC bug register; per-design
  `verify.toml` (expected-fail checks, tolerance overrides).
- `--root` documented and tested for `run-designs.sh` output and ecc-ci-designs checkouts run in place;
  `scripts/run_ci_designs.sh`.
- `ci/github-actions/ecc-verify.yml` example workflow.
- `pyproject.toml` (`ecc-verify` console script, optional `gds` extra), tests for config, compare, baseline, exit codes.

### Changed
- All paths (PDK, std-cell dir, oss-cad-suite, scratch dir, designs dir, summary dir) come from the config; nothing is
  hard-coded to a user's home directory. Default scratch dir is `~/.cache/ecc-verify/runs`.
- `--cpus` defaults to the upper half of the machine and `--pin-cpus` to the lower half (v1: 8-15 and 0-7).
- Fault injection now SKIPs when its target design is not among the selected designs (v1 silently used the first one).
- The (design x check) pytest matrix needs `ECC_VERIFY_ROOT`; without it the matrix is skipped.
