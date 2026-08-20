#!/usr/bin/env bash
set -euo pipefail

# Inner runner for the native rank-1 2x1/1x2 live gate (R2). Derived from
# run-ocudu-legacy-1x1-inner.sh: the srsUE/core/netns side is IDENTICAL to
# the 1x1 regression gate; only the gNB (2T2R rank-1 fixture), the broker
# topology (asymmetric 2x1 DL / 1x2 UL), and the run window (20 s) differ.

mode=""
parent_netns=""
parent_mntns=""
outer_uid=""
netns_dir=""
physical_gpu=""
hardware_probe=""
probe_broker=""
probe_config=""
native_root=""
repo_root=""
config_dir=""
log_dir=""
report_dir=""
timestamp=""

usage_error()
{
  printf 'error: %s\n' "$1" >&2
  exit 2
}

while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --mode) mode="${2:-}"; shift 2 ;;
    --parent-netns) parent_netns="${2:-}"; shift 2 ;;
    --parent-mntns) parent_mntns="${2:-}"; shift 2 ;;
    --outer-uid) outer_uid="${2:-}"; shift 2 ;;
    --netns-dir) netns_dir="${2:-}"; shift 2 ;;
    --physical-gpu) physical_gpu="${2:-}"; shift 2 ;;
    --hardware-probe) hardware_probe="${2:-}"; shift 2 ;;
    --probe-broker) probe_broker="${2:-}"; shift 2 ;;
    --probe-config) probe_config="${2:-}"; shift 2 ;;
    --native-root) native_root="${2:-}"; shift 2 ;;
    --repo-root) repo_root="${2:-}"; shift 2 ;;
    --config-dir) config_dir="${2:-}"; shift 2 ;;
    --log-dir) log_dir="${2:-}"; shift 2 ;;
    --report-dir) report_dir="${2:-}"; shift 2 ;;
    --timestamp) timestamp="${2:-}"; shift 2 ;;
    *) usage_error "unknown argument: $1" ;;
  esac
done

