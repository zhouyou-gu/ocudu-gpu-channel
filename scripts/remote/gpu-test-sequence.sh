#!/usr/bin/env bash
# Locked-in GPU validation sequence for the RTX workstation.
#
# Runs, in order, on the remote GPU host:
#   [1/9] CUDA release build
#   [2/9] ctest (config, processing incl. the CUDA AWGN/delay checks, ring, broker)
#   [3/9] synthetic CUDA relay loop  -- clean 0 dB channel, must relay IQ both
#         ways with zero data-integrity counters
#   [4/9] synthetic CUDA relay loop  -- AWGN channel, sink power must equal
#         signal power + noise_power
#   [5/9] synthetic CUDA 3-node interference graph -- the two UE uplinks must
#         superpose at the gNB RX (gnb0 RX power ~= 2x a desired uplink),
#         exercising the GPU superposition kernel and the multi-device broker
#   [6/9] synthetic CUDA 2-cell multi-gNB graph -- four nodes, eight links;
#         every RX is its serving link plus the other cell's interference
#   [9/9] live control plane -- a correlation_swap REQ sent to a RUNNING broker
#         must change the received power. Until M4.6 every control-plane gate
#         called the handler directly, so the wire path (REQ in, shadow written,
#         snapped at a slot boundary, output changed) had never run end to end.
#   [8/9] synthetic CUDA 2x2 correlated MIMO relay -- a declared spatial
#         correlation must be visible in the received power, and the same
#         topology with kind: iid must not show it. This is the only
#         broker-level test of the MIMO path: M1-M3 built lane expansion,
#         multi-row output, correlation and coherent LOS, and every one of
#         their gates called the processor directly.
#   [7/9] synthetic CUDA TDL-A profile relay -- TR 38.901 §7.7.2 NLOS 23-tap
#         multipath with Jakes fading on a bidirectional gNB↔UE pair; broker
#         counters must stay clean and the sink avg_power must match the sum
#         of tap powers (≈0.50 for the published TDL-A profile)
#
# It rsyncs the local working tree first, so it validates uncommitted code.
# The OCUDU Docker gNB/srsUE attach paths are separate, heavier tests
# (scripts/remote/ocudu-attach-smoke.sh -- single UE; ocudu-multi-ue-smoke.sh
# -- two UEs, one gNB; ocudu-multi-gnb-smoke.sh -- two gNBs, two cells) and are
# not part of this sequence.
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

# 2x2 MIMO through the real broker: four TX ports feeding four RX rows, with a
# declared TX-side correlation. Every source emits the SAME unit-power tone, so
# a row receives (g_r0 + g_r1) x and its expected power is
#   P_taps * (1 + 1 + 2*Re(R_tx)) = 3.468 * 2.8 = 9.71   (kronecker, R_tx = 0.4)
#   P_taps * (1 + 1)              = 3.468 * 2   = 6.94   (iid)
# The two are 2.8 apart, which is what makes this a test of the correlation and
# not merely of the relay: an implementation that dropped the mixing lands on
# the iid number with everything else looking healthy.
run_mimo_correlated_check() {
  local label="$1" topo="$2" expect="$3" tol="$4" base="$5"
  local dir; dir="$(mktemp -d)"
  local tx=("${base}" "$((base+2))" "$((base+4))" "$((base+6))")
  local rx=("$((base+1))" "$((base+3))" "$((base+5))" "$((base+7))")
  for p in "${tx[@]}" "${rx[@]}"; do
    if ss -ltn 2>/dev/null | grep -q ":${p}\b"; then fail "${label}: port ${p} busy"; fi
  done
  for p in "${tx[@]}"; do
    "${cuda_build}/ocudu-zmq-source" --endpoint "tcp://*:${p}" --batch-samples 23040 \
      --duration 7s >"${dir}/src_${p}" 2>&1 &
  done
  "${cuda_build}/ocudu-gpu-channel" --config "${topo}" --duration 7s >"${dir}/broker" 2>&1 &
  sleep 1
  for p in "${rx[@]}"; do
    "${cuda_build}/ocudu-zmq-sink" --endpoint "tcp://127.0.0.1:${p}" --duration 5s \
      >"${dir}/sink_${p}" 2>&1 &
  done
  wait

  local stop; stop="$(grep event=stop "${dir}/broker" || true)"
  [[ -n "${stop}" ]] || { cat "${dir}/broker"; fail "${label}: broker did not finish"; }
  local ovf gap zmq pulls
  ovf="$(counter_of tx_queue_overflows "${stop}")"; gap="$(counter_of tx_sequence_gaps "${stop}")"
  zmq="$(counter_of zmq_errors "${stop}")"; pulls="$(counter_of tx_pulls "${stop}")"
  [[ "${ovf:-1}" == 0 && "${gap:-1}" == 0 && "${zmq:-1}" == 0 ]] \
    || fail "${label}: broker data-integrity counter nonzero -- ${stop}"
  [[ "${pulls:-0}" -gt 0 ]] || fail "${label}: broker relayed no IQ"
  grep -q "event=radio_node_resolved id=gnb .*implicit=false" "${dir}/broker" \
    || fail "${label}: the broker did not resolve the declared multi-port nodes"

  # Judge the mean over the four RX ports: a single port's power carries the
  # whole single-realization spread of a 23-tap Rayleigh profile, the mean about
  # half of it.
  local sum=0 n=0 ap all=""
  for p in "${rx[@]}"; do
    ap="$(power_of "${dir}/sink_${p}")"
    [[ -n "${ap}" ]] || fail "${label}: RX port ${p} received no IQ"
    all="${all}${ap} "
    sum="$(awk -v a="${sum}" -v b="${ap}" 'BEGIN{print a+b}')"
    n=$((n+1))
  done
  local mean; mean="$(awk -v s="${sum}" -v n="${n}" 'BEGIN{print s/n}')"
  awk -v v="${mean}" -v e="${expect}" -v t="${tol}" \
    'BEGIN{d=v-e; if(d<0)d=-d; exit !(d<=t)}' \
    || fail "${label}: mean RX power ${mean} not within ${tol} of ${expect} (ports: ${all})"
  echo "    ${label}: pulls=${pulls} mean_rx_power=${mean} (expected ~${expect}) -- counters clean"
}

