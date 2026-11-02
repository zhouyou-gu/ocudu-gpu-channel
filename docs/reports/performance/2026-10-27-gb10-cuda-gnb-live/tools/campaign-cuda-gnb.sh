#!/bin/bash
# CUDA gNB live scaling campaign. env: GNB_BIN, STAGE, AHEAD (ms or empty), TAGP (tag prefix), SUBSET (1 = 8/16 UE + two-cell only)
export LD_LIBRARY_PATH=/usr/local/cuda/lib64; L=/workspace/gpuch; : "${GNB_BIN:?}" "${STAGE:?}" "${TAGP:?}"
idle() { [ -z "$(nvidia-smi --query-compute-apps=pid --format=csv,noheader 2>/dev/null)" ]; }
waitgpu() { until idle; do echo "gpu busy: $(nvidia-smi --query-compute-apps=pid,process_name --format=csv,noheader | tr '\n' ';')"; sleep 20; done; }
req() { echo "{\"ue_count\":$1,\"gnb_antennas\":1,\"mobility\":\"pedestrian\",\"placement\":\"uniform\",\"traffic_profile\":\"mixed\",\"tests\":[{\"scheduler\":\"rr\",\"link_adaptation\":\"harq_olla\"},{\"scheduler\":\"rr\",\"link_adaptation\":\"harq_olla\"}],\"seed\":20261016,\"measure_seconds\":30,\"warmup_seconds\":10,\"repeat_count\":1,\"comparison_order\":\"abba\"}"; }
AH=""; [ -n "${AHEAD:-}" ] && AH="OCUDU_LPHY_RX_TO_TX_MAX_MS=$AHEAD"
CUDA="SINGLE_CELL_GNB_BINARY=$GNB_BIN SINGLE_CELL_GNB_ACCELERATION=$STAGE SINGLE_CELL_GNB_START_TIMEOUT=150"
COMMON="SB_ALLOW_UNVALIDATED=1 SINGLE_CELL_CPUS_AUX=0-4 SINGLE_CELL_UE_RX_SLEEP=false SINGLE_CELL_QUIET_LOGS=1 SINGLE_CELL_PCAP=off SINGLE_CELL_CPUS_GNB=5-12 SINGLE_CELL_CPUS_BROKER=15-16 SINGLE_CELL_CPUS_UES=17-19 OCG_BROKER_SPIN=2 OCG_ZMQ_IO_THREADS=2 $AH"
PIN8="SINGLE_CELL_CPUS_UE0=17 SINGLE_CELL_CPUS_UE1=18 SINGLE_CELL_CPUS_UE2=19 SINGLE_CELL_CPUS_UE3=17 SINGLE_CELL_CPUS_UE4=18 SINGLE_CELL_CPUS_UE5=19 SINGLE_CELL_CPUS_UE6=13 SINGLE_CELL_CPUS_UE7=14"
PIN16="$PIN8 SINGLE_CELL_CPUS_UE8=17 SINGLE_CELL_CPUS_UE9=18 SINGLE_CELL_CPUS_UE10=19 SINGLE_CELL_CPUS_UE11=17 SINGLE_CELL_CPUS_UE12=18 SINGLE_CELL_CPUS_UE13=19 SINGLE_CELL_CPUS_UE14=13 SINGLE_CELL_CPUS_UE15=14"
run() { tag=$TAGP-$1; r=$2; shift 2; waitgpu
  ( sleep 170; { date -u +%T; nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv,noheader; echo "mps_servers=$(ps -eo args | grep -c '[n]vidia-cuda-mps-server')"; } > $L/ranb-run-$tag.gpu.txt 2>&1 ) &
  timeout 1800 bash $L/ranb-run.sh $tag $COMMON $CUDA "RANB_REQ=$r" "$@" > $L/ranb-run-$tag.log 2>&1; wait; echo "$tag $(grep -h EXIT= $L/ranb-run-$tag.log | tail -1)"; }
