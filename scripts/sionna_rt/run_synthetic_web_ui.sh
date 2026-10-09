#!/usr/bin/env bash
# Run one of the documented topologies with synthetic ZMQ radios, Sionna RT,
# broker telemetry, and the Web UI. This is an observability check; the Docker
# interop gates remain the attach + PDU + ping verdicts.
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/../.." && pwd)"
case_name="${1:-}"
[[ -n "${case_name}" ]] || {
  echo "usage: $0 {single|multi-ue|graph|multi-gnb|multi-gnb-sutd}" >&2
  exit 2
}
duration="${OCUDU_SIONNA_DEMO_DURATION_SECONDS:-60}"
ready_seconds="${OCUDU_SIONNA_READY_SECONDS:-120}"
web_port="${OCUDU_SIONNA_WEB_PORT:-8080}"
python_bin="${OCUDU_SIONNA_PYTHON:-${repo_root}/../venvs/sionna/bin/python}"
build_dir="${OCUDU_CHANNEL_BUILD:-${repo_root}/build}"
log_dir="${OCUDU_SIONNA_LOG_DIR:-}"
[[ "${duration}" =~ ^(0|[1-9][0-9]*)$ ]] || { echo "invalid demo duration" >&2; exit 2; }
[[ "${ready_seconds}" =~ ^[1-9][0-9]*$ ]] || { echo "invalid ready timeout" >&2; exit 2; }
if [[ -z "${log_dir}" ]]; then
  log_dir="$(mktemp -d /tmp/ocudu-sionna-web.XXXXXX)"
else
  mkdir -p "${log_dir}"
fi

declare -a source_ports sink_ports
case "${case_name}" in
  single)
    topology="${repo_root}/use_cases/configs/topologies/ocudu_docker/topology.ocudu-docker.cuda.yaml"
    scenario="${repo_root}/use_cases/configs/sionna/scenarios/simple_street/ocudu-docker.json"
    source_ports=(2000 2101); sink_ports=(2001 2100) ;;
  multi-ue)
    topology="${repo_root}/use_cases/configs/topologies/ocudu_docker/topology.ocudu-docker.multi-ue.cuda.yaml"
    scenario="${repo_root}/use_cases/configs/sionna/scenarios/simple_street/ocudu-docker-multi-ue.json"
    source_ports=(2000 2101 2103); sink_ports=(2001 2100 2102) ;;
  graph)
    topology="${repo_root}/use_cases/configs/topologies/basic/topology.graph.cuda.yaml"
    scenario="${repo_root}/use_cases/configs/sionna/scenarios/simple_street/graph.json"
    source_ports=(16000 16002 16004); sink_ports=(16001 16003 16005) ;;
  multi-gnb)
    topology="${repo_root}/use_cases/configs/topologies/ocudu_docker/topology.multi-gnb.cuda.yaml"
    scenario="${repo_root}/use_cases/configs/sionna/scenarios/simple_street/multi-gnb.json"
    source_ports=(3000 3002 3101 3103); sink_ports=(3001 3003 3100 3102) ;;
  # Same two-cell topology, on the OpenStreetMap SUTD campus instead of the
  # built-in street canyon.
  multi-gnb-sutd)
    topology="${repo_root}/use_cases/configs/topologies/sionna/topology.sionna-multi-gnb.cuda.yaml"
    scenario="${repo_root}/use_cases/configs/sionna/scenarios/sutd/multi-gnb-sutd.json"
    source_ports=(3000 3002 3004 3006 3010 3012 3014 3016 3101 3103)
    sink_ports=(3001 3003 3005 3007 3011 3013 3015 3017 3100 3102) ;;
  *) echo "unknown case: ${case_name}" >&2; exit 2 ;;
esac

for executable in ocudu-gpu-channel ocudu-zmq-source ocudu-zmq-sink; do
  [[ -x "${build_dir}/${executable}" ]] || {
    echo "missing ${build_dir}/${executable}; build the project first" >&2
    exit 2
  }
done
[[ -x "${python_bin}" ]] || { echo "missing Sionna Python: ${python_bin}" >&2; exit 2; }

if [[ "${case_name}" == "multi-gnb-sutd" ]]; then
  "${python_bin}" "${script_dir}/check_demo_topology.py" \
    --topology "${topology}" --scenario "${scenario}" \
    --sources "$(IFS=,; echo "${source_ports[*]}")" \
    --sinks "$(IFS=,; echo "${sink_ports[*]}")"
fi

declare -a child_pids
cleanup()
{
  local status="$?"
  trap - EXIT INT TERM HUP
  for pid in "${child_pids[@]:-}"; do
    [[ -n "${pid}" ]] && kill -TERM "${pid}" >/dev/null 2>&1 || true
  done
  for pid in "${child_pids[@]:-}"; do
    [[ -n "${pid}" ]] && wait "${pid}" >/dev/null 2>&1 || true
  done
  printf 'logs=%s\n' "${log_dir}"
  exit "${status}"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

if [[ "${duration}" == "0" ]]; then
  process_duration=86400
else
  process_duration=$((duration + ready_seconds + 15))
fi
for port in "${source_ports[@]}"; do
  "${build_dir}/ocudu-zmq-source" --endpoint "tcp://*:${port}" \
    --batch-samples 23040 --duration "${process_duration}s" \
    >"${log_dir}/source-${port}.log" 2>&1 &
  child_pids+=("$!")
done

"${build_dir}/ocudu-gpu-channel" --config "${topology}" \
  --control-endpoint 'tcp://*:5559' --telemetry-endpoint 'tcp://*:5560' \
  --telemetry-rate-hz 500 --duration "${process_duration}s" \
  >"${log_dir}/broker.log" 2>&1 &
broker_pid="$!"
child_pids+=("${broker_pid}")

for port in "${sink_ports[@]}"; do
  "${build_dir}/ocudu-zmq-sink" --endpoint "tcp://127.0.0.1:${port}" \
    --duration "${process_duration}s" >"${log_dir}/sink-${port}.log" 2>&1 &
  child_pids+=("$!")
done

for _ in $(seq 1 100); do
  grep -q 'event=control_start' "${log_dir}/broker.log" 2>/dev/null && break
  kill -0 "${broker_pid}" 2>/dev/null || { echo "broker exited before ready" >&2; exit 1; }
  sleep 0.1
done

"${script_dir}/run_web_ui.sh" --python "${python_bin}" \
  --scenario "${scenario}" --status-jsonl "${log_dir}/sionna-status.jsonl" \
  --duration "${duration}" --ready-seconds "${ready_seconds}" --port "${web_port}" \
  >"${log_dir}/sionna-web-ui.log" 2>&1 &
sidecar_pid="$!"
child_pids+=("${sidecar_pid}")

ready_deadline=$((SECONDS + ready_seconds))
while [[ "${SECONDS}" -lt "${ready_deadline}" ]]; do
  if grep -q 'event=sionna_web_ui_ready' "${log_dir}/sionna-web-ui.log" 2>/dev/null; then
    printf 'Sionna Web UI: http://127.0.0.1:%s\n' "${web_port}"
    printf 'case=%s topology=%s scenario=%s\n' "${case_name}" "${topology}" "${scenario}"
    break
  fi
  kill -0 "${sidecar_pid}" 2>/dev/null || { tail -40 "${log_dir}/sionna-web-ui.log" >&2; exit 1; }
  sleep 0.25
done
[[ "${SECONDS}" -lt "${ready_deadline}" ]] || { echo "dashboard readiness timed out" >&2; exit 1; }
wait "${sidecar_pid}"