# A correlation_swap against a live broker. The topology declares `kind: iid`,
# which is the opt-in that makes a link runtime-correlatable; the REQ then turns
# on a TX-side correlation of 0.4 partway through the run. Received power is the
# observable: 3.468 * 2 = 6.94 while iid, 3.468 * 2.8 = 9.71 once correlated, so
# the cumulative average lands between them and well above the iid figure.
run_control_swap_check() {
  local label="$1" topo="$2" base="$3" control_port="$4"
  local dir; dir="$(mktemp -d)"
  local tx=("${base}" "$((base+2))" "$((base+4))" "$((base+6))")
  local rx=("$((base+1))" "$((base+3))" "$((base+5))" "$((base+7))")
  for p in "${tx[@]}"; do
    "${cuda_build}/ocudu-zmq-source" --endpoint "tcp://*:${p}" --batch-samples 23040 \
      --duration 9s >"${dir}/src_${p}" 2>&1 &
  done
  "${cuda_build}/ocudu-gpu-channel" --config "${topo}" --duration 9s \
    --control-endpoint "tcp://*:${control_port}" >"${dir}/broker" 2>&1 &
  sleep 1
  for p in "${rx[@]}"; do
    "${cuda_build}/ocudu-zmq-sink" --endpoint "tcp://127.0.0.1:${p}" --duration 7s \
      >"${dir}/sink_${p}" 2>&1 &
  done
  sleep 1
  local swapped=0
  for link in "gnb>ue:tdl_a_corr" "ue>gnb:tdl_a_corr"; do
    if "${cuda_build}/ocudu-control-req" --endpoint "tcp://127.0.0.1:${control_port}" \
        --message "{\"type\":\"correlation_swap\",\"link_id\":\"${link}\",\"kind\":\"kronecker\",\"tx\":[{\"i\":0,\"j\":1,\"re\":0.4,\"im\":0.0}]}" \
        >>"${dir}/req" 2>&1; then
      swapped=$((swapped+1))
    fi
  done
  wait

  [[ "${swapped}" == 2 ]] || { cat "${dir}/req"; fail "${label}: the broker refused the swap"; }
  local stop; stop="$(grep event=stop "${dir}/broker" || true)"
  [[ -n "${stop}" ]] || { cat "${dir}/broker"; fail "${label}: broker did not finish"; }
  grep -q "param=correlation_swap" "${dir}/broker" \
    || fail "${label}: the broker logged no correlation_swap"

  local sum=0 n=0 ap
  for p in "${rx[@]}"; do
    ap="$(power_of "${dir}/sink_${p}")"
    [[ -n "${ap}" ]] || fail "${label}: RX port ${p} received no IQ"
    sum="$(awk -v a="${sum}" -v b="${ap}" 'BEGIN{print a+b}')"
    n=$((n+1))
  done
  local mean; mean="$(awk -v s="${sum}" -v n="${n}" 'BEGIN{print s/n}')"
  # Must have moved decisively off the iid value (6.94) toward 9.71. The
  # midpoint is 8.3; anything at or below it means the swap did not take.
  awk -v v="${mean}" 'BEGIN{exit !(v > 8.3)}' \
    || fail "${label}: mean RX power ${mean} did not move off the iid value -- the swap did not take"
  echo "    ${label}: mean_rx_power=${mean} after the swap (iid would be ~6.94) -- applied live"
}

