#!/usr/bin/env bash
# Locked-in GPU validation sequence for the RTX workstation.
#
# Runs, in order, on the remote GPU host:
#   [1/4] CUDA release build
#   [2/4] ctest (config, processing incl. the CUDA AWGN check, ring, broker)
#   [3/4] synthetic CUDA relay loop  -- clean 0 dB channel, must relay IQ both
#         ways with zero data-integrity counters
#   [4/4] synthetic CUDA relay loop  -- AWGN channel, sink power must equal
#         signal power + noise_power
#
# It rsyncs the local working tree first, so it validates uncommitted code.
# The OCUDU Docker gNB/srsUE attach path is a separate, heavier test
# (scripts/remote/ocudu-attach-smoke.sh) and is not part of this sequence.
#
# Run as a Bash script: scripts/remote/gpu-test-sequence.sh
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${script_dir}/common.sh"

case "${REMOTE_PROJECT_ROOT}" in
  "~/"*) remote_dest="${REMOTE_PROJECT_ROOT#\~/}" ;;
  *) remote_dest="${REMOTE_PROJECT_ROOT}" ;;
esac

echo "== syncing working tree to ${REMOTE_USER}@${REMOTE_HOST}:${remote_dest} =="
rsync -az --delete \
  --exclude '.git' --exclude 'build*' --exclude '.config' \
  -e "ssh -i ${REMOTE_SSH_KEY} -o BatchMode=yes -o ConnectTimeout=8" \
  "${repo_root}/" "${REMOTE_USER}@${REMOTE_HOST}:${remote_dest}/"

remote_sh bash -s -- "${REMOTE_WORKSPACE}" "${REMOTE_PROJECT_ROOT}" "${REMOTE_BUILDS_ROOT}" <<'REMOTE'
set -uo pipefail

workspace="$1"
project_root="$2"
builds_root="$3"

expand_remote_path() {
  case "$1" in
    "~") printf '%s\n' "${HOME}" ;;
    "~/"*) printf '%s/%s\n' "${HOME}" "${1#~/}" ;;
    *) printf '%s\n' "$1" ;;
  esac
}

workspace="$(expand_remote_path "${workspace}")"
project_root="$(expand_remote_path "${project_root}")"
builds_root="$(expand_remote_path "${builds_root}")"

if [[ ! -f "${workspace}/tools/env.sh" ]]; then
  echo "missing ${workspace}/tools/env.sh; run scripts/remote/bootstrap-user-tools.sh first" >&2
  exit 1
fi
# shellcheck source=/dev/null
source "${workspace}/tools/env.sh"

cuda_build="${builds_root}/ocudu-gpu-channel/cuda-release"
mkdir -p "${cuda_build}"

fail() { echo "GPU TEST SEQUENCE FAILED: $1" >&2; exit 1; }

echo "== [1/4] CUDA release build =="
cmake -S "${project_root}" -B "${cuda_build}" \
  -DCMAKE_BUILD_TYPE=Release \
  -DOCUDU_GPU_CHANNEL_ENABLE_CUDA=ON \
  -DCMAKE_CUDA_COMPILER="${workspace}/tools/cuda-12.8.1/bin/nvcc" \
  -DOCUDU_GPU_CHANNEL_CUDA_ARCHITECTURES=120 >/tmp/gpu-seq-cmake.log 2>&1 \
  || { tail -25 /tmp/gpu-seq-cmake.log; fail "cmake configure"; }
cmake --build "${cuda_build}" -j"$(nproc)" >/tmp/gpu-seq-build.log 2>&1 \
  || { tail -25 /tmp/gpu-seq-build.log; fail "cmake build"; }
echo "    build OK"

echo "== [2/4] unit tests =="
ctest --test-dir "${cuda_build}" --output-on-failure >/tmp/gpu-seq-ctest.log 2>&1 \
  || { tail -25 /tmp/gpu-seq-ctest.log; fail "ctest"; }
grep -E "tests passed" /tmp/gpu-seq-ctest.log | sed 's/^/    /'

