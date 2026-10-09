#!/usr/bin/env bash
# Milestone C: two OCUDU gNBs (two co-channel cells) + two srsUE containers,
# each UE camping on its own cell through the CUDA broker, with each cell's
# transmission leaking into the other as inter-cell interference.
#
# This is the locked-in multi-gNB OCUDU test -- the two-cell counterpart of
# ocudu-multi-ue-smoke.sh. The lightweight synthetic+ctest checks (including
# the 2-cell superposition) live in gpu-test-sequence.sh; this Docker stack is
# the heavier separate test.
#
# One-command test: rsyncs the working tree, builds, brings up Open5GS + two
# gNBs (distinct PCI / gnb_id / ZMQ ports / 5GC-network IP) + the broker on the
# multi-gNB topology, launches two srsUE containers, and reports whether both
# reach RRC / PDU / ping.
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
execution_mode="${OCUDU_MGNB_EXECUTION:-remote}"
if [[ "${execution_mode}" == "local" ]]; then
  repo_root="$(cd "${script_dir}/../.." && pwd)"
  local_workspace="$(cd "${repo_root}/.." && pwd)"
  REMOTE_WORKSPACE="${OCUDU_MGNB_WORKSPACE:-${local_workspace}}"
  REMOTE_PROJECT_ROOT="${OCUDU_MGNB_PROJECT_ROOT:-${repo_root}}"
  REMOTE_BUILDS_ROOT="${OCUDU_MGNB_BUILDS_ROOT:-${local_workspace}/builds}"
  REMOTE_RESULTS_ROOT="${OCUDU_MGNB_RESULTS_ROOT:-${repo_root}/results}"
  REMOTE_OCUDU_ROOT="${OCUDU_MGNB_OCUDU_ROOT:-${local_workspace}/ocudu}"
  remote_sh() {
    "$@"
  }
elif [[ "${execution_mode}" == "remote" ]]; then
  # shellcheck source=common.sh
  source "${script_dir}/common.sh"
else
  echo "OCUDU_MGNB_EXECUTION must be local or remote" >&2
  exit 2
fi

duration_seconds="${OCUDU_MGNB_DURATION_SECONDS:-60}"
build_docker="${OCUDU_MGNB_BUILD_DOCKER:-1}"
# Non-empty = run the broker as this Docker image (docker run --gpus all
# --network host) instead of the native build. Default = native binary.
broker_image="${OCUDU_MGNB_BROKER_IMAGE:-}"
# static keeps the proven topology unchanged. sionna switches only the
# external broker/controller path; OCUDU source and its base config stay
# untouched.
channel_mode="${OCUDU_MGNB_CHANNEL_MODE:-static}"
sionna_python="${OCUDU_MGNB_SIONNA_PYTHON:-}"
sionna_update_hz="${OCUDU_MGNB_SIONNA_UPDATE_HZ:-500}"
sionna_ready_seconds="${OCUDU_MGNB_SIONNA_READY_SECONDS:-120}"
sionna_web_port="${OCUDU_MGNB_WEB_PORT:-8080}"
# Which Sionna scenario feeds the two cells. The default is the built-in street
# canyon this milestone was validated on; any scenario naming gnb0/gnb1/ue0/ue1
# with the serving and intercell models works, which is how the OpenStreetMap
# SUTD campus scene is selected.
sionna_scenario="${OCUDU_MGNB_SIONNA_SCENARIO:-use_cases/configs/sionna/scenarios/simple_street/multi-gnb.json}"
# Host port for the 5GC, empty = do not publish it. Nothing in this gate talks
# to the 5GC from outside the compose network; the base compose publishes 9999
# only so a human can poke it, and that mapping is enough to abort the whole
# run on a machine where something else already listens there.
fivegc_host_port="${OCUDU_MGNB_5GC_HOST_PORT:-}"
# Seconds to keep the whole stack -- cells, UEs, broker, bridge and Web UI --
# running after the verdict is in. 0 tears down as soon as both UEs ping, which
# is what a gate wants and what makes the Web UI unwatchable; set it to look at
# the dashboard while the channel is still being traced.
hold_seconds="${OCUDU_MGNB_HOLD_SECONDS:-0}"
# The gNB releases an idle UE after its 120 s default, which ends the demo a
# couple of minutes after the acceptance ping: the UE drops to idle and the
# dashboard loses it. These two are the pair the native gate uses -- the timer
# raises the gNB's own bound (it enforces [1, 7200]) and the keepalive gives
# the DRB actual user-plane traffic to carry. Both are opt-in, so an unset run
# keeps the proven behaviour byte for byte.
ue_inactivity_seconds="${OCUDU_MGNB_UE_INACTIVITY_SECONDS:-}"
ue_keepalive_seconds="${OCUDU_MGNB_UE_KEEPALIVE_SECONDS:-0}"
# Which broker topology to run. Empty picks the one that matches the channel
# mode; see where topology_name is resolved for why the two differ.
topology_name="${OCUDU_MGNB_TOPOLOGY:-}"
cuda_compiler="${OCUDU_MGNB_CUDA_COMPILER:-}"
# srsUE launch stagger: the two UEs camp on different cells so they do not
# collide on RACH, but staggering ue1 until ue0 is RRC-connected still removes
# any startup race. 0 disables it.
ue_stagger_seconds="${OCUDU_MGNB_UE_STAGGER_SECONDS:-8}"
# srsUE base: latest zhouyou-gu/srsRAN_4G master, which carries the
# SRSUE_PRACH_PREAMBLE_INDEX override used to give each UE its own preamble.
srsran_ref="${SRSRAN_4G_REF:-master}"

