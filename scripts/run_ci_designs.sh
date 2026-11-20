#!/usr/bin/env bash
# Run the ecc-ci-designs through ecc (like ecc's nix/scripts/run-designs.sh) with pinned, disjoint CPU sets per job,
# then run `ecc-verify run` and gate against a baseline.
#
# Layout produced under --out-root (identical to run-designs.sh, so both are valid ecc-verify --root values):
#   <out-root>/<design>/default/...   copied project with the finished flow
#   <out-root>/<design>.log           ecc run log
#   <out-root>/summary.txt            PASS/FAIL per flow run
#   <out-root>/verify/                results.json, junit.xml, REPORT.md, report.html, compare.md, compare.json
#
# Usage:
#   scripts/run_ci_designs.sh --designs-dir DIR [--ecc BIN] [--out-root DIR] [--pdk-root DIR]
#                             [--design NAME ...] [--jobs N] [--cpus-per-job N] [--first-cpu N]
#                             [--baseline DIR] [--slow] [--layers L0,L1,..] [-- extra ecc-verify run args]
#
# Each job runs `taskset -c <its CPUs> ecc run ...` with OMP_NUM_THREADS=ECC_THREADS=<cpus-per-job>; job slot k owns
# CPUs [first-cpu + k*cpus-per-job, first-cpu + (k+1)*cpus-per-job). ecc performance collapses under thread
# oversubscription, hence the disjoint sets. ecc-verify itself runs on the CPUs below --first-cpu (default: the rest).
#
# Exit: 0 = flows finished and no new FAIL vs baseline; 1 = new FAIL/regression or a flow run failed; 2 = usage error.
set -uo pipefail

usage() { sed -n '2,24p' "$0"; exit 2; }

ecc="$(command -v ecc || true)"; designs_dir=""; out_root="ci-artifacts/e2e"; pdk_root="${CHIPCOMPILER_ICS55_PDK_ROOT:-}"
jobs=2; cpj=4; first_cpu=""; baseline=""; slow=""; layers=""
declare -a designs=() extra=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --ecc) ecc="${2:?}"; shift 2 ;;
    --designs-dir) designs_dir="${2:?}"; shift 2 ;;
    --design) designs+=("${2:?}"); shift 2 ;;
    --jobs) jobs="${2:?}"; shift 2 ;;
    --cpus-per-job) cpj="${2:?}"; shift 2 ;;
    --first-cpu) first_cpu="${2:?}"; shift 2 ;;
    --out-root) out_root="${2:?}"; shift 2 ;;
    --pdk-root) pdk_root="${2:?}"; shift 2 ;;
    --baseline) baseline="${2:?}"; shift 2 ;;
    --layers) layers="${2:?}"; shift 2 ;;
    --slow) slow="--slow"; shift ;;
    -h | --help) usage ;;
    --) shift; extra=("$@"); break ;;
    *) echo "unknown arg: $1" >&2; usage ;;
  esac
done

[[ -n "$ecc" && -x "$ecc" ]] || { echo "error: ecc binary not found (use --ecc)" >&2; exit 2; }
[[ -n "$designs_dir" && -d "$designs_dir" ]] || { echo "error: --designs-dir must be a directory" >&2; exit 2; }
mkdir -p "$out_root"; out_root="$(cd "$out_root" && pwd)"; designs_dir="$(cd "$designs_dir" && pwd)"
ecc="$(cd "$(dirname "$ecc")" && pwd)/$(basename "$ecc")"
if [[ -z "$pdk_root" ]]; then
  pdk_root="$("$ecc" pdk show --json 2>/dev/null | python3 -c 'import json,sys
for r in json.load(sys.stdin):
    if r.get("root"): print(r["root"]); break' 2>/dev/null || true)"
fi
[[ -n "$pdk_root" && -d "$pdk_root" ]] || { echo "error: PDK root not found; pass --pdk-root or set CHIPCOMPILER_ICS55_PDK_ROOT" >&2; exit 2; }

ncpu="$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 2)"
if [[ -z "$first_cpu" ]]; then first_cpu=$(( ncpu / 2 )); fi
need=$(( first_cpu + jobs * cpj ))
if (( need > ncpu )); then
  echo "error: $jobs jobs x $cpj CPUs from CPU $first_cpu needs $need CPUs, machine has $ncpu (lower --jobs/--cpus-per-job)" >&2; exit 2
