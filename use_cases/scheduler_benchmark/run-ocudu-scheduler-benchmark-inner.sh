#!/usr/bin/env bash
# Inner half of the native scheduler-benchmark gate. Runs INSIDE the user +
# network + mount namespace created by run-ocudu-scheduler-benchmark.sh;
# never invoke it directly.
#
# Process supervision, namespace layout, core start-up and bounded shutdown
# are the robot-fight inner's (run-ocudu-robot-fight-inner.sh). What differs:
# two UEs per cell, ONE Sionna bridge fanned out to both brokers on a held
# grid timeline, a startup check that each gNB loaded the intended scheduler
# policy, gNB metrics recorders, and the seeded traffic whose start is the
# same instant as the bridge's grid point 0.
set -uo pipefail

repo_root=""
native_root=""
config_dir=""
log_dir=""
netns_dir=""
timestamp=""
physical_gpu="0"
channel_build=""
parent_netns=""
parent_mntns=""
outer_uid=""
gnb=""
gnb_start_timeout="20"
measure_seconds="120"
drain_seconds="6"
attach_timeout="180"
sched_a=""
sched_b=""
control_endpoint_a=""
telemetry_endpoint_a=""
control_endpoint_b=""
telemetry_endpoint_b=""
sionna_python=""
sionna_update_hz="10"
sionna_ready_seconds="240"
gnb_metrics="1"
cpus_gnb_a=""
cpus_gnb_b=""
cpus_broker_a=""
cpus_broker_b=""
cpus_ues=""
cpus_aux=""

usage_error()
{
  printf 'error: %s\n' "$1" >&2
  exit 2
}

while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --repo-root) repo_root="${2:-}"; shift 2 ;;
    --native-root) native_root="${2:-}"; shift 2 ;;
    --config-dir) config_dir="${2:-}"; shift 2 ;;
    --log-dir) log_dir="${2:-}"; shift 2 ;;
    --netns-dir) netns_dir="${2:-}"; shift 2 ;;
    --timestamp) timestamp="${2:-}"; shift 2 ;;
    --physical-gpu) physical_gpu="${2:-}"; shift 2 ;;
    --channel-build) channel_build="${2:-}"; shift 2 ;;
    --parent-netns) parent_netns="${2:-}"; shift 2 ;;
    --parent-mntns) parent_mntns="${2:-}"; shift 2 ;;
    --outer-uid) outer_uid="${2:-}"; shift 2 ;;
    --gnb-binary) gnb="${2:-}"; shift 2 ;;
    --gnb-start-timeout) gnb_start_timeout="${2:-}"; shift 2 ;;
    --measure-seconds) measure_seconds="${2:-}"; shift 2 ;;
    --drain-seconds) drain_seconds="${2:-}"; shift 2 ;;
    --attach-timeout) attach_timeout="${2:-}"; shift 2 ;;
    --sched-a) sched_a="${2:-}"; shift 2 ;;
    --sched-b) sched_b="${2:-}"; shift 2 ;;
    --control-endpoint-a) control_endpoint_a="${2:-}"; shift 2 ;;
    --telemetry-endpoint-a) telemetry_endpoint_a="${2:-}"; shift 2 ;;
    --control-endpoint-b) control_endpoint_b="${2:-}"; shift 2 ;;
    --telemetry-endpoint-b) telemetry_endpoint_b="${2:-}"; shift 2 ;;
    --sionna-python) sionna_python="${2:-}"; shift 2 ;;
    --sionna-update-hz) sionna_update_hz="${2:-}"; shift 2 ;;
    --gnb-metrics) gnb_metrics="${2:-}"; shift 2 ;;
    --cpus-gnb-a) cpus_gnb_a="${2:-}"; shift 2 ;;
    --cpus-gnb-b) cpus_gnb_b="${2:-}"; shift 2 ;;
    --cpus-broker-a) cpus_broker_a="${2:-}"; shift 2 ;;
    --cpus-broker-b) cpus_broker_b="${2:-}"; shift 2 ;;
    --cpus-ues) cpus_ues="${2:-}"; shift 2 ;;
    --cpus-aux) cpus_aux="${2:-}"; shift 2 ;;
    *) usage_error "unexpected argument: $1" ;;
  esac
done