[[ "${mode}" == "probe" || "${mode}" == "run" ]] || usage_error "--mode must be probe or run"
[[ "${outer_uid}" =~ ^(0|[1-9][0-9]*)$ ]] || usage_error "invalid --outer-uid"
[[ "${physical_gpu}" =~ ^(0|[1-9][0-9]*)$ ]] || usage_error "invalid --physical-gpu"
[[ "${netns_dir}" == /* && -d "${netns_dir}" && ! -L "${netns_dir}" ]] || usage_error "invalid --netns-dir"
[[ "$(readlink /proc/self/ns/net)" != "${parent_netns}" ]] || usage_error "network namespace was not isolated"
[[ "$(readlink /proc/self/ns/mnt)" != "${parent_mntns}" ]] || usage_error "mount namespace was not isolated"
awk -v uid="${outer_uid}" '$1 == 0 && $2 == uid && $3 == 1 { found = 1 } END { exit !found }' /proc/self/uid_map || \
  usage_error "user namespace does not contain the exact root mapping"
cap_eff="$(awk '/^CapEff:/ {print $2}' /proc/self/status)"
cap_eff_value=$((16#${cap_eff}))
(( (cap_eff_value & (1 << 12)) != 0 )) || usage_error "CAP_NET_ADMIN is absent inside userns"
(( (cap_eff_value & (1 << 21)) != 0 )) || usage_error "CAP_SYS_ADMIN is absent inside userns"
[[ -c /dev/net/tun ]] || usage_error "/dev/net/tun is absent"
for command_name in ip mount umount nsenter timeout; do
  command -v "${command_name}" >/dev/null 2>&1 || usage_error "missing command: ${command_name}"
done
[[ -x /usr/bin/python3 ]] || usage_error "missing /usr/bin/python3"

mount_active=0
root_tun=""
nested_name=""
declare -a process_names=()
declare -a process_pids=()
declare -a process_pgids=()
started_pid=""

process_running()
{
  local pid="$1"
  local state
  state="$(ps -o stat= -p "${pid}" 2>/dev/null | awk '{print $1}')"
  [[ -n "${state}" && "${state:0:1}" != "Z" ]]
}

stop_group()
{
  local index="$1"
  local pid="${process_pids[index]}"
  local pgid="${process_pgids[index]}"
  local signal deadline
  [[ "${pid}" =~ ^[1-9][0-9]*$ && "${pgid}" =~ ^[1-9][0-9]*$ && "${pgid}" -gt 1 ]] || return 0
  process_running "${pid}" || return 0
  for signal in INT TERM KILL; do
    kill -s "${signal}" -- "-${pgid}" >/dev/null 2>&1 || true
    deadline=$((SECONDS + 4))
    while process_running "${pid}" && [[ "${SECONDS}" -lt "${deadline}" ]]; do
      sleep 0.1
    done
    process_running "${pid}" || break
  done
  if process_running "${pid}"; then
    printf 'error: %s pid=%s remains alive after bounded KILL\n' \
      "${process_names[index]}" "${pid}" >&2
    return 124
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
  # Stop Broker admission first while both radio requesters are still alive.
  # Then stop the radio peers and finally their core/database dependencies.
  for wanted in broker srsue gnb open5gs mongod; do
    for ((index=0; index<${#process_pids[@]}; index++)); do
      if [[ "${process_names[index]}" == "${wanted}" ]]; then
        if stop_group "${index}"; then
          process_pids[index]="0"
        else
          cleanup_failed=1
        fi
      fi
    done
  done
  for ((index=${#process_pids[@]}-1; index>=0; index--)); do
    stop_group "${index}" || cleanup_failed=1
  done
  if [[ -n "${nested_name}" ]]; then
    ip netns del "${nested_name}" >/dev/null 2>&1 || true
  fi
  if [[ -n "${root_tun}" ]]; then
    ip link del "${root_tun}" >/dev/null 2>&1 || true
  fi
  if [[ "${mount_active}" -eq 1 ]]; then
    umount /run/netns >/dev/null 2>&1 || true
  fi
  if [[ "${cleanup_failed}" -ne 0 && "${original_status}" -eq 0 ]]; then
    original_status=124
  fi
  exit "${original_status}"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

prepare_namespace()
{
  mount --make-rprivate /
  mount --bind "${netns_dir}" /run/netns
  mount_active=1
  ip link set lo up
}

run_probe()
{
  [[ -x "${hardware_probe}" ]] || usage_error "hardware probe is missing"
  [[ -x "${probe_broker}" ]] || usage_error "probe Broker is missing"
  [[ "${probe_config}" == /* && -f "${probe_config}" && ! -L "${probe_config}" ]] || \
    usage_error "probe topology is missing or unsafe"
  prepare_namespace
  /usr/bin/python3 -c 'import socket; s=socket.socket(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_SCTP); s.bind(("127.0.0.1", 0)); s.close()'
  root_tun="ogstun_probe"
  ip tuntap add dev "${root_tun}" mode tun
  ip addr add 10.254.0.1/30 dev "${root_tun}"
  ip link set "${root_tun}" up
  nested_name="ue1probe"
  ip netns add "${nested_name}"
  nsenter --net="/run/netns/${nested_name}" -- ip link set lo up
  nsenter --net="/run/netns/${nested_name}" -- ip tuntap add dev tun_srsue_probe mode tun
  nsenter --net="/run/netns/${nested_name}" -- ip link set tun_srsue_probe up
  nsenter --net="/run/netns/${nested_name}" -- ip link del tun_srsue_probe
  probe_output="$(CUDA_VISIBLE_DEVICES="${physical_gpu}" "${hardware_probe}" 2>&1)" || {
    printf '%s\n' "${probe_output}" >&2
    usage_error "CUDA hardware probe failed inside isolated userns"
  }
  printf '%s\n' "${probe_output}"
  grep -qx 'test_hardware_probe OK' <<<"${probe_output}" || \
    usage_error "CUDA hardware test did not emit its exact success token"
  broker_probe_output="$(CUDA_VISIBLE_DEVICES="${physical_gpu}" timeout -s TERM -k 2s 15s \
    "${probe_broker}" --config "${probe_config}" --duration 1ms --hardware-strict 2>&1)" || {
    printf '%s\n' "${broker_probe_output}" >&2
    usage_error "selected CUDA device Broker probe failed inside isolated userns"
  }
  printf '%s\n' "${broker_probe_output}" | grep -E \
    '^(event=start backend=cuda |event=hardware_probe ok=true device=0 |event=hardware_footprint |event=stop )'
  grep -q '^event=start backend=cuda ' <<<"${broker_probe_output}" || \
    usage_error "Broker probe did not start the CUDA backend"
  grep -q '^event=hardware_probe ok=true device=0 ' <<<"${broker_probe_output}" || \
    usage_error "Broker probe did not validate logical CUDA device 0"
  [[ "${broker_probe_output}" != *cuda_device_channel_fallback* ]] || \
    usage_error "Broker probe used a CUDA channel fallback"
  echo "event=native_userns_primitive_probe result=pass"
}

start_group()
{
  local name="$1"
  local output="$2"
  shift 2
  setsid stdbuf -oL -eL "$@" >"${output}" 2>&1 &
  local pid="$!"
  local pgid
  pgid="$(ps -o pgid= -p "${pid}" | tr -d '[:space:]')"
  [[ "${pid}" =~ ^[1-9][0-9]*$ && "${pgid}" == "${pid}" ]] || usage_error "invalid process group for ${name}"
  process_names+=("${name}")
  process_pids+=("${pid}")
  process_pgids+=("${pgid}")
  started_pid="${pid}"
}

wait_log()
{
  local path="$1"
  local token="$2"
  local pid="$3"
  local seconds="$4"
  local deadline=$((SECONDS + seconds))
  while [[ "${SECONDS}" -lt "${deadline}" ]]; do
    grep -q -- "${token}" "${path}" 2>/dev/null && return 0
    process_running "${pid}" || return 1
    sleep 0.25
  done
  return 1
}

write_summary()
{
  local status="$1"
  local broker_status="$2"
  local rrc="$3"
  local pdu="$4"
  local ping_ok="$5"
  local rx_starvations="$6"
  local tx_queue_overflows="$7"
  local tx_sequence_gaps="$8"
  local zmq_errors="$9"
  local gnb_alive="${10}"
  local fivegc_alive="${11}"
  local srsue_alive="${12}"
  /usr/bin/python3 - "${report_dir}/attach-summary.json" "${timestamp}" "${status}" \
    "${broker_status}" "${rrc}" "${pdu}" "${ping_ok}" "${rx_starvations}" \
    "${tx_queue_overflows}" "${tx_sequence_gaps}" "${zmq_errors}" \
    "${gnb_alive}" "${fivegc_alive}" "${srsue_alive}" \
    "${log_dir}" "${report_dir}" <<'PY'
import json
import sys

(path, timestamp, status, broker_status, rrc, pdu, ping_ok, rx_starvations,
 tx_queue_overflows, tx_sequence_gaps, zmq_errors, gnb_alive, fivegc_alive,
 srsue_alive, log_dir, report_dir) = sys.argv[1:]
data = {
    "timestamp": timestamp,
    "status": status,
    "duration_seconds": 20,
    "gate": "rank1-2x1",
    "srsran_ref": "release_23_11",
    "runtime_mode": "rootless_user_net_mount_namespace",
    "docker_used": False,
    "primitive_probe_passed": True,
    "broker_status": int(broker_status),
    "rrc_connected": int(rrc),
    "pdu_session_established": int(pdu),
    "ping_ok": int(ping_ok),
    "gnb_alive_at_broker_stop": int(gnb_alive),
    "open5gs_alive_at_broker_stop": int(fivegc_alive),
    "srsue_alive_at_broker_stop": int(srsue_alive),
    "rx_starvations": int(rx_starvations),
    "tx_queue_overflows": int(tx_queue_overflows),
    "tx_sequence_gaps": int(tx_sequence_gaps),
    "zmq_errors": int(zmq_errors),
    "log_dir": log_dir,
    "report_dir": report_dir,
}
with open(path, "x", encoding="utf-8") as output:
    json.dump(data, output, indent=2, sort_keys=True)
    output.write("\n")
PY
}

extract_counter()
{
  local key="$1"
  local line="$2"
  local value
  value="$(sed -n "s/.*${key}=\([0-9][0-9]*\).*/\1/p" <<<"${line}")"
  printf '%s\n' "${value:-0}"
}

run_stack()
{
  for required in "${native_root}" "${repo_root}" "${config_dir}" "${log_dir}" "${report_dir}"; do
    [[ "${required}" == /* && -d "${required}" && ! -L "${required}" ]] || usage_error "invalid run directory: ${required}"
  done
  local gnb="${native_root}/builds/ocudu-zmq-release/apps/gnb/gnb"
  local srsue="${native_root}/builds/srsran4g-zmq-release/srsue/src/srsue"
  local fivegc="${native_root}/builds/open5gs-v2.7.6/tests/app/5gc"
  local mongod="${native_root}/install/mongodb-6.0.29/bin/mongod"
  local broker="${native_root}/builds/ocudu-gpu-channel-cuda-release/ocudu-gpu-channel"
  local add_users="${native_root}/src/ocudu/docker/open5gs/add_users.py"
  local subscriber_verify="${repo_root}/scripts/native/verify-open5gs-subscriber.py"
  for binary in "${gnb}" "${srsue}" "${fivegc}" "${mongod}" "${broker}"; do
    [[ -x "${binary}" ]] || usage_error "missing executable: ${binary}"
  done

  prepare_namespace
  root_tun="ogstun"
  ip tuntap add dev "${root_tun}" mode tun
  # Open5GS advertises 10.45.0.1 for the dynamic pool, while the immutable
  # legacy subscriber is pinned to 10.45.1.2 and uses 10.45.1.1 as gateway.
  ip addr add 10.45.0.1/24 dev "${root_tun}"
  ip addr add 10.45.1.1/24 dev "${root_tun}"
  ip link set "${root_tun}" up
  nested_name="ue1"
  ip netns add "${nested_name}"
  nsenter --net="/run/netns/${nested_name}" -- ip link set lo up

  local data_dir="${native_root}/data/ocudu-rank1-2x1-native/${timestamp}"
  local mongod_pid fivegc_pid broker_pid gnb_pid srsue_pid broker_index broker_exit_deadline
  start_group mongod "${log_dir}/mongod-console.log" "${mongod}" --dbpath "${data_dir}" --bind_ip 127.0.0.1 --port 27017 --logpath "${log_dir}/mongod.log"
  mongod_pid="${started_pid}"
  /usr/bin/python3 - "${mongod_pid}" <<'PY'
import socket
import sys
import time
pid = int(sys.argv[1])
deadline = time.monotonic() + 20
while time.monotonic() < deadline:
    try:
        with socket.create_connection(("127.0.0.1", 27017), timeout=.25):
            raise SystemExit(0)
    except OSError:
        time.sleep(.25)
raise SystemExit(2)
PY
  # PYTHONDONTWRITEBYTECODE: add_users imports Open5GS.py straight out of the
  # pinned open5gs checkout, and the __pycache__ CPython would drop there makes
  # that checkout dirty -- which fails the workspace lock on the NEXT run, so
  # the gate could only ever be run once. Suppressing bytecode keeps the
  # audited source tree byte-identical across runs.
  PYTHONDONTWRITEBYTECODE=1 \
  PYTHONPATH="${native_root}/src/open5gs:${PYTHONPATH:-}" /usr/bin/python3 "${add_users}" \
    --mongodb 127.0.0.1 --mongodb_port 27017 --subscriber_data "${config_dir}/subscriber.csv" \
    >"${log_dir}/subscriber-insert.log" 2>&1
  /usr/bin/python3 "${subscriber_verify}" --subscriber-csv "${config_dir}/subscriber.csv" \
    >"${log_dir}/subscriber-verify.log" 2>&1

  start_group open5gs "${log_dir}/open5gs.log" "${fivegc}" -c "${config_dir}/open5gs.yaml"
  fivegc_pid="${started_pid}"
  /usr/bin/python3 - <<'PY'
import socket
import time
deadline = time.monotonic() + 30
while time.monotonic() < deadline:
    try:
        with socket.create_connection(("127.0.0.20", 7777), timeout=.25):
            raise SystemExit(0)
    except OSError:
        time.sleep(.25)
raise SystemExit(2)
PY
  mkdir -p "${report_dir}/wire-capture"
  start_group broker "${log_dir}/broker.log" env CUDA_VISIBLE_DEVICES="${physical_gpu}" "${broker}" --config "${config_dir}/topology.yaml" --duration 20s \
    --wire-capture-dir "${report_dir}/wire-capture" --wire-capture-samples 11520000 --wire-capture-skip 138240000
  broker_pid="${started_pid}"
  broker_index=$((${#process_pids[@]} - 1))
  # Absolute bound: the fixed 15-second run plus ten seconds for grouped
  # drain and orderly worker shutdown, independent of how quickly UE attach
  # and ping complete.
  broker_exit_deadline=$((SECONDS + 30))
  wait_log "${log_dir}/broker.log" 'event=radio_node_resolved id=ue0' "${broker_pid}" 15 || usage_error "broker did not become ready"
  start_group gnb "${log_dir}/gnb-console.log" "${gnb}" -c "${config_dir}/gnb.yaml"
  gnb_pid="${started_pid}"
  wait_log "${log_dir}/gnb-console.log" '==== gNB started ===' "${gnb_pid}" 15 || usage_error "gNB did not start"
  sleep 3
  start_group srsue "${log_dir}/srsue.log" "${srsue}" "${config_dir}/srsue.conf"
  srsue_pid="${started_pid}"

  local deadline=$((SECONDS + 20))
  local rrc=0 pdu=0 ping_ok=0
  while [[ "${SECONDS}" -lt "${deadline}" ]] && process_running "${srsue_pid}"; do
    grep -q 'RRC Connected' "${log_dir}/srsue.log" 2>/dev/null && rrc=1
    grep -q 'PDU Session Establishment successful' "${log_dir}/srsue.log" 2>/dev/null && pdu=1
    [[ "${rrc}" -eq 1 && "${pdu}" -eq 1 ]] && break
    sleep 0.5
  done
  if [[ "${rrc}" -eq 1 && "${pdu}" -eq 1 ]]; then
    if nsenter --net=/run/netns/ue1 -- ping -I tun_srsue -c 60 -i 0.1 -W 2 10.45.1.1 >"${log_dir}/ue-ping.log" 2>&1; then
      ping_ok=1
    fi
  fi

  while process_running "${broker_pid}" && [[ "${SECONDS}" -lt "${broker_exit_deadline}" ]]; do
    sleep 0.1
  done
  local broker_status
  if process_running "${broker_pid}"; then
    printf 'error: broker exceeded its bounded natural-exit window\n' >&2
    stop_group "${broker_index}" || true
    broker_status=124
  else
    set +e
    wait "${broker_pid}"
    broker_status="$?"
    set -e
  fi
  process_pids[broker_index]="0"
  local gnb_alive=0 fivegc_alive=0 srsue_alive=0
  process_running "${gnb_pid}" && gnb_alive=1
  process_running "${fivegc_pid}" && fivegc_alive=1
  process_running "${srsue_pid}" && srsue_alive=1
  local stop_line rx_starvations tx_queue_overflows tx_sequence_gaps zmq_errors status
  stop_line="$(grep '^event=stop ' "${log_dir}/broker.log" | tail -n 1 || true)"
  rx_starvations="$(extract_counter rx_starvations "${stop_line}")"
  tx_queue_overflows="$(extract_counter tx_queue_overflows "${stop_line}")"
  tx_sequence_gaps="$(extract_counter tx_sequence_gaps "${stop_line}")"
  zmq_errors="$(extract_counter zmq_errors "${stop_line}")"
  status="passed"
  if [[ "${broker_status}" -ne 0 || "${tx_queue_overflows}" -ne 0 || "${tx_sequence_gaps}" -ne 0 || "${zmq_errors}" -ne 0 ]]; then
    status="broker_failed"
  elif [[ "${gnb_alive}" -ne 1 || "${fivegc_alive}" -ne 1 || "${srsue_alive}" -ne 1 ]]; then
    status="stack_process_exited_before_broker_stop"
  elif [[ "${rrc}" -ne 1 || "${pdu}" -ne 1 ]]; then
    status="ue_stack_blocker_no_attach"
  elif [[ "${ping_ok}" -ne 1 ]]; then
    status="ue_stack_blocker_ping_failed"
  fi
  write_summary "${status}" "${broker_status}" "${rrc}" "${pdu}" "${ping_ok}" \
    "${rx_starvations}" "${tx_queue_overflows}" "${tx_sequence_gaps}" "${zmq_errors}" \
    "${gnb_alive}" "${fivegc_alive}" "${srsue_alive}"
  [[ "${status}" == "passed" ]]
}

if [[ "${mode}" == "probe" ]]; then
  run_probe
else
  run_stack
fi
