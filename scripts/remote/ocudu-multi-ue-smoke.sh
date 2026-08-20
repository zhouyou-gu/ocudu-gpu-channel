#!/usr/bin/env bash
# Milestone B2: one OCUDU gNB + two srsUE containers attaching together through
# the CUDA broker, the two uplinks superposing at the gNB RX.
#
# This is the locked-in multi-UE OCUDU test -- the two-UE counterpart of the
# single-UE ocudu-attach-smoke.sh. The lightweight synthetic+ctest checks live
# in gpu-test-sequence.sh; this Docker stack is the heavier separate test.
#
# One-command test: rsyncs the working tree, builds, brings up Open5GS (with
# both subscribers) + the gNB + the broker on the multi-UE topology, launches
# two srsUE containers, and reports whether both reach RRC / PDU / ping.
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${script_dir}/common.sh"

duration_seconds="${OCUDU_MUE_DURATION_SECONDS:-45}"
build_docker="${OCUDU_MUE_BUILD_DOCKER:-1}"
# Non-empty = run the broker as this Docker image (docker run --gpus all
# --network host) instead of the native build. Default = native binary.
broker_image="${OCUDU_MUE_BROKER_IMAGE:-}"
# srsUE launch stagger: srsRAN ZMQ radios share the broker's lock-step virtual
# time, so two UEs started together transmit the identical RACH preamble on the
# identical PRACH occasion and the gNB merges them onto one C-RNTI. Starting ue1
# after ue0 is RRC-connected makes ue1 RACH on a later occasion -> distinct
# C-RNTIs. 0 disables the stagger (the both-at-once collision repro).
ue_stagger_seconds="${OCUDU_MUE_UE_STAGGER_SECONDS:-10}"
srsran_ref="${SRSRAN_4G_REF:-release_23_11}"
ping_count="${OCUDU_MUE_PING_COUNT:-3}"
# How many srsUEs to attach. Two is the historical value and keeps this gate's
# original behaviour; the harness itself is not limited to two.
ue_count="${OCUDU_MUE_UE_COUNT:-2}"
# Multi-port knobs. Defaults reproduce the original single-antenna gate, so an
# unset environment behaves exactly as before. Values crossing the ssh hop must
# be whitespace-free: ssh flattens the remote argument vector into one string.
gnb_config_rel="${OCUDU_MUE_GNB_CONFIG:-examples/ocudu/gnb_zmq_b210_fdd_srsue.yaml}"
gnb_tx_ports="${OCUDU_MUE_GNB_TX_PORTS:-2000}"
gnb_tx_ports="${gnb_tx_ports// /,}"
topology_rel="${OCUDU_MUE_TOPOLOGY:-examples/topology.ocudu-docker.multi-ue.cuda.yaml}"
matrix_enabled="${OCUDU_MUE_MATRIX:-0}"
matrix_args="${OCUDU_MUE_MATRIX_ARGS:-}"
gate_name="${OCUDU_MUE_GATE_NAME:-ocudu-multi-ue}"

case "${REMOTE_PROJECT_ROOT}" in
  "~/"*) remote_dest="${REMOTE_PROJECT_ROOT#\~/}" ;;
  *) remote_dest="${REMOTE_PROJECT_ROOT}" ;;
esac
echo "syncing working tree to ${REMOTE_USER}@${REMOTE_HOST}:${remote_dest}"
rsync -az --delete \
  --exclude '.git' --exclude 'build*' --exclude '.config' \
  -e "ssh -i ${REMOTE_SSH_KEY} -o BatchMode=yes -o ConnectTimeout=8" \
  "${repo_root}/" "${REMOTE_USER}@${REMOTE_HOST}:${remote_dest}/"

remote_sh bash -s -- \
  "${REMOTE_WORKSPACE}" \
  "${REMOTE_PROJECT_ROOT}" \
  "${REMOTE_BUILDS_ROOT}" \
  "${REMOTE_RESULTS_ROOT}" \
  "${REMOTE_OCUDU_ROOT}" \
  "${duration_seconds}" \
  "${build_docker}" \
  "${srsran_ref}" \
  "${ue_stagger_seconds}" \
  "${broker_image:--}" \
  "${gnb_config_rel}" \
  "${gnb_tx_ports}" \
  "${topology_rel}" \
  "${matrix_enabled}" \
  "$(printf '%s' "${matrix_args}" | base64 | tr -d '\n'):-" \
  "${ping_count}" \
  "${ue_count}" \
  "${gate_name}" <<'REMOTE'