if [ "${SUBSET:-0}" != 1 ]; then
  run 1ue "$(req 1)" SINGLE_CELL_MPS=on SINGLE_CELL_BROKER_IN_MPS=1 SINGLE_CELL_CPUS_UE0=17
  run 2ue "$(req 2)" SINGLE_CELL_MPS=on SINGLE_CELL_BROKER_IN_MPS=1 SINGLE_CELL_CPUS_UE0=17 SINGLE_CELL_CPUS_UE1=18
  run 4ue "$(req 4)" SINGLE_CELL_MPS=on SINGLE_CELL_BROKER_IN_MPS=1
fi
run 8ue "$(req 8)" SINGLE_CELL_MPS=on SINGLE_CELL_BROKER_IN_MPS=1 $PIN8
run 16ue "$(req 16)" SINGLE_CELL_MPS=on SINGLE_CELL_BROKER_IN_MPS=1 $PIN16
run 8ue-nomps "$(req 8)" SINGLE_CELL_MPS=off $PIN8
[ "${SUBSET:-0}" != 1 ] && run 16ue-nomps "$(req 16)" SINGLE_CELL_MPS=off $PIN16
# two cells, default placement
cd $L/ranb || exit 9
LINK=/workspace/ocudu-spark-ranb/builds/ocudu-zmq-release; ORIG=/workspace/ocudu-spark/builds/ocudu-zmq-release
restore() { rm -f $LINK; ln -s $ORIG $LINK; echo "symlink restored: $(readlink $LINK)"; }
trap restore EXIT
rm -f $LINK; ln -s "$(dirname "$(dirname "$(dirname "$GNB_BIN")")")" $LINK; echo "symlink -> $(readlink $LINK)"
export HOME=/root PATH=/usr/local/cuda/bin:$PATH CUDACXX=/usr/local/cuda/bin/nvcc OCUDU_NATIVE_CUDA_ARCH=121 OCUDU_NATIVE_SKIP_WORKSPACE_LOCK=1 OCUDU_NATIVE_ROOT=/workspace/ocudu-spark-ranb OCUDU_NATIVE_SIONNA_PYTHON=/workspace/sionna-venv/bin/python OCUDU_NATIVE_CHANNEL_BUILD=/workspace/ocudu-spark-ranb/builds/ocudu-gpu-channel-twocell OCUDU_NATIVE_SB_WEB_UI=0 OCUDU_NATIVE_SB_SKIP_CTEST=1 OCUDU_NATIVE_SB_MEASURE_SECONDS=30 OCUDU_NATIVE_SB_UE_RX_SLEEP=false OCUDU_NATIVE_SB_SEED=1 OCG_BROKER_SPIN=2 OCG_ZMQ_IO_THREADS=2 OCUDU_NATIVE_SB_GNB_ACCELERATION=$STAGE OCUDU_NATIVE_GNB_START_TIMEOUT_SECONDS=150
[ -n "${AHEAD:-}" ] && export OCUDU_LPHY_RX_TO_TX_MAX_MS=$AHEAD
tc() { tag=$TAGP-$1; mps=$2; waitgpu; echo "== tag=$tag mps=$mps start=$(date -u +%FT%TZ)"
  ( sleep 200; { date -u +%T; nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv,noheader; echo "mps_servers=$(ps -eo args | grep -c '[n]vidia-cuda-mps-server')"; } > $L/ranb-run-$tag.gpu.txt 2>&1 ) &
  if [ $mps = on ]; then OCUDU_NATIVE_SB_BROKER_IN_MPS=1 OCUDU_NATIVE_SB_SCHED_A=rr OCUDU_NATIVE_SB_SCHED_B=rr timeout 1200 /usr/bin/python3 scripts/cuda/with-cuda-mps.py -- bash use_cases/ran_benchmark/run-ocudu-scheduler-benchmark.sh
  else OCUDU_NATIVE_SB_SCHED_A=rr OCUDU_NATIVE_SB_SCHED_B=rr timeout 1200 bash use_cases/ran_benchmark/run-ocudu-scheduler-benchmark.sh; fi
  echo "== tag=$tag EXIT=$? end=$(date -u +%FT%TZ)"; wait; sleep 3; }
{ tc twocell-mps on; tc twocell-nomps off; } > $L/ranb-run-twocell-$TAGP.log 2>&1