fi
have_taskset=1; command -v taskset >/dev/null || { have_taskset=0; echo "warning: taskset not found, jobs are not pinned" >&2; }

if [[ ${#designs[@]} -eq 0 ]]; then
  while IFS= read -r path; do designs+=("$(basename "$(dirname "$path")")"); done \
    < <(find "$designs_dir" -mindepth 2 -maxdepth 2 -name ecc.toml | sort)
fi
[[ ${#designs[@]} -gt 0 ]] || { echo "error: no designs (no */ecc.toml under $designs_dir)" >&2; exit 2; }

run_one() {  # name slot
  local name="$1" slot="$2" src="$designs_dir/$1" dst="$out_root/$1" log="$out_root/$1.log"
  local lo=$(( first_cpu + slot * cpj )) hi=$(( first_cpu + (slot + 1) * cpj - 1 ))
  [[ -f "$src/ecc.toml" ]] || { echo "FAIL $name (missing $src/ecc.toml)"; return 1; }
  rm -rf "$dst"; cp -a "$src" "$dst"
  local pin=(); (( have_taskset )) && pin=(taskset -c "$lo-$hi")
  if CHIPCOMPILER_ICS55_PDK_ROOT="$pdk_root" PYTHONUNBUFFERED=1 OMP_NUM_THREADS="$cpj" ECC_THREADS="$cpj" \
      "${pin[@]}" "$ecc" run --project "$dst" --workspace default --plain >"$log" 2>&1 \
      && [[ -f "$dst/default/home/flow.json" ]]; then
    echo "PASS $name (cpus $lo-$hi)"
  else
    echo "FAIL $name (cpus $lo-$hi, log: $log)"; return 1
  fi
}

declare -A pid_slot=() pid_name=()
free=(); for ((i = 0; i < jobs; i++)); do free+=("$i"); done
: >"$out_root/summary.txt"; flow_fail=0
reap() {  # collect finished jobs, free their slots
  local p
  for p in "${!pid_slot[@]}"; do
    if ! kill -0 "$p" 2>/dev/null; then
      wait "$p" || flow_fail=1
      free+=("${pid_slot[$p]}"); unset "pid_slot[$p]" "pid_name[$p]"
    fi
  done
}
for d in "${designs[@]}"; do
  while ((${#free[@]} == 0)); do reap; ((${#free[@]} == 0)) && sleep 2; done
  slot="${free[0]}"; free=("${free[@]:1}")
  ( run_one "$d" "$slot" ) >>"$out_root/summary.txt" 2>&1 &
  pid_slot[$!]="$slot"; pid_name[$!]="$d"
done
while ((${#pid_slot[@]} > 0)); do reap; ((${#pid_slot[@]} > 0)) && sleep 2; done
cat "$out_root/summary.txt"
grep -q '^FAIL' "$out_root/summary.txt" && flow_fail=1

# ---- verification: ecc-verify on the CPUs not used by the flow jobs
ev=(ecc-verify); command -v ecc-verify >/dev/null || ev=(python3 -m ecc_verify)
verify_pin=(); (( have_taskset )) && (( first_cpu > 0 )) && verify_pin=(taskset -c "0-$((first_cpu - 1))")
args=(run --root "$out_root" --out "$out_root/verify" --ecc "$ecc" --pdk-root "$pdk_root" --designs-dir "$designs_dir")
[[ -n "$baseline" ]] && args+=(--baseline "$baseline")
[[ -n "$slow" ]] && args+=("$slow")
[[ -n "$layers" ]] && args+=(--layers "$layers")
(( have_taskset )) && (( first_cpu > 0 )) && args+=(--pin-cpus "0-$((first_cpu - 1))")
args+=(--designs "$(IFS=,; echo "${designs[*]}")")
"${verify_pin[@]}" "${ev[@]}" "${args[@]}" "${extra[@]}"
rc=$?
echo "[run_ci_designs] ecc-verify exit $rc, flow failures: $flow_fail; results in $out_root/verify"
(( rc == 2 )) && exit 2
(( rc == 1 || flow_fail )) && exit 1
exit 0
