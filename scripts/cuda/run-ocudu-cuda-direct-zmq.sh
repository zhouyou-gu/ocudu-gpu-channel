#!/usr/bin/env bash
# Direct ZMQ run: the CUDA gNB and srsUE wired to each other, nothing between.
#
# The 1x1 gate always puts the channel emulator between the two radios. That is
# what the gate is for, but it means every attach the CUDA gNB has passed so
# far was measured with a relay in the IQ path. A reader of the upstream report
# will reproduce without one -- gNB tx_port <-> UE rx_port, the plain srsRAN
# wiring -- so the claim that a ZMQ gNB attaches with every acceleration mode
# enabled needs one run in exactly that shape. This is that run.
#
# Nothing shipping is modified. The gNB config comes from the same renderer the
# CUDA gate uses (so the acceleration block is the audited one); the only
# change is the UE's device_args, rewritten from the emulator's ports to the
# gNB's own. No topology, no broker, no probe.
#
#   OCUDU_NATIVE_GNB_ACCELERATION      stage, default all; `cpu` runs the lock's
#                                      non-CUDA gNB (cpu_baseline) with a plain render
#   OCUDU_CUDA_DIRECT_TRAFFIC_SECONDS  keepalive ping window after attach, default 60
#   OCUDU_CUDA_DIRECT_ATTACH_SECONDS   RRC + PDU session window after srsUE starts, default 60
#                                      (Jetson Orin: srsUE's PHY init alone takes ~200 s)
#   OCUDU_DIRECT_SCHED_METRICS_MS      if set, append a scheduler metrics block (log, verbose,
#                                      this DU report period) -- diagnostic, recorded below
set -uo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/../.." && pwd)"
native_dir="${repo_root}/scripts/native"
source "${native_dir}/env.sh"

process_running() {
  local state
  state="$(ps -o stat= -p "$1" 2>/dev/null | awk '{print $1}')"
  [[ -n "${state}" && "${state:0:1}" != "Z" ]]
}

