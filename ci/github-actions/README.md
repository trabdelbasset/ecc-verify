# ecc-verify GitHub Actions example

`ecc-verify.yml` is meant to be **copied into the ecc repository** (`.github/workflows/ecc-verify.yml`); it is not active
in this repo.

| Trigger | Designs | Layers | Time budget |
|---|---|---|---|
| pull request | `xtea gcd_icg Fstage-scpu usb zipdiv` | L0-L3 (static) | 90 min |
| nightly (`schedule`) / manual with `full` | all 14 CI designs | static + `--slow` (L4/L5, real ecc runs) | 300 min |

What the job does: checks out ecc-verify (pinned tag), ecc-ci-designs and a baselines repo, runs
`scripts/run_ci_designs.sh` (copies each design, runs `ecc run` pinned to disjoint CPU sets, then `ecc-verify run
--baseline ...`), uploads `junit.xml`, `report.html`, `REPORT.md`, `compare.md`/`compare.json`, publishes the JUnit
report, writes `compare.md` to the job summary, and **fails the job on exit code 1** (new FAIL or metric regression).
Exit code 2 (usage/infrastructure error) also fails the job, with a different message.

Things to adapt (marked `ADAPT` in the file): repository names, the `paths:` filter, the runner labels (use a dedicated
machine with at least 16 cores: ecc collapses under thread oversubscription), and the step that builds or installs the
`ecc` binary under test.

Baselines live in a plain repo (`ecc-verify baseline export --results results.json --out <dir>`). Update them after a
reviewed, green nightly: the `propose-baseline` job uploads a ready-to-commit directory as an artifact. Known failures
(ECC bugs) are tracked in `known_issues.toml` and never turn the job red; a FAIL that is not in the baseline and not in
that file does.
