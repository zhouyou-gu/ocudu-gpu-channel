#!/usr/bin/env bash
# Start the Sionna controller and read-only Web UI for an already-running
# broker. The broker must expose the matching control and telemetry endpoints.
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/../.." && pwd)"
python_bin="${OCUDU_SIONNA_PYTHON:-python3}"
scenario=""
status_jsonl=""
control_endpoint="tcp://127.0.0.1:5559"
telemetry_endpoint="tcp://127.0.0.1:5560"
bind_address="127.0.0.1"
port="8080"
duration="0"
update_hz="500"
ready_seconds="120"

usage()
{
  echo "usage: $0 --scenario FILE --status-jsonl FILE [--python FILE] [--port N] [-- BRIDGE_ARGS...]" >&2
  exit 2
}

while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --scenario) scenario="${2:-}"; shift 2 ;;
    --status-jsonl) status_jsonl="${2:-}"; shift 2 ;;
    --python) python_bin="${2:-}"; shift 2 ;;
    --control-endpoint) control_endpoint="${2:-}"; shift 2 ;;
    --telemetry-endpoint) telemetry_endpoint="${2:-}"; shift 2 ;;
    --bind) bind_address="${2:-}"; shift 2 ;;
    --port) port="${2:-}"; shift 2 ;;
    --duration) duration="${2:-}"; shift 2 ;;
    --update-hz) update_hz="${2:-}"; shift 2 ;;
    --ready-seconds) ready_seconds="${2:-}"; shift 2 ;;
    --) shift; break ;;
    *) usage ;;
  esac
done

[[ -f "${scenario}" && -f "${repo_root}/apps/sionna_bridge/run_bridge.py" ]] || usage
[[ -n "${status_jsonl}" ]] || usage
[[ -x "${python_bin}" ]] || { echo "Sionna Python is not executable: ${python_bin}" >&2; exit 2; }
[[ "${bind_address}" == "127.0.0.1" || "${bind_address}" == "localhost" ]] || {
  echo "Web UI bind is restricted to loopback" >&2
  exit 2
}
[[ "${port}" =~ ^[1-9][0-9]*$ && "${port}" -le 65535 ]] || usage
mkdir -p "$(dirname "${status_jsonl}")"

bridge_pid=""
web_pid=""
cleanup()
{
  local status="$?"
  trap - EXIT INT TERM HUP
  [[ -n "${web_pid}" ]] && kill -TERM "${web_pid}" >/dev/null 2>&1 || true
  [[ -n "${bridge_pid}" ]] && kill -TERM "${bridge_pid}" >/dev/null 2>&1 || true
  [[ -n "${web_pid}" ]] && wait "${web_pid}" >/dev/null 2>&1 || true
  [[ -n "${bridge_pid}" ]] && wait "${bridge_pid}" >/dev/null 2>&1 || true
  exit "${status}"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

"${python_bin}" "${repo_root}/apps/sionna_bridge/run_bridge.py" \
  --scenario-config "${scenario}" \
  --control-endpoint "${control_endpoint}" \
  --duration "${duration}" --update-hz "${update_hz}" \
  --status-jsonl "${status_jsonl}" "$@" &
bridge_pid="$!"

"${python_bin}" "${repo_root}/apps/dashboard/server.py" \
  --bind "${bind_address}" --port "${port}" \
  --telemetry-endpoint "${telemetry_endpoint}" \
  --status-jsonl "${status_jsonl}" \
  --index "${repo_root}/apps/dashboard/index.html" &
web_pid="$!"

deadline=$((SECONDS + ready_seconds))
web_url="http://${bind_address}:${port}"
while [[ "${SECONDS}" -lt "${deadline}" ]]; do
  kill -0 "${bridge_pid}" 2>/dev/null || { echo "Sionna bridge exited before ready" >&2; exit 1; }
  kill -0 "${web_pid}" 2>/dev/null || { echo "Web UI exited before ready" >&2; exit 1; }
  if grep -q '"event":"sionna_rt_update"' "${status_jsonl}" 2>/dev/null && \
     "${python_bin}" -c 'import sys,urllib.request; urllib.request.urlopen(sys.argv[1],timeout=1)' \
       "${web_url}/healthz" >/dev/null 2>&1; then
    printf 'event=sionna_web_ui_ready url=%s scenario=%s status_jsonl=%s\n' \
      "${web_url}" "${scenario}" "${status_jsonl}"
    break
  fi
  sleep 0.25
done
[[ "${SECONDS}" -lt "${deadline}" ]] || { echo "Sionna/Web UI readiness timed out" >&2; exit 1; }

wait "${bridge_pid}"