if [[ "${channel_mode}" != "static" && "${channel_mode}" != "sionna" ]]; then
  echo "OCUDU_MGNB_CHANNEL_MODE must be static or sionna" >&2
  exit 2
fi
broker_image_arg="${broker_image:-__native__}"
sionna_python_arg="${sionna_python:-__default__}"
cuda_compiler_arg="${cuda_compiler:-__default__}"

if [[ "${execution_mode}" == "remote" ]]; then
  case "${REMOTE_PROJECT_ROOT}" in
    "~/"*) remote_dest="${REMOTE_PROJECT_ROOT#\~/}" ;;
    *) remote_dest="${REMOTE_PROJECT_ROOT}" ;;
  esac
  echo "syncing working tree to ${REMOTE_USER}@${REMOTE_HOST}:${remote_dest}"
  rsync -az --delete \
    --exclude '.git' --exclude 'build*' --exclude '.config' \
    -e "ssh -i ${REMOTE_SSH_KEY} -o BatchMode=yes -o ConnectTimeout=8" \
    "${repo_root}/" "${REMOTE_USER}@${REMOTE_HOST}:${remote_dest}/"
else
  echo "running local 2-gNB/2-UE smoke from ${REMOTE_PROJECT_ROOT}"
fi

# Encode before ssh so whitespace and shell metacharacters remain data.
sionna_extra_args_b64="$(printf '%s' "${OCUDU_MGNB_SIONNA_EXTRA_ARGS:-}" | base64 | tr -d '\n')"

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
  "${broker_image_arg}" \
  "${channel_mode}" \
  "${sionna_python_arg}" \
  "${sionna_update_hz}" \
  "${sionna_ready_seconds}" \
  "${sionna_web_port}" \
  "${sionna_scenario}" \
  "${cuda_compiler_arg}" \
  "${execution_mode}" \
  "${fivegc_host_port:-__unset__}" \
  "${hold_seconds}" \
  "${ue_inactivity_seconds:-__unset__}" \
  "${ue_keepalive_seconds}" \
  "${topology_name:-__default__}" \
  "${sionna_extra_args_b64:-__none__}" <<'REMOTE'
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
broker_image="${10}"
channel_mode="${11}"
sionna_python="${12}"
sionna_update_hz="${13}"
sionna_ready_seconds="${14}"
sionna_web_port="${15}"
sionna_scenario="${16}"
cuda_compiler="${17}"
execution_mode="${18}"
fivegc_host_port="${19}"
[[ "${fivegc_host_port}" == "__unset__" ]] && fivegc_host_port=""
hold_seconds="${20}"
ue_inactivity_seconds="${21}"
ue_keepalive_seconds="${22}"
topology_name="${23}"
sionna_bridge_args=()
if [[ "${24:-__none__}" != "__none__" ]]; then
  sionna_extra_args="$(printf '%s' "${24}" | base64 --decode)"
  # Legacy EXTRA_ARGS uses whitespace-separated words, without shell evaluation
  # or glob expansion. Direct wrapper callers can pass quoted argv after --.
  IFS=$' \t\n' read -r -a sionna_bridge_args <<< "${sionna_extra_args//$'\n'/ }"
fi
[[ "${hold_seconds}" =~ ^(0|[1-9][0-9]*)$ ]] || {
  echo "OCUDU_MGNB_HOLD_SECONDS must be a non-negative integer" >&2
  exit 2
}
[[ "${ue_inactivity_seconds}" == "__unset__" ]] && ue_inactivity_seconds=""
# The bound is the gNB's own: apps/units/o_cu_cp/cu_cp/cu_cp_unit_config_cli11_schema.cpp
# declares range(1, 7200), and anything outside it makes the cell refuse to start.
if [[ -n "${ue_inactivity_seconds}" ]]; then
  [[ "${ue_inactivity_seconds}" =~ ^[1-9][0-9]*$ && "${ue_inactivity_seconds}" -le 7200 ]] || {
    echo "OCUDU_MGNB_UE_INACTIVITY_SECONDS must be an integer in [1, 7200]" >&2
    exit 2
  }
fi
# ping refuses an interval below 0.2 s for a non-root caller and the flood it
# would become is not what this is for; 60 s is past every inactivity timer.
[[ "${ue_keepalive_seconds}" =~ ^(0|0?\.[0-9]+|[1-9][0-9]*(\.[0-9]+)?)$ ]] || {
  echo "OCUDU_MGNB_UE_KEEPALIVE_SECONDS must be 0 or a positive number" >&2
  exit 2
}
if [[ "${ue_keepalive_seconds}" != "0" ]]; then
  awk -v value="${ue_keepalive_seconds}" 'BEGIN { exit !(value >= 0.2 && value <= 60) }' || {
    echo "OCUDU_MGNB_UE_KEEPALIVE_SECONDS must be 0 or within [0.2, 60] seconds" >&2
    exit 2
  }
fi
# An empty list is what tells Compose to publish nothing at all.
if [[ -n "${fivegc_host_port}" ]]; then
  fivegc_ports="      - \"${fivegc_host_port}:9999/tcp\""
else
  fivegc_ports="      []"
fi
[[ "${broker_image}" == "__native__" ]] && broker_image=""

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
if [[ "${sionna_python}" == "__default__" ]]; then
  sionna_python="${workspace}/venvs/sionna/bin/python"
