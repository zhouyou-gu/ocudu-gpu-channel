#!/usr/bin/env bash
# Inner half of the native multi-gNB attach gate. Runs INSIDE the user + network
# + mount namespace created by run-ocudu-multi-gnb.sh; never invoke it directly.
#
# Derived from run-ocudu-multi-ue-inner.sh: the namespace layout, process
# supervision and bounded shutdown are that gate's, unchanged. The differences
# are the second cell -- two gNB processes started together (a CUDA gNB spends
# tens of seconds in device initialisation, so starting them one after the
# other would leave the broker's first node starved), a four-device broker
# topology, a per-UE record of which cell (PCI) it camped on, and a sampler of
# each gNB's memory for the multi-process GPU question.
set -uo pipefail

repo_root=""
native_root=""
config_dir=""
log_dir=""
netns_dir=""
timestamp=""
physical_gpu="0"
channel_build=""
gnb=""
gnb_start_timeout="15"
parent_netns=""
parent_mntns=""
outer_uid=""
# Sionna mode (ported from run-ocudu-multi-ue-inner.sh). Empty channel_mode
# keeps the fixed-TDL behaviour this gate always had.
channel_mode="legacy"
control_endpoint=""
telemetry_endpoint=""
sionna_python=""
sionna_bridge=""
sionna_scenario_config=""
sionna_status_jsonl=""
sionna_update_hz="10"
sionna_ready_seconds="180"

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
    --gnb-binary) gnb="${2:-}"; shift 2 ;;
    --gnb-start-timeout) gnb_start_timeout="${2:-}"; shift 2 ;;
    --parent-netns) parent_netns="${2:-}"; shift 2 ;;
    --parent-mntns) parent_mntns="${2:-}"; shift 2 ;;
    --outer-uid) outer_uid="${2:-}"; shift 2 ;;
    --channel-mode) channel_mode="${2:-}"; shift 2 ;;
    --control-endpoint) control_endpoint="${2:-}"; shift 2 ;;
    --telemetry-endpoint) telemetry_endpoint="${2:-}"; shift 2 ;;
    --sionna-python) sionna_python="${2:-}"; shift 2 ;;
    --sionna-bridge) sionna_bridge="${2:-}"; shift 2 ;;
    --sionna-scenario-config) sionna_scenario_config="${2:-}"; shift 2 ;;
    --sionna-status-jsonl) sionna_status_jsonl="${2:-}"; shift 2 ;;
    --sionna-update-hz) sionna_update_hz="${2:-}"; shift 2 ;;
    --sionna-ready-seconds) sionna_ready_seconds="${2:-}"; shift 2 ;;
    *) usage_error "unexpected argument: $1" ;;
  esac
done

for value in "${repo_root}" "${native_root}" "${config_dir}" "${log_dir}" \
             "${netns_dir}" "${timestamp}" "${parent_netns}" "${parent_mntns}" "${outer_uid}" "${gnb}"; do
  [[ -n "${value}" ]] || usage_error "missing required argument"
done
[[ "$(readlink /proc/self/ns/net)" != "${parent_netns}" ]] || usage_error "network namespace was not isolated"
[[ "$(readlink /proc/self/ns/mnt)" != "${parent_mntns}" ]] || usage_error "mount namespace was not isolated"
# /proc/self/uid_map is column-aligned with leading whitespace, so compare
# fields rather than matching the line literally.
awk -v uid="${outer_uid}" '$1 == 0 && $2 == uid && $3 == 1 { found = 1 } END { exit !found }' \
  /proc/self/uid_map || usage_error "user namespace does not contain the exact root mapping"
for command_name in ip mount umount nsenter ps; do
  command -v "${command_name}" >/dev/null 2>&1 || usage_error "missing command: ${command_name}"
done
[[ -x /usr/bin/python3 ]] || usage_error "missing /usr/bin/python3"
[[ "${channel_mode}" == "legacy" || "${channel_mode}" == "sionna" ]] || \
  usage_error "unsupported channel mode: ${channel_mode}"