for value in "${repo_root}" "${native_root}" "${config_dir}" "${log_dir}" "${netns_dir}" "${timestamp}" \
             "${parent_netns}" "${parent_mntns}" "${outer_uid}" "${gnb}" "${channel_build}" \
             "${control_endpoint_a}" "${telemetry_endpoint_a}" "${control_endpoint_b}" "${telemetry_endpoint_b}" \
             "${sionna_python}" "${sched_a}" "${sched_b}"; do
  [[ -n "${value}" ]] || usage_error "missing required argument"
done
for endpoint in "${control_endpoint_a}" "${telemetry_endpoint_a}" "${control_endpoint_b}" "${telemetry_endpoint_b}"; do
  [[ "${endpoint}" == ipc://* ]] || usage_error "control and telemetry endpoints must be ipc://"
done
[[ -x "${sionna_python}" ]] || usage_error "missing Sionna Python: ${sionna_python}"
[[ "$(readlink /proc/self/ns/net)" != "${parent_netns}" ]] || usage_error "network namespace was not isolated"
[[ "$(readlink /proc/self/ns/mnt)" != "${parent_mntns}" ]] || usage_error "mount namespace was not isolated"
awk -v uid="${outer_uid}" '$1 == 0 && $2 == uid && $3 == 1 { found = 1 } END { exit !found }' \
  /proc/self/uid_map || usage_error "user namespace does not contain the exact root mapping"
for command_name in ip mount umount nsenter ps taskset; do
  command -v "${command_name}" >/dev/null 2>&1 || usage_error "missing command: ${command_name}"
done
[[ -x /usr/bin/python3 ]] || usage_error "missing /usr/bin/python3"

# Must agree with use_cases/scheduler_benchmark/definitions.py CELLS and the
# UE records of render-multi-ue-configs.py.
cells=(a b)
gnb_ids=(gnb0 gnb1)
gnb_pcis=(1 2)
scheds=("${sched_a}" "${sched_b}")
ue_ids=(ue0 ue1 ue2 ue3)
ue_cells=(a a b b)
ue_netns=(ue1 ue2 ue3 ue4)
ue_ipv4=(10.45.1.2 10.45.1.3 10.45.1.4 10.45.1.5)
ue_gateway="10.45.1.1"
control_endpoints=("${control_endpoint_a}" "${control_endpoint_b}")
telemetry_endpoints=("${telemetry_endpoint_a}" "${telemetry_endpoint_b}")
gnb_cpu_sets=("${cpus_gnb_a}" "${cpus_gnb_b}")
broker_cpu_sets=("${cpus_broker_a}" "${cpus_broker_b}")
IFS='|' read -r -a ue_cpu_sets <<<"${cpus_ues}|"
metrics_ports=(8001 8002)
bench_dir="${repo_root}/use_cases/scheduler_benchmark"
run_dir="${netns_dir%/netns}"
hold_file="${run_dir}/measure.start"
traffic_stop_file="${run_dir}/traffic.stop"

mount_active=0
root_tun=""
declare -a created_netns=()
declare -a process_names=()
declare -a process_pids=()
declare -a process_pgids=()
started_pid=""

process_running()
{
  local state
  state="$(ps -o stat= -p "$1" 2>/dev/null | awk '{print $1}')"
  [[ -n "${state}" && "${state:0:1}" != "Z" ]]
}

start_group()
{
  local name="$1" output="$2"; shift 2
  local gate_lock_fd="${OCUDU_NATIVE_GATE_LOCK_FD:-}"
  if [[ "${gate_lock_fd}" =~ ^[0-9]+$ && "${gate_lock_fd}" -gt 2 ]]; then
    setsid stdbuf -oL -eL "$@" >"${output}" 2>&1 {gate_lock_fd}>&- &
  else
    setsid stdbuf -oL -eL "$@" >"${output}" 2>&1 &
  fi
  started_pid="$!"
  process_names+=("${name}")
  process_pids+=("${started_pid}")
  process_pgids+=("${started_pid}")
}

group_alive()
{
  ps -eo pgid=,sid=,stat= | awk -v g="$1" '($1 == g || $2 == g) && $3 !~ /^Z/ { found = 1 } END { exit !found }'
}

signal_group()
{
  local signal="$1" pgid="$2" member
  kill -s "${signal}" -- "-${pgid}" >/dev/null 2>&1 || true
  for member in $(ps -eo pid=,sid= | awk -v g="${pgid}" '$2 == g { print $1 }'); do
    kill -s "${signal}" "${member}" >/dev/null 2>&1 || true
  done
}

stop_group()
{
  local index="$1"
  local pid="${process_pids[index]}"
  local pgid="${process_pgids[index]}"
  local signal deadline
  [[ "${pid}" =~ ^[1-9][0-9]*$ && "${pgid}" =~ ^[1-9][0-9]*$ && "${pgid}" -gt 1 ]] || return 0
  if group_alive "${pgid}"; then
    for signal in INT TERM KILL; do
      signal_group "${signal}" "${pgid}"
      deadline=$((SECONDS + 4))
      while group_alive "${pgid}" && [[ "${SECONDS}" -lt "${deadline}" ]]; do
        sleep 0.1
      done
      group_alive "${pgid}" || break
    done
    if group_alive "${pgid}"; then
      printf 'error: %s group %s remains alive after bounded KILL\n' "${process_names[index]}" "${pgid}" >&2
      return 124
    fi
  fi
  wait "${pid}" >/dev/null 2>&1 || true
  return 0
}

stop_named()
{
  local wanted="$1" index failed=0
  for ((index=0; index<${#process_pids[@]}; index++)); do
    if [[ "${process_names[index]}" == "${wanted}"* && "${process_pids[index]}" != "0" ]]; then
      if stop_group "${index}"; then process_pids[index]="0"; else failed=1; fi
    fi
  done
  return "${failed}"
}

cleanup()
{
  local original_status="$?"
  set +e
  trap - EXIT INT TERM HUP
  local index wanted cleanup_failed=0
  # Traffic and observers first, then the brokers while both radios still
  # request, then the radios, then the bridge and the core.
  for wanted in traffic gnb-metrics-recorder broker srsue gnb-metrics-relay gnb sionna open5gs mongod; do
    stop_named "${wanted}" || cleanup_failed=1
  done
  for ((index=${#process_pids[@]}-1; index>=0; index--)); do
    stop_group "${index}" || cleanup_failed=1
  done
  for name in "${created_netns[@]}"; do
    ip netns del "${name}" >/dev/null 2>&1 || true
  done
  [[ -n "${root_tun}" ]] && { ip link del "${root_tun}" >/dev/null 2>&1 || true; }
  [[ "${mount_active}" -eq 1 ]] && { umount /run/netns >/dev/null 2>&1 || true; }
  if [[ "${cleanup_failed}" -ne 0 && "${original_status}" -eq 0 ]]; then
    original_status=124
  fi
  exit "${original_status}"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

wait_log()
{
  local file="$1" pattern="$2" pid="$3" timeout="$4"
  local deadline=$((SECONDS + timeout))
  while [[ "${SECONDS}" -lt "${deadline}" ]]; do
    grep -q -- "${pattern}" "${file}" 2>/dev/null && return 0
    process_running "${pid}" || return 1
    sleep 0.2
  done
  return 1
}

# pin <cpu list> -> the taskset words, or nothing when the list is empty.
pin()
{
  [[ -n "$1" ]] && printf '%s\n' taskset -c "$1"
  return 0
}

srsue="${native_root}/builds/srsran4g-zmq-local/srsue/src/srsue"
srsue_manifest="${native_root}/builds/srsran4g-zmq-local/BUILD-MANIFEST.txt"
[[ -f "${srsue_manifest}" ]] || usage_error "local srsUE is not built: run scripts/native/build-srsue-local.sh"
while read -r srsue_patch_path srsue_patch_sha; do
  grep -qx "patch=${srsue_patch_path##*/} sha256=${srsue_patch_sha}" "${srsue_manifest}" || \
    usage_error "local srsUE was built from other patches than srsue-local-patches.lock.json; rebuild it"
done < <(/usr/bin/python3 -c 'import json,sys
for p in json.load(open(sys.argv[1]))["patches"]: print(p["path"], p["sha256"])' "${repo_root}/scripts/native/srsue-local-patches.lock.json")
fivegc="${native_root}/builds/open5gs-v2.7.6/tests/app/5gc"
mongod="${native_root}/install/mongodb-6.0.29/bin/mongod"
broker="${channel_build}/ocudu-gpu-channel"
add_users="${native_root}/src/ocudu/docker/open5gs/add_users.py"
subscriber_verify="${repo_root}/scripts/native/verify-open5gs-subscribers-multi.py"
data_dir="${native_root}/data/ocudu-scheduler-benchmark-native/${timestamp}"
for binary in "${gnb}" "${srsue}" "${fivegc}" "${mongod}" "${broker}"; do
  [[ -x "${binary}" ]] || usage_error "missing executable: ${binary}"
done
[[ -d "${data_dir}" ]] || usage_error "missing data dir: ${data_dir}"
mapfile -t aux_pin < <(pin "${cpus_aux}")

# --- namespace layout -------------------------------------------------------
mount --make-rprivate /
mount --bind "${netns_dir}" /run/netns
mount_active=1
ip link set lo up
root_tun="ogstun"
ip tuntap add dev "${root_tun}" mode tun
ip addr add 10.45.0.1/24 dev "${root_tun}"
ip addr add 10.45.1.1/24 dev "${root_tun}"
ip link set "${root_tun}" up
for name in "${ue_netns[@]}"; do
  ip netns add "${name}"
  created_netns+=("${name}")
  nsenter --net="/run/netns/${name}" -- ip link set lo up
done

# --- core -------------------------------------------------------------------
start_group mongod "${log_dir}/mongod-console.log" "${aux_pin[@]}" "${mongod}" \
  --dbpath "${data_dir}" --bind_ip 127.0.0.1 --port 27017 --logpath "${log_dir}/mongod.log"
/usr/bin/python3 - <<'PY' || usage_error "mongod did not accept connections"
import socket, time
deadline = time.monotonic() + 20
while time.monotonic() < deadline:
    try:
        with socket.create_connection(("127.0.0.1", 27017), timeout=.25):
            raise SystemExit(0)
    except OSError:
        time.sleep(.25)
raise SystemExit(2)
PY
PYTHONDONTWRITEBYTECODE=1 \
PYTHONPATH="${native_root}/src/open5gs:${PYTHONPATH:-}" /usr/bin/python3 \
  "${add_users}" \
  --mongodb 127.0.0.1 --mongodb_port 27017 --subscriber_data "${config_dir}/subscriber.csv" \
  >"${log_dir}/subscriber-insert.log" 2>&1 || usage_error "subscriber insert failed"
/usr/bin/python3 "${subscriber_verify}" --subscriber-csv "${config_dir}/subscriber.csv" \
  >"${log_dir}/subscriber-verify.log" 2>&1 || usage_error "subscriber verification failed"
start_group open5gs "${log_dir}/open5gs.log" "${aux_pin[@]}" "${fivegc}" -c "${config_dir}/open5gs.yaml"
/usr/bin/python3 - <<'PY' || usage_error "Open5GS AMF did not come up"
import socket, time
deadline = time.monotonic() + 30
while time.monotonic() < deadline:
    try:
        with socket.create_connection(("127.0.0.20", 7777), timeout=.25):
            raise SystemExit(0)
    except OSError:
        time.sleep(.25)
raise SystemExit(2)
PY

# --- brokers (identical scheduling: plain, own context, mirror-image cores) --
broker_duration=$((attach_timeout + measure_seconds + drain_seconds + 120))
declare -a broker_pids=()
# Opt-in diagnostic: OCUDU_NATIVE_SB_WIRE_CAPTURE="<samples>:<skip>" records
# each broker port's input and output IQ under wire-<cell>/ (per port).
wire_capture="${OCUDU_NATIVE_SB_WIRE_CAPTURE:-}"
[[ -z "${wire_capture}" || "${wire_capture}" =~ ^[1-9][0-9]*:[0-9]+$ ]] || \
  usage_error "OCUDU_NATIVE_SB_WIRE_CAPTURE must be <samples>:<skip>"
for index in "${!cells[@]}"; do
  cell="${cells[index]}"
  mapfile -t broker_pin < <(pin "${broker_cpu_sets[index]}")
  declare -a capture_args=()
  if [[ -n "${wire_capture}" ]]; then
    mkdir -p "${log_dir}/wire-${cell}"
    capture_args=(--wire-capture-dir "${log_dir}/wire-${cell}"
                  --wire-capture-samples "${wire_capture%%:*}" --wire-capture-skip "${wire_capture##*:}")
  fi
  start_group "broker-${cell}" "${log_dir}/broker-${cell}.log" \
    env -u CUDA_MPS_PIPE_DIRECTORY "CUDA_VISIBLE_DEVICES=${physical_gpu}" "${broker_pin[@]}" \
    "${broker}" --config "${config_dir}/topology-${cell}.yaml" --duration "${broker_duration}s" \
    --control-endpoint "${control_endpoints[index]}" \
    --telemetry-endpoint "${telemetry_endpoints[index]}" --telemetry-rate-hz 500 "${capture_args[@]}"
  broker_pids+=("${started_pid}")
done
for index in "${!cells[@]}"; do
  cell="${cells[index]}"
  wait_log "${log_dir}/broker-${cell}.log" 'event=control_start ' "${broker_pids[index]}" 15 || \
    usage_error "broker ${cell} control server did not become ready"
done

# --- ONE Sionna bridge, both brokers, grid timeline held at scenario time 0 ---
fanout_spec="${control_endpoint_b}=$(/usr/bin/python3 -c 'import json,sys
sys.path.insert(0, sys.argv[1]); import definitions as d
print(",".join(f"{k}:{v}" for k, v in d.FANOUT_RENAME.items()))' "${bench_dir}")"
start_group sionna "${log_dir}/sionna-bridge.log" "${aux_pin[@]}" \
  env -u CUDA_MPS_PIPE_DIRECTORY "CUDA_VISIBLE_DEVICES=${physical_gpu}" \
  "${sionna_python}" "${repo_root}/scripts/sionna_rt/run_bridge.py" \
  --scenario-config "${config_dir}/scenario.json" \
  --control-endpoint "${control_endpoint_a}" --fanout-control-endpoint "${fanout_spec}" \
  --timeline grid --hold-until-file "${hold_file}" --profile-digest \
  --duration 0 --update-hz "${sionna_update_hz}" --status-jsonl "${log_dir}/sionna-status.jsonl"
sionna_pid="${started_pid}"
wait_log "${log_dir}/sionna-bridge.log" '"event":"sionna_rt_update"' "${sionna_pid}" "${sionna_ready_seconds}" || \
  usage_error "Sionna bridge did not publish its scenario-time-0 profile"

# --- gNBs, each with its policy, and the proof that it loaded it -----------------
declare -a gnb_pids=()
for index in "${!gnb_ids[@]}"; do
  id="${gnb_ids[index]}"
  mapfile -t gnb_pin < <(pin "${gnb_cpu_sets[index]}")
  start_group "gnb-${id}" "${log_dir}/${id}-console.log" env "CUDA_VISIBLE_DEVICES=${physical_gpu}" \
    "${gnb_pin[@]}" "${gnb}" -c "${config_dir}/${id}.yaml"
  gnb_pids+=("${started_pid}")
done
for index in "${!gnb_ids[@]}"; do
  wait_log "${log_dir}/${gnb_ids[index]}-console.log" '==== gNB started ===' "${gnb_pids[index]}" "${gnb_start_timeout}" \
    || usage_error "${gnb_ids[index]} did not start"
done
/usr/bin/python3 - "${log_dir}" "${sched_a}" "${sched_b}" "${bench_dir}" >"${log_dir}/policy-check.json" <<'PY' \
  || { cat "${log_dir}/policy-check.json" >&2; usage_error "a gNB did not load the requested scheduler policy"; }
import json, pathlib, re, sys, time
log_dir, sched_a, sched_b, bench_dir = sys.argv[1:]
sys.path.insert(0, bench_dir)
import definitions as d
result, ok = {}, True
for gnb, sched in (("gnb0", sched_a), ("gnb1", sched_b)):
    # The gNB logs asynchronously: give the start-up dump a few seconds to land.
    deadline = time.monotonic() + 15
    while True:
        text = (pathlib.Path(log_dir) / f"{gnb}-internal.log").read_text(errors="replace")
        start = text.find("gNB input configuration (only non-default values)")
        if (start >= 0 and "\n  scheduler:" in text[start:start + 20000]) or time.monotonic() > deadline:
            break
        time.sleep(0.5)
    dump = text[start:start + 20000] if start >= 0 else ""
    block = re.search(r"\n( *)policy:\n((?:\1  .*\n)+)", dump)
    body = block.group(2) if block else ""
    found = [name for name, info in d.SCHEDULERS.items() if info["config_dump_token"] in body]
    good = found == [sched]
    ok &= good
    result[gnb] = {"requested": sched, "found_in_config_dump": found, "ok": good, "policy_block": body}
print(json.dumps({"ok": ok, "gnbs": result}, indent=2))
raise SystemExit(0 if ok else 1)
PY
printf 'event=scheduler_policy_verified gnb0=%s gnb1=%s\n' "${sched_a}" "${sched_b}"

if [[ "${gnb_metrics}" == "1" ]]; then
  for index in "${!gnb_ids[@]}"; do
    socket_path="${run_dir}/gnb-metrics-${cells[index]}.sock"
    start_group "gnb-metrics-relay-${gnb_ids[index]}" "${log_dir}/gnb-metrics-relay-${gnb_ids[index]}.log" \
      "${aux_pin[@]}" /usr/bin/python3 "${repo_root}/scripts/native/gnb-metrics-relay.py" \
      --socket "${socket_path}" --port "${metrics_ports[index]}"
    start_group "gnb-metrics-recorder-${cells[index]}" "${log_dir}/gnb-metrics-recorder-${cells[index]}.log" \
      "${aux_pin[@]}" /usr/bin/python3 "${bench_dir}/gnb_metrics_recorder.py" \
      --endpoint "ws+unix://${socket_path}" --out "${log_dir}/gnb-metrics-${cells[index]}.jsonl"
  done
fi
sleep 3

# --- UEs --------------------------------------------------------------------
declare -a srsue_pids=()
for index in "${!ue_ids[@]}"; do
  id="${ue_ids[index]}"
  mapfile -t ue_pin < <(pin "${ue_cpu_sets[index]:-}")
  date -u +%s%3N >"${log_dir}/srsue-${id}.start_unix_ms"
  start_group "srsue-${id}" "${log_dir}/srsue-${id}.log" env "SRSUE_PRACH_PREAMBLE_INDEX=$((index * 8))" \
    "${ue_pin[@]}" "${srsue}" \
    --general.metrics_csv_enable 1 --general.metrics_period_secs 1 \
    --general.metrics_csv_filename "${log_dir}/srsue-metrics-${id}.csv" \
    "${config_dir}/srsue-${id}.conf"
  srsue_pids+=("${started_pid}")
done

# --- attach (scenario time 0 is held for as long as this takes) --------------
declare -a rrc=() pdu=() ping_ok=()
for _ in "${ue_ids[@]}"; do rrc+=(0); pdu+=(0); ping_ok+=(0); done
all_done=0
deadline=$((SECONDS + attach_timeout))
while [[ "${SECONDS}" -lt "${deadline}" ]]; do
  all_done=1
  for index in "${!ue_ids[@]}"; do
    log="${log_dir}/srsue-${ue_ids[index]}.log"
    grep -q 'RRC Connected' "${log}" 2>/dev/null && rrc[index]=1
    grep -q 'PDU Session Establishment successful' "${log}" 2>/dev/null && pdu[index]=1
    if [[ "${rrc[index]}" -eq 1 && "${pdu[index]}" -eq 1 && "${ping_ok[index]}" -eq 0 ]]; then
      nsenter --net="/run/netns/${ue_netns[index]}" -- \
        ping -I tun_srsue -c 3 -W 2 "${ue_gateway}" >"${log_dir}/ue-ping-${ue_ids[index]}.log" 2>&1 && ping_ok[index]=1
    fi
    [[ "${ping_ok[index]}" -eq 1 ]] || all_done=0
  done
  [[ "${all_done}" -eq 1 ]] && break
  sleep 0.5
done
attach_ok="${all_done}"
printf 'event=attach rrc=%s pdu=%s ping=%s ok=%s t=%s\n' "${rrc[*]}" "${pdu[*]}" "${ping_ok[*]}" "${attach_ok}" "${SECONDS}"

measure_status="skipped"
if [[ "${attach_ok}" -eq 1 ]]; then
  # --- traffic + grid timeline, started at one instant ----------------------
  start_group traffic-ul-recv "${log_dir}/traffic-ul-recv.log" "${aux_pin[@]}" \
    /usr/bin/python3 "${bench_dir}/traffic.py" ul-recv --benchmark-json "${config_dir}/benchmark.json" \
    --log "${log_dir}/rx-ul.bin" --until-file "${traffic_stop_file}"
  for index in "${!ue_ids[@]}"; do
    start_group "traffic-dl-recv-${ue_ids[index]}" "${log_dir}/traffic-dl-recv-${ue_ids[index]}.log" "${aux_pin[@]}" \
      nsenter --net="/run/netns/${ue_netns[index]}" -- \
      /usr/bin/python3 "${bench_dir}/traffic.py" dl-recv --benchmark-json "${config_dir}/benchmark.json" \
      --ue "${ue_ids[index]}" --log "${log_dir}/rx-dl-${ue_ids[index]}.bin" --until-file "${traffic_stop_file}"
  done
  sleep 1
  start_at_ms=$(( $(date -u +%s%3N) + 3000 ))
  printf '{"start_at_unix_ms": %s, "duration_s": %s}\n' "${start_at_ms}" "${measure_seconds}" \
    >"${log_dir}/traffic-start.json"
  start_group traffic-dl-send "${log_dir}/traffic-dl-send.log" "${aux_pin[@]}" \
    /usr/bin/python3 "${bench_dir}/traffic.py" dl-send --benchmark-json "${config_dir}/benchmark.json" \
    --log "${log_dir}/tx-dl.csv" --start-at-unix-ms "${start_at_ms}" --duration-s "${measure_seconds}"
  for index in "${!ue_ids[@]}"; do
    start_group "traffic-ul-send-${ue_ids[index]}" "${log_dir}/traffic-ul-send-${ue_ids[index]}.log" "${aux_pin[@]}" \
      nsenter --net="/run/netns/${ue_netns[index]}" -- \
      /usr/bin/python3 "${bench_dir}/traffic.py" ul-send --benchmark-json "${config_dir}/benchmark.json" \
      --ue "${ue_ids[index]}" --log "${log_dir}/tx-ul-${ue_ids[index]}.csv" \
      --start-at-unix-ms "${start_at_ms}" --duration-s "${measure_seconds}"
  done
  # Release the bridge's grid point 0 at the traffic's offset 0.
  /usr/bin/python3 -c 'import sys, time
target = int(sys.argv[1]) / 1000.0
while time.time() < target:
    time.sleep(min(0.005, max(0.0, target - time.time())))' "${start_at_ms}"
  : >"${hold_file}"
  printf 'event=measure_start unix_ms=%s seconds=%s\n' "${start_at_ms}" "${measure_seconds}"
  measure_deadline=$((SECONDS + measure_seconds + 5))
  next_load_sample=0
  while [[ "${SECONDS}" -lt "${measure_deadline}" ]]; do
    # The host is shared: record what else was running, so a slow run can be
    # told apart from a slow scheduler (the two cells always share the load).
    if [[ "${SECONDS}" -ge "${next_load_sample}" ]]; then
      next_load_sample=$((SECONDS + 5))
      { printf '{"unix_ms": %s, "loadavg": "%s", "top": [' "$(date -u +%s%3N)" "$(cut -d' ' -f1-3 /proc/loadavg)"
        ps -eo pcpu=,comm= --sort=-pcpu | head -8 | awk '{printf "%s{\"cpu\": %s, \"comm\": \"%s\"}", (NR>1?", ":""), $1, $2}'
        printf ']}\n'; } >>"${log_dir}/host-load.jsonl"
    fi
    sleep 1
    process_running "${sionna_pid}" || { printf 'error: Sionna bridge exited during the measurement\n' >&2; break; }
  done
  sleep "${drain_seconds}"
  : >"${traffic_stop_file}"
  sleep 1
  measure_status="completed"
fi

# --- stop, then summarize -----------------------------------------------------
stop_named traffic || true
stop_named gnb-metrics-recorder || true
gnb_alive=1
for pid in "${gnb_pids[@]}"; do process_running "${pid}" || gnb_alive=0; done
srsue_alive=1
for pid in "${srsue_pids[@]}"; do process_running "${pid}" || srsue_alive=0; done
stop_named broker || true

/usr/bin/python3 - "${log_dir}" "${native_root}/results/reports/ocudu-scheduler-benchmark/${timestamp}/gate-summary.json" \
  "${timestamp}" "${rrc[*]}" "${pdu[*]}" "${ping_ok[*]}" "${ue_ids[*]}" "${ue_cells[*]}" "${gnb_alive}" \
  "${srsue_alive}" "${measure_status}" "${sched_a}" "${sched_b}" <<'PY'
import json, pathlib, re, sys
(log_dir, out_path, timestamp, rrc, pdu, ping_ok, ue_ids, ue_cells, gnb_alive, srsue_alive, measure_status,
 sched_a, sched_b) = sys.argv[1:]
log_dir = pathlib.Path(log_dir)
ids = ue_ids.split()
cells = ue_cells.split()
expected_pci = {"a": 1, "b": 2}


def camped_pci(ue):
    found = []
    for name in (f"srsue-{ue}.log", f"srsue-{ue}-internal.log"):
        path = log_dir / name
        if path.exists():
            found += re.findall(r"PCI=(\d+)", path.read_text(encoding="utf-8", errors="replace"))
    return int(found[-1]) if found else -1


def broker(cell):
    text = (log_dir / f"broker-{cell}.log").read_text(encoding="utf-8", errors="replace") if \
        (log_dir / f"broker-{cell}.log").exists() else ""
    stop = [line for line in text.splitlines() if line.startswith("event=stop ")]
    counters = {k: int(v) for k, v in re.findall(r"(\w+)=(\d+)", stop[-1])} if stop else {}
    return {k: counters.get(k, -1) for k in ("tx_pulls", "rx_requests", "rx_starvations", "tx_queue_overflows",
                                               "tx_sequence_gaps", "zmq_errors", "control_batches_committed")}


def ue_events(ue):
    path = log_dir / f"srsue-{ue}.log"
    text = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
    return {"out_of_sync": text.count("detected out-of-sync"),
            "rrc_release": text.count("releasing RRC") + text.count("RRC Release"),
            "random_access_complete": text.count("Random Access Complete"),
            "crashed": int("srsRAN crashed" in text)}


per_ue = {}
for i, ue in enumerate(ids):
    per_ue[ue] = {"cell": cells[i], "rrc_connected": int(rrc.split()[i]), "pdu_session": int(pdu.split()[i]),
                  "ping_ok": int(ping_ok.split()[i]), "camped_pci": camped_pci(ue),
                  "expected_pci": expected_pci[cells[i]], **ue_events(ue)}
policy = json.loads((log_dir / "policy-check.json").read_text()) if (log_dir / "policy-check.json").exists() else None
brokers = {cell: broker(cell) for cell in ("a", "b")}
attached = all(v["rrc_connected"] and v["pdu_session"] and v["ping_ok"] for v in per_ue.values())
own_cells = all(v["camped_pci"] == v["expected_pci"] for v in per_ue.values())
clean = all(b[k] == 0 for b in brokers.values() for k in ("tx_queue_overflows", "tx_sequence_gaps", "zmq_errors"))
summary = {
    "timestamp": timestamp, "schedulers": {"a": sched_a, "b": sched_b},
    "policy_check": policy, "per_ue": per_ue, "brokers": brokers,
    "measure_status": measure_status, "gnb_alive_at_stop": int(gnb_alive), "srsue_alive_at_stop": int(srsue_alive),
    "each_ue_on_its_own_cell": int(own_cells),
    # The gate verdict covers set-up, transport and process health. Whether the
    # radio links stayed up for the whole measurement is the report's segment
    # validity. A crashed srsUE stalls its whole cell (the broker is lock-step):
    # run 20261001T083826Z lost cell a for 60 of 120 s to an srsUE RLC AM
    # assertion (rlc_am_nr.cc:1776), so a crash fails the gate.
    "status": "passed" if (attached and own_cells and clean and measure_status == "completed"
                           and (policy or {}).get("ok") and int(gnb_alive) == 1 and int(srsue_alive) == 1
                           and not any(v["crashed"] for v in per_ue.values())) else "failed",
}
out = pathlib.Path(out_path)
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print(f"summary={out} status={summary['status']}")
raise SystemExit(0 if summary["status"] == "passed" else 1)
PY