else
  sionna_python="$(expand_remote_path "${sionna_python}")"
fi
if [[ "${cuda_compiler}" == "__default__" ]]; then
  if [[ "${execution_mode}" == "local" && -x /opt/conda/envs/torch/bin/nvcc ]]; then
    cuda_compiler=/opt/conda/envs/torch/bin/nvcc
  else
    cuda_compiler="${workspace}/tools/cuda-12.8.1/bin/nvcc"
  fi
else
  cuda_compiler="$(expand_remote_path "${cuda_compiler}")"
fi

if [[ -f "${workspace}/tools/env.sh" ]]; then
  # shellcheck source=/dev/null
  source "${workspace}/tools/env.sh"
elif [[ "${execution_mode}" == "remote" ]]; then
  echo "missing ${workspace}/tools/env.sh; run scripts/remote/bootstrap-user-tools.sh first" >&2
  exit 1
fi
command -v cmake >/dev/null 2>&1 || { echo "cmake not found" >&2; exit 1; }
if [[ ! -x "${cuda_compiler}" ]]; then
  echo "CUDA compiler not found or not executable: ${cuda_compiler}" >&2
  echo "set OCUDU_MGNB_CUDA_COMPILER to the nvcc path" >&2
  exit 1
fi

if [[ ! -d "${ocudu_root}/docker" ]]; then
  echo "missing OCUDU checkout with docker directory: ${ocudu_root}" >&2
  exit 1
fi
if [[ "${execution_mode}" == "local" ]]; then
  command -v docker >/dev/null 2>&1 || { echo "docker not found" >&2; exit 1; }
  if ! docker info >/dev/null 2>&1; then
    if sudo -n docker info >/dev/null 2>&1; then
      docker_bin="$(command -v docker)"
      docker() {
        sudo -n env \
          GNB_CONFIG_PATH="${GNB_CONFIG_PATH:-}" \
          GNB1_CONFIG_PATH="${GNB1_CONFIG_PATH:-}" \
          OCUDU_ZMQ_DOCKERFILE="${OCUDU_ZMQ_DOCKERFILE:-}" \
          OS="${OS:-}" OS_VERSION="${OS_VERSION:-}" \
          "${docker_bin}" "$@"
      }
      echo "Docker socket requires sudo; using passwordless sudo for this run"
    else
      echo "cannot access the Docker daemon; grant this user Docker socket access" >&2
      exit 1
    fi
  fi
  if ! nvidia-smi -L >/dev/null 2>&1; then
    echo "NVIDIA GPU/driver is not accessible; the CUDA and Sionna test cannot run" >&2
    exit 1
  fi
  if [[ "$(systemd-detect-virt 2>/dev/null || true)" == "lxc" ]] && \
     docker image inspect hello-world:latest >/dev/null 2>&1 && \
     ! docker run --rm hello-world:latest >/dev/null 2>&1; then
    echo "nested Docker cannot create a network namespace in this LXC/LXD guest" >&2
    echo "fix the host LXC/LXD AppArmor profile, restart the guest, then rerun" >&2
    exit 1
  fi
fi

