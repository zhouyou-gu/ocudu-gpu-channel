#!/bin/bash
# Live antenna/port sweep: OCUDU gNB (CPU and CUDA D10) + one OAI nrUE, N x M antennas, 20/50/100 MHz, idle cell (1 Mb/s iperf).
cd /workspace/gpuch/lead || exit 1
export LD_LIBRARY_PATH=/usr/local/cuda/lib64
log=/workspace/gpuch/bound/nxm/campaign.log; map=/workspace/gpuch/bound/nxm/runs.tsv; : > $map
idle() { [ -z "$(nvidia-smi --query-compute-apps=pid --format=csv,noheader 2>/dev/null)" ] && [ "$(ps -eo args | grep -c '[n]vidia-cuda-mps-server')" = 0 ]; }
waitgpu() { until idle; do echo "$(date -u +%T) gpu busy: $(nvidia-smi --query-compute-apps=pid,process_name --format=csv,noheader | tr '\n' ';')" >> $log; sleep 20; done; }
r() { # combo bw gnb rate
  local c=$1 bw=$2 gnb=$3 rate=$4 tag="n$1-$2$([ "$4" = 1M ] && echo i || echo l)-$3"; waitgpu
  echo "$(date -u +%T) START $tag" >> $log
  IPERF_RATE=$rate X="LD_LIBRARY_PATH=/usr/local/cuda/lib64 OAI2X2_IPERF_SECONDS=15 OAI2X2_GNB_ANT=${c%x*} OAI2X2_UE_ANT=${c#*x} OAI2X2_ALLOW_UNHEALTHY=1" TOPDELAY=10 \
    ./run.sh $tag $bw zero_copy $gnb - ../../../bound/nxm/topology.oai-$c.yaml >> $log 2>&1
  local d; d=$(ls -dt runs/$tag-* | head -1); printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$tag" "$c" "$bw" "$gnb" "$rate" "$d" >> $map
  echo "$(date -u +%T) END $tag $(python3 /workspace/gpuch/bound/oai-cuda/oai-summ.py $d | grep -oE 'rt_rank=[^ ]+ health=\{[^}]*"result": "[a-z]+"' | tr -d '\n' | cut -c1-160)" >> $log; sleep 5; }
for gnb in cpu cuda; do for bw in 20 50 100; do for c in 4x1 4x2 2x4 4x4; do r $c $bw $gnb 1M; done; done; done
echo "$(date -u +%T) NXM_DONE" >> $log
