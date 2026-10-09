#!/usr/bin/env bash
# Fresh-seed side swap with fixed speed parameters, five minutes per assignment.
# Run only after other Spark radio/GPU experiments finish.
set -euo pipefail
prefix="${1:?provide a unique output prefix}"
[[ "$prefix" =~ ^[a-zA-Z0-9_-]+$ ]] || exit 2
tree="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
busy=(OCUDU_NATIVE_RF_BROKER_A_SCHED=plain OCUDU_NATIVE_RF_BROKER_B_SCHED=plain
      OCUDU_NATIVE_RF_CONTENTION=busy OCUDU_NATIVE_RF_HOG_MPS=1
      OCUDU_NATIVE_RF_HOG_KERNEL_US=200 OCUDU_NATIVE_RF_HOG_STREAMS=2
      OCUDU_NATIVE_RF_HOG_QUEUE_DEPTH=16)
for side in 0 1; do
  if [[ "$side" == 0 ]]; then p0=balance_comp; p1=balance; else p0=balance; p1=balance_comp; fi
  printf '%s START %s-comp%s\n' "$(date -Is)" "$prefix" "$side"
  RF_PARAM_JITTER=0 RF_DURATION="${RF_DURATION:-300}" RF_SEED="${RF_SEED:-9000}" \
    RF_POLICY_0="$p0" RF_POLICY_1="$p1" \
    bash "$tree/use_cases/robot_fight/launch/run_r7_spark.sh" "$prefix-comp$side" "${busy[@]}"
  printf '%s END %s-comp%s\n' "$(date -Is)" "$prefix" "$side"
done
printf 'REPEAT_DONE\n'
