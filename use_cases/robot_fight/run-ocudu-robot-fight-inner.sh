#!/usr/bin/env bash
# Inner half of the native robot-fight gate. Runs INSIDE the user + network +
# mount namespace created by run-ocudu-robot-fight.sh; never invoke it directly.
#
# The R4a multi-UE inner with a second cell: two brokers (each on its own
# topology, control and telemetry sockets, and scheduling profile), two Sionna
# bridges, two gNBs, one srsUE per cell, and an optional GPU contention source
# started once both UEs have attached. Same namespace layout, same process
# supervision, same bounded shutdown.
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
gnb_start_timeout="15"
run_duration_seconds="300"
strict_realtime="0"
sched_a="plain"
sched_b="plain"
contention="none"
contention_start="after-attach"
hog_kernel_us="2000"
hog_duty="1.0"
hog_mps="1"
hog_sm_percent=""
hog_streams="1"
hog_queue_depth="1"
sionna_mps="0"
rt_priority="0"
protected_cpus=""
plain_cpus=""
control_endpoint_a=""
telemetry_endpoint_a=""
control_endpoint_b=""
telemetry_endpoint_b=""
sionna_python=""
sionna_bridge=""
sionna_update_hz="10"
sionna_ready_seconds="180"
sionna_position_endpoint=""
sionna_position_offset=""
sionna_position_timeout_s=""
ue_exec=""
root_exec=""
# Empty unless the outer enabled gNB metrics; ports must match the rendered gNB yamls.
gnb_metrics_socket_a=""
gnb_metrics_port_a="8001"
gnb_metrics_socket_b=""
gnb_metrics_port_b="8002"

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
    --run-duration-seconds) run_duration_seconds="${2:-}"; shift 2 ;;
    --strict-realtime) strict_realtime="${2:-}"; shift 2 ;;
    --sched-a) sched_a="${2:-}"; shift 2 ;;
    --sched-b) sched_b="${2:-}"; shift 2 ;;
    --contention) contention="${2:-}"; shift 2 ;;
    --contention-start) contention_start="${2:-}"; shift 2 ;;
    --hog-kernel-us) hog_kernel_us="${2:-}"; shift 2 ;;
    --hog-duty) hog_duty="${2:-}"; shift 2 ;;
    --hog-mps) hog_mps="${2:-}"; shift 2 ;;
    --hog-sm-percent) hog_sm_percent="${2:-}"; shift 2 ;;
    --hog-streams) hog_streams="${2:-}"; shift 2 ;;
    --hog-queue-depth) hog_queue_depth="${2:-}"; shift 2 ;;
    --sionna-mps) sionna_mps="${2:-}"; shift 2 ;;
    --rt-priority) rt_priority="${2:-}"; shift 2 ;;
    --protected-cpus) protected_cpus="${2:-}"; shift 2 ;;
    --plain-cpus) plain_cpus="${2:-}"; shift 2 ;;
    --control-endpoint-a) control_endpoint_a="${2:-}"; shift 2 ;;
    --telemetry-endpoint-a) telemetry_endpoint_a="${2:-}"; shift 2 ;;
    --control-endpoint-b) control_endpoint_b="${2:-}"; shift 2 ;;
    --telemetry-endpoint-b) telemetry_endpoint_b="${2:-}"; shift 2 ;;
    --sionna-python) sionna_python="${2:-}"; shift 2 ;;
    --sionna-bridge) sionna_bridge="${2:-}"; shift 2 ;;
    --sionna-update-hz) sionna_update_hz="${2:-}"; shift 2 ;;
    --sionna-ready-seconds) sionna_ready_seconds="${2:-}"; shift 2 ;;
    --sionna-position-endpoint) sionna_position_endpoint="${2:-}"; shift 2 ;;
    --sionna-position-offset) sionna_position_offset="${2:-}"; shift 2 ;;
    --sionna-position-timeout-s) sionna_position_timeout_s="${2:-}"; shift 2 ;;
    --ue-exec) ue_exec="${2:-}"; shift 2 ;;
    --root-exec) root_exec="${2:-}"; shift 2 ;;
    --gnb-metrics-socket-a) gnb_metrics_socket_a="${2:-}"; shift 2 ;;
    --gnb-metrics-port-a) gnb_metrics_port_a="${2:-}"; shift 2 ;;
    --gnb-metrics-socket-b) gnb_metrics_socket_b="${2:-}"; shift 2 ;;
    --gnb-metrics-port-b) gnb_metrics_port_b="${2:-}"; shift 2 ;;
    *) usage_error "unexpected argument: $1" ;;
  esac
done