set -euo pipefail

workspace="$1"
project_root="$2"
builds_root="$3"
results_root="$4"
ocudu_root="$5"
duration_seconds="$6"
build_docker="$7"
srsran_ref="$8"
ue_stagger_seconds="$9"
# Default-empty: an empty trailing arg can be dropped in ssh transport.
broker_image="${10:-}"
[[ "${broker_image}" == "-" ]] && broker_image=""
gnb_config_rel="${11}"
gnb_tx_ports="${12}"
topology_rel="${13}"
matrix_enabled="${14}"
matrix_args_b64="${15%:-}"
matrix_args=""
if [[ -n "${matrix_args_b64}" ]]; then
  matrix_args="$(printf '%s' "${matrix_args_b64}" | base64 -d)"
fi
ping_count="${16}"
ue_count="${17}"
gate_name="${18}"

expand_remote_path() {
  case "$1" in
    "~") printf '%s\n' "${HOME}" ;;
    "~/"*) printf '%s/%s\n' "${HOME}" "${1#~/}" ;;
    *) printf '%s\n' "$1" ;;
  esac
}

extract_counter() {
  local value
  value="$(printf '%s\n' "$2" | sed -n "s/.*$1=\([0-9][0-9]*\).*/\1/p")"
  printf '%s\n' "${value:-0}"
}

workspace="$(expand_remote_path "${workspace}")"
project_root="$(expand_remote_path "${project_root}")"
builds_root="$(expand_remote_path "${builds_root}")"
results_root="$(expand_remote_path "${results_root}")"
ocudu_root="$(expand_remote_path "${ocudu_root}")"

if [[ ! -f "${workspace}/tools/env.sh" ]]; then
  echo "missing ${workspace}/tools/env.sh; run scripts/remote/bootstrap-user-tools.sh first" >&2
  exit 1
fi
# shellcheck source=/dev/null
source "${workspace}/tools/env.sh"

if [[ ! -d "${ocudu_root}/docker" ]]; then
  echo "missing OCUDU checkout with docker directory: ${ocudu_root}" >&2
  exit 1
fi

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
log_dir="${results_root}/logs/${gate_name}/${timestamp}"
report_dir="${results_root}/reports/${gate_name}/${timestamp}"
config_dir="${workspace}/configs/ocudu-multi-ue/${timestamp}"
cuda_build="${builds_root}/ocudu-gpu-channel/cuda-release"
summary_path="${report_dir}/multi-ue-summary.json"
mkdir -p "${log_dir}" "${report_dir}" "${config_dir}" "${cuda_build}"

declare -a rrc pdu ping ue_pids
for ((i = 0; i < ue_count; i++)); do
  rrc[i]=0; pdu[i]=0; ping[i]=0; ue_pids[i]=""
done
broker_status=0; rx_starvations=0; tx_queue_overflows=0; tx_sequence_gaps=0; zmq_errors=0; gnb_overflow=0

write_summary() {
  local status="$1"
  local exit_code="$2"
  cat >"${summary_path}" <<JSON
{
  "timestamp": "${timestamp}",
  "status": "${status}",
  "duration_seconds": ${duration_seconds},
  "ue_count": ${ue_count},
  "ues": [$(for ((i = 0; i < ue_count; i++)); do
      printf '%s{ "id": "ue%d", "rrc_connected": %d, "pdu_session_established": %d, "ping_ok": %d }' \
        "$([[ $i -gt 0 ]] && printf ', ')" "$i" "${rrc[i]}" "${pdu[i]}" "${ping[i]}"
    done)],
  "gnb_rt_overflow": ${gnb_overflow},
  "broker_status": ${broker_status},
  "rx_starvations": ${rx_starvations},
  "tx_queue_overflows": ${tx_queue_overflows},
  "tx_sequence_gaps": ${tx_sequence_gaps},
  "zmq_errors": ${zmq_errors},
  "matrix_enabled": ${matrix_enabled:-0},
  "matrix_status": ${matrix_status:-0},
  "gate": "${gate_name}",
  "log_dir": "${log_dir}"
}
JSON
  printf 'summary=%s\n' "${summary_path}"
  printf 'status=%s\n' "${status}"
  exit "${exit_code}"
}