echo "== [1/9] CUDA release build =="
cmake -S "${project_root}" -B "${cuda_build}" \
  -DCMAKE_BUILD_TYPE=Release \
  -DOCUDU_GPU_CHANNEL_ENABLE_CUDA=ON \
  -DCMAKE_CUDA_COMPILER="${workspace}/tools/cuda-12.8.1/bin/nvcc" \
  -DOCUDU_GPU_CHANNEL_CUDA_ARCHITECTURES=120 >/tmp/gpu-seq-cmake.log 2>&1 \
  || { tail -25 /tmp/gpu-seq-cmake.log; fail "cmake configure"; }
cmake --build "${cuda_build}" -j"$(nproc)" >/tmp/gpu-seq-build.log 2>&1 \
  || { tail -25 /tmp/gpu-seq-build.log; fail "cmake build"; }
echo "    build OK"

echo "== [2/9] unit tests =="
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

check_power() {
  # $1 label  $2 measured avg_power  $3 expected  $4 tolerance
  [[ -n "$2" ]] || fail "$1: received no IQ"
  awk -v v="$2" -v e="$3" -v t="$4" 'BEGIN{d=v-e; if(d<0)d=-d; exit !(d<=t)}' \
    || fail "$1: avg_power $2 not within $4 of $3"
}

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

run_graph_check() {
  # Drives the CUDA broker on the 3-node interference graph: gnb0 plus two UEs,
  # the two UE uplinks superposing at the gnb0 RX. $1 is the topology file.
  local label="3-node graph" topo="$1"
  for p in 16000 16001 16002 16003 16004 16005; do
    if ss -ltn 2>/dev/null | grep -q ":${p}\b"; then fail "${label}: port ${p} busy"; fi
  done
  local dir; dir="$(mktemp -d)"
  # One synthetic source per device TX, one synthetic sink per device RX.
  "${cuda_build}/ocudu-zmq-source" --endpoint 'tcp://*:16000' --batch-samples 23040 --duration 6s >"${dir}/s_gnb" 2>&1 &
  "${cuda_build}/ocudu-zmq-source" --endpoint 'tcp://*:16002' --batch-samples 23040 --duration 6s >"${dir}/s_ue0" 2>&1 &
  "${cuda_build}/ocudu-zmq-source" --endpoint 'tcp://*:16004' --batch-samples 23040 --duration 6s >"${dir}/s_ue1" 2>&1 &
  "${cuda_build}/ocudu-gpu-channel" --config "${topo}" --duration 6s >"${dir}/broker" 2>&1 &
  sleep 1
  "${cuda_build}/ocudu-zmq-sink" --endpoint 'tcp://127.0.0.1:16001' --duration 5s >"${dir}/k_gnb" 2>&1 &
  "${cuda_build}/ocudu-zmq-sink" --endpoint 'tcp://127.0.0.1:16003' --duration 5s >"${dir}/k_ue0" 2>&1 &
  "${cuda_build}/ocudu-zmq-sink" --endpoint 'tcp://127.0.0.1:16005' --duration 5s >"${dir}/k_ue1" 2>&1 &
  wait

  local stop; stop="$(grep event=stop "${dir}/broker" || true)"
  [[ -n "${stop}" ]] || { cat "${dir}/broker"; fail "${label}: broker did not finish"; }
  local ovf gap zmq pulls
  ovf="$(counter_of tx_queue_overflows "${stop}")"
  gap="$(counter_of tx_sequence_gaps "${stop}")"
  zmq="$(counter_of zmq_errors "${stop}")"
  pulls="$(counter_of tx_pulls "${stop}")"
  [[ "${ovf:-1}" == 0 && "${gap:-1}" == 0 && "${zmq:-1}" == 0 ]] \
    || fail "${label}: broker data-integrity counter nonzero -- ${stop}"
  [[ "${pulls:-0}" -gt 0 ]] || fail "${label}: broker relayed no IQ"

  local ap_gnb ap_ue0 ap_ue1
  ap_gnb="$(power_of "${dir}/k_gnb")"
  ap_ue0="$(power_of "${dir}/k_ue0")"
  ap_ue1="$(power_of "${dir}/k_ue1")"
  # gnb0 RX = two coherent UE uplinks (desired -3 dB each) summed + 0.01 floor.
  check_power "${label} gnb0 RX" "${ap_gnb}" 2.015 0.15
  # ue RX = desired downlink (-3 dB) + negligible -40 dB crosstalk + 0.01 floor.
  check_power "${label} ue0 RX" "${ap_ue0}" 0.511 0.07
  check_power "${label} ue1 RX" "${ap_ue1}" 0.511 0.07
  echo "    ${label}: pulls=${pulls} gnb0_rx=${ap_gnb} ue0_rx=${ap_ue0} ue1_rx=${ap_ue1} -- counters clean"
}