for value in "${repo_root}" "${native_root}" "${config_dir}" "${log_dir}" "${netns_dir}" "${timestamp}" \
             "${parent_netns}" "${parent_mntns}" "${outer_uid}" "${gnb}" "${channel_build}" \
             "${control_endpoint_a}" "${telemetry_endpoint_a}" "${control_endpoint_b}" "${telemetry_endpoint_b}" \
             "${sionna_python}" "${sionna_bridge}"; do
  [[ -n "${value}" ]] || usage_error "missing required argument"
done
for endpoint in "${control_endpoint_a}" "${telemetry_endpoint_a}" "${control_endpoint_b}" "${telemetry_endpoint_b}"; do
  [[ "${endpoint}" == ipc://* ]] || usage_error "control and telemetry endpoints must be ipc://"
done
[[ -x "${sionna_python}" ]] || usage_error "missing Sionna Python: ${sionna_python}"
[[ -f "${sionna_bridge}" ]] || usage_error "missing Sionna bridge: ${sionna_bridge}"
[[ -z "${sionna_position_endpoint}" || "${sionna_position_endpoint}" == ipc:///* ]] || \
  usage_error "Sionna position endpoint must be ipc://"
sionna_position_args=()
if [[ -n "${sionna_position_endpoint}" ]]; then
  sionna_position_args+=(--position-endpoint "${sionna_position_endpoint}")
  [[ -n "${sionna_position_offset}" ]] && sionna_position_args+=(--position-frame-offset "${sionna_position_offset}")
  [[ -n "${sionna_position_timeout_s}" ]] && sionna_position_args+=(--position-timeout-s "${sionna_position_timeout_s}")
fi
[[ "${run_duration_seconds}" =~ ^[1-9][0-9]*$ && "${run_duration_seconds}" -ge 160 ]] || usage_error "invalid run duration"
[[ "${strict_realtime}" =~ ^[01]$ ]] || usage_error "invalid strict-realtime flag"
[[ "$(readlink /proc/self/ns/net)" != "${parent_netns}" ]] || usage_error "network namespace was not isolated"
[[ "$(readlink /proc/self/ns/mnt)" != "${parent_mntns}" ]] || usage_error "mount namespace was not isolated"
awk -v uid="${outer_uid}" '$1 == 0 && $2 == uid && $3 == 1 { found = 1 } END { exit !found }' \
  /proc/self/uid_map || usage_error "user namespace does not contain the exact root mapping"
for command_name in ip mount umount nsenter ps taskset chrt; do
  command -v "${command_name}" >/dev/null 2>&1 || usage_error "missing command: ${command_name}"
done
[[ -x /usr/bin/python3 ]] || usage_error "missing /usr/bin/python3"

# Must agree with use_cases/robot_fight/render-robot-fight-configs.py CELLS: cell i =
# gNB i (PCI i+1) + UE i (netns ue(i+1), 10.45.1.(i+2)).
cells=(a b)
gnb_ids=(gnb0 gnb1)
gnb_pcis=(1 2)
ue_ids=(ue0 ue1)
ue_netns=(ue1 ue2)
ue_ipv4=(10.45.1.2 10.45.1.3)
ue_gateway="10.45.1.1"
scheds=("${sched_a}" "${sched_b}")
control_endpoints=("${control_endpoint_a}" "${control_endpoint_b}")
telemetry_endpoints=("${telemetry_endpoint_a}" "${telemetry_endpoint_b}")

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

cleanup()
{
  local original_status="$?"
  set +e
  trap - EXIT INT TERM HUP
  local index wanted cleanup_failed=0
  # Side processes and the contention source first, then broker admission
  # while both radio requesters are still alive, then the radio peers, then
  # their core/database dependencies.
  for wanted in ue-exec root-exec hog broker srsue gnb-metrics-relay gnb sionna open5gs mongod; do
    for ((index=0; index<${#process_pids[@]}; index++)); do
      if [[ "${process_names[index]}" == "${wanted}"* ]]; then
        if stop_group "${index}"; then process_pids[index]="0"; else cleanup_failed=1; fi
      fi
    done
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

srsue="${native_root}/builds/srsran4g-zmq-release/srsue/src/srsue"
srsue_variant="${OCUDU_NATIVE_SRSUE:-local}"
case "${srsue_variant}" in
  stock) ;;
  local)
    srsue="${native_root}/builds/srsran4g-zmq-local/srsue/src/srsue"
    srsue_manifest="${native_root}/builds/srsran4g-zmq-local/BUILD-MANIFEST.txt"
    [[ -f "${srsue_manifest}" ]] || usage_error "local srsUE is not built: run scripts/native/build-srsue-local.sh (or OCUDU_NATIVE_SRSUE=stock)"
    while read -r srsue_patch_path srsue_patch_sha; do
      grep -qx "patch=${srsue_patch_path##*/} sha256=${srsue_patch_sha}" "${srsue_manifest}" || \
        usage_error "local srsUE was built from other patches than srsue-local-patches.lock.json; rebuild it"
    done < <(/usr/bin/python3 -c 'import json,sys
for p in json.load(open(sys.argv[1]))["patches"]: print(p["path"], p["sha256"])' "${repo_root}/integrations/srsran/srsue-local-patches.lock.json")
    ;;
  *) usage_error "OCUDU_NATIVE_SRSUE must be local or stock" ;;
esac
srsue_preambles="${OCUDU_NATIVE_SRSUE_PREAMBLES:-distinct}"
[[ "${srsue_preambles}" =~ ^(distinct|random)$ ]] || usage_error "OCUDU_NATIVE_SRSUE_PREAMBLES must be distinct or random"
printf 'event=srsue_select variant=%s preambles=%s binary=%s\n' "${srsue_variant}" "${srsue_preambles}" "${srsue}"
fivegc="${native_root}/builds/open5gs-v2.7.6/tests/app/5gc"
mongod="${native_root}/install/mongodb-6.0.29/bin/mongod"
broker="${channel_build}/ocudu-gpu-channel"
hog="${channel_build}/ocudu-gpu-hog"
add_users="${native_root}/src/ocudu/docker/open5gs/add_users.py"
subscriber_verify="${repo_root}/scripts/native/verify-open5gs-subscribers-multi.py"
data_dir="${native_root}/data/ocudu-robot-fight-native/${timestamp}"

for binary in "${gnb}" "${srsue}" "${fivegc}" "${mongod}" "${broker}" "${hog}"; do
  [[ -x "${binary}" ]] || usage_error "missing executable: ${binary}"
done
[[ -d "${data_dir}" ]] || usage_error "missing data dir: ${data_dir}"

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

run_dir="${netns_dir%/netns}"
expand_exec_template()
{
  local text="$1"
  text="${text//\{log_dir\}/${log_dir}}"
  text="${text//\{run_dir\}/${run_dir}}"
  text="${text//\{config_dir\}/${config_dir}}"
  text="${text//\{ue_ids\}/${ue_ids[*]}}"
  text="${text//\{ue_ips\}/${ue_ipv4[*]}}"
  text="${text//\{ue_gateway\}/${ue_gateway}}"
  printf '%s' "${text}"
}
if [[ -n "${root_exec}" ]]; then
  root_exec_command="$(expand_exec_template "${root_exec}")"
  printf 'event=root_exec_start command=%q\n' "${root_exec_command}"
  start_group root-exec "${log_dir}/root-exec.log" bash -c "${root_exec_command}"
fi

# --- core -------------------------------------------------------------------
start_group mongod "${log_dir}/mongod-console.log" "${mongod}" \
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

start_group open5gs "${log_dir}/open5gs.log" "${fivegc}" -c "${config_dir}/open5gs.yaml"
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

# --- scheduling profiles ----------------------------------------------------
# MPS membership is per process: the server's pipe directory is inherited
# from the outer gate; a process meant to have its own context drops it.
mps_server=0
[[ -n "${CUDA_MPS_PIPE_DIRECTORY:-}" ]] && mps_server=1
# SCHED_FIFO is available inside the user namespace only through the rlimit
# the outer gate raised; probe it once and record the answer.
rt_available=0
if [[ "${rt_priority}" -gt 0 ]]; then
  if chrt -f "${rt_priority}" true 2>/dev/null; then
    rt_available=1
  else
    printf 'event=rt_unavailable requested=%s\n' "${rt_priority}"
  fi
fi
# broker_prefix <sched>: the env/taskset/chrt words that precede the broker.
broker_prefix()
{
  local sched="$1"
  local -a words=(env)
  if [[ "${sched}" == "protected" ]]; then
    [[ "${mps_server}" -eq 1 ]] || words+=(-u CUDA_MPS_PIPE_DIRECTORY)
    words+=("CUDA_VISIBLE_DEVICES=${physical_gpu}")
    # shellcheck disable=SC2206  # OCUDU_NATIVE_BROKER_ENV is a KEY=VALUE list
    [[ -n "${OCUDU_NATIVE_BROKER_ENV:-}" ]] && words+=(${OCUDU_NATIVE_BROKER_ENV})
    [[ -n "${protected_cpus}" ]] && words+=(taskset -c "${protected_cpus}")
    [[ "${rt_available}" -eq 1 ]] && words+=(chrt -f "${rt_priority}")
  else
    words+=(-u CUDA_MPS_PIPE_DIRECTORY "CUDA_VISIBLE_DEVICES=${physical_gpu}")
    [[ -n "${plain_cpus}" ]] && words+=(taskset -c "${plain_cpus}")
  fi
  printf '%s\n' "${words[@]}"
}
printf 'event=robot_fight_sched a=%s b=%s mps_server=%s rt_available=%s protected_cpus=%s plain_cpus=%s broker_env=%s\n' \
  "${sched_a}" "${sched_b}" "${mps_server}" "${rt_available}" "${protected_cpus:-any}" "${plain_cpus:-any}" \
  "${OCUDU_NATIVE_BROKER_ENV:-none}"

# --- brokers ----------------------------------------------------------------
gnb_pin=() ue_pin=()
[[ -n "${OCUDU_NATIVE_GNB_CPUS:-}" ]] && gnb_pin=(taskset -c "${OCUDU_NATIVE_GNB_CPUS}")
[[ -n "${OCUDU_NATIVE_NRUE_CPUS:-}" && "${OCUDU_NATIVE_MUE_PIN_UES:-1}" == "1" ]] && \
  ue_pin=(taskset -c "${OCUDU_NATIVE_NRUE_CPUS}")
declare -a broker_pids=() broker_indices=() broker_start_seconds=()
for index in "${!cells[@]}"; do
  cell="${cells[index]}"
  mapfile -t prefix < <(broker_prefix "${scheds[index]}")
  declare -a broker_args=(
    "${broker}" --config "${config_dir}/topology-${cell}.yaml" --duration "${run_duration_seconds}s"
    --control-endpoint "${control_endpoints[index]}"
    --telemetry-endpoint "${telemetry_endpoints[index]}" --telemetry-rate-hz 500
  )
  [[ "${strict_realtime}" == "1" ]] && broker_args+=(--strict-realtime)
  printf 'event=broker_start cell=%s sched=%s command=%q\n' "${cell}" "${scheds[index]}" "${prefix[*]} ${broker_args[*]}"
  broker_start_seconds+=("${SECONDS}")
  start_group "broker-${cell}" "${log_dir}/broker-${cell}.log" "${prefix[@]}" "${broker_args[@]}"
  broker_pids+=("${started_pid}")
  broker_indices+=($((${#process_pids[@]} - 1)))
done
broker_exit_deadline=$((SECONDS + run_duration_seconds + 10))
for index in "${!cells[@]}"; do
  cell="${cells[index]}"
  for id in "${gnb_ids[index]}" "${ue_ids[index]}"; do
    wait_log "${log_dir}/broker-${cell}.log" "event=socket_ready device=${id}" "${broker_pids[index]}" 15 \
      || usage_error "broker ${cell} did not bind ${id}"
  done
  wait_log "${log_dir}/broker-${cell}.log" 'event=control_start ' "${broker_pids[index]}" 15 || \
    usage_error "broker ${cell} control server did not become ready"
done

# --- Sionna bridges (one per cell, same scene, same position feed) ----------
declare -a sionna_pids=()
for index in "${!cells[@]}"; do
  cell="${cells[index]}"
  # env options (-u) must precede the assignments.
  sionna_env=(env)
  [[ "${sionna_mps}" == "1" && "${mps_server}" -eq 1 ]] || sionna_env+=(-u CUDA_MPS_PIPE_DIRECTORY)
  sionna_env+=("CUDA_VISIBLE_DEVICES=${physical_gpu}")
  start_group "sionna-${cell}" "${log_dir}/sionna-bridge-${cell}.log" \
    "${sionna_env[@]}" "${sionna_python}" "${sionna_bridge}" \
    --scenario-config "${config_dir}/scenario-${cell}.json" \
    --control-endpoint "${control_endpoints[index]}" --duration 0 \
    --update-hz "${sionna_update_hz}" --status-jsonl "${log_dir}/sionna-status-${cell}.jsonl" \
    ${sionna_position_args[@]+"${sionna_position_args[@]}"}
  sionna_pids+=("${started_pid}")
done
for index in "${!cells[@]}"; do
  wait_log "${log_dir}/sionna-bridge-${cells[index]}.log" '"event":"sionna_rt_update"' \
    "${sionna_pids[index]}" "${sionna_ready_seconds}" || \
    usage_error "Sionna bridge ${cells[index]} did not publish its first matrix profile update"
done

# --- contention -------------------------------------------------------------
contention_started_seconds=""
start_contention()
{
  [[ -z "${contention_started_seconds}" ]] || return 0
  contention_started_seconds="${SECONDS}"
  if [[ "${contention}" == *busy* ]]; then
    local remaining=$((broker_exit_deadline - SECONDS))
    hog_env=(env)
    if [[ "${hog_mps}" == "1" && "${mps_server}" -eq 1 ]]; then
      [[ -n "${hog_sm_percent}" ]] && hog_env+=("CUDA_MPS_ACTIVE_THREAD_PERCENTAGE=${hog_sm_percent}")
    else
      hog_env+=(-u CUDA_MPS_PIPE_DIRECTORY)
    fi
    hog_env+=("CUDA_VISIBLE_DEVICES=${physical_gpu}")
    start_group hog "${log_dir}/hog.log" "${hog_env[@]}" "${hog}" \
      --duration-s "${remaining}" --kernel-us "${hog_kernel_us}" --duty "${hog_duty}" \
      --streams "${hog_streams}" --queue-depth "${hog_queue_depth}"
    printf 'event=contention_start kind=%s t=%s hog_pid=%s hog_mps=%s hog_sm_percent=%s hog_streams=%s hog_queue_depth=%s\n' \
      "${contention}" "${SECONDS}" "${started_pid}" "${hog_mps}" "${hog_sm_percent:-all}" "${hog_streams}" "${hog_queue_depth}"
  else
    printf 'event=contention_start kind=%s t=%s\n' "${contention}" "${SECONDS}"
  fi
  date -u +%s%3N >"${log_dir}/contention.start_unix_ms"
}
[[ "${contention_start}" == "immediate" ]] && start_contention

# --- gNBs -------------------------------------------------------------------
declare -a gnb_pids=()
for index in "${!gnb_ids[@]}"; do
  id="${gnb_ids[index]}"
  start_group "gnb-${id}" "${log_dir}/${id}-console.log" env "CUDA_VISIBLE_DEVICES=${physical_gpu}" \
    "${gnb_pin[@]}" "${gnb}" -c "${config_dir}/${id}.yaml"
  gnb_pids+=("${started_pid}")
done
for index in "${!gnb_ids[@]}"; do
  wait_log "${log_dir}/${gnb_ids[index]}-console.log" '==== gNB started ===' "${gnb_pids[index]}" "${gnb_start_timeout}" \
    || usage_error "${gnb_ids[index]} did not start"
done
# Each gNB's remote-control WebSocket lives on this namespace's loopback; a
# relay per gNB re-exports it as a socket file in the shared run dir for the
# Web UI's KPI panel. Empty socket = metrics off, nothing started.
gnb_metrics_sockets=("${gnb_metrics_socket_a:-}" "${gnb_metrics_socket_b:-}")
gnb_metrics_ports=("${gnb_metrics_port_a:-8001}" "${gnb_metrics_port_b:-8002}")
for index in "${!gnb_ids[@]}"; do
  [[ -n "${gnb_metrics_sockets[index]}" ]] || continue
  start_group "gnb-metrics-relay-${gnb_ids[index]}" "${log_dir}/gnb-metrics-relay-${gnb_ids[index]}.log" \
    /usr/bin/python3 "${repo_root}/scripts/native/gnb-metrics-relay.py" \
    --socket "${gnb_metrics_sockets[index]}" --port "${gnb_metrics_ports[index]}"
  printf 'event=gnb_metrics_relay gnb=%s port=%s socket=%s\n' \
    "${gnb_ids[index]}" "${gnb_metrics_ports[index]}" "${gnb_metrics_sockets[index]}"
done
sleep 3

# --- UEs --------------------------------------------------------------------
declare -a srsue_pids=()
for index in "${!ue_ids[@]}"; do
  id="${ue_ids[index]}"
  srsue_env=()
  if [[ "${srsue_variant}" == "local" && "${srsue_preambles}" == "distinct" ]]; then
    srsue_env=(env "SRSUE_PRACH_PREAMBLE_INDEX=$((index * 8))")
  fi
  date -u +%s%3N >"${log_dir}/srsue-${id}.start_unix_ms"
  start_group "srsue-${id}" "${log_dir}/srsue-${id}.log" "${srsue_env[@]}" "${ue_pin[@]}" "${srsue}" \
    --general.metrics_csv_enable 1 --general.metrics_period_secs 1 \
    --general.metrics_csv_filename "${log_dir}/srsue-metrics-${id}.csv" \
    "${config_dir}/srsue-${id}.conf"
  srsue_pids+=("${started_pid}")
done

# --- attach -----------------------------------------------------------------
declare -a rrc=() pdu=() ping_ok=()
for _ in "${ue_ids[@]}"; do rrc+=(0); pdu+=(0); ping_ok+=(0); done
deadline=$((SECONDS + 150))
while [[ "${SECONDS}" -lt "${deadline}" ]]; do
  all_done=1
  for index in "${!ue_ids[@]}"; do
    log="${log_dir}/srsue-${ue_ids[index]}.log"
    grep -q 'RRC Connected' "${log}" 2>/dev/null && rrc[index]=1
    grep -q 'PDU Session Establishment successful' "${log}" 2>/dev/null && pdu[index]=1
    if [[ "${rrc[index]}" -eq 1 && "${pdu[index]}" -eq 1 && "${ping_ok[index]}" -eq 0 ]]; then
      if nsenter --net="/run/netns/${ue_netns[index]}" -- \
          ping -I tun_srsue -c 3 -W 2 "${ue_gateway}" \
          >"${log_dir}/ue-ping-${ue_ids[index]}.log" 2>&1; then
        ping_ok[index]=1
        if [[ -n "${ue_exec}" ]]; then
          ue_exec_command="$(expand_exec_template "${ue_exec}")"
          ue_exec_command="${ue_exec_command//\{ue_id\}/${ue_ids[index]}}"
          ue_exec_command="${ue_exec_command//\{ue_ip\}/${ue_ipv4[index]}}"
          ue_exec_command="${ue_exec_command//\{ue_index\}/${index}}"
          ue_exec_command="${ue_exec_command//\{ue_netns\}/${ue_netns[index]}}"
          printf 'event=ue_exec_start ue=%s command=%q\n' "${ue_ids[index]}" "${ue_exec_command}"
          start_group "ue-exec-${ue_ids[index]}" "${log_dir}/ue-exec-${ue_ids[index]}.log" \
            nsenter --net="/run/netns/${ue_netns[index]}" -- bash -c "${ue_exec_command}"
        fi
      fi
    fi
    [[ "${ping_ok[index]}" -eq 1 ]] || all_done=0
  done
  [[ "${all_done}" -eq 1 ]] && break
  sleep 0.5
done
attach_done_seconds="${SECONDS}"
start_contention

# A ping burst per UE while the contention runs: the link-level latency the
# arena's control loop would see, on the same footing for both cells.
contention_ping_seconds=20
if [[ $((broker_exit_deadline - SECONDS)) -gt $((contention_ping_seconds + 30)) ]]; then
  sleep "${contention_ping_seconds}"
  declare -a ping_pids=()
  for index in "${!ue_ids[@]}"; do
    if [[ "${ping_ok[index]}" -eq 1 ]]; then
      nsenter --net="/run/netns/${ue_netns[index]}" -- \
        ping -I tun_srsue -c 50 -i 0.2 -W 2 "${ue_gateway}" \
        >"${log_dir}/ue-ping-${ue_ids[index]}-contention.log" 2>&1 &
      ping_pids+=("$!")
    fi
  done
  # Only the pings: a bare `wait` would block on every supervised group.
  for pid in ${ping_pids[@]+"${ping_pids[@]}"}; do wait "${pid}" >/dev/null 2>&1 || true; done
fi

# --- bounded broker drain ---------------------------------------------------
declare -a broker_status=()
for index in "${!cells[@]}"; do
  while process_running "${broker_pids[index]}" && [[ "${SECONDS}" -lt "${broker_exit_deadline}" ]]; do
    sleep 0.1
  done
  if process_running "${broker_pids[index]}"; then
    printf 'error: broker %s exceeded its bounded natural-exit window\n' "${cells[index]}" >&2
    stop_group "${broker_indices[index]}" || true
    broker_status+=(124)
  else
    set +e
    wait "${broker_pids[index]}"
    broker_status+=("$?")
    set -o pipefail
  fi
done

gnb_alive=1
for pid in "${gnb_pids[@]}"; do process_running "${pid}" || gnb_alive=0; done
srsue_alive=1
for pid in "${srsue_pids[@]}"; do process_running "${pid}" || srsue_alive=0; done

/usr/bin/python3 - \
  "${log_dir}" "${native_root}/results/reports/ocudu-robot-fight/${timestamp}/attach-summary.json" \
  "${timestamp}" "${broker_status[*]}" "${gnb_alive}" "${srsue_alive}" \
  "${rrc[*]}" "${pdu[*]}" "${ping_ok[*]}" "${ue_ids[*]}" "${gnb_pcis[*]}" "${cells[*]}" "${scheds[*]}" \
  "${broker_start_seconds[*]}" "${contention_started_seconds:-0}" "${attach_done_seconds}" "${contention}" <<'PY'
import json, pathlib, re, statistics, sys

(log_dir, out_path, timestamp, broker_status, gnb_alive, srsue_alive, rrc, pdu, ping_ok, ue_ids, gnb_pcis,
 cells, scheds, broker_starts, contention_started, attach_done, contention) = sys.argv[1:]
log_dir = pathlib.Path(log_dir)
ids = ue_ids.split()
cells = cells.split()
scheds = scheds.split()
expected_pci = [int(p) for p in gnb_pcis.split()]
broker_status = [int(s) for s in broker_status.split()]
broker_starts = [int(s) for s in broker_starts.split()]
contention_started = int(contention_started)
attach_done = int(attach_done)


def percentile(values, q):
    if not values:
        return None
    values = sorted(values)
    return values[min(len(values) - 1, int(len(values) * q))]


def camped_pci(ue):
    found = []
    for name in (f"srsue-{ue}.log", f"srsue-{ue}-internal.log"):
        path = log_dir / name
        if path.exists():
            found += re.findall(r"PCI=(\d+)", path.read_text(encoding="utf-8", errors="replace"))
    return int(found[-1]) if found else -1


def ping_stats(path):
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8", errors="replace")
    rtts = [float(v) for v in re.findall(r"time=([0-9.]+) ms", text)]
    loss = re.search(r"(\d+)% packet loss", text)
    return {"count": len(rtts), "loss_pct": int(loss.group(1)) if loss else None,
            "p50_ms": percentile(rtts, 0.5), "p90_ms": percentile(rtts, 0.9),
            "max_ms": max(rtts) if rtts else None, "avg_ms": statistics.fmean(rtts) if rtts else None}


def broker_report(cell, sched, start_seconds):
    log = log_dir / f"broker-{cell}.log"
    text = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
    stop = ""
    priority = None
    timings = []          # (t, h2d, kernel, d2h)
    stalls = []           # t of node_stall (approximate: the last heartbeat t seen)
    heartbeat_idle = {}   # dev -> [(t, idle, stall)]
    last_t = 0
    for line in text.splitlines():
        if line.startswith("event=stop "):
            stop = line
        elif line.startswith("event=cuda_stream_priority "):
            priority = line
        elif line.startswith("event=gpu_timings "):
            m = re.search(r" t=(\d+) h2d_us=([0-9.]+) kernel_us=([0-9.]+) d2h_us=([0-9.]+)", line)
            if m:
                timings.append((int(m.group(1)), float(m.group(2)), float(m.group(3)), float(m.group(4))))
        elif line.startswith("event=heartbeat ") and "puller[" in line:
            m_t = re.search(r" t=(\d+)", line)
            m_dev = re.search(r" dev=([A-Za-z0-9_-]+)", line)
            if m_t and m_dev:
                last_t = int(m_t.group(1))
                idle = int((re.search(r"puller\[[^\]]*idle=(\d+)", line) or [0, -1])[1])
                stall = int((re.search(r"producer\[[^\]]*stall=(\d+)", line) or [0, -1])[1])
                heartbeat_idle.setdefault(m_dev.group(1), []).append((last_t, idle, stall))
        elif line.startswith("event=node_stall "):
            stalls.append(last_t)
    counters = {k: int(v) for k, v in re.findall(r"(\w+)=(\d+)", stop)}
    # Broker t is seconds since its own start; the gate's clocks are SECONDS.
    contention_t = (contention_started - start_seconds) if contention_started else None
    attach_t = attach_done - start_seconds

    def window(rows, t0):
        return [r for r in rows if t0 is None or r[0] >= t0 + 2]

    def timing_stats(rows):
        if not rows:
            return None
        out = {"samples": len(rows)}
        for name, col in (("h2d_us", 1), ("kernel_us", 2), ("d2h_us", 3)):
            values = [r[col] for r in rows]
            out[name] = {"p50": percentile(values, 0.5), "p90": percentile(values, 0.9),
                         "p99": percentile(values, 0.99), "max": max(values)}
        return out

    idle_delta = {}
    for dev, rows in heartbeat_idle.items():
        if contention_t is not None:
            before = [r for r in rows if r[0] < contention_t]
            after = [r for r in rows if r[0] >= contention_t]
            if before and after:
                idle_delta[dev] = {"puller_idle": after[-1][1] - before[-1][1],
                                   "producer_stall": after[-1][2] - before[-1][2]}
        if rows:
            idle_delta.setdefault(dev, {})["puller_idle_total"] = rows[-1][1]
    return {
        "sched": sched,
        "status": None,
        "counters": {k: counters.get(k, -1) for k in ("tx_pulls", "rx_requests", "rx_starvations",
                                                       "tx_queue_overflows", "tx_sequence_gaps", "zmq_errors")},
        "control": {k: counters.get(k) for k in ("control_msgs_received", "control_updates_applied",
                                                 "control_batches_committed", "telemetry_frames") if k in counters},
        "cuda_stream_priority": priority,
        "gpu_timings_all": timing_stats(timings),
        "gpu_timings_before_contention": timing_stats([r for r in timings if contention_t is None or r[0] < contention_t]),
        "gpu_timings_under_contention": timing_stats(window(timings, contention_t)) if contention_t is not None else None,
        "node_stalls_total": len(stalls),
        "node_stalls_under_contention": len([t for t in stalls if contention_t is not None and t >= contention_t]),
        "heartbeat_idle_since_contention": idle_delta,
        "contention_t": contention_t,
        "attach_done_t": attach_t,
    }


brokers = {}
for i, cell in enumerate(cells):
    brokers[cell] = broker_report(cell, scheds[i], broker_starts[i])
    brokers[cell]["status"] = broker_status[i]

per_ue = {}
for i, ue in enumerate(ids):
    per_ue[ue] = {
        "cell": cells[i],
        "rrc_connected": int(rrc.split()[i]),
        "pdu_session_established": int(pdu.split()[i]),
        "ping_ok": int(ping_ok.split()[i]),
        "camped_pci": camped_pci(ue),
        "expected_pci": expected_pci[i],
        "ping_attach": ping_stats(log_dir / f"ue-ping-{ue}.log"),
        "ping_under_contention": ping_stats(log_dir / f"ue-ping-{ue}-contention.log"),
    }
all_attached = all(v["rrc_connected"] and v["pdu_session_established"] and v["ping_ok"] for v in per_ue.values())
own_cells = all(v["camped_pci"] == v["expected_pci"] for v in per_ue.values())
strict_clean = all(b["counters"].get(k, 1) == 0 for b in brokers.values()
                   for k in ("tx_queue_overflows", "tx_sequence_gaps", "zmq_errors"))
brokers_clean = all(b["status"] == 0 for b in brokers.values())

sionna = {}
for cell in cells:
    path = log_dir / f"sionna-status-{cell}.jsonl"
    updates = 0
    last = None
    if path.exists():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if '"sionna_rt_update"' in line:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("event") == "sionna_rt_update":
                    updates += 1
                    last = record
    position = (last or {}).get("position_status") or {}
    sionna[cell] = {"updates": updates, "position_source": (last or {}).get("position_source"),
                    "position_messages_received": position.get("messages_received"),
                    "last_channel_generation_ms": ((last or {}).get("timing_ms") or {}).get("channel_generation")}

hog = None
hog_log = log_dir / "hog.log"
if hog_log.exists():
    lines = [l for l in hog_log.read_text(encoding="utf-8", errors="replace").splitlines() if l.startswith("event=hog ")]
    if lines:
        hog = {"last": lines[-1], "reports": len(lines)}

summary = {
    "timestamp": timestamp,
    "docker_used": False,
    "runtime_mode": "rootless_user_net_mount_namespace",
    "cells": {cell: {"sched": scheds[i], "gnb": f"gnb{i}", "ue": ids[i]} for i, cell in enumerate(cells)},
    "contention": contention,
    "per_ue": per_ue,
    "brokers": brokers,
    "sionna": sionna,
    "hog": hog,
    "each_ue_on_its_own_cell": int(own_cells),
    "gnb_alive_at_broker_stop": int(gnb_alive),
    "srsue_alive_at_broker_stop": int(srsue_alive),
    "log_dir": str(log_dir),
    # rx_starvations, node stalls and the GPU timings are the measurement,
    # not the gate: they are recorded per broker and never fail the run
    # (unless --strict-realtime made the broker itself exit non-zero).
    "status": "passed" if (all_attached and own_cells and strict_clean and brokers_clean
                           and int(gnb_alive) == 1 and int(srsue_alive) == 1) else "failed",
}
parameters = pathlib.Path(out_path).parent / "run-parameters.json"
if parameters.exists():
    try:
        summary["run_parameters"] = json.loads(parameters.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
shape = pathlib.Path(log_dir).parent.parent.parent / "configs" / "ocudu-robot-fight-native" / timestamp / "robot-fight-shape.json"
if shape.exists():
    try:
        summary["shape"] = json.loads(shape.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
out = pathlib.Path(out_path)
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print(f"summary={out}")
for cell, b in brokers.items():
    under = b["gpu_timings_under_contention"] or {}
    kernel = under.get("kernel_us") or {}
    print(f"broker={cell} sched={b['sched']} status={b['status']} rx_starvations={b['counters']['rx_starvations']} "
          f"stalls_under_contention={b['node_stalls_under_contention']} "
          f"kernel_p50={kernel.get('p50')} kernel_p99={kernel.get('p99')} kernel_max={kernel.get('max')}")
raise SystemExit(0 if summary["status"] == "passed" else 1)
PY