# --- build ---------------------------------------------------------------------
cmake -S "${project_root}" -B "${cuda_build}" \
  -DCMAKE_BUILD_TYPE=Release -DOCUDU_GPU_CHANNEL_ENABLE_CUDA=ON \
  -DCMAKE_CUDA_COMPILER="${workspace}/tools/cuda-12.8.1/bin/nvcc" \
  -DOCUDU_GPU_CHANNEL_CUDA_ARCHITECTURES=120 >"${log_dir}/cmake-configure.log" 2>&1
cmake --build "${cuda_build}" -j"$(nproc)" >"${log_dir}/cmake-build.log" 2>&1

# --- generated config ----------------------------------------------------------
gnb_config="${config_dir}/gnb_zmq_b210_fdd_srsue.yaml"
compose_override="${config_dir}/docker-compose.ocudu-gpu-channel.yml"
ocudu_dockerfile="${config_dir}/Dockerfile.ocudu-zmq"
srsue_dockerfile="${config_dir}/Dockerfile.srsue"
cp "${project_root}/${gnb_config_rel}" "${gnb_config}"

awk '
  { print }
  /install_docker_dependencies\.sh build/ {
    print "RUN apt-get update && apt-get install -y --no-install-recommends libzmq3-dev && rm -rf /var/lib/apt/lists/*"
  }
  /^FROM .* AS runtime/ {
    print "RUN apt-get update && apt-get install -y --no-install-recommends libzmq5 && rm -rf /var/lib/apt/lists/*"
  }
' "${ocudu_root}/docker/Dockerfile" >"${ocudu_dockerfile}"

# Compose override: publish the gNB TX port, and point Open5GS at a two-UE
# subscriber CSV (environment overrides the single-UE inline SUBSCRIBER_DB).
# OCUDU's compose hard-codes the `ran` and `metrics` subnets. On a workstation
# already hosting another 5G stack those pools are taken and compose fails the
# whole run before a container starts, so pick pools nothing else claims and
# render an Open5GS env file carrying the matching address -- the core binds the
# literal address from that file and aborts if it is left behind.
pick_free_subnet() {
  local -a taken
  mapfile -t taken < <(docker network ls --format '{{.Name}}' | while read -r net; do
    docker network inspect "${net}" --format '{{range .IPAM.Config}}{{.Subnet}} {{end}}' 2>/dev/null
  done | tr ' ' '\n' | sed '/^$/d')
  local candidate existing clash
  for candidate in "$@"; do
    clash=0
    for existing in "${taken[@]}"; do
      [[ "${existing}" == "${candidate}" ]] && clash=1 && break
    done
    [[ "${clash}" -eq 0 ]] && printf '%s\n' "${candidate}" && return 0
  done
  return 1
}
ran_subnet="${OCUDU_MUE_RAN_SUBNET:-$(pick_free_subnet 10.53.1.0/24 10.63.1.0/24 10.64.1.0/24 10.65.1.0/24)}"
metrics_subnet="${OCUDU_MUE_METRICS_SUBNET:-$(pick_free_subnet 172.19.1.0/24 172.29.1.0/24 172.30.1.0/24)}"
[[ -n "${ran_subnet}" && -n "${metrics_subnet}" ]] || { echo "no free /24 for the gate networks" >&2; exit 1; }
ran_prefix="${ran_subnet%.0/24}"
metrics_prefix="${metrics_subnet%.0/24}"
export OPEN5GS_IP="${ran_prefix}.2"
export GNB_IP="${ran_prefix}.3"
printf 'ran_subnet=%s\nmetrics_subnet=%s\n' "${ran_subnet}" "${metrics_subnet}" >"${log_dir}/network-selection.txt"

open5gs_env="${config_dir}/open5gs.env"
sed -e "s|^OPEN5GS_IP=.*|OPEN5GS_IP=${OPEN5GS_IP}|" \
    -e "s|^UPF_ADVERTISE_IP=.*|UPF_ADVERTISE_IP=${OPEN5GS_IP}|" \
    "${ocudu_root}/docker/open5gs/open5gs.env" >"${open5gs_env}"
export OPEN_5GS_ENV_FILE="${open5gs_env}"

gnb_port_lines=""
IFS=',' read -r -a gnb_tx_port_list <<<"${gnb_tx_ports}"
for port in "${gnb_tx_port_list[@]}"; do
  [[ -n "${port}" ]] || continue
  gnb_port_lines+="      - \"${port}:${port}\"
"
done

