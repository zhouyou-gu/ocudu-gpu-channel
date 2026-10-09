#!/usr/bin/env bash
# Run one paired-policy R7 radio experiment on Spark. Run sequentially.
# Usage: bash run_r7_spark.sh TAG [OCUDU_NATIVE_...=value ...]
# Every experiment override must be an argument, not inherited through sudo.
# RF_POLICY_0/1, RF_DURATION, RF_SEED and RF_PARAM_JITTER are forwarded explicitly.
set -euo pipefail
tag="${1:?provide a unique run tag}"; shift
[[ "$tag" =~ ^[a-zA-Z0-9_-]+$ ]] || { echo "invalid tag" >&2; exit 2; }
tree="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
native_root="${R7_NATIVE_ROOT:-/workspace/ocudu-spark}"
build="${R7_CHANNEL_BUILD:-${native_root}/builds/gpuch-r7-0930-release}"
out="${R7_OUTPUT_ROOT:-/workspace/gpuch/r7-validation-results}"
mkdir -p "$out"
[[ ! -e "$out/$tag.out" ]] || { echo "refusing to overwrite $tag" >&2; exit 2; }
for assignment in "$@"; do
  [[ "$assignment" =~ ^OCUDU_NATIVE_[A-Z0-9_]+= ]] || { echo "invalid override: $assignment" >&2; exit 2; }
done
# Reject silently ignored inherited native knobs. The explicit arguments below
# cross sudo's environment reset and are recorded by the native gate.
while IFS= read -r variable; do
  [[ "$variable" != OCUDU_NATIVE_* ]] || {
    echo "pass $variable as a command argument, not an inherited environment variable" >&2; exit 2;
  }
done < <(compgen -e)
nvidia-smi --query-compute-apps=pid,name --format=csv,noheader > "$out/$tag.gpu-before"
[[ ! -s "$out/$tag.gpu-before" ]] || { echo "GPU already in use; defer this experiment" >&2; exit 3; }
pos="ipc://${native_root}/run/r7-arena/positions.sock"
jitter="${RF_PARAM_JITTER:-0.1}"
[[ "$jitter" =~ ^(0([.][0-9]+)?|1([.]0+)?)$ ]] || { echo 'RF_PARAM_JITTER must be between 0 and 1' >&2; exit 2; }
root_hook="RF_PYTHON=/workspace/robot-venv/bin/python RF_BOT=balance RF_POLICY=balance RF_POLICY_0=${RF_POLICY_0:-balance_comp} RF_POLICY_1=${RF_POLICY_1:-balance} RF_SEED=${RF_SEED:-7000} RF_TIME_LIMIT=${RF_TIME_LIMIT:-20} RF_BRAIN_EXTRA='--param-jitter $jitter' RF_POS_ENDPOINT=$pos bash $tree/use_cases/robot_fight/launch/root_exec.sh {run_dir} {log_dir} '{ue_ids}' '{ue_ips}' {ue_gateway}"
cd "$tree"
set +e
sudo env PATH="$PATH" CUDACXX=/usr/local/cuda/bin/nvcc \
  OCUDU_NATIVE_ROOT="$native_root" OCUDU_NATIVE_CHANNEL_BUILD="$build" \
  OCUDU_NATIVE_SKIP_WORKSPACE_LOCK=1 OCUDU_NATIVE_RF_SKIP_CTEST=1 \
  OCUDU_NATIVE_SIONNA_PYTHON=/workspace/sionna-venv/bin/python \
  DRJIT_LIBOPTIX_PATH=/usr/lib/aarch64-linux-gnu/libnvoptix.so.1 \
  OCUDU_NATIVE_SIONNA_SCENARIO="$tree/use_cases/configs/sionna/scenarios/robot_ring/robot-ring-fight.json" \
  OCUDU_NATIVE_SIONNA_POSITION_ENDPOINT="$pos" OCUDU_NATIVE_SIONNA_POSITION_OFFSET=2.0,0,0 \
  OCUDU_NATIVE_SIONNA_TX_POWER_UL=3.0e2 \
  OCUDU_NATIVE_RF_BROKER_A_SCHED=protected OCUDU_NATIVE_RF_BROKER_B_SCHED=protected \
  OCUDU_NATIVE_RF_CONTENTION=none OCUDU_NATIVE_RF_DURATION_SECONDS="${RF_DURATION:-180}" \
  OCUDU_NATIVE_MUE_STRICT_REALTIME=0 \
  OCUDU_NATIVE_MUE_ROOT_EXEC="$root_hook" \
  OCUDU_NATIVE_MUE_UE_EXEC="bash $tree/use_cases/robot_fight/launch/ue_exec.sh {ue_id} {ue_ip} {ue_index} {run_dir} {log_dir}" \
  "$@" bash use_cases/robot_fight/run-ocudu-robot-fight.sh > "$out/$tag.out" 2>&1
status=$?
set -e
printf 'R7_EXIT=%s\n' "$status" | tee -a "$out/$tag.out"
nvidia-smi --query-compute-apps=pid,name --format=csv,noheader > "$out/$tag.gpu-after"
exit "$status"
