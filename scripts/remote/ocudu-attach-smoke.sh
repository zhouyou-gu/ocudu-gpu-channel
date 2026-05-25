#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${script_dir}/common.sh"

branch="${OCUDU_ATTACH_BRANCH:-$(git -C "${repo_root}" branch --show-current)}"
duration_seconds="${OCUDU_ATTACH_DURATION_SECONDS:-15}"
build_docker="${OCUDU_ATTACH_BUILD_DOCKER:-1}"
srsran_ref="${SRSRAN_4G_REF:-release_23_11}"
skip_remote_pull="${OCUDU_ATTACH_SKIP_REMOTE_PULL:-0}"
sync_worktree="${OCUDU_ATTACH_SYNC_WORKTREE:-1}"
# Override the broker topology with OCUDU_ATTACH_TOPOLOGY (path is relative
# to the project root, e.g. examples/topology.ocudu-docker.tx-offset.cuda.yaml).
topology_rel="${OCUDU_ATTACH_TOPOLOGY:-examples/topology.ocudu-docker.cuda.yaml}"

if [[ -z "${branch}" ]]; then
  echo "unable to determine current branch" >&2
  exit 1
fi

# By default, rsync the local working tree to the remote so the test exercises
# the current (possibly uncommitted) code. Set OCUDU_ATTACH_SYNC_WORKTREE=0 to
# instead use whatever is already on the remote.
if [[ "${sync_worktree}" == "1" ]]; then
  case "${REMOTE_PROJECT_ROOT}" in
    "~/"*) remote_dest="${REMOTE_PROJECT_ROOT#\~/}" ;;
    *) remote_dest="${REMOTE_PROJECT_ROOT}" ;;
  esac
  echo "syncing working tree to ${REMOTE_USER}@${REMOTE_HOST}:${remote_dest}"
  rsync -az --delete \
    --exclude '.git' --exclude 'build*' --exclude '.config' \
    -e "ssh -i ${REMOTE_SSH_KEY} -o BatchMode=yes -o ConnectTimeout=8" \
    "${repo_root}/" "${REMOTE_USER}@${REMOTE_HOST}:${remote_dest}/"
  skip_remote_pull=1
fi

remote_sh bash -s -- \
  "${REMOTE_WORKSPACE}" \
  "${REMOTE_PROJECT_ROOT}" \
  "${REMOTE_BUILDS_ROOT}" \
  "${REMOTE_RESULTS_ROOT}" \
  "${REMOTE_OCUDU_ROOT}" \
  "${REMOTE_REPO_URL}" \
  "${branch}" \
  "${duration_seconds}" \
  "${build_docker}" \
  "${srsran_ref}" \
  "${skip_remote_pull}" \
  "${topology_rel}" <<'REMOTE'
set -euo pipefail

workspace="$1"
project_root="$2"
builds_root="$3"
results_root="$4"
ocudu_root="$5"
repo_url="$6"
branch="$7"
duration_seconds="$8"
build_docker="$9"
srsran_ref="${10}"
skip_remote_pull="${11}"
topology_rel="${12}"

expand_remote_path() {
  case "$1" in
    "~") printf '%s\n' "${HOME}" ;;
    "~/"*) printf '%s/%s\n' "${HOME}" "${1#~/}" ;;
    *) printf '%s\n' "$1" ;;
  esac
}

extract_counter() {
  local key="$1"
  local line="$2"
  local value
  value="$(printf '%s\n' "${line}" | sed -n "s/.*${key}=\([0-9][0-9]*\).*/\1/p")"
  printf '%s\n' "${value:-0}"
}

write_summary() {
  local status="$1"
  local exit_code="$2"
  cat >"${summary_path}" <<JSON
{
  "timestamp": "${timestamp}",
  "status": "${status}",
  "duration_seconds": ${duration_seconds},
  "srsran_ref": "${srsran_ref}",
  "broker_status": ${broker_status:-0},
  "rrc_connected": ${rrc_connected:-0},
  "pdu_session_established": ${pdu_session_established:-0},
  "ping_ok": ${ping_ok:-0},
  "rx_starvations": ${rx_starvations:-0},
  "tx_queue_overflows": ${tx_queue_overflows:-0},
  "tx_sequence_gaps": ${tx_sequence_gaps:-0},
  "zmq_errors": ${zmq_errors:-0},
  "log_dir": "${log_dir}",
  "report_dir": "${report_dir}"
}
JSON
  printf 'summary=%s\n' "${summary_path}"
  printf 'status=%s\n' "${status}"
  exit "${exit_code}"
}

