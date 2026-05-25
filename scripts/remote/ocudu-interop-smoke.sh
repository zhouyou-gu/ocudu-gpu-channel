#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${script_dir}/common.sh"

branch="${OCUDU_INTEROP_BRANCH:-$(git -C "${repo_root}" branch --show-current)}"
duration_seconds="${OCUDU_INTEROP_DURATION_SECONDS:-30}"
build_docker="${OCUDU_INTEROP_BUILD_DOCKER:-1}"
skip_remote_pull="${OCUDU_INTEROP_SKIP_REMOTE_PULL:-0}"

if [[ -z "${branch}" ]]; then
  echo "unable to determine current branch" >&2
  exit 1
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
  "${skip_remote_pull}" <<'REMOTE'
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
skip_remote_pull="${10}"

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

extract_log_value() {
  local key="$1"
  local path="$2"
  local value
  value="$(awk -v key="${key}=" 'index($0, key) { split($0, a, key); split(a[2], b, " "); print b[1] }' "${path}" 2>/dev/null | tail -n 1)"
  printf '%s\n' "${value:-0}"
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

wait_for_gnb() {
  for _ in $(seq 1 120); do
    local state
    state="$(docker inspect -f '{{.State.Status}}' "${gnb_cid}" 2>/dev/null || true)"
    if [[ "${state}" == "running" ]]; then
      return 0
    fi
    sleep 1
  done
  docker ps -a --format '{{.Names}} {{.Status}}' >"${log_dir}/docker-ps-gnb-timeout.log" 2>&1 || true
  echo "ocudu gnb container did not reach running state" >&2
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
run_duration_seconds=$((duration_seconds + 15))
mkdir -p "${log_dir}" "${report_dir}" "${config_dir}" "${cuda_build}"

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
    # Pin the OCUDU gNB to dedicated cores 4-23 so its ZMQ async tasks and PHY
    # workers are not migrated or preempted by the host-side broker/UE tools.
    cpuset: "4-23"
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

compose=(docker compose -f "${ocudu_root}/docker/docker-compose.yml" -f "${compose_override}")
export GNB_CONFIG_PATH="${gnb_config}"
export OCUDU_ZMQ_DOCKERFILE="${ocudu_dockerfile}"
export OS="${OS:-ubuntu}"
export OS_VERSION="${OS_VERSION:-24.04}"

broker_pid=""
ue_source_pid=""
ue_sink_pid=""
gnb_cid=""

cleanup() {
  set +e
  if [[ -n "${ue_sink_pid}" ]]; then kill "${ue_sink_pid}" >/dev/null 2>&1; fi
  if [[ -n "${ue_source_pid}" ]]; then kill "${ue_source_pid}" >/dev/null 2>&1; fi
  if [[ -n "${broker_pid}" ]]; then kill "${broker_pid}" >/dev/null 2>&1; fi
  if [[ -n "${gnb_cid}" ]]; then
    docker cp "${gnb_cid}:/tmp/gnb.log" "${log_dir}/ocudu-gnb-internal.log" >/dev/null 2>&1
    docker cp "${gnb_cid}:/tmp/gnb_mac.pcap" "${log_dir}/gnb_mac.pcap" >/dev/null 2>&1
    docker cp "${gnb_cid}:/tmp/gnb_ngap.pcap" "${log_dir}/gnb_ngap.pcap" >/dev/null 2>&1
    docker logs "${gnb_cid}" >"${log_dir}/ocudu-gnb.log" 2>&1
  fi
  docker ps -a --format '{{.Names}} {{.Status}}' >"${log_dir}/docker-ps.log" 2>&1
  "${compose[@]}" logs --no-color >"${log_dir}/docker-compose.log" 2>&1
  docker logs open5gs_5gc >"${log_dir}/open5gs.log" 2>&1
  "${compose[@]}" down --remove-orphans --volumes >"${log_dir}/docker-down.log" 2>&1
}
trap cleanup EXIT

"${compose[@]}" down --remove-orphans --volumes >"${log_dir}/docker-preclean.log" 2>&1 || true
docker rm -f open5gs_5gc ocudu_gnb >"${log_dir}/docker-rm.log" 2>&1 || true

if [[ "${build_docker}" == "1" ]]; then
  "${compose[@]}" build 5gc gnb >"${log_dir}/docker-build.log" 2>&1
fi

"${compose[@]}" up -d 5gc >"${log_dir}/docker-core-up.log" 2>&1
wait_for_open5gs

# Bring the OCUDU gNB up first and wait for the container, then start the
# synthetic UE source/sink and broker as one group. The sink may connect before
# the broker binds; ZeroMQ will complete that connection once the broker is up,
# which avoids an artificial startup gap where the DL ring has no consumer.
"${compose[@]}" up -d gnb >"${log_dir}/docker-gnb-up.log" 2>&1
gnb_cid="$("${compose[@]}" ps -q gnb 2>/dev/null | head -n 1 || true)"
if [[ -z "${gnb_cid}" ]]; then
  echo "unable to resolve ocudu gnb container id" >&2
  exit 1
fi
wait_for_gnb

# Pin the host-side tools to their own cores (0-2), kept clear of the gNB's
# cores (4-23), so the broker's busy-spin loop never preempts a gNB thread.
taskset -c 1 "${cuda_build}/ocudu-zmq-source" \
  --endpoint tcp://*:2101 \
  --batch-samples 23040 \
  --duration "${run_duration_seconds}s" >"${log_dir}/synthetic-ue-source.log" 2>&1 &
ue_source_pid="$!"

taskset -c 2 "${cuda_build}/ocudu-zmq-sink" \
  --endpoint tcp://127.0.0.1:2100 \
  --duration "${run_duration_seconds}s" >"${log_dir}/synthetic-ue-sink.log" 2>&1 &
ue_sink_pid="$!"

taskset -c 0 "${cuda_build}/ocudu-gpu-channel" \
  --config "${project_root}/examples/topology.ocudu-docker.cuda.yaml" \
  --duration "${run_duration_seconds}s" \
  --strict-realtime >"${log_dir}/broker.log" 2>&1 &
broker_pid="$!"

set +e
wait "${ue_sink_pid}"
ue_sink_status="$?"
ue_sink_pid=""
wait "${ue_source_pid}"
ue_source_status="$?"
ue_source_pid=""
wait "${broker_pid}"
broker_status="$?"
broker_pid=""
set -e

"${compose[@]}" logs --no-color >"${log_dir}/docker-compose.log" 2>&1 || true
docker ps -a --format '{{.Names}} {{.Status}}' >"${log_dir}/docker-ps.log" 2>&1 || true
docker cp "${gnb_cid}:/tmp/gnb.log" "${log_dir}/ocudu-gnb-internal.log" >/dev/null 2>&1 || true
docker cp "${gnb_cid}:/tmp/gnb_mac.pcap" "${log_dir}/gnb_mac.pcap" >/dev/null 2>&1 || true
docker cp "${gnb_cid}:/tmp/gnb_ngap.pcap" "${log_dir}/gnb_ngap.pcap" >/dev/null 2>&1 || true
docker logs open5gs_5gc >"${log_dir}/open5gs.log" 2>&1 || true
docker logs "${gnb_cid}" >"${log_dir}/ocudu-gnb.log" 2>&1 || true

broker_stop="$(grep 'event=stop' "${log_dir}/broker.log" | tail -n 1 || true)"
rx_starvations="$(extract_counter rx_starvations "${broker_stop}")"
tx_queue_overflows="$(extract_counter tx_queue_overflows "${broker_stop}")"
tx_sequence_gaps="$(extract_counter tx_sequence_gaps "${broker_stop}")"
zmq_errors="$(extract_counter zmq_errors "${broker_stop}")"
samples_sent="$(extract_log_value samples_sent "${log_dir}/synthetic-ue-source.log")"
samples_received="$(extract_log_value samples_received "${log_dir}/synthetic-ue-sink.log")"
gnb_gate_log="${log_dir}/ocudu-gnb-internal.log"
if [[ ! -s "${gnb_gate_log}" ]]; then
  gnb_gate_log="${log_dir}/ocudu-gnb.log"
fi
ocudu_runtime_failures="$(grep -Eic 'late|overflow|underflow|fatal|assert|segmentation|fail(ed|ure)|zmq.*(fail|error)' "${gnb_gate_log}" 2>/dev/null || true)"

status="passed"
if [[ "${broker_status}" -ne 0 || "${ue_sink_status}" -ne 0 || "${ue_source_status}" -ne 0 ]]; then
  status="failed_process"
elif [[ "${rx_starvations}" -ne 0 || "${tx_queue_overflows}" -ne 0 || "${tx_sequence_gaps}" -ne 0 || "${zmq_errors}" -ne 0 ]]; then
  status="failed_broker_counters"
elif [[ "${samples_sent}" -eq 0 || "${samples_received}" -eq 0 ]]; then
  status="failed_zero_samples"
elif [[ "${ocudu_runtime_failures}" -ne 0 ]]; then
  status="failed_ocudu_log_gate"
fi

summary_path="${report_dir}/sample-flow-summary.json"
cat >"${summary_path}" <<JSON
{
  "timestamp": "${timestamp}",
  "status": "${status}",
  "duration_seconds": ${duration_seconds},
  "broker_status": ${broker_status},
  "ue_source_status": ${ue_source_status},
  "ue_sink_status": ${ue_sink_status},
  "rx_starvations": ${rx_starvations},
  "tx_queue_overflows": ${tx_queue_overflows},
  "tx_sequence_gaps": ${tx_sequence_gaps},
  "zmq_errors": ${zmq_errors},
  "samples_sent": ${samples_sent},
  "samples_received": ${samples_received},
  "ocudu_runtime_failures": ${ocudu_runtime_failures},
  "log_dir": "${log_dir}",
  "report_dir": "${report_dir}"
}
JSON

printf 'summary=%s\n' "${summary_path}"
printf 'status=%s\n' "${status}"

if [[ "${status}" != "passed" ]]; then
  exit 1
fi
REMOTE