if [[ "${channel_mode}" == "sionna" ]]; then
  for value in "${control_endpoint}" "${telemetry_endpoint}" "${sionna_python}" \
               "${sionna_bridge}" "${sionna_scenario_config}" "${sionna_status_jsonl}"; do
    [[ -n "${value}" ]] || usage_error "sionna mode requires the sionna arguments"
  done
  [[ "${control_endpoint}" == ipc://* && "${telemetry_endpoint}" == ipc://* ]] || \
    usage_error "sionna control and telemetry endpoints must be ipc://"
  [[ -x "${sionna_python}" ]] || usage_error "missing Sionna Python: ${sionna_python}"
  [[ -f "${sionna_bridge}" ]] || usage_error "missing Sionna bridge: ${sionna_bridge}"
  [[ -f "${sionna_scenario_config}" ]] || usage_error "missing Sionna scenario: ${sionna_scenario_config}"
  [[ "${sionna_update_hz}" =~ ^[0-9]+(\.[0-9]+)?$ ]] || usage_error "invalid Sionna update rate: ${sionna_update_hz}"
  [[ "${sionna_ready_seconds}" =~ ^[1-9][0-9]*$ ]] || usage_error "invalid Sionna ready window: ${sionna_ready_seconds}"
fi

# Must agree with scripts/native/render-multi-gnb-configs.py CELLS and UES.
# UE i is expected to camp on cell i (the topology's serving vs intercell loss).
gnb_ids=(gnb0 gnb1)
gnb_pcis=(1 2)
ue_ids=(ue0 ue1)
ue_netns=(ue1 ue2)
ue_ipv4=(10.45.1.2 10.45.1.3)
ue_gateway="10.45.1.1"
# Start the UEs together (0) or hold each until its predecessor is RRC-connected (1).
#
# Simultaneous is the DEFAULT, and deliberately so. Delaying a UE is what makes
# the gNB deaf: its producer needs a window on every incoming lane, so while one
# UE has not started, the gNB hears none of them -- and every UE is then released
# to RACH at the same instant, which is the collision the delay was meant to
# avoid. Starting together keeps the gNB fed from the first slot and lets the
# per-UE near/far channel asymmetry set the sync times, which is where the
# separation has to come from.
stagger_ues="${OCUDU_NATIVE_MUE_STAGGER:-0}"
# Strict attach verdict (X0): a UE counts as attached only if it also stayed
# attached -- no scheduling-request failure, no PRACH after RRC Connected, no
# RLF, and every ping answered. 0 restores the old "ever pinged once" verdict.
attach_strict="${OCUDU_NATIVE_ATTACH_STRICT:-1}"

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
  # The outer gate holds its flock on OCUDU_NATIVE_GATE_LOCK_FD. Close it in
  # the child: a process that outlives teardown must not keep the lock (a
  # stray open5gs-scpd once refused seven later runs, S15).
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

# Session members of a group started by start_group (setsid makes pid = pgid
# = sid). Children the leader forked stay in its session even if they change
# group, and they can outlive the leader.
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
  # Wait on the whole group, not the leader: a leader that exits or aborts
  # during teardown (open5gs 5gc) used to leave its forked daemons running.
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
      printf 'error: %s group %s remains alive after bounded KILL\n' \
        "${process_names[index]}" "${pgid}" >&2
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
  # Stop broker admission first while both radio requesters are still alive,
  # then the radio peers, then their core/database dependencies.
  for wanted in sampler broker sionna srsue gnb open5gs mongod; do
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
# srsUE with the local patches (srsue-local-patches.lock.json) by default: the
# pinned one always sends preamble 0 and a UE that loses contention still
# completes RA, so two UEs on one lock-step broker merge onto one C-RNTI (S16).
# OCUDU_NATIVE_SRSUE=stock runs the pinned build.
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
# Preamble per UE: distinct (default, deterministic) or random (the patch's
# own per-attempt choice). Only the local build reads SRSUE_PRACH_PREAMBLE_INDEX.
srsue_preambles="${OCUDU_NATIVE_SRSUE_PREAMBLES:-distinct}"
[[ "${srsue_preambles}" =~ ^(distinct|random)$ ]] || usage_error "OCUDU_NATIVE_SRSUE_PREAMBLES must be distinct or random"
printf 'event=srsue_select variant=%s preambles=%s binary=%s\n' "${srsue_variant}" "${srsue_preambles}" "${srsue}"
fivegc="${native_root}/builds/open5gs-v2.7.6/tests/app/5gc"
mongod="${native_root}/install/mongodb-6.0.29/bin/mongod"
broker="${channel_build:-${native_root}/builds/ocudu-gpu-channel-cuda-release}/ocudu-gpu-channel"
add_users="${native_root}/src/ocudu/docker/open5gs/add_users.py"
subscriber_verify="${repo_root}/scripts/native/verify-open5gs-subscribers-multi.py"
data_dir="${native_root}/data/ocudu-multi-gnb-native/${timestamp}"

for binary in "${gnb}" "${srsue}" "${fivegc}" "${mongod}" "${broker}"; do
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
# 10.45.0.1 is the Open5GS dynamic-pool advertisement; 10.45.1.1 is the gateway
# the pinned subscriber addresses (10.45.1.2/.3) use and the ping target.
ip addr add 10.45.0.1/24 dev "${root_tun}"
ip addr add 10.45.1.1/24 dev "${root_tun}"
ip link set "${root_tun}" up

# One namespace per UE: srsUE moves its TUN into the namespace named in [gw],
# and two UEs sharing one namespace would collide on ip_devname.
for name in "${ue_netns[@]}"; do
  ip netns add "${name}"
  created_netns+=("${name}")
  nsenter --net="/run/netns/${name}" -- ip link set lo up
done

# --- core -------------------------------------------------------------------
start_group mongod "${log_dir}/mongod-console.log" "${mongod}" \
  --dbpath "${data_dir}" --bind_ip 127.0.0.1 --port 27017 --logpath "${log_dir}/mongod.log"
mongod_pid="${started_pid}"
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

# PYTHONDONTWRITEBYTECODE: add_users imports Open5GS.py out of the pinned
# open5gs checkout; the __pycache__ CPython would leave there makes that
# checkout dirty and fails the workspace lock on the NEXT run.
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

# --- broker, gNB, UEs -------------------------------------------------------
broker_args=("${broker}" --config "${config_dir}/topology.yaml" --duration "$((240 + gnb_start_timeout))s")
# Optional wire capture (per port, first N samples after the skip), as in the
# multi-UE gate: OCUDU_NATIVE_MGNB_WIRE_CAPTURE_SAMPLES / _SKIP_SECONDS (60).
wire_capture_samples="${OCUDU_NATIVE_MGNB_WIRE_CAPTURE_SAMPLES:-0}"
wire_capture_skip_seconds="${OCUDU_NATIVE_MGNB_WIRE_CAPTURE_SKIP_SECONDS:-60}"
[[ "${wire_capture_samples}" =~ ^[0-9]+$ && "${wire_capture_skip_seconds}" =~ ^[0-9]+$ ]] || \
  usage_error "OCUDU_NATIVE_MGNB_WIRE_CAPTURE_SAMPLES/_SKIP_SECONDS must be integers"
if [[ "${wire_capture_samples}" -gt 0 ]]; then
  mkdir -p "${log_dir}/wire-capture"
  broker_args+=(
    --wire-capture-dir "${log_dir}/wire-capture"
    --wire-capture-samples "${wire_capture_samples}"
    --wire-capture-skip "$((wire_capture_skip_seconds * 23040000))"
  )
fi
if [[ "${channel_mode}" == "sionna" ]]; then
  broker_args+=(
    --control-endpoint "${control_endpoint}"
    --telemetry-endpoint "${telemetry_endpoint}"
    --telemetry-rate-hz 500
  )
fi
start_group broker "${log_dir}/broker.log" \
  env CUDA_VISIBLE_DEVICES="${physical_gpu}" "${broker_args[@]}"
broker_pid="${started_pid}"
broker_index=$((${#process_pids[@]} - 1))
# The broker's clock starts now, so the gNB start window is added to the 240 s
# the multi-UE gate runs; ten more seconds for grouped drain and shutdown.
broker_exit_deadline=$((SECONDS + 250 + gnb_start_timeout))
# Every UE row must resolve before the gNB is admitted, otherwise a UE could
# attach against a broker that has not finished standing its node up.
# event=socket_ready is used rather than event=radio_node_resolved because the
# latter only exists from M0.5 onward, and this gate has to be runnable against
# an older revision to tell a live regression apart from pre-existing behaviour.
# Both lines mean the same thing here: that node's transport is bound.
for id in "${gnb_ids[@]}" "${ue_ids[@]}"; do
  wait_log "${log_dir}/broker.log" "event=socket_ready device=${id}" "${broker_pid}" 15 \
    || { grep -m1 "event=fatal" "${log_dir}/broker.log" >&2 || true; usage_error "broker did not bind ${id}"; }
done

# Sionna has to be streaming before the gNBs are admitted: every sionna_rt link
# starts as a quiet -100 dB TDL, so a UE that RACHes before the first
# matrix_profile_swap lands is transmitting into a dead channel.
sionna_pid=""
if [[ "${channel_mode}" == "sionna" ]]; then
  wait_log "${log_dir}/broker.log" 'event=control_start ' "${broker_pid}" 15 || \
    usage_error "broker control server did not become ready"
  start_group sionna "${log_dir}/sionna-bridge.log" \
    env CUDA_VISIBLE_DEVICES="${physical_gpu}" "${sionna_python}" "${sionna_bridge}" \
    --scenario-config "${sionna_scenario_config}" \
    --control-endpoint "${control_endpoint}" --duration 0 \
    --update-hz "${sionna_update_hz}" --status-jsonl "${sionna_status_jsonl}"
  sionna_pid="${started_pid}"
  wait_log "${log_dir}/sionna-bridge.log" '"event":"sionna_rt_update"' \
    "${sionna_pid}" "${sionna_ready_seconds}" || \
    usage_error "Sionna RT did not publish its first matrix profile update"
fi

# Both cells start together; each must reach its banner within the window.
declare -a gnb_pids=()
for id in "${gnb_ids[@]}"; do
  start_group "gnb-${id}" "${log_dir}/${id}-console.log" "${gnb}" -c "${config_dir}/${id}.yaml"
  gnb_pids+=("${started_pid}")
done
# Per-gNB memory while the run lasts: resident set, and the GPU's per-process
# view where the driver offers one (it is empty on some integrated GPUs).
start_group sampler "${log_dir}/memory-samples.log" /bin/bash -c '
  while :; do
    printf "t=%s" "$(date +%s)"
    for pid in "$@"; do
      printf " pid=%s rss_kb=%s" "${pid}" "$(awk "/^VmRSS/ {print \$2}" "/proc/${pid}/status" 2>/dev/null)"
    done
    printf " gpu_apps=%s\n" "$(nvidia-smi --query-compute-apps=pid,used_memory --format=csv,noheader 2>/dev/null | tr "\n" ";")"
    sleep 2
  done' sampler "${gnb_pids[@]}"
for index in "${!gnb_ids[@]}"; do
  wait_log "${log_dir}/${gnb_ids[index]}-console.log" '==== gNB started ===' "${gnb_pids[index]}" "${gnb_start_timeout}" \
    || usage_error "${gnb_ids[index]} did not start"
done
sleep 3

# UE launch stagger. srsRAN ZMQ radios share the broker's lock-step virtual
# time, so two UEs started together transmit the IDENTICAL RACH preamble on the
# IDENTICAL PRACH occasion and the gNB merges them onto one C-RNTI. The second
# UE then decodes NAS built against the first UE's security context and reports
# "Integrity check failure" followed by "K_amf requested before a valid NAS
# security context was established". Holding each UE until its predecessor is
# RRC-connected puts them on different PRACH occasions and distinct C-RNTIs.
# scripts/remote/ocudu-multi-ue-smoke.sh does the same thing for the same
# reason; a fixed sleep is not enough because attach time varies with the
# per-UE channel model.
declare -a srsue_pids=()
for index in "${!ue_ids[@]}"; do
  id="${ue_ids[index]}"
  if [[ "${index}" -gt 0 && "${stagger_ues}" -eq 1 ]]; then
    previous="${ue_ids[index-1]}"
    # Observed on this host (broker.log event=node_stall + heartbeat): the gNB
    # node's producer needs a common window across EVERY incoming lane, so
    # while a UE has not started, its TX ring is empty and the gNB is deaf to
    # the UEs that HAVE started. A "hold ue1 until ue0 is RRC-connected"
    # stagger therefore cannot fire -- ue0 cannot connect while ue1 is absent
    # -- and both UEs are released to RACH the instant the last one streams.
    # (Verified pre-MIMO behaviour: the baseline server loop computes the same
    # min-over-incoming-edges window, so this is not an M0 regression.)
    #
    # So the wait is bounded short and the real separation comes from srsRAN's
    # own RACH backoff and contention resolution, which the attach window below
    # is sized to allow.
    stagger_deadline=$((SECONDS + 10))
    while [[ "${SECONDS}" -lt "${stagger_deadline}" ]]; do
      grep -q 'RRC Connected' "${log_dir}/srsue-${previous}.log" 2>/dev/null && break
      sleep 0.5
    done
    sleep 2
  fi
  srsue_env=()
  if [[ "${srsue_variant}" == "local" && "${srsue_preambles}" == "distinct" ]]; then
    srsue_env=(env "SRSUE_PRACH_PREAMBLE_INDEX=$((index * 8))")
  fi
  start_group "srsue-${id}" "${log_dir}/srsue-${id}.log" "${srsue_env[@]}" "${srsue}" "${config_dir}/srsue-${id}.conf"
  srsue_pids+=("${started_pid}")
done

# --- attach verdict ---------------------------------------------------------
declare -a rrc=() pdu=() ping_ok=()
for _ in "${ue_ids[@]}"; do rrc+=(0); pdu+=(0); ping_ok+=(0); done

# Each UE is pinged as soon as IT is up rather than after all of them, because
# the ping has to happen while the broker is still relaying -- once the broker
# exits there is no radio and every ping fails regardless of attach state.
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
      fi
    fi
    [[ "${ping_ok[index]}" -eq 1 ]] || all_done=0
  done
  [[ "${all_done}" -eq 1 ]] && break
  sleep 0.5
done

# --- bounded broker drain ---------------------------------------------------
while process_running "${broker_pid}" && [[ "${SECONDS}" -lt "${broker_exit_deadline}" ]]; do
  sleep 0.1
done
if process_running "${broker_pid}"; then
  printf 'error: broker exceeded its bounded natural-exit window\n' >&2
  stop_group "${broker_index}" || true
  broker_status=124
else
  set +e
  wait "${broker_pid}"
  broker_status="$?"
  set -o pipefail
fi

gnb_alive=1
for pid in "${gnb_pids[@]}"; do process_running "${pid}" || gnb_alive=0; done
srsue_alive=1
for pid in "${srsue_pids[@]}"; do process_running "${pid}" || srsue_alive=0; done

/usr/bin/python3 - \
  "${log_dir}" "${native_root}/results/reports/ocudu-multi-gnb/${timestamp}/attach-summary.json" \
  "${timestamp}" "${broker_status}" "${gnb_alive}" "${srsue_alive}" \
  "${rrc[*]}" "${pdu[*]}" "${ping_ok[*]}" "${ue_ids[*]}" "${gnb_pcis[*]}" "${channel_mode}" "${attach_strict}" <<'PY'
import json, pathlib, re, sys

(log_dir, out_path, timestamp, broker_status, gnb_alive, srsue_alive,
 rrc, pdu, ping_ok, ue_ids, gnb_pcis, channel_mode, attach_strict) = sys.argv[1:]

log_dir = pathlib.Path(log_dir)
stop = ""
for line in (log_dir / "broker.log").read_text(encoding="utf-8", errors="replace").splitlines():
    if line.startswith("event=stop "):
        stop = line
counters = {k: int(v) for k, v in re.findall(r"(\w+)=(\d+)", stop)}

ids = ue_ids.split()
expected_pci = [int(p) for p in gnb_pcis.split()]


def camped_pci(ue):
    """The PCI srsUE last reported for its serving cell (console and internal log)."""
    found = []
    for name in (f"srsue-{ue}.log", f"srsue-{ue}-internal.log"):
        path = log_dir / name
        if path.exists():
            found += re.findall(r"PCI=(\d+)", path.read_text(encoding="utf-8", errors="replace"))
    return int(found[-1]) if found else -1


per_ue = {
    ue: {
        "rrc_connected": int(r),
        "pdu_session_established": int(p),
        "ping_ok": int(q),
        "camped_pci": camped_pci(ue),
        "expected_pci": expected_pci[i],
    }
    for i, (ue, r, p, q) in enumerate(zip(ids, rrc.split(), pdu.split(), ping_ok.split()))
}

def attach_evidence(ue):
    """Strict evidence from the srsUE console log and the ping log. A UE that
    attached and then lost the link (Scheduling request failed -> RRC release
    -> renewed PRACH) once pinged, so rrc/pdu/ping_ok alone call it attached.
    "Received RRC Release" is deliberately not counted: the gate's own
    teardown releases every UE once right before "Stopping ..".
    """
    text = ""
    path = log_dir / f"srsue-{ue}.log"
    if path.exists():
        text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    first_connected = next((i for i, l in enumerate(lines) if "RRC Connected" in l), None)
    reattach = 0
    if first_connected is not None:
        reattach = sum(1 for l in lines[first_connected + 1:] if "Random Access Transmission" in l)
    sent = received = 0
    ping_path = log_dir / f"ue-ping-{ue}.log"
    if ping_path.exists():
        m = re.search(r"(\d+) packets transmitted, (\d+) (?:packets )?received",
                      ping_path.read_text(encoding="utf-8", errors="replace"))
        if m:
            sent, received = int(m.group(1)), int(m.group(2))
    return {
        "sr_failures": text.count("Scheduling request failed"),
        "reattach_attempts": reattach,
        "rlf_count": sum(1 for l in lines if re.search(r"RLF|Radio Link Failure", l, re.I)),
        "ping_sent": sent,
        "ping_received": received,
    }


for ue, v in per_ue.items():
    v.update(attach_evidence(ue))
    v["attach_clean"] = int(bool(v["rrc_connected"] and v["pdu_session_established"]
                                 and v["ping_sent"] > 0 and v["ping_received"] == v["ping_sent"]
                                 and v["sr_failures"] == 0 and v["reattach_attempts"] == 0
                                 and v["rlf_count"] == 0))
    print(f"event=ue_attach_evidence ue={ue} rrc={v['rrc_connected']} "
          f"pdu={v['pdu_session_established']} ping={v['ping_received']}/{v['ping_sent']} "
          f"sr_failures={v['sr_failures']} reattach_attempts={v['reattach_attempts']} "
          f"rlf={v['rlf_count']} clean={v['attach_clean']}", flush=True)
all_attached = all(v["rrc_connected"] and v["pdu_session_established"] and v["ping_ok"]
                   for v in per_ue.values())
if int(attach_strict):
    all_attached = all_attached and all(v["attach_clean"] for v in per_ue.values())
# Each UE on its own cell is what makes this a two-cell test rather than two UEs
# on one surviving cell.
own_cells = all(v["camped_pci"] == v["expected_pci"] for v in per_ue.values())
strict_clean = all(counters.get(k, 1) == 0
                   for k in ("tx_queue_overflows", "tx_sequence_gaps", "zmq_errors"))
summary = {
    "timestamp": timestamp,
    "docker_used": False,
    "runtime_mode": "rootless_user_net_mount_namespace",
    "ue_count": len(ids),
    "cell_count": len(expected_pci),
    "each_ue_on_its_own_cell": int(own_cells),
    "attach_strict": int(attach_strict),
    "per_ue": per_ue,
    "broker_status": int(broker_status),
    "gnb_alive_at_broker_stop": int(gnb_alive),
    "srsue_alive_at_broker_stop": int(srsue_alive),
    "log_dir": str(log_dir),
    "status": "passed" if (all_attached and own_cells and strict_clean and int(broker_status) == 0
                           and int(gnb_alive) == 1 and int(srsue_alive) == 1) else "failed",
}
summary.update({k: counters.get(k, -1) for k in
                ("tx_pulls", "rx_requests", "rx_starvations",
                 "tx_queue_overflows", "tx_sequence_gaps", "zmq_errors")})
summary["channel_mode"] = channel_mode
status_jsonl = log_dir / "sionna-status.jsonl"
if channel_mode == "sionna" and status_jsonl.exists():
    updates = 0
    for line in status_jsonl.read_text(encoding="utf-8", errors="replace").splitlines():
        if '"sionna_rt_update"' in line:
            updates += 1
    summary["sionna"] = {"updates": updates,
                         "control_updates_applied": counters.get("control_updates_applied", -1),
                         "control_updates_rejected": counters.get("control_updates_rejected", -1)}
out = pathlib.Path(out_path)
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print(f"summary={out}")
raise SystemExit(0 if summary["status"] == "passed" else 1)
PY
