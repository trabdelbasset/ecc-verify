# ecc-verify

Independent artifact-recomputing verification harness for `ecc` RTL-to-GDS workspaces and PR regression gating.

Requires Python 3.11+ (stdlib only for core checks). Optional tools: `yosys`/`iverilog` (`L3.fn`), `klayout` (`L3.gds`), and `sta`/`openroad`/`klayout -b` locally or via `--oss-docker-image hpretl/iic-osic-tools:latest` (`L3.oss.*`, checked against official `icsprout55-pdk` via `--pdk-root` or optional `--openpdk-root`).

## Usage

```bash
pip install -e .
ecc-verify run --root path/to/work --out out/verify
ecc-verify baseline export --results out/verify/results.json --out ../ecc-verify-baselines
ecc-verify run --root path/to/work --out out/verify --baseline ../ecc-verify-baselines
ecc-verify compare --baseline ../ecc-verify-baselines --results out/verify/results.json
```

- Exit codes: `0` pass, `1` new FAIL or metric regression, `2` usage/config/baseline error.
- Config precedence: CLI flag > `ECC_VERIFY_*` env > `ecc-verify.toml` (`ecc-verify config example`) > auto-detect.
- Details: [`docs/guide.md`](docs/guide.md) | CI workflow: [`ci/github-actions/ecc-verify.yml`](ci/github-actions/ecc-verify.yml).

## Layers (85 checks: 79 core + 6 `L3.oss.*`)

| Layer | Scope | Coverage |
|---|---|---|
| L0 CLI | global | `--help`, `--json`, flags, version |
| L1 flow | per design | step status, outputs, JSON, logs, mtimes, locks |
| L2 integration | per design | DEF/netlist/masters cross-checks, recomputed metrics vs JSON, SDC |
| L3 physics | per design | floorplan, placement, CTS, route, STA, RC, power, GDS, signoff, `L3.fn`, `L3.oss.*` |
| L4 metamorphic | `--slow` | live `ecc` determinism, resume, monotonicity, signoff truth |
| L5 robustness | global | 44-fault injection, relocation, kill/resume (`--slow`) |

## Tests

```bash
uv run --with pytest pytest tests -q
ECC_VERIFY_ROOT=path/to/work uv run --with pytest pytest tests -q
```