wait_for_open5gs() {
  for _ in $(seq 1 90); do
    local health
    health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' open5gs_5gc 2>/dev/null || true)"
    if [[ "${health}" == "healthy" || "${health}" == "running" ]]; then
      return 0
    fi
    sleep 1
  done
  docker ps --format '{{.Names}} {{.Status}}' >"${log_dir}/docker-ps-timeout.log" 2>&1 || true
  echo "open5gs_5gc did not become healthy" >&2
  return 1
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

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
log_dir="${results_root}/logs/ocudu-interop/${timestamp}"
report_dir="${results_root}/reports/ocudu-interop/${timestamp}"
config_dir="${workspace}/configs/ocudu/${timestamp}"
cuda_build="${builds_root}/ocudu-gpu-channel/cuda-release"
summary_path="${report_dir}/attach-summary.json"
mkdir -p "${log_dir}" "${report_dir}" "${config_dir}" "${cuda_build}"

broker_status=0
rrc_connected=0
pdu_session_established=0
ping_ok=0
rx_starvations=0
tx_queue_overflows=0
tx_sequence_gaps=0
zmq_errors=0

if [[ "${skip_remote_pull}" == "1" ]]; then
  if [[ ! -f "${project_root}/CMakeLists.txt" ]]; then
    echo "missing rsynced project worktree: ${project_root}" >&2
    exit 1
  fi
  if [[ -d "${project_root}/.git" ]]; then
    git -C "${project_root}" status --short --ignored >"${log_dir}/project-status.txt" || true
  else
    find "${project_root}" -maxdepth 2 -type f | sort >"${log_dir}/project-status.txt"
  fi
else
  if [[ ! -d "${project_root}/.git" ]]; then
    mkdir -p "$(dirname "${project_root}")"
    git clone "${repo_url}" "${project_root}"
  fi
  git -C "${project_root}" remote set-url origin "${repo_url}"
  git -C "${project_root}" fetch origin "${branch}"
  git -C "${project_root}" checkout -B "${branch}" "origin/${branch}"
  git -C "${project_root}" reset --hard "origin/${branch}"
  git -C "${project_root}" clean -fdx
  git -C "${project_root}" status --short --ignored >"${log_dir}/project-status.txt"
fi

if [[ ! -d "${ocudu_root}/docker" ]]; then
  echo "missing OCUDU checkout with docker directory: ${ocudu_root}" >&2
  exit 1
fi

cmake -S "${project_root}" -B "${cuda_build}" \
  -DCMAKE_BUILD_TYPE=Release \
  -DOCUDU_GPU_CHANNEL_ENABLE_CUDA=ON \
  -DCMAKE_CUDA_COMPILER="${workspace}/tools/cuda-12.8.1/bin/nvcc" \
  -DOCUDU_GPU_CHANNEL_CUDA_ARCHITECTURES=120 >"${log_dir}/cmake-configure.log" 2>&1
cmake --build "${cuda_build}" -j"$(nproc)" >"${log_dir}/cmake-build.log" 2>&1
ctest --test-dir "${cuda_build}" --output-on-failure >"${log_dir}/ctest.log" 2>&1

gnb_config="${config_dir}/gnb_zmq_b210_fdd_srsue.yaml"
compose_override="${config_dir}/docker-compose.ocudu-gpu-channel.yml"
ocudu_dockerfile="${config_dir}/Dockerfile.ocudu-zmq"
srsue_dockerfile="${config_dir}/Dockerfile.srsue"
srsue_config="${config_dir}/srsue_zmq.conf"
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

cat >"${compose_override}" <<'YAML'
services:
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
    ca-certificates \
    cmake \
    g++ \
    gcc \
    git \
    iproute2 \
    iputils-ping \
    libboost-program-options-dev \
    libconfig++-dev \
    libfftw3-dev \
    libmbedtls-dev \
    libsctp-dev \
    libzmq3-dev \
    make \
    net-tools \
    pkg-config \
  && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 --branch "${SRSRAN_4G_REF}" https://github.com/srsran/srsRAN_4G.git /src/srsran_4g \
  && cmake -S /src/srsran_4g -B /src/srsran_4g/build \
      -DCMAKE_BUILD_TYPE=Release \
      -DENABLE_EXPORT=ON \
      -DENABLE_ZEROMQ=ON \
      -DENABLE_UHD=OFF \
  && cmake --build /src/srsran_4g/build -j"$(nproc)" --target srsue \
  && find /src/srsran_4g/build -type f -name srsue -perm -111 -exec cp {} /usr/local/bin/srsue \; -quit
ENTRYPOINT ["srsue"]
DOCKER

cat >"${srsue_config}" <<'CONF'
[rf]
freq_offset = 0
tx_gain = 50
rx_gain = 40
srate = 23.04e6
nof_antennas = 1
device_name = zmq
device_args = tx_port=tcp://*:2101,rx_port=tcp://host.docker.internal:2100,base_srate=23.04e6

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
imsi = 001010123456780
imei = 353490069873319

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

compose=(docker compose -f "${ocudu_root}/docker/docker-compose.yml" -f "${compose_override}")
export GNB_CONFIG_PATH="${gnb_config}"
export OCUDU_ZMQ_DOCKERFILE="${ocudu_dockerfile}"
export OS="${OS:-ubuntu}"
export OS_VERSION="${OS_VERSION:-24.04}"
srsue_image="ocudu-gpu-channel/srsue-zmq:${srsran_ref}"

broker_pid=""
srsue_pid=""

cleanup() {
  set +e
  if [[ -n "${srsue_pid}" ]]; then kill "${srsue_pid}" >/dev/null 2>&1; fi
  if [[ -n "${broker_pid}" ]]; then kill "${broker_pid}" >/dev/null 2>&1; fi
  docker rm -f ocudu_srsue >/dev/null 2>&1
  docker cp ocudu_gnb:/tmp/gnb.log "${log_dir}/ocudu-gnb-internal.log" >/dev/null 2>&1
  docker cp ocudu_gnb:/tmp/gnb_mac.pcap "${log_dir}/gnb_mac.pcap" >/dev/null 2>&1
  docker cp ocudu_gnb:/tmp/gnb_ngap.pcap "${log_dir}/gnb_ngap.pcap" >/dev/null 2>&1
  "${compose[@]}" logs --no-color >"${log_dir}/docker-compose.log" 2>&1
  docker logs open5gs_5gc >"${log_dir}/open5gs.log" 2>&1
  docker logs ocudu_gnb >"${log_dir}/ocudu-gnb.log" 2>&1
  "${compose[@]}" down --remove-orphans --volumes >"${log_dir}/docker-down.log" 2>&1
}
trap cleanup EXIT

"${compose[@]}" down --remove-orphans --volumes >"${log_dir}/docker-preclean.log" 2>&1 || true
docker rm -f open5gs_5gc ocudu_gnb ocudu_srsue >"${log_dir}/docker-rm.log" 2>&1 || true

if [[ "${build_docker}" == "1" ]]; then
  "${compose[@]}" build 5gc gnb >"${log_dir}/docker-build.log" 2>&1
fi

if ! docker build \
  --build-arg "SRSRAN_4G_REF=${srsran_ref}" \
  -f "${srsue_dockerfile}" \
  -t "${srsue_image}" \
  "${config_dir}" >"${log_dir}/srsue-docker-build.log" 2>&1; then
  write_summary "ue_stack_blocker_srsue_build" 2
fi

"${compose[@]}" up -d 5gc >"${log_dir}/docker-core-up.log" 2>&1
wait_for_open5gs

# The broker runs without --strict-realtime: this script reads the counters
# from its event=stop line and applies its own verdict (see below), treating
# rx_starvations as a soft realtime-margin signal rather than a hard failure.
"${cuda_build}/ocudu-gpu-channel" \
  --config "${project_root}/${topology_rel}" \
  --duration "${duration_seconds}s" >"${log_dir}/broker.log" 2>&1 &
broker_pid="$!"

"${compose[@]}" up -d gnb >"${log_dir}/docker-gnb-up.log" 2>&1
sleep 3

docker run --rm \
  --name ocudu_srsue \
  --privileged \
  --cap-add NET_ADMIN \
  --device /dev/net/tun \
  --add-host host.docker.internal:host-gateway \
  -p 2101:2101 \
  -v "${srsue_config}:/config/ue.conf:ro" \
  --entrypoint /bin/sh \
  "${srsue_image}" -lc 'mkdir -p /var/run/netns && ip netns add ue1 && exec srsue /config/ue.conf' \
  >"${log_dir}/srsue.log" 2>&1 &
srsue_pid="$!"

deadline=$((SECONDS + duration_seconds))
while [[ "${SECONDS}" -lt "${deadline}" ]]; do
  if grep -q 'RRC Connected' "${log_dir}/srsue.log" 2>/dev/null; then
    rrc_connected=1
  fi
  if grep -q 'PDU Session Establishment successful' "${log_dir}/srsue.log" 2>/dev/null; then
    pdu_session_established=1
  fi
  if [[ "${rrc_connected}" -eq 1 && "${pdu_session_established}" -eq 1 ]]; then
    break
  fi
  if ! kill -0 "${srsue_pid}" >/dev/null 2>&1; then
    break
  fi
  sleep 2
done

if [[ "${rrc_connected}" -eq 1 && "${pdu_session_established}" -eq 1 ]]; then
  if docker exec ocudu_srsue sh -lc '
      if ip netns list 2>/dev/null | grep -q ue1; then ns="ip netns exec ue1"; else ns=""; fi
      gw=$($ns ip route 2>/dev/null | awk "/default/ {print \$3; exit}")
      if [ -z "$gw" ]; then gw="10.45.1.1"; fi
      $ns ping -c 3 -W 2 "$gw"
    ' >"${log_dir}/ue-ping.log" 2>&1; then
    ping_ok=1
  fi
fi

set +e
wait "${broker_pid}"
broker_status="$?"
broker_pid=""
if [[ -n "${srsue_pid}" ]]; then
  # Force-remove the container first: srsUE can wedge on shutdown ("Couldn't
  # stop after 5s"), and killing only the `docker run` process then leaves
  # `wait` blocked forever. `docker rm -f` guarantees the container exits.
  docker rm -f ocudu_srsue >/dev/null 2>&1
  kill "${srsue_pid}" >/dev/null 2>&1
  wait "${srsue_pid}" >/dev/null 2>&1
  srsue_pid=""
fi
set -e

"${compose[@]}" logs --no-color >"${log_dir}/docker-compose.log" 2>&1 || true
docker cp ocudu_gnb:/tmp/gnb.log "${log_dir}/ocudu-gnb-internal.log" >/dev/null 2>&1 || true
docker cp ocudu_gnb:/tmp/gnb_mac.pcap "${log_dir}/gnb_mac.pcap" >/dev/null 2>&1 || true
docker cp ocudu_gnb:/tmp/gnb_ngap.pcap "${log_dir}/gnb_ngap.pcap" >/dev/null 2>&1 || true
docker logs open5gs_5gc >"${log_dir}/open5gs.log" 2>&1 || true
docker logs ocudu_gnb >"${log_dir}/ocudu-gnb.log" 2>&1 || true

broker_stop="$(grep 'event=stop' "${log_dir}/broker.log" | tail -n 1 || true)"
rx_starvations="$(extract_counter rx_starvations "${broker_stop}")"
tx_queue_overflows="$(extract_counter tx_queue_overflows "${broker_stop}")"
tx_sequence_gaps="$(extract_counter tx_sequence_gaps "${broker_stop}")"
zmq_errors="$(extract_counter zmq_errors "${broker_stop}")"

# Hard broker failure = a crash or a data-integrity counter (queue overflow,
# sequence gap, ZMQ error). rx_starvations is a host-scheduling-sensitive
# realtime-margin signal: it is recorded in the summary but does not by itself
# fail the run.
if [[ "${rx_starvations}" -ne 0 ]]; then
  echo "note: rx_starvations=${rx_starvations} (realtime-margin signal; not a hard failure)"
fi
if [[ "${broker_status}" -ne 0 || "${tx_queue_overflows}" -ne 0 || "${tx_sequence_gaps}" -ne 0 || "${zmq_errors}" -ne 0 ]]; then
  write_summary "broker_failed" 1
fi
if [[ "${rrc_connected}" -ne 1 || "${pdu_session_established}" -ne 1 ]]; then
  write_summary "ue_stack_blocker_no_attach" 2
fi
if [[ "${ping_ok}" -ne 1 ]]; then
  write_summary "ue_stack_blocker_ping_failed" 2
fi

write_summary "passed" 0
REMOTE