run_multi_gnb_check() {
  # Drives the CUDA broker on the 2-cell multi-gNB graph: gnb0/gnb1 plus one UE
  # each, every RX a superposition of its serving link and the other cell's
  # interference. $1 is the topology file.
  local label="2-cell multi-gNB" topo="$1"
  for p in 3000 3001 3002 3003 3100 3101 3102 3103; do
    if ss -ltn 2>/dev/null | grep -q ":${p}\b"; then fail "${label}: port ${p} busy"; fi
  done
  local dir; dir="$(mktemp -d)"
  # One synthetic source per device TX, one synthetic sink per device RX.
  "${cuda_build}/ocudu-zmq-source" --endpoint 'tcp://*:3000' --batch-samples 23040 --duration 6s >"${dir}/s_gnb0" 2>&1 &
  "${cuda_build}/ocudu-zmq-source" --endpoint 'tcp://*:3002' --batch-samples 23040 --duration 6s >"${dir}/s_gnb1" 2>&1 &
  "${cuda_build}/ocudu-zmq-source" --endpoint 'tcp://*:3101' --batch-samples 23040 --duration 6s >"${dir}/s_ue0" 2>&1 &
  "${cuda_build}/ocudu-zmq-source" --endpoint 'tcp://*:3103' --batch-samples 23040 --duration 6s >"${dir}/s_ue1" 2>&1 &
  "${cuda_build}/ocudu-gpu-channel" --config "${topo}" --duration 6s >"${dir}/broker" 2>&1 &
  sleep 1
  "${cuda_build}/ocudu-zmq-sink" --endpoint 'tcp://127.0.0.1:3001' --duration 5s >"${dir}/k_gnb0" 2>&1 &
  "${cuda_build}/ocudu-zmq-sink" --endpoint 'tcp://127.0.0.1:3003' --duration 5s >"${dir}/k_gnb1" 2>&1 &
  "${cuda_build}/ocudu-zmq-sink" --endpoint 'tcp://127.0.0.1:3100' --duration 5s >"${dir}/k_ue0" 2>&1 &
  "${cuda_build}/ocudu-zmq-sink" --endpoint 'tcp://127.0.0.1:3102' --duration 5s >"${dir}/k_ue1" 2>&1 &
  wait

  local stop; stop="$(grep event=stop "${dir}/broker" || true)"
  [[ -n "${stop}" ]] || { cat "${dir}/broker"; fail "${label}: broker did not finish"; }
  local ovf gap zmq pulls
  ovf="$(counter_of tx_queue_overflows "${stop}")"
  gap="$(counter_of tx_sequence_gaps "${stop}")"
  zmq="$(counter_of zmq_errors "${stop}")"
  pulls="$(counter_of tx_pulls "${stop}")"
  [[ "${ovf:-1}" == 0 && "${gap:-1}" == 0 && "${zmq:-1}" == 0 ]] \
    || fail "${label}: broker data-integrity counter nonzero -- ${stop}"
  [[ "${pulls:-0}" -gt 0 ]] || fail "${label}: broker relayed no IQ"

  # Every RX = serving link (path loss 6 dB -> 0.251) + the other cell's
  # interference (path loss 20 dB -> 0.010) + serving AWGN (snr 25 dB ->
  # 0.0008). The serving CFO sweeps the cross term out of the mean power, so
  # each RX averages ~= 0.262 regardless of node.
  local ap
  for who in gnb0 gnb1 ue0 ue1; do
    ap="$(power_of "${dir}/k_${who}")"
    check_power "${label} ${who} RX" "${ap}" 0.262 0.06
    echo "    ${label}: ${who}_rx=${ap}"
  done
  echo "    ${label}: pulls=${pulls} -- counters clean"
}

