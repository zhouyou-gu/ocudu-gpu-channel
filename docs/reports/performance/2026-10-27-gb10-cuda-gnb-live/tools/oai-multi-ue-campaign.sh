#!/bin/bash
# OAI nrUE multi-instance grid: bandwidth 20/50/100 MHz (TDD n78 30 kHz) x UE count 2/4 x gNB cpu/cuda(D10, MPS). Idle cell (pings).
L=/workspace/gpuch/bound/oaimue; log=$L/campaign.log; map=$L/runs.tsv
cd /workspace/gpuch/ranb || exit 9
export HOME=/root PATH=/usr/local/cuda/bin:$PATH LD_LIBRARY_PATH=/usr/local/cuda/lib64 CUDACXX=/usr/local/cuda/bin/nvcc
export OCUDU_NATIVE_ROOT=/workspace/ocudu-spark OCUDU_NATIVE_CUDA_ARCH=121 OCUDU_NATIVE_SKIP_WORKSPACE_LOCK=1
export OCUDU_NATIVE_CHANNEL_BUILD=/workspace/ocudu-spark-ranb/builds/ocudu-gpu-channel-single-cell
export OCUDU_NATIVE_SIONNA_PYTHON=/workspace/sionna-venv/bin/python OCUDU_NATIVE_OAI_SHLIBPATH=/workspace/ocudu-spark/builds/oai-zmq-patched
export OCUDU_NATIVE_MUE_SKIP_CTEST=1 OCUDU_NATIVE_MUE_DURATION_SECONDS=${DUR:-160} OCUDU_NATIVE_WEB_PORT=0 OCUDU_NATIVE_GNB_START_TIMEOUT_SECONDS=150
GNB_CUDA=/workspace/ocudu-spark/builds/d10-cuda-patched-sm121/apps/gnb/gnb
idle() { [ -z "$(nvidia-smi --query-compute-apps=pid --format=csv,noheader 2>/dev/null)" ] && [ "$(ps -eo args | grep -c '[n]vidia-cuda-mps-server')" = 0 ]; }
waitgpu() { until idle; do echo "$(date -u +%T) gpu busy: $(nvidia-smi --query-compute-apps=pid,process_name --format=csv,noheader | tr '\n' ';')" >> $log; sleep 20; done; }
run() { # tag bw n gnb
  local tag=$1 bw=$2 n=$3 gnb=$4 cpus; waitgpu
  if [ "$n" = 2 ]; then cpus="18,4;19,14"; else cpus="18,4;19,3;13;14"; fi
  local before; before=$(ls /workspace/ocudu-spark/results/reports/ocudu-oai-multi-ue 2>/dev/null | tail -1)
  echo "$(date -u +%T) START $tag bw=$bw ue=$n gnb=$gnb" >> $log
  local env=(OAIMUE_BW_MHZ=$bw OAIMUE_UE_COUNT=$n OCUDU_NATIVE_OAI_MUE_UE_CPUS="$cpus" OCUDU_NATIVE_SIONNA_SCENARIO=$L/scen-${n}ue.json)
  if [ "$gnb" = cuda ]; then
    env "${env[@]}" OCUDU_NATIVE_GNB_BINARY=$GNB_CUDA OCUDU_NATIVE_GNB_ACCELERATION=all timeout 1200 /usr/bin/python3 scripts/cuda/with-cuda-mps.py -- bash scripts/native/run-ocudu-oai-multi-ue.sh > $L/run-$tag.log 2>&1
  else
    env "${env[@]}" timeout 1200 bash scripts/native/run-ocudu-oai-multi-ue.sh > $L/run-$tag.log 2>&1
  fi
  local rc=$?; local after; after=$(ls /workspace/ocudu-spark/results/reports/ocudu-oai-multi-ue 2>/dev/null | tail -1)
  [ "$after" = "$before" ] && after="-"
  printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$tag" "$bw" "$n" "$gnb" "$rc" "$after" >> $map
  echo "$(date -u +%T) END $tag rc=$rc report=$after $(grep -oE 'error: .*|status=[a-z]+|verdict=[a-z]+' $L/run-$tag.log | tail -2 | tr '\n' ' ' | cut -c1-160)" >> $log
  sleep 5; return $rc; }
: > $map
run m20-2-cpu 20 2 cpu || { echo "$(date -u +%T) ABORT: baseline 20 MHz 2-UE CPU run failed" >> $log; exit 1; }
run m50-2-cpu 50 2 cpu;  run m100-2-cpu 100 2 cpu
run m20-2-cuda 20 2 cuda; run m50-2-cuda 50 2 cuda; run m100-2-cuda 100 2 cuda
run m20-4-cpu 20 4 cpu;  run m50-4-cpu 50 4 cpu;  run m100-4-cpu 100 4 cpu
run m20-4-cuda 20 4 cuda; run m50-4-cuda 50 4 cuda; run m100-4-cuda 100 4 cuda
echo "$(date -u +%T) OAIMUE_DONE" >> $log