capture_args=()
capture_dir=""
if [[ "${matrix_enabled}" == "1" ]]; then
  capture_dir="${report_dir}/wire-capture"
  mkdir -p "${capture_dir}"
  capture_args=(--wire-capture-dir "${capture_dir}"
                --wire-capture-samples "${OCUDU_MUE_CAPTURE_SAMPLES:-4608000}"
                --wire-capture-skip "${OCUDU_MUE_CAPTURE_SKIP:-460800000}")
fi

cat >"${compose_override}" <<YAML
services:
  5gc:
    environment:
      SUBSCRIBER_DB: /open5gs/subscriber_db.csv
    networks:
      ran:
        ipv4_address: ${OPEN5GS_IP}
  gnb:
    ports:
${gnb_port_lines}    extra_hosts:
      - "host.docker.internal:host-gateway"
    networks:
      ran:
        ipv4_address: ${GNB_IP}
      metrics:
        ipv4_address: ${metrics_prefix}.3
    build:
      dockerfile: \${OCUDU_ZMQ_DOCKERFILE}
      args:
        EXTRA_CMAKE_ARGS: "-DENABLE_ZEROMQ=ON -DENABLE_EXPORT=ON -DZEROMQ_INCLUDE_DIRS=/usr/include -DZEROMQ_LIBRARIES=/usr/lib/x86_64-linux-gnu/libzmq.so"
        OS: "ubuntu"
        OS_VERSION: "24.04"
networks:
  ran:
    ipam:
      driver: default
      config:
        - subnet: ${ran_subnet}
  metrics:
    ipam:
      driver: default
      config:
        - subnet: ${metrics_subnet}
YAML

cat >"${srsue_dockerfile}" <<'DOCKER'
FROM ubuntu:22.04
ARG SRSRAN_4G_REF=release_23_11
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates cmake g++ gcc git iproute2 iputils-ping \
    libboost-program-options-dev libconfig++-dev libfftw3-dev \
    libmbedtls-dev libsctp-dev libzmq3-dev make net-tools pkg-config \
  && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 --branch "${SRSRAN_4G_REF}" https://github.com/srsran/srsRAN_4G.git /src/srsran_4g \
  && cmake -S /src/srsran_4g -B /src/srsran_4g/build -DCMAKE_BUILD_TYPE=Release \
       -DENABLE_EXPORT=ON -DENABLE_ZEROMQ=ON -DENABLE_UHD=OFF \
  && cmake --build /src/srsran_4g/build -j"$(nproc)" --target srsue \
  && find /src/srsran_4g/build -type f -name srsue -perm -111 -exec cp {} /usr/local/bin/srsue \; -quit
ENTRYPOINT ["srsue"]
DOCKER

# One srsUE config per UE: distinct ZMQ ports and IMSI/IMEI, shared key/OPc.
write_srsue_config() {
  cat >"$1" <<CONF
[rf]
freq_offset = 0
tx_gain = 50
rx_gain = 40
srate = 23.04e6
nof_antennas = 1
device_name = zmq
device_args = tx_port=tcp://*:$2,rx_port=tcp://host.docker.internal:$3,base_srate=23.04e6

[rat.eutra]
dl_earfcn = 2850
nof_carriers = 0

[rat.nr]
bands = 3
nof_carriers = 1
max_nof_prb = 106
nof_prb = 106

[usim]
mode = soft
algo = milenage
opc = 63BFA50EE6523365FF14C1F45F88737D
k = 00112233445566778899AABBCCDDEEFF
imsi = $4
imei = $5

[rrc]
release = 15
ue_category = 4

[nas]
apn = internet
apn_protocol = ipv4

[gw]
netns = ue1
ip_devname = tun_srsue
ip_netmask = 255.255.255.0

[log]
all_level = info
filename = /tmp/srsue.log

[pcap]
enable = none
CONF
}
# One srsUE config and one subscriber per UE. Port pairs step by two from
# 2101/2100, IMSIs and IPs step by one.
#
# The IMEI increments its last TWO digits (...19, ...20, ...21, ...) and is
# zero-padded to keep it 15 digits. Incrementing a single trailing digit
# produces a 16-digit IMEI from the second UE onward, which is malformed: the
# UE reaches RRC and then never completes registration, so it looks exactly
# like a contention failure while being nothing of the kind.
declare -a srsue_configs
for ((i = 0; i < ue_count; i++)); do
  srsue_configs[i]="${config_dir}/srsue${i}_zmq.conf"
  write_srsue_config "${srsue_configs[i]}" \
    "$((2101 + 2 * i))" "$((2100 + 2 * i))" \
    "$(printf '00101012345678%d' "$i")" "$(printf '3534900698733%02d' "$((19 + i))")"
