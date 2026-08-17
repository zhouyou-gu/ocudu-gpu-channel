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
  "${broker_image}" <<'REMOTE'
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
log_dir="${results_root}/logs/ocudu-multi-ue/${timestamp}"
report_dir="${results_root}/reports/ocudu-multi-ue/${timestamp}"
config_dir="${workspace}/configs/ocudu-multi-ue/${timestamp}"
cuda_build="${builds_root}/ocudu-gpu-channel/cuda-release"
summary_path="${report_dir}/multi-ue-summary.json"
mkdir -p "${log_dir}" "${report_dir}" "${config_dir}" "${cuda_build}"

rrc0=0; rrc1=0; pdu0=0; pdu1=0; ping0=0; ping1=0
broker_status=0; rx_starvations=0; tx_queue_overflows=0; tx_sequence_gaps=0; zmq_errors=0; gnb_overflow=0

write_summary() {
  local status="$1"
  local exit_code="$2"
  cat >"${summary_path}" <<JSON
{
  "timestamp": "${timestamp}",
  "status": "${status}",
  "duration_seconds": ${duration_seconds},
  "ue0": { "rrc_connected": ${rrc0}, "pdu_session_established": ${pdu0}, "ping_ok": ${ping0} },
  "ue1": { "rrc_connected": ${rrc1}, "pdu_session_established": ${pdu1}, "ping_ok": ${ping1} },
  "gnb_rt_overflow": ${gnb_overflow},
  "broker_status": ${broker_status},
  "rx_starvations": ${rx_starvations},
  "tx_queue_overflows": ${tx_queue_overflows},
  "tx_sequence_gaps": ${tx_sequence_gaps},
  "zmq_errors": ${zmq_errors},
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
cp "${project_root}/examples/ocudu/gnb_zmq_b210_fdd_srsue.yaml" "${gnb_config}"

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
cat >"${compose_override}" <<'YAML'
services:
  5gc:
    environment:
      SUBSCRIBER_DB: /open5gs/subscriber_db.csv
  gnb:
    ports:
      - "2000:2000"
    extra_hosts:
      - "host.docker.internal:host-gateway"
    build:
      dockerfile: ${OCUDU_ZMQ_DOCKERFILE}
      args:
        EXTRA_CMAKE_ARGS: "-DENABLE_ZEROMQ=ON -DENABLE_EXPORT=ON -DZEROMQ_INCLUDE_DIRS=/usr/include -DZEROMQ_LIBRARIES=/usr/lib/x86_64-linux-gnu/libzmq.so"
        OS: "ubuntu"
        OS_VERSION: "24.04"
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
srsue0_config="${config_dir}/srsue0_zmq.conf"
srsue1_config="${config_dir}/srsue1_zmq.conf"
write_srsue_config "${srsue0_config}" 2101 2100 001010123456780 353490069873319
write_srsue_config "${srsue1_config}" 2103 2102 001010123456781 353490069873320

# Two-UE Open5GS subscriber CSV (name,imsi,key,op_type,opc,amf,qci,ip). Replaces
# the docker-auto-created placeholder so the base compose mounts a real file.
subscriber_db="${ocudu_root}/docker/open5gs/subscriber_db.csv"
rm -rf "${subscriber_db}"
cat >"${subscriber_db}" <<'CSV'
ue0,001010123456780,00112233445566778899aabbccddeeff,opc,63bfa50ee6523365ff14c1f45f88737d,8000,9,10.45.1.2
ue1,001010123456781,00112233445566778899aabbccddeeff,opc,63bfa50ee6523365ff14c1f45f88737d,8000,9,10.45.1.3
CSV

compose=(docker compose -f "${ocudu_root}/docker/docker-compose.yml" -f "${compose_override}")
export GNB_CONFIG_PATH="${gnb_config}"
export OCUDU_ZMQ_DOCKERFILE="${ocudu_dockerfile}"
export OS=ubuntu OS_VERSION=24.04
srsue_image="ocudu-gpu-channel/srsue-zmq:${srsran_ref}"
broker_pid=""
ue0_pid=""
ue1_pid=""

cleanup() {
  set +e
  [[ -n "${ue0_pid}" ]] && kill "${ue0_pid}" >/dev/null 2>&1
  [[ -n "${ue1_pid}" ]] && kill "${ue1_pid}" >/dev/null 2>&1
  [[ -n "${broker_pid}" ]] && kill "${broker_pid}" >/dev/null 2>&1
  [[ -n "${broker_image}" ]] && docker rm -f ocudu_broker_mue >/dev/null 2>&1
  docker rm -f ocudu_srsue_0 ocudu_srsue_1 >/dev/null 2>&1
  docker cp ocudu_gnb:/tmp/gnb.log "${log_dir}/ocudu-gnb-internal.log" >/dev/null 2>&1
  "${compose[@]}" logs --no-color >"${log_dir}/docker-compose.log" 2>&1
  docker logs open5gs_5gc >"${log_dir}/open5gs.log" 2>&1
  "${compose[@]}" down --remove-orphans --volumes >"${log_dir}/docker-down.log" 2>&1
}
trap cleanup EXIT

"${compose[@]}" down --remove-orphans --volumes >"${log_dir}/docker-preclean.log" 2>&1 || true
docker rm -f open5gs_5gc ocudu_gnb ocudu_srsue_0 ocudu_srsue_1 >"${log_dir}/docker-rm.log" 2>&1 || true

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
    --config "${project_root}/examples/topology.ocudu-docker.multi-ue.cuda.yaml" \
    --duration "${duration_seconds}s" >"${log_dir}/broker.log" 2>&1 &
  broker_pid="$!"
else
  echo "broker mode: container image ${broker_image}" >"${log_dir}/broker-mode.txt"
  docker run --rm --name ocudu_broker_mue \
    --gpus all --network host \
    -v "${project_root}:/work:ro" \
    "${broker_image}" \
    --config "/work/examples/topology.ocudu-docker.multi-ue.cuda.yaml" \
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
run_srsue ocudu_srsue_0 2101 "${srsue0_config}" "${log_dir}/srsue0.log"
ue0_pid="$!"
# Hold ue1 until ue0 is RRC-connected (capped at ue_stagger_seconds), then a
# short settle, so the two UEs RACH on different PRACH occasions instead of
# colliding on a single shared C-RNTI in the broker's lock-step virtual time.
if [[ "${ue_stagger_seconds}" -gt 0 ]]; then
  for _ in $(seq 1 "${ue_stagger_seconds}"); do
    grep -q 'RRC Connected' "${log_dir}/srsue0.log" 2>/dev/null && break
    sleep 1
  done
  sleep 2
fi
run_srsue ocudu_srsue_1 2103 "${srsue1_config}" "${log_dir}/srsue1.log"
ue1_pid="$!"

deadline=$((SECONDS + duration_seconds))
while [[ "${SECONDS}" -lt "${deadline}" ]]; do
  grep -q 'RRC Connected' "${log_dir}/srsue0.log" 2>/dev/null && rrc0=1
  grep -q 'RRC Connected' "${log_dir}/srsue1.log" 2>/dev/null && rrc1=1
  grep -q 'PDU Session Establishment successful' "${log_dir}/srsue0.log" 2>/dev/null && pdu0=1
  grep -q 'PDU Session Establishment successful' "${log_dir}/srsue1.log" 2>/dev/null && pdu1=1
  [[ "${rrc0}" -eq 1 && "${rrc1}" -eq 1 && "${pdu0}" -eq 1 && "${pdu1}" -eq 1 ]] && break
  if ! kill -0 "${ue0_pid}" 2>/dev/null && ! kill -0 "${ue1_pid}" 2>/dev/null; then break; fi
  sleep 2
done

ping_ue() {
  # $1 container name -> echoes 1 on success
  docker exec "$1" sh -lc '
      if ip netns list 2>/dev/null | grep -q ue1; then ns="ip netns exec ue1"; else ns=""; fi
      gw=$($ns ip route 2>/dev/null | awk "/default/ {print \$3; exit}")
      [ -z "$gw" ] && gw="10.45.1.1"
      $ns ping -c 3 -W 2 "$gw"
    ' >/dev/null 2>&1 && echo 1 || echo 0
}
[[ "${rrc0}" -eq 1 && "${pdu0}" -eq 1 ]] && ping0="$(ping_ue ocudu_srsue_0)"
[[ "${rrc1}" -eq 1 && "${pdu1}" -eq 1 ]] && ping1="$(ping_ue ocudu_srsue_1)"

set +e
wait "${broker_pid}"; broker_status="$?"; broker_pid=""
docker rm -f ocudu_srsue_0 ocudu_srsue_1 >/dev/null 2>&1
[[ -n "${ue0_pid}" ]] && { kill "${ue0_pid}" >/dev/null 2>&1; wait "${ue0_pid}" >/dev/null 2>&1; }
[[ -n "${ue1_pid}" ]] && { kill "${ue1_pid}" >/dev/null 2>&1; wait "${ue1_pid}" >/dev/null 2>&1; }
ue0_pid=""; ue1_pid=""
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
if [[ "${rrc0}" -ne 1 || "${rrc1}" -ne 1 ]]; then
  write_summary "ue_stack_blocker_no_attach" 2
fi
if [[ "${pdu0}" -ne 1 || "${pdu1}" -ne 1 ]]; then
  write_summary "ue_stack_blocker_no_pdu" 2
fi
if [[ "${ping0}" -ne 1 || "${ping1}" -ne 1 ]]; then
  write_summary "ue_stack_blocker_ping_failed" 2
fi
write_summary "passed" 0
REMOTE