if [[ "${channel_mode}" == "sionna" ]]; then
  [[ "${sionna_web_port}" =~ ^[1-9][0-9]*$ && "${sionna_web_port}" -le 65535 ]] || {
    echo "invalid OCUDU_MGNB_WEB_PORT" >&2
    exit 2
  }
  case "${sionna_scenario}" in
    /*) ;;
    *) sionna_scenario="${project_root}/${sionna_scenario}" ;;
  esac
  if [[ ! -f "${sionna_scenario}" ]]; then
    echo "missing Sionna scenario: ${sionna_scenario}" >&2
    exit 2
  fi
  if [[ ! -x "${sionna_python}" ]]; then
    echo "missing Sionna Python: ${sionna_python}" >&2
    echo "create ${workspace}/venvs/sionna and install scripts/sionna_rt/requirements.txt" >&2
    exit 2
  fi
  if [[ -z "${DRJIT_LIBOPTIX_PATH:-}" ]]; then
    optix_file="$(find "${workspace}/local" -type f -name 'libnvoptix.so*' -print -quit 2>/dev/null || true)"
    if [[ -n "${optix_file}" ]]; then
      export DRJIT_LIBOPTIX_PATH="${optix_file}"
      export LD_LIBRARY_PATH="$(dirname "${optix_file}"):${LD_LIBRARY_PATH:-}"
    fi
  fi
  if ! "${sionna_python}" -c 'import sionna.rt, zmq' >/dev/null 2>&1; then
    echo "${sionna_python} cannot import sionna.rt and zmq" >&2
    "${sionna_python}" -c 'import sionna.rt, zmq' 2>&1 | tail -n 12 >&2 || true
    exit 2
  fi
fi

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
log_dir="${results_root}/logs/ocudu-multi-gnb/${timestamp}"
report_dir="${results_root}/reports/ocudu-multi-gnb/${timestamp}"
config_dir="${workspace}/configs/ocudu-multi-gnb/${timestamp}"
cuda_build="${builds_root}/ocudu-gpu-channel/cuda-release"
summary_path="${report_dir}/multi-gnb-summary.json"
mkdir -p "${log_dir}" "${report_dir}" "${config_dir}" "${cuda_build}"

rrc0=0; rrc1=0; pdu0=0; pdu1=0; ping0=0; ping1=0
gnb0_up=0; gnb1_up=0
broker_status=0; rx_starvations=0; tx_queue_overflows=0; tx_sequence_gaps=0; zmq_errors=0
gnb0_overflow=0; gnb1_overflow=0
sionna_updates=0; telemetry_ok=0

write_summary() {
  local status="$1"
  local exit_code="$2"
  cat >"${summary_path}" <<JSON
{
  "timestamp": "${timestamp}",
  "status": "${status}",
  "channel_mode": "${channel_mode}",
  "duration_seconds": ${duration_seconds},
  "gnb0": { "cell_up": ${gnb0_up}, "rt_overflow": ${gnb0_overflow} },
  "gnb1": { "cell_up": ${gnb1_up}, "rt_overflow": ${gnb1_overflow} },
  "ue0": { "rrc_connected": ${rrc0}, "pdu_session_established": ${pdu0}, "ping_ok": ${ping0} },
  "ue1": { "rrc_connected": ${rrc1}, "pdu_session_established": ${pdu1}, "ping_ok": ${ping1} },
  "broker_status": ${broker_status},
  "rx_starvations": ${rx_starvations},
  "tx_queue_overflows": ${tx_queue_overflows},
  "tx_sequence_gaps": ${tx_sequence_gaps},
  "zmq_errors": ${zmq_errors},
  "sionna_updates": ${sionna_updates},
  "telemetry_ok": ${telemetry_ok},
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
  -DCMAKE_CUDA_COMPILER="${cuda_compiler}" \
  -DOCUDU_GPU_CHANNEL_CUDA_ARCHITECTURES=120 >"${log_dir}/cmake-configure.log" 2>&1
cmake --build "${cuda_build}" -j"$(nproc)" >"${log_dir}/cmake-build.log" 2>&1

# --- generated config ----------------------------------------------------------
gnb0_config="${config_dir}/gnb0_zmq.yml"
gnb1_config="${config_dir}/gnb1_zmq.yml"
compose_override="${config_dir}/docker-compose.ocudu-gpu-channel.yml"
ocudu_dockerfile="${config_dir}/Dockerfile.ocudu-zmq"
srsue_dockerfile="${config_dir}/Dockerfile.srsue"

# One gNB config per cell from the shared ZMQ base: rewrite the ZMQ ports and
# add a distinct PCI, gnb_id and node name so the two cells are independent.
#
# The base is the 4T4R rank-1 fixture, so each cell carries FOUR antennas in
# both directions and each srsUE keeps one: per UE the downlink is a 1x4 row
# and the uplink a 4x1 column, still rank-1 MISO/SIMO. Everything that fixture
# is careful about carries over unchanged and is load-bearing -- ue_dedicated
# with DCI 0_1/1_1, CSI-RS off, and above all the pusch.max_ue_mcs cap, without
# which UL 4R registration is a coin flip (root-caused 2026-08-17).
#
# The gains go to 0 for the same reason the native renderers zero them: the
# fixture carries the B210's tx_gain/rx_gain of 75, and OCUDU's ZMQ device
# refuses anything above 0 dB ("Channel gain must be <= 0.0 dB for
# ZMQ-device"), which kills the cell right after the DU is created.
gen_gnb_config() {
  # $1 dst  $2 first_port  $3 pci  $4 gnb_id  $5 ran_node_name
  #
  # A cell takes eight consecutive ports from $2: tx on the even offsets and
  # rx on the odd ones, port by port, which is the pairing both the fixture's
  # device_args and the broker topology's gnbN_pM devices use. The two cells
  # take disjoint blocks so a stale socket cannot cross between them.
  #
  # inactivity_timer goes inside the fixture's existing cu_cp block, not after
  # it: a second top-level `cu_cp:` mapping is a duplicate key, not an override.
  local base="$2" args="" port
  for port in 0 1 2 3; do
    args+="tx_port${port}=tcp://*:$((base + port * 2)),"
  done
  for port in 0 1 2 3; do
    args+="rx_port${port}=tcp://host.docker.internal:$((base + port * 2 + 1)),"
  done
  args+="base_srate=23.04e6"
  awk -v args="${args}" -v pci="$3" -v gid="$4" -v nm="$5" \
      -v inactivity="${ue_inactivity_seconds}" '
    /^[[:space:]]*device_args:/ { print "  device_args: " args; next }
    /^[[:space:]]*tx_gain:/ { print "  tx_gain: 0"; next }
    /^[[:space:]]*rx_gain:/ { print "  rx_gain: 0"; next }
    { print }
    /^cu_cp:/ { if (inactivity != "") print "  inactivity_timer: " inactivity }
    /^cell_cfg:/ { print "  pci: " pci }
    END { print ""; print "gnb_id: " gid; print "ran_node_name: " nm }
  ' "${project_root}/use_cases/configs/ran/ocudu/docker/gnb_zmq_b210_fdd_4t4r_rank1_srsue.yaml" >"$1"
}
gen_gnb_config "${gnb0_config}" 3000 1 411 gnb0
gen_gnb_config "${gnb1_config}" 3010 2 412 gnb1

awk '
  { print }
  /install_docker_dependencies\.sh build/ {
    print "RUN apt-get update && apt-get install -y --no-install-recommends libzmq3-dev && rm -rf /var/lib/apt/lists/*"
  }
  /^FROM .* AS runtime/ {
    print "RUN apt-get update && apt-get install -y --no-install-recommends libzmq5 && rm -rf /var/lib/apt/lists/*"
  }
' "${ocudu_root}/docker/Dockerfile" >"${ocudu_dockerfile}"

# Compose override: point Open5GS at a two-UE subscriber CSV, publish the cell-0
# gNB TX port, and add a second gNB (cell 1) on its own 5GC-network IP, ZMQ
# port and layered config.
# OCUDU's compose hard-codes the `ran` and `metrics` subnets. On a workstation
# that already hosts another 5G stack -- or a second gate running concurrently --
# those pools are taken and compose fails the whole run at network creation,
# before a container starts:
#   "invalid pool request: Pool overlaps with other one on this address space"
# Pick pools nothing else claims, and render an Open5GS env file carrying the
# matching address -- the core binds the literal address from that file and
# aborts with "Cannot assign requested address" if it is left behind.
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
ran_subnet="${OCUDU_MGNB_RAN_SUBNET:-$(pick_free_subnet 10.53.1.0/24 10.63.1.0/24 10.64.1.0/24 10.65.1.0/24)}"
metrics_subnet="${OCUDU_MGNB_METRICS_SUBNET:-$(pick_free_subnet 172.19.1.0/24 172.29.1.0/24 172.30.1.0/24)}"
[[ -n "${ran_subnet}" && -n "${metrics_subnet}" ]] || { echo "no free /24 for the gate networks" >&2; exit 1; }
ran_prefix="${ran_subnet%.0/24}"
metrics_prefix="${metrics_subnet%.0/24}"
export OPEN5GS_IP="${ran_prefix}.2"
export GNB_IP="${ran_prefix}.3"
open5gs_env="${config_dir}/open5gs.env"
sed -e "s|^OPEN5GS_IP=.*|OPEN5GS_IP=${OPEN5GS_IP}|" \
    -e "s|^UPF_ADVERTISE_IP=.*|UPF_ADVERTISE_IP=${OPEN5GS_IP}|" \
    "${ocudu_root}/docker/open5gs/open5gs.env" >"${open5gs_env}"
export OPEN_5GS_ENV_FILE="${open5gs_env}"
printf 'ran_subnet=%s\nmetrics_subnet=%s\n' "${ran_subnet}" "${metrics_subnet}" >"${log_dir}/network-selection.txt"

cat >"${compose_override}" <<YAML
services:
  5gc:
    environment:
      SUBSCRIBER_DB: /open5gs/subscriber_db.csv
    networks:
      ran:
        ipv4_address: ${OPEN5GS_IP}
    # The base compose publishes 9999 on the host, which collides with anything
    # else already listening there and takes the whole stack down before a
    # single cell comes up. Nothing in this gate reaches the 5GC from outside
    # the compose network, so the mapping is replaced rather than merged --
    # !override is what stops Compose appending to the base list.
    ports: !override
${fivegc_ports}
  gnb:
    # One published port per transmit antenna: these are the four the cell
    # binds, and the broker connects in to each. The receive ports are dialled
    # out to host.docker.internal and need no mapping.
    ports:
      - "3000:3000"
      - "3002:3002"
      - "3004:3004"
      - "3006:3006"
    extra_hosts:
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
  gnb1:
    container_name: ocudu_gnb1
    image: ocudu/gnb
    privileged: true
    cap_add:
      - SYS_NICE
      - CAP_SYS_PTRACE
    volumes:
      - gnb1-storage:/tmp
    configs:
      - gnb1_config.yml
      - gnb1_compose_config.yml
    networks:
      ran:
        ipv4_address: ${ran_prefix}.4
      metrics:
        ipv4_address: ${metrics_prefix}.4
    ports:
      - "3010:3010"
      - "3012:3012"
      - "3014:3014"
      - "3016:3016"
    extra_hosts:
      - "host.docker.internal:host-gateway"
    depends_on:
      5gc:
        condition: service_healthy
    command: gnb -c /gnb1_config.yml -c /gnb1_compose_config.yml
configs:
  gnb1_config.yml:
    file: \${GNB1_CONFIG_PATH}
  gnb1_compose_config.yml:
    content: |
      cu_cp:
        amf:
          addrs: ${OPEN5GS_IP}
          bind_addrs: ${ran_prefix}.4
      metrics:
        autostart_stdout_metrics: true
        enable_json: true
      remote_control:
        bind_addr: 0.0.0.0
        enabled: true
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
volumes:
  gnb1-storage:
YAML

cat >"${srsue_dockerfile}" <<'DOCKER'
FROM ubuntu:22.04
ARG SRSRAN_4G_REPO=https://github.com/zhouyou-gu/srsRAN_4G.git
ARG SRSRAN_4G_REF=master
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates cmake g++ gcc git iproute2 iputils-ping \
    libboost-program-options-dev libconfig++-dev libfftw3-dev \
    libmbedtls-dev libsctp-dev libzmq3-dev make net-tools pkg-config \
  && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 --branch "${SRSRAN_4G_REF}" "${SRSRAN_4G_REPO}" /src/srsran_4g \
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
write_srsue_config "${srsue0_config}" 3101 3100 001010123456780 353490069873319
write_srsue_config "${srsue1_config}" 3103 3102 001010123456781 353490069873320

# Two-UE Open5GS subscriber CSV (name,imsi,key,op_type,opc,amf,qci,ip).
subscriber_db="${ocudu_root}/docker/open5gs/subscriber_db.csv"
rm -rf "${subscriber_db}"
cat >"${subscriber_db}" <<'CSV'
ue0,001010123456780,00112233445566778899aabbccddeeff,opc,63bfa50ee6523365ff14c1f45f88737d,8000,9,10.45.1.2
ue1,001010123456781,00112233445566778899aabbccddeeff,opc,63bfa50ee6523365ff14c1f45f88737d,8000,9,10.45.1.3
CSV

compose=(docker compose -f "${ocudu_root}/docker/docker-compose.yml" -f "${compose_override}")
export GNB_CONFIG_PATH="${gnb0_config}"
export GNB1_CONFIG_PATH="${gnb1_config}"
export OCUDU_ZMQ_DOCKERFILE="${ocudu_dockerfile}"
export OS=ubuntu OS_VERSION=24.04
srsue_image="ocudu-gpu-channel/srsue-zmq:${srsran_ref}"
broker_pid=""
sionna_pid=""
telemetry_pid=""
ue0_pid=""
ue1_pid=""
keepalive0_pid=""
keepalive1_pid=""

cleanup() {
  set +e
  # The keepalive goes first: no reason to keep injecting user-plane traffic
  # into a stack that is being torn down.
  [[ -n "${keepalive0_pid}" ]] && kill "${keepalive0_pid}" >/dev/null 2>&1
  [[ -n "${keepalive1_pid}" ]] && kill "${keepalive1_pid}" >/dev/null 2>&1
  [[ -n "${ue0_pid}" ]] && kill "${ue0_pid}" >/dev/null 2>&1
  [[ -n "${ue1_pid}" ]] && kill "${ue1_pid}" >/dev/null 2>&1
  [[ -n "${telemetry_pid}" ]] && kill "${telemetry_pid}" >/dev/null 2>&1
  [[ -n "${sionna_pid}" ]] && kill "${sionna_pid}" >/dev/null 2>&1
  [[ -n "${broker_pid}" ]] && kill "${broker_pid}" >/dev/null 2>&1
  [[ -n "${broker_image}" ]] && docker rm -f ocudu_broker_mgnb >/dev/null 2>&1
  docker rm -f ocudu_srsue_0 ocudu_srsue_1 >/dev/null 2>&1
  docker cp ocudu_gnb:/tmp/gnb.log "${log_dir}/ocudu-gnb0-internal.log" >/dev/null 2>&1
  docker cp ocudu_gnb1:/tmp/gnb.log "${log_dir}/ocudu-gnb1-internal.log" >/dev/null 2>&1
  "${compose[@]}" logs --no-color >"${log_dir}/docker-compose.log" 2>&1
  docker logs open5gs_5gc >"${log_dir}/open5gs.log" 2>&1
  "${compose[@]}" down --remove-orphans --volumes >"${log_dir}/docker-down.log" 2>&1
}
trap cleanup EXIT

"${compose[@]}" down --remove-orphans --volumes >"${log_dir}/docker-preclean.log" 2>&1 || true
docker rm -f open5gs_5gc ocudu_gnb ocudu_gnb1 ocudu_srsue_0 ocudu_srsue_1 >"${log_dir}/docker-rm.log" 2>&1 || true

if [[ "${build_docker}" == "1" ]]; then
  "${compose[@]}" build 5gc gnb >"${log_dir}/docker-build.log" 2>&1
fi
if ! docker build --build-arg "SRSRAN_4G_REPO=${SRSRAN_4G_REPO:-https://github.com/zhouyou-gu/srsRAN_4G.git}" \
     --build-arg "SRSRAN_4G_REF=${srsran_ref}" -f "${srsue_dockerfile}" \
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

# CUDA broker on the multi-gNB topology (4 nodes, inter-cell interference).
# The proven static path remains the default. Sionna mode selects the external
# runtime profiles on the same validated topology and enables control/telemetry
# without changing OCUDU. A Sionna run is stopped explicitly after attach/ping so tracing setup
# time does not consume the radio validation window.
# The static gate's topology hand-tunes the two cells apart (serving 6 dB vs
# intercell 20 dB of fixed path loss) and a profile_swap replaces only a link's
# taps, so those steps would sit on top of every ray-traced channel. Sionna
# mode therefore defaults to the bare-anchor topology, where the scene alone
# decides which cell is stronger.
if [[ "${topology_name}" == "__default__" ]]; then
  if [[ "${channel_mode}" == "sionna" ]]; then
    topology_name="use_cases/configs/topologies/sionna/topology.sionna-multi-gnb.cuda.yaml"
  else
    topology_name="use_cases/configs/topologies/ocudu_docker/topology.multi-gnb.cuda.yaml"
  fi
fi
case "${topology_name}" in
  /*) topology_host="${topology_name}" ;;
  *) topology_host="${project_root}/${topology_name}" ;;
esac
topology_container="/work/${topology_name#"${project_root}/"}"
[[ -f "${topology_host}" ]] || { echo "missing topology: ${topology_host}" >&2; exit 2; }

# The telemetry topic is the broker's own link key, "from>to:model", so the
# list of links to expect has to come from the topology the broker was handed.
# check_feed.py's built-in default names ten ":sionna_rt" links, which belongs
# to topology.sionna-2gnb-2ue.cuda.yaml, not to this gate's eight
# serving/intercell ones -- left implicit it reports every real link as
# missing while the feed is perfectly healthy.
topology_links() {
  awk '
    /^links:/ { in_links = 1; next }
    /^[^[:space:]]/ { in_links = 0 }
    in_links && $1 == "-" && $2 == "from:" { from = $3; next }
    in_links && $1 == "to:" { to = $2; next }
    in_links && $1 == "model:" {
      printf "%s%s>%s:%s", separator, from, to, $2
      separator = ","
    }
    END { printf "\n" }
  ' "$1"
}
broker_duration="${duration_seconds}s"
broker_extra=()
if [[ "${channel_mode}" == "sionna" ]]; then
  broker_duration="0s"
  broker_extra=(
    --control-endpoint 'tcp://*:5559'
    --telemetry-endpoint 'tcp://*:5560'
    --telemetry-rate-hz 500
  )
fi

# Native binary by default; container image when OCUDU_MGNB_BROKER_IMAGE is set.
if [[ -z "${broker_image}" ]]; then
  "${cuda_build}/ocudu-gpu-channel" \
    --config "${topology_host}" \
    --duration "${broker_duration}" "${broker_extra[@]}" \
    >"${log_dir}/broker.log" 2>&1 &
  broker_pid="$!"
else
  echo "broker mode: container image ${broker_image}" >"${log_dir}/broker-mode.txt"
  docker run --rm --name ocudu_broker_mgnb \
    --gpus all --network host \
    -v "${project_root}:/work:ro" \
    "${broker_image}" \
    --config "${topology_container}" \
    --duration "${broker_duration}" "${broker_extra[@]}" \
    >"${log_dir}/broker.log" 2>&1 &
  broker_pid="$!"
fi

if [[ "${channel_mode}" == "sionna" ]]; then
  # Wait for the REP control server before starting the sole channel writer.
  for _ in $(seq 1 50); do
    grep -q 'event=control_start' "${log_dir}/broker.log" 2>/dev/null && break
    kill -0 "${broker_pid}" 2>/dev/null || write_summary "broker_failed_before_sionna" 1
    sleep 0.1
  done
  if ! grep -q 'event=control_start' "${log_dir}/broker.log" 2>/dev/null; then
    write_summary "control_server_not_ready" 1
  fi

  "${project_root}/scripts/sionna_rt/run_web_ui.sh" \
    --python "${sionna_python}" \
    --scenario "${sionna_scenario}" \
    --control-endpoint tcp://127.0.0.1:5559 \
    --telemetry-endpoint tcp://127.0.0.1:5560 \
    --duration 0 \
    --update-hz "${sionna_update_hz}" \
    --port "${sionna_web_port}" \
    --ready-seconds "${sionna_ready_seconds}" \
    --status-jsonl "${log_dir}/sionna-status.jsonl" \
    -- "${sionna_bridge_args[@]}" \
    >"${log_dir}/sionna-bridge.log" 2>&1 &
  sionna_pid="$!"

  # Do not start the radios against the topology's -100 dB placeholder. The
  # first successful record means all ten profiles were committed atomically.
  sionna_ready=0
  for _ in $(seq 1 "${sionna_ready_seconds}"); do
    if grep -q 'event=sionna_web_ui_ready' "${log_dir}/sionna-bridge.log" 2>/dev/null; then
      sionna_ready=1
      break
    fi
    kill -0 "${sionna_pid}" 2>/dev/null || break
    sleep 1
  done
  if [[ "${sionna_ready}" -ne 1 ]]; then
    write_summary "sionna_bridge_not_ready" 2
  fi
  sionna_updates="$(grep -c '"event":"sionna_rt_update"' "${log_dir}/sionna-bridge.log" 2>/dev/null)" || sionna_updates=0
  echo "Sionna Web UI: http://127.0.0.1:${sionna_web_port}"

  expected_links="$(topology_links "${topology_host}")"
  [[ -n "${expected_links}" ]] || write_summary "topology_has_no_links" 2
  "${sionna_python}" "${project_root}/scripts/tools/check_feed.py" \
    --endpoint tcp://127.0.0.1:5560 --duration 10 --links "${expected_links}" \
    >"${log_dir}/telemetry-check.json" 2>&1 &
  telemetry_pid="$!"
fi

"${compose[@]}" up -d gnb gnb1 >"${log_dir}/docker-gnb-up.log" 2>&1
# Wait for both cells to broadcast SIB1 / activate.
for _ in $(seq 1 40); do
  docker cp ocudu_gnb:/tmp/gnb.log "${log_dir}/ocudu-gnb0-internal.log" >/dev/null 2>&1 || true
  docker cp ocudu_gnb1:/tmp/gnb.log "${log_dir}/ocudu-gnb1-internal.log" >/dev/null 2>&1 || true
  grep -q 'Cell.*activated\|SIB1' "${log_dir}/ocudu-gnb0-internal.log" 2>/dev/null && gnb0_up=1
  grep -q 'Cell.*activated\|SIB1' "${log_dir}/ocudu-gnb1-internal.log" 2>/dev/null && gnb1_up=1
  [[ "${gnb0_up}" -eq 1 && "${gnb1_up}" -eq 1 ]] && break
  sleep 1
done
echo "cells: gnb0_up=${gnb0_up} gnb1_up=${gnb1_up}"

run_srsue() {
  # $1 container name  $2 tx port  $3 config  $4 log
  docker run --rm --name "$1" --privileged --cap-add NET_ADMIN --device /dev/net/tun \
    --add-host host.docker.internal:host-gateway -p "$2:$2" \
    -v "$3:/config/ue.conf:ro" --entrypoint /bin/sh \
    "${srsue_image}" -lc 'mkdir -p /var/run/netns && ip netns add ue1 && exec srsue /config/ue.conf' \
    >"$4" 2>&1 &
}
run_srsue ocudu_srsue_0 3101 "${srsue0_config}" "${log_dir}/srsue0.log"
ue0_pid="$!"
# Hold ue1 until ue0 is RRC-connected (capped), then a short settle.
if [[ "${ue_stagger_seconds}" -gt 0 ]]; then
  for _ in $(seq 1 "${ue_stagger_seconds}"); do
    grep -q 'RRC Connected' "${log_dir}/srsue0.log" 2>/dev/null && break
    sleep 1
  done
  sleep 2
fi
run_srsue ocudu_srsue_1 3103 "${srsue1_config}" "${log_dir}/srsue1.log"
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

# Keep the DRBs carrying traffic so the gNB has no idle UE to release. Started
# only after the verdict is in, so it cannot manufacture the attach it is meant
# to preserve.
keepalive_ue() {
  # $1 container name
  docker exec "$1" sh -lc '
      if ip netns list 2>/dev/null | grep -q ue1; then ns="ip netns exec ue1"; else ns=""; fi
      exec $ns ping -I tun_srsue -i '"${ue_keepalive_seconds}"' 10.45.1.1
    ' >/dev/null 2>&1 &
}
if [[ "${ue_keepalive_seconds}" != "0" ]]; then
  [[ "${ping0}" -eq 1 ]] && { keepalive_ue ocudu_srsue_0; keepalive0_pid="$!"; }
  [[ "${ping1}" -eq 1 ]] && { keepalive_ue ocudu_srsue_1; keepalive1_pid="$!"; }
  printf 'UE keepalive every %ss\n' "${ue_keepalive_seconds}"
fi

# Hold the live stack open so the dashboard can be watched. Everything stays
# running: both cells keep serving, the UEs keep their PDU sessions, and the
# bridge keeps re-tracing the moving UEs into the broker. Ctrl-C cuts the hold
# short and still runs the normal teardown and summary.
if [[ "${hold_seconds}" -gt 0 ]]; then
  if [[ "${channel_mode}" == "sionna" ]]; then
    printf 'holding the stack for %ss -- Web UI: http://127.0.0.1:%s\n' \
      "${hold_seconds}" "${sionna_web_port}"
  else
    printf 'holding the stack for %ss\n' "${hold_seconds}"
  fi
  sleep "${hold_seconds}" || true
fi

set +e
if [[ "${channel_mode}" == "sionna" ]]; then
  [[ -n "${telemetry_pid}" ]] && { wait "${telemetry_pid}"; telemetry_ok=$(( $? == 0 )); telemetry_pid=""; }
  [[ -n "${sionna_pid}" ]] && { kill -INT "${sionna_pid}" >/dev/null 2>&1; wait "${sionna_pid}" >/dev/null 2>&1; sionna_pid=""; }
  sionna_updates="$(grep -c '"event":"sionna_rt_update"' "${log_dir}/sionna-bridge.log" 2>/dev/null)" || sionna_updates=0
  if [[ -z "${broker_image}" ]]; then
    kill -INT "${broker_pid}" >/dev/null 2>&1
  else
    docker stop -t 10 ocudu_broker_mgnb >/dev/null 2>&1
  fi
fi
wait "${broker_pid}"; broker_status="$?"; broker_pid=""
docker rm -f ocudu_srsue_0 ocudu_srsue_1 >/dev/null 2>&1
[[ -n "${ue0_pid}" ]] && { kill "${ue0_pid}" >/dev/null 2>&1; wait "${ue0_pid}" >/dev/null 2>&1; }
[[ -n "${ue1_pid}" ]] && { kill "${ue1_pid}" >/dev/null 2>&1; wait "${ue1_pid}" >/dev/null 2>&1; }
ue0_pid=""; ue1_pid=""
set -e

docker cp ocudu_gnb:/tmp/gnb.log "${log_dir}/ocudu-gnb0-internal.log" >/dev/null 2>&1 || true
docker cp ocudu_gnb1:/tmp/gnb.log "${log_dir}/ocudu-gnb1-internal.log" >/dev/null 2>&1 || true
"${compose[@]}" logs --no-color >"${log_dir}/docker-compose.log" 2>&1 || true

# grep -c prints the count (0 included) and exits 1 on no match; capture the
# single count and fall back to 0 only when grep cannot read the file.
if [[ -f "${log_dir}/ocudu-gnb0-internal.log" ]]; then
  gnb0_overflow="$(grep -c 'Real-time failure in RF: overflow' "${log_dir}/ocudu-gnb0-internal.log" 2>/dev/null)" || gnb0_overflow=0
fi
if [[ -f "${log_dir}/ocudu-gnb1-internal.log" ]]; then
  gnb1_overflow="$(grep -c 'Real-time failure in RF: overflow' "${log_dir}/ocudu-gnb1-internal.log" 2>/dev/null)" || gnb1_overflow=0
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
if [[ "${channel_mode}" == "sionna" && ( "${sionna_updates}" -eq 0 || "${telemetry_ok}" -ne 1 ) ]]; then
  write_summary "sionna_observability_failed" 1
fi
if [[ "${gnb0_up}" -ne 1 || "${gnb1_up}" -ne 1 ]]; then
  write_summary "gnb_cell_blocker" 2
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