# ---------------------------------------------------------------- inner
# Runs under unshare --user --map-root-user --net --mount. Everything it needs
# arrives in the environment (DIRECT_*).
if [[ "${1:-}" == "--inner" ]]; then
  declare -a names=() pids=() pgids=()
  nested=""; root_tun=""; mounted=0

  start_group() { # name log cmd...
    local name="$1" log="$2"; shift 2
    setsid stdbuf -oL -eL "$@" >"${log}" 2>&1 &
    local pid="$!" pgid
    pgid="$(ps -o pgid= -p "${pid}" | tr -d '[:space:]')"
    [[ "${pgid}" == "${pid}" ]] || { echo "bad pgid for ${name}" >&2; exit 2; }
    names+=("${name}"); pids+=("${pid}"); pgids+=("${pgid}")
    started_pid="${pid}"
  }
  stop_group() { # index
    local pid="${pids[$1]}" pgid="${pgids[$1]}" sig deadline
    [[ "${pid}" =~ ^[1-9][0-9]*$ ]] || return 0
    process_running "${pid}" || return 0
    for sig in INT TERM KILL; do
      kill -s "${sig}" -- "-${pgid}" 2>/dev/null || true
      deadline=$((SECONDS + 6))
      while process_running "${pid}" && [[ "${SECONDS}" -lt "${deadline}" ]]; do sleep 0.1; done
      process_running "${pid}" || break
    done
    wait "${pid}" 2>/dev/null || true
  }
  cleanup() {
    local st="$?" wanted i
    set +e; trap - EXIT INT TERM HUP
    for wanted in ue-keepalive srsue gnb open5gs mongod; do
      for ((i=0; i<${#pids[@]}; i++)); do
        [[ "${names[i]}" == "${wanted}" ]] && { stop_group "${i}"; pids[i]=0; }
      done
    done
    [[ -n "${nested}" ]] && ip netns del "${nested}" 2>/dev/null
    [[ -n "${root_tun}" ]] && ip link del "${root_tun}" 2>/dev/null
    [[ "${mounted}" -eq 1 ]] && umount /run/netns 2>/dev/null
    exit "${st}"
  }
  trap cleanup EXIT; trap 'exit 129' HUP; trap 'exit 130' INT; trap 'exit 143' TERM
  wait_log() { # path token pid seconds
    local deadline=$((SECONDS + $4))
    while [[ "${SECONDS}" -lt "${deadline}" ]]; do
      grep -q -- "$2" "$1" 2>/dev/null && return 0
      process_running "$3" || return 1
      sleep 0.25
    done
    return 1
  }
  wait_port() { # host port seconds
    /usr/bin/python3 - "$1" "$2" "$3" <<'PY'
import socket, sys, time
host, port, seconds = sys.argv[1], int(sys.argv[2]), float(sys.argv[3])
deadline = time.monotonic() + seconds
while time.monotonic() < deadline:
    try:
        with socket.create_connection((host, port), timeout=.25):
            raise SystemExit(0)
    except OSError:
        time.sleep(.25)
raise SystemExit(2)
PY
  }

  cfg="${DIRECT_CONFIGS}"; logs="${DIRECT_LOGS}"; data="${DIRECT_DATA}"
  rrc=0; pdu=0; ping_ok=0; keepalive_replies=0; gnb_alive=0; ue_alive=0; core_alive=0

  mount --make-rprivate /
  mount --bind "${DIRECT_NETNS}" /run/netns; mounted=1
  ip link set lo up
  root_tun="ogstun"
  ip tuntap add dev "${root_tun}" mode tun
  ip addr add 10.45.0.1/24 dev "${root_tun}"
  ip addr add 10.45.1.1/24 dev "${root_tun}"
  ip link set "${root_tun}" up
  nested="ue1"
  ip netns add "${nested}"
  nsenter --net="/run/netns/${nested}" -- ip link set lo up

  start_group mongod "${logs}/mongod-console.log" "${DIRECT_MONGOD}" \
    --dbpath "${data}" --bind_ip 127.0.0.1 --port 27017 --logpath "${logs}/mongod.log"
  wait_port 127.0.0.1 27017 20 || { echo "mongod did not come up" >&2; exit 3; }
  PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="${OCUDU_NATIVE_ROOT}/src/open5gs:${PYTHONPATH:-}" \
    /usr/bin/python3 "${OCUDU_NATIVE_ROOT}/src/ocudu/docker/open5gs/add_users.py" \
    --mongodb 127.0.0.1 --mongodb_port 27017 --subscriber_data "${cfg}/subscriber.csv" \
    >"${logs}/subscriber-insert.log" 2>&1 || { echo "subscriber insert failed" >&2; exit 3; }
  start_group open5gs "${logs}/open5gs.log" "${DIRECT_5GC}" -c "${cfg}/open5gs.yaml"
  core_pid="${started_pid}"
  wait_port 127.0.0.20 7777 30 || { echo "Open5GS did not come up" >&2; exit 3; }
  # The SBI port answering does not mean the AMF listens on N2 yet. On a slow
  # host (Jetson Orin, MODE_30W) the gNB's first N2 connect came before it and
  # the CU-CP gives up at once ("Connection refused", timeout=0ms).
  wait_log "${logs}/open5gs.log" 'ngap_server()' "${core_pid}" 60 || {
    echo "AMF did not open N2" >&2; exit 3; }

  # The gNB binds tx_port 2000 and connects its rx to 2001. Nothing else is
  # started before it: no relay, no probe.
  gnb_t0="${SECONDS}"
  start_group gnb "${logs}/gnb-console.log" env CUDA_VISIBLE_DEVICES="${DIRECT_GPU}" \
    "${DIRECT_GNB}" -c "${cfg}/gnb.yaml"
  gnb_pid="${started_pid}"
  wait_log "${logs}/gnb-console.log" '==== gNB started ===' "${gnb_pid}" 120 || {
    echo "gNB did not start" >&2; exit 4; }
  echo "gnb_started_after_s=$((SECONDS - gnb_t0))" >"${logs}/inner-result.env"
  sleep 3

  # srsUE binds tx_port 2001 and connects its rx to the gNB's 2000.
  start_group srsue "${logs}/srsue.log" "${DIRECT_SRSUE}" "${cfg}/srsue.conf"
  ue_pid="${started_pid}"
  deadline=$((SECONDS + DIRECT_ATTACH_SECONDS))
  while [[ "${SECONDS}" -lt "${deadline}" ]] && process_running "${ue_pid}"; do
    grep -q 'RRC Connected' "${logs}/srsue.log" 2>/dev/null && rrc=1
    grep -q 'PDU Session Establishment successful' "${logs}/srsue.log" 2>/dev/null && pdu=1
    [[ "${rrc}" -eq 1 && "${pdu}" -eq 1 ]] && break
    sleep 0.5
  done
  # Process snapshot inside the namespace: this is the evidence that only the
  # five stack processes exist here, and no emulator.
  ps -eo pid,pgid,comm,args --no-headers | grep -v -E ' ps -eo | grep ' >"${logs}/processes.txt" || true
  ss -ltnp >"${logs}/listeners.txt" 2>&1 || true
  if [[ "${rrc}" -eq 1 && "${pdu}" -eq 1 ]]; then
    nsenter --net=/run/netns/ue1 -- ping -I tun_srsue -c 3 -W 2 10.45.1.1 \
      >"${logs}/ue-ping.log" 2>&1 && ping_ok=1
  fi
  if [[ "${ping_ok}" -eq 1 && "${DIRECT_TRAFFIC_SECONDS}" -gt 0 ]]; then
    # Traffic so the PUSCH sample is large enough for a BLER figure to mean
    # something (0.2 s is ping's unprivileged floor).
    start_group ue-keepalive "${logs}/ue-keepalive.log" \
      timeout -s INT "${DIRECT_TRAFFIC_SECONDS}s" \
      nsenter --net=/run/netns/ue1 -- ping -I tun_srsue -i 0.2 10.45.1.1
    ka_pid="${started_pid}"
    while process_running "${ka_pid}"; do
      process_running "${gnb_pid}" || break
      process_running "${ue_pid}" || break
      sleep 0.5
    done
    keepalive_replies="$(grep -c 'icmp_seq' "${logs}/ue-keepalive.log" 2>/dev/null || echo 0)"
  fi
  process_running "${gnb_pid}" && gnb_alive=1
  process_running "${ue_pid}" && ue_alive=1
  process_running "${core_pid}" && core_alive=1
  cat >>"${logs}/inner-result.env" <<EOF
rrc_connected=${rrc}
pdu_session_established=${pdu}
ping_ok=${ping_ok}
keepalive_replies=${keepalive_replies}
gnb_alive_at_end=${gnb_alive}
srsue_alive_at_end=${ue_alive}
open5gs_alive_at_end=${core_alive}
EOF
  [[ "${rrc}" -eq 1 && "${pdu}" -eq 1 && "${ping_ok}" -eq 1 && "${gnb_alive}" -eq 1 ]]
  exit $?
fi

# ---------------------------------------------------------------- outer
stage="${OCUDU_NATIVE_GNB_ACCELERATION:-all}"
case "${stage}" in
  cpu|disabled|low-phy-rx|low-phy-tx|pusch|pdsch|prach|all) ;;
  *) echo "invalid OCUDU_NATIVE_GNB_ACCELERATION: ${stage}" >&2; exit 2 ;;
esac
traffic="${OCUDU_CUDA_DIRECT_TRAFFIC_SECONDS:-60}"
[[ "${traffic}" =~ ^(0|[1-9][0-9]*)$ ]] || { echo "invalid traffic seconds" >&2; exit 2; }
attach="${OCUDU_CUDA_DIRECT_ATTACH_SECONDS:-60}"
[[ "${attach}" =~ ^[1-9][0-9]*$ ]] || { echo "invalid attach seconds" >&2; exit 2; }
gpu="${OCUDU_NATIVE_GPU_DEVICE:-0}"

# Same audit as the CUDA gate: pinned source, reviewed patch, build, binary.
# The CPU baseline is audited against the lock's cpu_baseline entry instead.
resolve_args=(--root "${OCUDU_NATIVE_ROOT}" --fields)
[[ "${stage}" == cpu ]] && resolve_args+=(--cpu-baseline)
selection="$(/usr/bin/python3 "${script_dir}/resolve-cuda-gnb.py" "${resolve_args[@]}")" || exit 1
IFS=$'\t' read -r gnb_binary gnb_source gnb_build gnb_commit <<<"${selection}"

srsue="${OCUDU_NATIVE_ROOT}/builds/srsran4g-zmq-release/srsue/src/srsue"
fivegc="${OCUDU_NATIVE_ROOT}/builds/open5gs-v2.7.6/tests/app/5gc"
mongod="${OCUDU_NATIVE_ROOT}/install/mongodb-6.0.29/bin/mongod"
for b in "${gnb_binary}" "${srsue}" "${fivegc}" "${mongod}"; do
  [[ -x "${b}" ]] || { echo "missing executable: ${b}" >&2; exit 2; }
done

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
out="${OCUDU_NATIVE_ROOT}/results/cuda-rebuild/direct-zmq-${stamp}"
cfg="${out}/configs"; logs="${out}/logs"; data="${out}/data"; netns="${out}/netns"
mkdir -p "${cfg}" "${logs}" "${data}" "${netns}"
echo "event=cuda_direct_zmq_launch gnb=${gnb_binary} commit=${gnb_commit:0:7} acceleration=${stage} traffic_seconds=${traffic} out=${out}"

# The audited render, acceleration block included (none for the CPU baseline:
# with the variable empty the renderer is byte-identical to the plain render).
render_stage="${stage}"; [[ "${stage}" == cpu ]] && render_stage=""
OCUDU_NATIVE_GNB_ACCELERATION="${render_stage}" /usr/bin/python3 "${script_dir}/render-cuda-1x1-configs.py" \
  --repo-root "${repo_root}" --native-root "${OCUDU_NATIVE_ROOT}" \
  --output-dir "${cfg}" --log-dir "${logs}" >"${logs}/render.log" 2>&1 || { cat "${logs}/render.log" >&2; exit 2; }
# Scheduler metrics (UL MCS, OLLA offset) are off in the audited render; a
# diagnostic run can switch them on. Printed so the run is never mistaken for a plain one.
if [[ -n "${OCUDU_DIRECT_SCHED_METRICS_MS:-}" ]]; then
  [[ "${OCUDU_DIRECT_SCHED_METRICS_MS}" =~ ^[1-9][0-9]*$ ]] || { echo "invalid OCUDU_DIRECT_SCHED_METRICS_MS" >&2; exit 2; }
  grep -q '^metrics:' "${cfg}/gnb.yaml" && { echo "rendered gnb.yaml already has metrics:" >&2; exit 2; }
  printf '\nmetrics:\n  enable_log: true\n  enable_verbose: true\n  periodicity:\n    du_report_period: %s\n' \
    "${OCUDU_DIRECT_SCHED_METRICS_MS}" >>"${cfg}/gnb.yaml"
  echo "event=diagnostic_sched_metrics du_report_period_ms=${OCUDU_DIRECT_SCHED_METRICS_MS}"
fi
# The emulator's topology has no role here.
rm -f "${cfg}/topology.yaml"
# UE ports: from the emulator's (2101/2100) to the gNB's own (2001/2000).
/usr/bin/python3 - "${cfg}/srsue.conf" <<'PY' || exit 2
import sys
path = sys.argv[1]
text = open(path).read()
old = "device_args = tx_port=tcp://127.0.0.1:2101,rx_port=tcp://127.0.0.1:2100,base_srate=23.04e6\n"
new = "device_args = tx_port=tcp://127.0.0.1:2001,rx_port=tcp://127.0.0.1:2000,base_srate=23.04e6\n"
if text.count(old) != 1:
    raise SystemExit("srsue.conf device_args not found exactly once")
open(path, "w").write(text.replace(old, new))
PY
grep -n 'device_args' "${cfg}/gnb.yaml" "${cfg}/srsue.conf" | tee "${logs}/wiring.txt"
"${gnb_binary}" -c "${cfg}/gnb.yaml" --dryrun >"${logs}/gnb-dryrun.log" 2>&1 || { cat "${logs}/gnb-dryrun.log" >&2; exit 2; }
"${gnb_binary}" --version >"${out}/gnb-version.txt" 2>&1
nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader >"${out}/gpu.txt" 2>&1 || true

parent_netns="$(readlink /proc/self/ns/net)"
DIRECT_CONFIGS="${cfg}" DIRECT_LOGS="${logs}" DIRECT_DATA="${data}" DIRECT_NETNS="${netns}" \
DIRECT_GNB="${gnb_binary}" DIRECT_SRSUE="${srsue}" DIRECT_5GC="${fivegc}" DIRECT_MONGOD="${mongod}" \
DIRECT_GPU="${gpu}" DIRECT_TRAFFIC_SECONDS="${traffic}" DIRECT_ATTACH_SECONDS="${attach}" \
unshare --user --map-root-user --net --mount --fork --kill-child=TERM --propagation private \
  "${BASH_SOURCE[0]}" --inner >"${logs}/inner-console.log" 2>&1
inner_status="$?"
cat "${logs}/inner-console.log"

# What ran on the device, from the gNB's own log; and the PHY KPIs.
if [[ "${stage}" == cpu ]]; then
  echo '{"verdict": "not_applicable"}' >"${out}/backends.json"; backends_status=0
else
  /usr/bin/python3 "${script_dir}/verify-stage-backends.py" "${stage}" "${logs}/gnb-internal.log" --json \
    >"${out}/backends.json" 2>&1; backends_status="$?"
fi
/usr/bin/python3 "${script_dir}/summarize-phy-kpis.py" --json "${logs}/gnb-internal.log" \
  >"${out}/kpis.json" 2>/dev/null || echo '{}' >"${out}/kpis.json"

# The reviewed patch named by the lock in use (the 5090 lock unless overridden).
lock_patch="$(/usr/bin/python3 -c 'import json,os,sys; print(json.load(open(os.environ.get("OCUDU_CUDA_WORKSPACE_LOCK", sys.argv[1])))["source"]["patch"]["path"])' \
  "${repo_root}/integrations/ocudu/cuda-workspace.lock.json")"
/usr/bin/python3 - "${out}" "${stage}" "${inner_status}" "${backends_status}" "${gnb_binary}" \
  "${gnb_commit}" "${repo_root}/${lock_patch}" \
  "${parent_netns}" "${traffic}" <<'PY'
import hashlib, json, pathlib, sys
out, stage, inner_status, backends_status, gnb, commit, patch, parent_netns, traffic = sys.argv[1:]
out = pathlib.Path(out)
def sha(p): return hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest()
res = {}
for line in (out / 'logs/inner-result.env').read_text().splitlines() if (out / 'logs/inner-result.env').exists() else []:
    k, _, v = line.partition('='); res[k] = int(v) if v.lstrip('-').isdigit() else v
backends = json.loads((out / 'backends.json').read_text() or '{}')
kpis = json.loads((out / 'kpis.json').read_text() or '{}')
procs = (out / 'logs/processes.txt').read_text().splitlines() if (out / 'logs/processes.txt').exists() else []
comms = sorted({l.split()[2] for l in procs if len(l.split()) > 2})
passed = (int(inner_status) == 0 and res.get('rrc_connected') == 1 and res.get('pdu_session_established') == 1
          and res.get('ping_ok') == 1)
summary = {
    'schema': 'ocudu-cuda-direct-zmq/v1',
    'topology': 'gNB tx_port=tcp://127.0.0.1:2000 <-> srsUE rx_port=tcp://127.0.0.1:2000; '
                'srsUE tx_port=tcp://127.0.0.1:2001 <-> gNB rx_port=tcp://127.0.0.1:2001',
    'channel_emulator_in_path': False,
    'processes_in_namespace': comms,
    'acceleration_stage': stage,
    'status': 'passed' if passed else 'failed',
    'inner_exit': int(inner_status),
    'attach': {k: res.get(k) for k in ('rrc_connected', 'pdu_session_established', 'ping_ok',
                                       'keepalive_replies', 'gnb_started_after_s',
                                       'gnb_alive_at_end', 'srsue_alive_at_end', 'open5gs_alive_at_end')},
    'traffic_seconds': int(traffic),
    'backends_verdict': backends.get('verdict'),
    'backends': backends,
    'kpis': kpis,
    'gnb': {'binary': gnb, 'sha256': sha(gnb), 'source_commit': commit,
            'patch_sha256': None if stage == 'cpu' else sha(patch), 'version': (out / 'gnb-version.txt').read_text().strip()},
    'gpu': (out / 'gpu.txt').read_text().strip(),
    'config_sha256': {p.name: sha(p) for p in sorted((out / 'configs').iterdir())},
}
(out / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
a = summary['attach']
print(f"result={summary['status']} stage={stage} rrc={a['rrc_connected']} pdu={a['pdu_session_established']} "
      f"ping={a['ping_ok']} keepalive_replies={a['keepalive_replies']} gnb_start_s={a['gnb_started_after_s']} "
      f"backends={summary['backends_verdict']} processes={','.join(comms)}")
pusch = (kpis.get('channels') or {}).get('PUSCH') or {}
print("pusch=" + json.dumps({k: pusch.get(k) for k in ('transmissions', 'crc_ko', 'bler_pct',
                                                       'bler_resolution_pp', 'sinr')}))
print(f"summary={out / 'summary.json'}")
PY
exit "${inner_status}"
