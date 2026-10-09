#!/usr/bin/env bash
# One direct-ZMQ live run on the Spark (gNB <-> srsUE, no emulator), via the
# workstation runner scripts/cuda/run-ocudu-cuda-direct-zmq.sh.
#
#   bash live-direct-zmq.sh <stage> [traffic_seconds]      stage: cpu|disabled|low-phy-rx|...|all
#   SPARK_LOCK=<lock file>  selects another CUDA build (default cuda-workspace.spark.lock.json)
#   ATTACH_SECONDS=<s>      attach window (default 60; 400 on the Jetson, srsUE PHY init ~200 s)
#   PLATFORM_ROOT=<root>    work root (default /workspace/ocudu-spark); on the Jetson:
#     PLATFORM_ROOT=/workspace/ocudu-jetson SPARK_LOCK=.../cuda-workspace.jetson-live.lock.json
#
# Runs the runner as container root with HOME=/root. Two reasons, both found on
# 2026-09-24 (SPARK_MILESTONES.md S3):
#   - the runner's `unshare --user --map-root-user` is refused for an unprivileged
#     user on this Ubuntu 24.04 host (uid_map write denied);
#   - inside that user namespace dev's 750 home is untraversable, so with
#     HOME=/home/dev srsUE silently skips its FFTW wisdom and re-plans on every
#     start (~260 s on GB10), overrunning the runner's 60 s attach window.
set -uo pipefail
stage="${1:?stage}"; traffic="${2:-20}"
repo="$(cd "$(dirname "$0")/../../.." && pwd)"
root="${PLATFORM_ROOT:-/workspace/ocudu-spark}"
console="${root}/results/live-${stage}-$(date -u +%Y%m%dT%H%M%SZ).console.log"
# Diagnostic knobs (OCUDU_LDPC_*, OCUDU_PUSCH_*, OCUDU_DIRECT_*, OCUDU_TIME_INTERP, OCUDU_NOISE_MODE) are forwarded
# explicitly: sudo starts from a clean environment. They are recorded in the
# console log so a run can never be mistaken for a default one.
mapfile -t knobs < <(env | grep -E '^OCUDU_(LDPC_[A-Z_]+|PUSCH_[A-Z_]+|DIRECT_[A-Z_]+|NATIVE_CUDA_GRID_MODE|TIME_INTERP|NOISE_MODE)=' | sort)
printf 'diagnostic_env=%s\n' "${knobs[*]:-none}" > "$console"
sudo env HOME=/root PATH="$PATH" "${knobs[@]}" \
  OCUDU_NATIVE_ROOT="${root}" \
  OCUDU_CUDA_WORKSPACE_LOCK="${SPARK_LOCK:-${repo}/integrations/ocudu/cuda-workspace.spark.lock.json}" \
  OCUDU_NATIVE_GNB_ACCELERATION="$stage" OCUDU_CUDA_DIRECT_TRAFFIC_SECONDS="$traffic" \
  OCUDU_CUDA_DIRECT_ATTACH_SECONDS="${ATTACH_SECONDS:-60}" \
  bash "${repo}/scripts/cuda/run-ocudu-cuda-direct-zmq.sh" >> "$console" 2>&1
rc=$?
echo "LIVE_EXIT=${rc}" >> "$console"
echo "$console"
exit "$rc"