# --- synthetic CUDA relay loop -------------------------------------------------
# Drives the CUDA broker between four synthetic ZMQ peers on private ports.
write_topology() {
  cat >"$1" <<YAML
runtime:
  backend: cuda
  gpu_device: 0
  batch_samples: 23040
  queue_samples: 2457600
devices:
  - id: gnb0
    role: gnb
    sample_rate_hz: 23040000
    tx_endpoint: tcp://127.0.0.1:15000
    rx_endpoint: tcp://*:15001
  - id: ue0
    role: ue
    sample_rate_hz: 23040000
    tx_endpoint: tcp://127.0.0.1:15101
    rx_endpoint: tcp://*:15100
links:
  - from: gnb0
    to: ue0
    model: chan
  - from: ue0
    to: gnb0
    model: chan
models:
  chan:
    chain:
$2
YAML
}

counter_of() { printf '%s\n' "$2" | sed -n "s/.*$1=\([0-9][0-9]*\).*/\1/p"; }
power_of() { sed -n 's/.*avg_power=\([0-9.eE+-]*\).*/\1/p' "$1"; }

run_relay_check() {
  # $1 label  $2 topology file  $3 expected sink avg_power  $4 tolerance
  local label="$1" topo="$2" expect="$3" tol="$4"
  for p in 15000 15001 15100 15101; do
    if ss -ltn 2>/dev/null | grep -q ":${p}\b"; then fail "${label}: port ${p} busy"; fi
  done
  local dir; dir="$(mktemp -d)"
  "${cuda_build}/ocudu-zmq-source" --endpoint 'tcp://*:15000' --batch-samples 23040 --duration 6s >"${dir}/s1" 2>&1 &
  "${cuda_build}/ocudu-zmq-source" --endpoint 'tcp://*:15101' --batch-samples 23040 --duration 6s >"${dir}/s2" 2>&1 &
  "${cuda_build}/ocudu-gpu-channel" --config "${topo}" --duration 6s >"${dir}/broker" 2>&1 &
  sleep 1
  "${cuda_build}/ocudu-zmq-sink" --endpoint 'tcp://127.0.0.1:15001' --duration 5s >"${dir}/k1" 2>&1 &
  "${cuda_build}/ocudu-zmq-sink" --endpoint 'tcp://127.0.0.1:15100' --duration 5s >"${dir}/k2" 2>&1 &
  wait

  local stop; stop="$(grep event=stop "${dir}/broker" || true)"
  [[ -n "${stop}" ]] || { cat "${dir}/broker"; fail "${label}: broker did not finish"; }
  local ovf gap zmq pulls; ovf="$(counter_of tx_queue_overflows "${stop}")"
  gap="$(counter_of tx_sequence_gaps "${stop}")"; zmq="$(counter_of zmq_errors "${stop}")"
  pulls="$(counter_of tx_pulls "${stop}")"
  [[ "${ovf:-1}" == 0 && "${gap:-1}" == 0 && "${zmq:-1}" == 0 ]] \
    || fail "${label}: broker data-integrity counter nonzero -- ${stop}"
  [[ "${pulls:-0}" -gt 0 ]] || fail "${label}: broker relayed no IQ"

  local ap1 ap2; ap1="$(power_of "${dir}/k1")"; ap2="$(power_of "${dir}/k2")"
  for ap in "${ap1}" "${ap2}"; do
    [[ -n "${ap}" ]] || fail "${label}: a sink received no IQ"
    awk -v v="${ap}" -v e="${expect}" -v t="${tol}" \
      'BEGIN{d=v-e; if(d<0)d=-d; exit !(d<=t)}' \
      || fail "${label}: sink avg_power ${ap} not within ${tol} of ${expect}"
  done
  echo "    ${label}: pulls=${pulls} avg_power=${ap1}/${ap2} (expected ~${expect}) -- counters clean"
}

echo "== [3/4] synthetic CUDA relay loop -- clean 0 dB channel =="
clean_topo="$(mktemp --suffix=.yaml)"
write_topology "${clean_topo}" "      - type: gain
        gain_db: 0"
run_relay_check "clean relay" "${clean_topo}" 1.0 0.05

echo "== [4/4] synthetic CUDA relay loop -- AWGN channel (noise_power 0.25) =="
awgn_topo="$(mktemp --suffix=.yaml)"
write_topology "${awgn_topo}" "      - type: awgn
        noise_power: 0.25"
# Unit-power tone + AWGN noise_power 0.25 -> sink avg_power ~= 1.25.
run_relay_check "AWGN relay" "${awgn_topo}" 1.25 0.05

echo "GPU TEST SEQUENCE PASSED"
REMOTE
