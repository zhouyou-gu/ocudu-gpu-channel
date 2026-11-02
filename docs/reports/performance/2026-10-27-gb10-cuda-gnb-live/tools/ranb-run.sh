#!/bin/bash
# usage: ranb-run.sh <tag> [ENV=VAL ...] -- one single-cell benchmark session (A/A rr+harq_olla, 4 pedestrian UEs, seed 20261016, 30 s)
tag="$1"; shift
cd /workspace/gpuch/ranb || exit 9
export LD_LIBRARY_PATH=/usr/local/cuda/lib64 HOME=/root SKIP_WORKSPACE_LOCK=1 OCUDU_NATIVE_ROOT=/workspace/ocudu-spark-ranb \
  OCUDU_NATIVE_SIONNA_PYTHON=/workspace/sionna-venv/bin/python \
  OCUDU_NATIVE_CHANNEL_BUILD=/workspace/ocudu-spark-ranb/builds/ocudu-gpu-channel-single-cell SB_ARCHIVE=1
for kv in "$@"; do export "$kv"; done
req='{"ue_count":4,"gnb_antennas":1,"mobility":"pedestrian","placement":"uniform","traffic_profile":"mixed","tests":[{"scheduler":"rr","link_adaptation":"harq_olla"},{"scheduler":"rr","link_adaptation":"harq_olla"}],"seed":20261016,"measure_seconds":30,"warmup_seconds":10,"repeat_count":1,"comparison_order":"abba"}'
[ -n "${RANB_REQ:-}" ] && req="$RANB_REQ"
# guard: never start while a previous session (same active-dir marker) is still being torn down
for i in $(seq 1 120); do
  n=$(grep -lz "SINGLE_CELL_SESSION_DIR=" /proc/[0-9]*/environ 2>/dev/null | wc -l)
  r=$(pgrep -f single_cell_runner.py | wc -l)
  [ "$n" = 0 ] && [ "$r" = 0 ] && break
  echo "guard: waiting for $n marker / $r runner processes to exit ($i)"; sleep 5
done
echo "tag=$tag env=$* start=$(date -u +%FT%TZ)"
if [ "${SINGLE_CELL_MPS:-off}" = on ]; then /usr/bin/python3 scripts/cuda/with-cuda-mps.py -- bash use_cases/ran_benchmark/run-single-cell-scheduler-benchmark.sh "$req"; else bash use_cases/ran_benchmark/run-single-cell-scheduler-benchmark.sh "$req"; fi
rc=$?
echo "tag=$tag EXIT=$rc end=$(date -u +%FT%TZ)"
# keep the session dir under a tagged name
S=/workspace/ocudu-spark-ranb/results/single-cell-scheduler-benchmark
sid=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["session_id"])' $S/active/session.json 2>/dev/null)
[ -n "$sid" ] && cp -r $S/active $S/tagged-$tag-$sid && echo "saved $S/tagged-$tag-$sid"
