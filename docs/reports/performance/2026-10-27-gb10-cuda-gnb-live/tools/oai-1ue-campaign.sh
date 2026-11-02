#!/usr/bin/env bash
# CUDA gNB (D10 lock) x one OAI nrUE: bandwidth 20/50/100 MHz x antennas 1x1 / 2x2 (idle + 200 Mb/s), lead gates.
cd /workspace/gpuch/lead || exit 1
export LD_LIBRARY_PATH=/usr/local/cuda/lib64
OUT=/workspace/gpuch/bound/oai-cuda; log=$OUT/campaign.log
wait_gpu(){ while :; do
  apps=$(nvidia-smi --query-compute-apps=pid --format=csv,noheader | grep -c .)
  mps=$(ps -eo args | grep -c "[n]vidia-cuda-mps-server")
  left=$(ps -eo pid,args | grep -cE "[o]pen5gs-|[n]r-uesoftmodem|/apps/gnb/[g]nb |[o]cudu-gpu-channel --config|[s]rsue ")
  [[ $apps -eq 0 && $mps -eq 0 && $left -eq 0 ]] && return
  echo "$(date -u +%T) waiting: gpu_apps=$apps mps=$mps ran_procs=$left" >> $log; sleep 30; done; }
r1(){ wait_gpu; echo "$(date -u +%T) START 1x1 $1 bw=$2" >> $log
  X="LD_LIBRARY_PATH=/usr/local/cuda/lib64" ./run1x1.sh "$1" "$2" zero_copy cuda >> $log 2>&1; sleep 5; }
r2(){ wait_gpu; echo "$(date -u +%T) START 2x2 $1 bw=$2 rate=$3" >> $log
  IPERF_RATE=$3 X="LD_LIBRARY_PATH=/usr/local/cuda/lib64 OAI2X2_IPERF_SECONDS=15" TOPDELAY=10 \
    ./run.sh "$1" "$2" zero_copy cuda - topology.ocudu.oai-2x2-unitary.cuda.yaml >> $log 2>&1; sleep 5; }
r1 c1x1-20 20; r1 c1x1-50 50; r1 c1x1-100 100
r2 c2x2-20i 20 1M; r2 c2x2-20l 20 200M; r2 c2x2-50i 50 1M; r2 c2x2-50l 50 200M; r2 c2x2-100i 100 1M; r2 c2x2-100l 100 200M
echo "$(date -u +%T) OAI_CUDA_DONE" >> $log
