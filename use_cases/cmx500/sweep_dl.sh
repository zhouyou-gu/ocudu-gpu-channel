#!/bin/bash
# Live DL SNR sweep through the GPU channel (transmits). Attaches once with topology.cmx-bridge.sweep-dl.yaml, then
# raises the DL path loss in front of the fixed UE noise floor step by step and reports per-step DL KPIs.
#
#   ./sweep_dl.sh
#   STEPS="0 20 30 40 0" STEP_S=10 ./sweep_dl.sh
#
# Env: STEPS (path loss values in dB), STEP_S seconds per step (8), TOPO, LINK (gnb0>ue0:dl), PARAM (path_loss_db),
#      PHY_LOG (warning; info adds per-slot PDSCH statistics but slows the UE), ATTEMPTS (3) to attach
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
L=/workspace/cmx-loop
CTL=/workspace/ocudu-gpu-channel/build-sm121/ocudu-control-req
STEPS=${STEPS:-"0 15 20 25 30 35 40 43 0"}
STEP_S=${STEP_S:-8}
LINK=${LINK:-gnb0>ue0:dl}
PARAM=${PARAM:-path_loss_db}
NSTEPS=$(echo $STEPS | wc -w)
cd $L

echo "[$(date +%T)] load average $(cut -d' ' -f1-3 /proc/loadavg) (timing suffers when other experiments share the machine)"
LOG=$L/runs/.sweep-console.txt
# srsUE gives up after one failed random access, so a failed attach is retried with a fresh run
for attempt in $(seq ${ATTEMPTS:-3}); do
  PHY_LOG=${PHY_LOG:-warning} TOPO=${TOPO:-${EX:-$(cd "${script_dir}/../configs/topologies/cmx" && pwd)}/topology.cmx-bridge.sweep-dl.yaml} DUR=$((NSTEPS * STEP_S + 30)) \
    BROKER_ARGS="--control-endpoint tcp://127.0.0.1:5559" "${script_dir}/run_cmx_loop.sh" > $LOG 2>&1 &
  RUN=$!
  sleep 2
  R=$(ls -td $L/runs/*/ | head -1)
  R=${R%/}
  echo "[$(date +%T)] attempt $attempt: run $R, waiting for registration"
  for i in $(seq 60); do
    grep -q "Handling Registration Accept" $R/srsue.log 2>/dev/null && break
    sleep 0.5
  done
  grep -q "Handling Registration Accept" $R/srsue.log 2>/dev/null && break
  echo "[$(date +%T)] no registration within 30 s, stopping this run"
  pkill -INT -f srsue/src/srsue
  wait $RUN
  grep -E "cell_found|sfn_mismatch" $LOG
  [ $attempt = ${ATTEMPTS:-3} ] && { echo "no attach after $attempt attempts, sweep skipped"; exit 1; }
  sleep 5
done
echo "[$(date +%T)] registered, stepping $PARAM on $LINK: $STEPS ($STEP_S s each)"
echo "t_utc,value" > $R/steps.csv
for v in $STEPS; do
  $CTL --endpoint tcp://127.0.0.1:5559 \
    --message "{\"type\":\"scalar\",\"link_id\":\"$LINK\",\"param\":\"$PARAM\",\"value\":$v}" > /dev/null ||
    echo "control plane refused $PARAM=$v"
  echo "$(date -u +%Y-%m-%dT%H:%M:%S.%6N),$v" >> $R/steps.csv
  sleep $STEP_S
done
echo "$(date -u +%Y-%m-%dT%H:%M:%S.%6N),end" >> $R/steps.csv
wait $RUN
cat $LOG
python3 "${script_dir}/sweep_report.py" $R "$PARAM"