done

# Open5GS subscriber CSV (name,imsi,key,op_type,opc,amf,qci,ip). Replaces the
# single-UE inline SUBSCRIBER_DB via the compose override.
subscriber_db="${ocudu_root}/docker/open5gs/subscriber_db.csv"
rm -rf "${subscriber_db}"
for ((i = 0; i < ue_count; i++)); do
  printf 'ue%d,%s,00112233445566778899aabbccddeeff,opc,63bfa50ee6523365ff14c1f45f88737d,8000,9,10.45.1.%d\n' \
    "$i" "$(printf '00101012345678%d' "$i")" "$((2 + i))" >>"${subscriber_db}"
done

compose=(docker compose -f "${ocudu_root}/docker/docker-compose.yml" -f "${compose_override}")
export GNB_CONFIG_PATH="${gnb_config}"
export OCUDU_ZMQ_DOCKERFILE="${ocudu_dockerfile}"
export OS=ubuntu OS_VERSION=24.04
srsue_image="ocudu-gpu-channel/srsue-zmq:${srsran_ref}"
broker_pid=""

cleanup() {
  set +e
  for ((i = 0; i < ue_count; i++)); do
    [[ -n "${ue_pids[i]:-}" ]] && kill "${ue_pids[i]}" >/dev/null 2>&1
  done
  [[ -n "${broker_pid}" ]] && kill "${broker_pid}" >/dev/null 2>&1
  [[ -n "${broker_image}" ]] && docker rm -f ocudu_broker_mue >/dev/null 2>&1
  for ((i = 0; i < ue_count; i++)); do docker rm -f "ocudu_srsue_${i}" >/dev/null 2>&1; done
  docker cp ocudu_gnb:/tmp/gnb.log "${log_dir}/ocudu-gnb-internal.log" >/dev/null 2>&1
  "${compose[@]}" logs --no-color >"${log_dir}/docker-compose.log" 2>&1
  docker logs open5gs_5gc >"${log_dir}/open5gs.log" 2>&1
  "${compose[@]}" down --remove-orphans --volumes >"${log_dir}/docker-down.log" 2>&1
}
trap cleanup EXIT

"${compose[@]}" down --remove-orphans --volumes >"${log_dir}/docker-preclean.log" 2>&1 || true
docker rm -f open5gs_5gc ocudu_gnb $(for ((i = 0; i < ue_count; i++)); do printf "ocudu_srsue_%d " "$i"; done) >"${log_dir}/docker-rm.log" 2>&1 || true

if [[ "${build_docker}" == "1" ]]; then
  "${compose[@]}" build 5gc gnb >"${log_dir}/docker-build.log" 2>&1
fi
if ! docker build --build-arg "SRSRAN_4G_REF=${srsran_ref}" -f "${srsue_dockerfile}" \
     -t "${srsue_image}" "${config_dir}" >"${log_dir}/srsue-docker-build.log" 2>&1; then
  echo "SRSUE BUILD FAILED"; tail -25 "${log_dir}/srsue-docker-build.log"
  write_summary "srsue_build_failed" 2
fi

"${compose[@]}" up -d 5gc >"${log_dir}/docker-core-up.log" 2>&1
for _ in $(seq 1 90); do
  h="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' open5gs_5gc 2>/dev/null || true)"
  [[ "${h}" == healthy || "${h}" == running ]] && break
  sleep 1
done
echo "open5gs: ${h:-?}"

# CUDA broker on the multi-UE topology (gnb0 RX = ue0->gnb0 + ue1->gnb0).
# Native binary by default; container image when OCUDU_MUE_BROKER_IMAGE is set.
if [[ -z "${broker_image}" ]]; then
  "${cuda_build}/ocudu-gpu-channel" \
    --config "${project_root}/${topology_rel}" \
    "${capture_args[@]}" \
    --duration "${duration_seconds}s" >"${log_dir}/broker.log" 2>&1 &
  broker_pid="$!"
