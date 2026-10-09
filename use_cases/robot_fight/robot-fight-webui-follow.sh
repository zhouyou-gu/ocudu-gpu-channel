#!/usr/bin/env bash
# Keep two read-only Web UIs pointed at the NEWEST robot-fight gate run:
# port 8080 = cell a (broker a, gnb0), port 8081 = cell b (broker b, gnb1).
#
# The gate's own optional Web UI (OCUDU_NATIVE_RF_WEB_UI=1) lives and dies
# with one run; this follower survives across runs, so a browser (through an
# ssh tunnel, e.g. `ssh -L 8080:127.0.0.1:8080 -L 8081:127.0.0.1:8081 ...`)
# keeps showing whatever run is live. Each UI gets the run's telemetry socket,
# its Sionna status jsonl and -- when the run was rendered with gNB metrics
# (run-parameters.json `gnb_metrics`, the gate's default) -- that cell's
# gNB KPI relay socket, named gnb0/gnb1 so the panel says which cell it is. The relay socket appears after the gNB starts; the UI
# reconnects with backoff until then. Run as the gate's user (root in the
# container): the sockets are root-owned 0755, so another user cannot connect.
#
# Env: OCUDU_NATIVE_ROOT (default /workspace/ocudu-spark), OCUDU_GPUCH_REPO
# (default /workspace/gpuch/int0928), OCUDU_NATIVE_SIONNA_PYTHON (default
# /workspace/sionna-venv/bin/python), WEBUI_LOG_DIR (default /workspace/gpuch).
set -u
native_root="${OCUDU_NATIVE_ROOT:-/workspace/ocudu-spark}"
repo="${OCUDU_GPUCH_REPO:-/workspace/gpuch/int0928}"
py="${OCUDU_NATIVE_SIONNA_PYTHON:-/workspace/sionna-venv/bin/python}"
log_root="${WEBUI_LOG_DIR:-/workspace/gpuch}"
run_root="${native_root}/run/ocudu-robot-fight-native"
cur=""; pa=""; pb=""

metrics_endpoint()
{
  # Prints ws+unix://<socket> for cell $2 of run $1 when the run was rendered
  # with gNB metrics, else nothing. Read from the report, not from the socket
  # file, so the UI is started with the feed before the relay is up.
  /usr/bin/python3 - "${native_root}/results/reports/ocudu-robot-fight/$1/run-parameters.json" "$2" <<'PY' 2>/dev/null
import json, sys
try:
    params = json.load(open(sys.argv[1]))
except (OSError, ValueError):
    sys.exit(0)
cell = (params.get("gnb_metrics") or {}).get(sys.argv[2]) or {}
print(cell.get("endpoint", ""), end="")
PY
}

while true; do
  d="$(ls -td "${run_root}"/* 2>/dev/null | head -1)"
  if [[ -n "${d}" && "${d}" != "${cur}" && -S "${d}/telemetry-a.sock" ]]; then
    ts="$(basename "${d}")"
    l="${native_root}/results/logs/ocudu-robot-fight/${ts}"
    [[ -n "${pa}" ]] && kill "${pa}" 2>/dev/null
    [[ -n "${pb}" ]] && kill "${pb}" 2>/dev/null
    sleep 1
    ma="$(metrics_endpoint "${ts}" a)"; mb="$(metrics_endpoint "${ts}" b)"
    args_a=(); args_b=()
    [[ -z "${ma}" ]] || args_a=(--gnb-metrics-source "gnb0=${ma}")
    [[ -z "${mb}" ]] || args_b=(--gnb-metrics-source "gnb1=${mb}")
    "${py}" "${repo}/scripts/web_ui/server.py" --bind 127.0.0.1 --port 8080 \
      --telemetry-endpoint "ipc://${d}/telemetry-a.sock" --status-jsonl "${l}/sionna-status-a.jsonl" \
      --index "${repo}/scripts/web_ui/index.html" "${args_a[@]}" >"${log_root}/webui-a.log" 2>&1 &
    pa=$!
    "${py}" "${repo}/scripts/web_ui/server.py" --bind 127.0.0.1 --port 8081 \
      --telemetry-endpoint "ipc://${d}/telemetry-b.sock" --status-jsonl "${l}/sionna-status-b.jsonl" \
      --index "${repo}/scripts/web_ui/index.html" "${args_b[@]}" >"${log_root}/webui-b.log" 2>&1 &
    pb=$!
    cur="${d}"
    echo "$(date -u +%T) webui -> ${ts} (pids ${pa} ${pb}) metrics a=${ma:-off} b=${mb:-off}"
  fi
  sleep 5
done
