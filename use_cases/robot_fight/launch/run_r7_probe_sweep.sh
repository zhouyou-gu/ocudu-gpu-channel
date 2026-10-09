#!/usr/bin/env bash
# Sequential radio characterization; every condition crosses sudo explicitly.
# Usage: bash run_r7_probe_sweep.sh UNIQUE_PREFIX
# R7_PROBE_CASES=sr40,k1k2,retx1 selects a subset with a fresh prefix.
set -euo pipefail
prefix="${1:?provide a unique output prefix}"
[[ "$prefix" =~ ^[a-zA-Z0-9_-]+$ ]] || exit 2
tree="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
out="${R7_OUTPUT_ROOT:-/workspace/gpuch/r7-validation-results}"
mkdir -p "$out"
[[ ! -e "$out/$prefix-manifest.tsv" ]] || { echo 'manifest already exists' >&2; exit 2; }
cases="${R7_PROBE_CASES:-base,awgn34,awgn30,awgn27,awgn24,k2,sr40,k1k2,retx1}"
[[ "$cases" != ,* && "$cases" != *, && "$cases" != *,,* ]] || { echo 'empty condition' >&2; exit 2; }
IFS=',' read -r -a selected <<< "$cases"
for condition in "${selected[@]}"; do
  case "$condition" in base|awgn34|awgn30|awgn27|awgn24|k2|sr40|k1k2|retx1) ;;
    *) echo "unknown condition: $condition" >&2; exit 2;;
  esac
done
probe="$tree/use_cases/robot_fight/robot-fight-probe.py"
common=(
  "OCUDU_NATIVE_SIONNA_POSITION_ENDPOINT="
  "OCUDU_NATIVE_MUE_ROOT_EXEC=/usr/bin/python3 $probe server --bind {ue_gateway}:7000 --log {log_dir}/probe-server.jsonl"
  "OCUDU_NATIVE_MUE_UE_EXEC=/usr/bin/python3 $probe client --robot {ue_id} --bind {ue_ip}:0 --server {ue_gateway}:7000 --rate-hz 100 --log {log_dir}/probe-{ue_id}.jsonl"
)
run() {
  local condition="$1"; shift
  [[ ",$cases," == *",$condition,"* ]] || return 0
  local tag="$prefix-$condition" status
  printf '%s\t%s\n' "$tag" "$*" >> "$out/$prefix-manifest.tsv"
  printf '%s START %s\n' "$(date -Is)" "$tag"
  if RF_DURATION="${RF_DURATION:-160}" bash "$tree/use_cases/robot_fight/launch/run_r7_spark.sh" "$tag" "${common[@]}" "$@"; then
    status=0
  else
    status=$?
  fi
  printf '%s END %s exit=%s\n' "$(date -Is)" "$tag" "$status"
  # Native dry-run rejection is a result too (exit 2). Wrapper refusals such
  # as a busy GPU have no R7_EXIT marker and must stop this sweep.
  if [[ "$status" != 0 ]] && ! grep -q "^R7_EXIT=$status$" "$out/$tag.out" 2>/dev/null; then
    return "$status"
  fi
  return 0
}
run base
run awgn34 OCUDU_NATIVE_SIONNA_AWGN_SNR_DB=34
run awgn30 OCUDU_NATIVE_SIONNA_AWGN_SNR_DB=30
run awgn27 OCUDU_NATIVE_SIONNA_AWGN_SNR_DB=27
run awgn24 OCUDU_NATIVE_SIONNA_AWGN_SNR_DB=24
run k2 OCUDU_NATIVE_RF_GNB_CELL_OVERRIDES=pusch.min_k2=8
run sr40 OCUDU_NATIVE_RF_GNB_CELL_OVERRIDES=pucch.sr_period_ms=40
run k1k2 OCUDU_NATIVE_RF_GNB_CELL_OVERRIDES=pusch.min_k2=8,pucch.min_k1=7
run retx1 OCUDU_NATIVE_SIONNA_AWGN_SNR_DB=30 OCUDU_NATIVE_RF_GNB_CELL_OVERRIDES=pdsch.max_nof_harq_retxs=1,pusch.max_nof_harq_retxs=1
printf 'SWEEP_DONE\n'