else
  echo "broker mode: container image ${broker_image}" >"${log_dir}/broker-mode.txt"
  docker run --rm --name ocudu_broker_mue \
    --gpus all --network host \
    -v "${project_root}:/work:ro" \
    "${broker_image}" \
    --config "/work/${topology_rel}" \
    --duration "${duration_seconds}s" >"${log_dir}/broker.log" 2>&1 &
  broker_pid="$!"
fi

"${compose[@]}" up -d gnb >"${log_dir}/docker-gnb-up.log" 2>&1
sleep 3

run_srsue() {
  # $1 container name  $2 tx port  $3 config  $4 log
  docker run --rm --name "$1" --privileged --cap-add NET_ADMIN --device /dev/net/tun \
    --add-host host.docker.internal:host-gateway -p "$2:$2" \
    -v "$3:/config/ue.conf:ro" --entrypoint /bin/sh \
    "${srsue_image}" -lc 'mkdir -p /var/run/netns && ip netns add ue1 && exec srsue /config/ue.conf' \
    >"$4" 2>&1 &
}
# Launch the UEs one at a time. srsRAN ZMQ radios share the broker's lock-step
# virtual time, so UEs started together transmit the identical RACH preamble on
# the identical PRACH occasion. Each UE waits for its predecessor to attach
# before starting, capped so one failing UE cannot hang the gate.
for ((i = 0; i < ue_count; i++)); do
  if [[ "${i}" -gt 0 && "${ue_stagger_seconds}" -gt 0 ]]; then
    for _ in $(seq 1 "${ue_stagger_seconds}"); do
      grep -q 'RRC Connected' "${log_dir}/srsue$((i - 1)).log" 2>/dev/null && break
      sleep 1
    done
    sleep 2
  fi
  run_srsue "ocudu_srsue_${i}" "$((2101 + 2 * i))" "${srsue_configs[i]}" "${log_dir}/srsue${i}.log"
  ue_pids[i]="$!"
  for _ in $(seq 1 "${OCUDU_MUE_ATTACH_WAIT_SECONDS:-40}"); do
    grep -q 'PDU Session Establishment successful' "${log_dir}/srsue${i}.log" 2>/dev/null && break
    sleep 1
  done
done

deadline=$((SECONDS + duration_seconds))
while [[ "${SECONDS}" -lt "${deadline}" ]]; do
  all_up=1
  any_alive=0
  for ((i = 0; i < ue_count; i++)); do
    grep -q 'RRC Connected' "${log_dir}/srsue${i}.log" 2>/dev/null && rrc[i]=1
    grep -q 'PDU Session Establishment successful' "${log_dir}/srsue${i}.log" 2>/dev/null && pdu[i]=1
    [[ "${rrc[i]}" -eq 1 && "${pdu[i]}" -eq 1 ]] || all_up=0
    kill -0 "${ue_pids[i]}" 2>/dev/null && any_alive=1
  done
  [[ "${all_up}" -eq 1 ]] && break
  [[ "${any_alive}" -eq 0 ]] && break
  sleep 2
done

ping_ue() {
  # $1 container name -> echoes 1 on success
  docker exec "$1" sh -lc '
      if ip netns list 2>/dev/null | grep -q ue1; then ns="ip netns exec ue1"; else ns=""; fi
      gw=$($ns ip route 2>/dev/null | awk "/default/ {print \$3; exit}")
      [ -z "$gw" ] && gw="10.45.1.1"
      $ns ping -c '"${ping_count}"' -i 0.1 -W 2 "$gw"
    ' >/dev/null 2>&1 && echo 1 || echo 0
}
for ((i = 0; i < ue_count; i++)); do
  [[ "${rrc[i]}" -eq 1 && "${pdu[i]}" -eq 1 ]] && ping[i]="$(ping_ue "ocudu_srsue_${i}")"
done

# Matrix scoring needs live uplink traffic inside the capture window, and the
# verdict ping above is over long before the window closes. Keep both UEs
# transmitting in the background for the remainder of the broker run.
if [[ "${matrix_enabled}" == "1" ]]; then
  for ((i = 0; i < ue_count; i++)); do
    container="ocudu_srsue_${i}"
    docker exec -d "${container}" sh -lc '
        if ip netns list 2>/dev/null | grep -q ue1; then ns="ip netns exec ue1"; else ns=""; fi
        gw=$($ns ip route 2>/dev/null | awk "/default/ {print \$3; exit}")
        [ -z "$gw" ] && gw="10.45.1.1"
        $ns ping -i 0.05 -W 2 "$gw"
      ' >/dev/null 2>&1 || true
  done