echo "== [3/9] synthetic CUDA relay loop -- clean 0 dB channel =="
clean_topo="$(mktemp --suffix=.yaml)"
write_topology "${clean_topo}" "      - type: tdl
        taps:
          - delay_samples: 0.0
            gain_db: 0.0
            phase_rad: 0.0"
run_relay_check "clean relay" "${clean_topo}" 1.0 0.05

echo "== [4/9] synthetic CUDA relay loop -- AWGN channel (noise_power 0.25) =="
awgn_topo="$(mktemp --suffix=.yaml)"
write_topology "${awgn_topo}" "      - type: awgn
        noise_power: 0.25"
# Unit-power tone + AWGN noise_power 0.25 -> sink avg_power ~= 1.25.
run_relay_check "AWGN relay" "${awgn_topo}" 1.25 0.05

echo "== [5/9] synthetic CUDA 3-node interference graph =="
run_graph_check "${project_root}/examples/topology.graph.cuda.yaml"

echo "== [6/9] synthetic CUDA 2-cell multi-gNB graph =="
run_multi_gnb_check "${project_root}/examples/topology.multi-gnb.cuda.yaml"

echo "== [7/9] synthetic CUDA TDL-A profile relay (TR 38.901 §7.7.2, 23-tap NLOS, Jakes 100 Hz) =="
# Sum of TDL-A tap linear powers ≈ 3.468 -- the ensemble-mean sink avg_power for
# a unit-power input (uncorrelated Rayleigh taps, WSSUS). A single 5 s
# realization has ~±0.5 stddev cross-tap residual because Rayleigh tap pairs
# don't fully decorrelate in 5 s × 100 Hz Doppler; tolerance widened to ±1.5
# (about 3 sigma) so this smoke test does not flake on benign statistical
# fluctuation. Heavier statistical fading validation is a separate test.
tdl_a_topo="$(mktemp --suffix=.yaml)"
# Reuse the example YAML's chain; rewrite endpoints to the test's 15000-range
# ports so it can run through the same run_relay_check helper.
sed -e 's|tcp://127.0.0.1:17000|tcp://127.0.0.1:15000|' \
    -e 's|tcp://\*:17001|tcp://*:15001|' \
    -e 's|tcp://127.0.0.1:17101|tcp://127.0.0.1:15101|' \
    -e 's|tcp://\*:17100|tcp://*:15100|' \
    "${project_root}/examples/topology.tdl-a.cuda.yaml" > "${tdl_a_topo}"
run_relay_check "TDL-A profile" "${tdl_a_topo}" 3.468 1.5

echo "== [8/9] synthetic CUDA 2x2 correlated MIMO relay =="
# Correlated first, then the SAME topology with the correlation block stripped.
# Running both is the point: 9.71 alone could be a coincidence of the profile,
# while 9.71 next to 6.94 is the declared R_tx and nothing else.
mimo_corr_topo="$(mktemp --suffix=.yaml)"
sed -e 's|:172|:152|g' "${project_root}/examples/topology.mimo-2x2-correlated.cuda.yaml" \
  > "${mimo_corr_topo}"
run_mimo_correlated_check "2x2 correlated" "${mimo_corr_topo}" 9.71 1.5 15200

mimo_iid_topo="$(mktemp --suffix=.yaml)"
sed -e 's|:172|:153|g' "${project_root}/examples/topology.mimo-2x2-correlated.cuda.yaml" \
  | awk '/^    spatial_correlation:/{skip=1} /^    chain:/{skip=0} !skip' > "${mimo_iid_topo}"
run_mimo_correlated_check "2x2 iid (control)" "${mimo_iid_topo}" 6.94 1.5 15300

echo "== [9/9] live control plane -- correlation_swap against a running broker =="
control_topo="$(mktemp --suffix=.yaml)"
sed -e 's|:172|:154|g' "${project_root}/examples/topology.mimo-2x2-correlated.cuda.yaml" \
  | awk '/^      kind: kronecker/{print "      kind: iid"; skip=1; next} /^    chain:/{skip=0} !skip' \
  > "${control_topo}"
run_control_swap_check "live correlation_swap" "${control_topo}" 15400 15599

echo "GPU TEST SEQUENCE PASSED"
REMOTE