fi

set +e
wait "${broker_pid}"; broker_status="$?"; broker_pid=""
for ((i = 0; i < ue_count; i++)); do
  docker rm -f "ocudu_srsue_${i}" >/dev/null 2>&1
  [[ -n "${ue_pids[i]:-}" ]] && { kill "${ue_pids[i]}" >/dev/null 2>&1; wait "${ue_pids[i]}" >/dev/null 2>&1; }
  ue_pids[i]=""
done
set -e

docker cp ocudu_gnb:/tmp/gnb.log "${log_dir}/ocudu-gnb-internal.log" >/dev/null 2>&1 || true
"${compose[@]}" logs --no-color >"${log_dir}/docker-compose.log" 2>&1 || true
docker logs ocudu_gnb >"${log_dir}/ocudu-gnb.log" 2>&1 || true

if [[ -f "${log_dir}/ocudu-gnb-internal.log" ]]; then
  # grep -c prints the count (0 included) and exits 1 on no match; capture the
  # single count and fall back to 0 only when grep cannot read the file at all,
  # so the JSON field never gets a stray second "0" line.
  gnb_overflow="$(grep -c 'Real-time failure in RF: overflow' "${log_dir}/ocudu-gnb-internal.log" 2>/dev/null)" || gnb_overflow=0
fi
broker_stop="$(grep 'event=stop' "${log_dir}/broker.log" | tail -n 1 || true)"
rx_starvations="$(extract_counter rx_starvations "${broker_stop}")"
tx_queue_overflows="$(extract_counter tx_queue_overflows "${broker_stop}")"
tx_sequence_gaps="$(extract_counter tx_sequence_gaps "${broker_stop}")"
zmq_errors="$(extract_counter zmq_errors "${broker_stop}")"

[[ "${rx_starvations}" -ne 0 ]] && echo "note: rx_starvations=${rx_starvations} (soft signal)"
if [[ "${broker_status}" -ne 0 || "${tx_queue_overflows}" -ne 0 || "${tx_sequence_gaps}" -ne 0 || "${zmq_errors}" -ne 0 ]]; then
  write_summary "broker_failed" 1
fi
all_rrc=1; all_pdu=1; all_ping=1
for ((i = 0; i < ue_count; i++)); do
  [[ "${rrc[i]}" -eq 1 ]] || all_rrc=0
  [[ "${pdu[i]}" -eq 1 ]] || all_pdu=0
  [[ "${ping[i]}" -eq 1 ]] || all_ping=0
done
if [[ "${all_rrc}" -ne 1 ]]; then
  write_summary "ue_stack_blocker_no_attach" 2
fi
if [[ "${all_pdu}" -ne 1 ]]; then
  write_summary "ue_stack_blocker_no_pdu" 2
fi
if [[ "${all_ping}" -ne 1 ]]; then
  write_summary "ue_stack_blocker_ping_failed" 2
fi
# Score the declared channel matrices against the captured wire. Both uplinks
# land on the same gNB receive rows, so the checker reconstructs each row from
# ALL incoming links and additionally proves each one moved it.
if [[ "${matrix_enabled}" == "1" ]]; then
  matrix_venv="${workspace}/tools/matrix-verify-venv"
  if [[ ! -x "${matrix_venv}/bin/python" ]]; then
    /usr/bin/python3 -m venv "${matrix_venv}" >/dev/null 2>&1
    "${matrix_venv}/bin/pip" install --quiet --disable-pip-version-check numpy PyYAML >/dev/null 2>&1
  fi
  if ! "${matrix_venv}/bin/python" -c 'import numpy, yaml' >/dev/null 2>&1; then
    echo "matrix verification needs numpy and PyYAML; provisioning ${matrix_venv} failed" >&2
    write_summary "matrix_deps_missing" 3
  fi
  if "${matrix_venv}/bin/python" \
      "${project_root}/scripts/native/verify-mimo-matrix-capture.py" \
      --capture-dir "${capture_dir}" \
      --topology "${project_root}/${topology_rel}" \
      --report "${report_dir}/matrix-report.json" \
      ${matrix_args} >"${log_dir}/matrix-verify.log" 2>&1; then
    matrix_status=1
  else
    matrix_status=0
  fi
  cat "${log_dir}/matrix-verify.log"
  [[ "${matrix_status}" -eq 1 ]] || write_summary "matrix_failed" 3
fi

write_summary "passed" 0
REMOTE
