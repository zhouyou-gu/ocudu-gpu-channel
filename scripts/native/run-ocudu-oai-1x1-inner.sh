#!/usr/bin/env bash
set -euo pipefail

# Inner runner for the native OAI nrUE 1x1 attach gate (M6.2). Runs inside a
# rootless user+net+mount namespace created by run-ocudu-oai-1x1.sh.
#
# Differences against run-ocudu-legacy-1x1-inner.sh, all of them UE-side:
#   - the OAI nrUE has no netns config option, so the WHOLE process runs
#     inside the nested ue1 namespace and a veth pair carries its ZMQ path
#     to the broker in the gate namespace (10.201.0.1 <-> 10.201.0.2);
#   - the attach verdict tokens are OAI's ("State = NR_RRC_CONNECTED",
#     "Received PDU Session Establishment Accept"), the TUN interface is
#     OAI's (oaitun_ue1), and the ping runs against it inside ue1;
#   - the broker window is 25 s (OAI cold sync + RA is slower than srsUE's).

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
for command_name in ip mount umount nsenter timeout setpriv; do
  command -v "${command_name}" >/dev/null 2>&1 || usage_error "missing command: ${command_name}"
done
[[ -x /usr/bin/python3 ]] || usage_error "missing /usr/bin/python3"

mount_active=0
root_tun=""
root_veth=""
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
  for wanted in broker nrue gnb open5gs mongod; do
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
  if [[ -n "${root_veth}" ]]; then
    ip link del "${root_veth}" >/dev/null 2>&1 || true
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
  # The OAI gate additionally needs a veth crossing into the nested namespace;
  # prove the primitive here before any stack process exists.
  root_veth="vethoaiprobe"
  ip link add "${root_veth}" type veth peer name vethoaiprobep
  ip link set vethoaiprobep netns "${nested_name}"
  ip addr add 10.254.1.1/30 dev "${root_veth}"
  ip link set "${root_veth}" up
  nsenter --net="/run/netns/${nested_name}" -- ip addr add 10.254.1.2/30 dev vethoaiprobep
  nsenter --net="/run/netns/${nested_name}" -- ip link set vethoaiprobep up
  nsenter --net="/run/netns/${nested_name}" -- ping -c 1 -W 2 10.254.1.1 >/dev/null || \
    usage_error "veth path between gate namespace and nested UE namespace is not routable"
  nsenter --net="/run/netns/${nested_name}" -- ip tuntap add dev oaitun_probe mode tun
  nsenter --net="/run/netns/${nested_name}" -- ip link set oaitun_probe up
  nsenter --net="/run/netns/${nested_name}" -- ip link del oaitun_probe
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
  local nrue_alive="${12}"
  /usr/bin/python3 - "${report_dir}/attach-summary.json" "${timestamp}" "${status}" \
    "${broker_status}" "${rrc}" "${pdu}" "${ping_ok}" "${rx_starvations}" \
    "${tx_queue_overflows}" "${tx_sequence_gaps}" "${zmq_errors}" \
    "${gnb_alive}" "${fivegc_alive}" "${nrue_alive}" \
    "${log_dir}" "${report_dir}" <<'PY'
import json
import sys

(path, timestamp, status, broker_status, rrc, pdu, ping_ok, rx_starvations,
 tx_queue_overflows, tx_sequence_gaps, zmq_errors, gnb_alive, fivegc_alive,
 nrue_alive, log_dir, report_dir) = sys.argv[1:]
data = {
    "timestamp": timestamp,
    "status": status,
    "duration_seconds": 25,
    "ue": "oai-nrue",
    "oai_ref": "2026.w33",
    "runtime_mode": "rootless_user_net_mount_namespace",
    "docker_used": False,
    "primitive_probe_passed": True,
    "broker_status": int(broker_status),
    "rrc_connected": int(rrc),
    "pdu_session_established": int(pdu),
    "ping_ok": int(ping_ok),
    "gnb_alive_at_broker_stop": int(gnb_alive),
    "open5gs_alive_at_broker_stop": int(fivegc_alive),
    "nrue_alive_at_broker_stop": int(nrue_alive),
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
  local nrue="${native_root}/builds/oai-zmq-release/nr-uesoftmodem"
  local oai_build="${native_root}/builds/oai-zmq-release"
  local fivegc="${native_root}/builds/open5gs-v2.7.6/tests/app/5gc"
  local mongod="${native_root}/install/mongodb-6.0.29/bin/mongod"
  local broker="${native_root}/builds/ocudu-gpu-channel-cuda-release/ocudu-gpu-channel"
  local add_users="${native_root}/src/ocudu/docker/open5gs/add_users.py"
  local subscriber_verify="${repo_root}/scripts/native/verify-open5gs-subscriber.py"
  for binary in "${gnb}" "${nrue}" "${fivegc}" "${mongod}" "${broker}"; do
    [[ -x "${binary}" ]] || usage_error "missing executable: ${binary}"
  done
  [[ -f "${oai_build}/liboai_zmqdevif.so" ]] || usage_error "missing OAI ZMQ radio module"
  local uecap_file="${native_root}/src/oai/targets/PROJECTS/GENERIC-NR-5GC/CONF/uecap_ports1.xml"
  [[ -f "${uecap_file}" ]] || usage_error "missing OAI UE capability file: ${uecap_file}"

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
  # The OAI nrUE lives entirely inside ue1; this veth pair is its ZMQ path to
  # the broker. The addresses are the gate contract shared with the renderer.
  root_veth="vethoai"
  ip link add "${root_veth}" type veth peer name vethoaip
  ip link set vethoaip netns "${nested_name}"
  ip addr add 10.201.0.1/30 dev "${root_veth}"
  ip link set "${root_veth}" up
  nsenter --net="/run/netns/${nested_name}" -- ip addr add 10.201.0.2/30 dev vethoaip
  nsenter --net="/run/netns/${nested_name}" -- ip link set vethoaip up

  local data_dir="${native_root}/data/ocudu-oai-1x1-native/${timestamp}"
  local mongod_pid fivegc_pid broker_pid gnb_pid nrue_pid broker_index broker_exit_deadline
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
  # pinned open5gs checkout, and a dropped __pycache__ would make the audited
  # checkout dirty and fail the workspace lock on the NEXT run.
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
  start_group broker "${log_dir}/broker.log" env CUDA_VISIBLE_DEVICES="${physical_gpu}" "${broker}" --config "${config_dir}/topology.yaml" --duration 25s
  broker_pid="${started_pid}"
  broker_index=$((${#process_pids[@]} - 1))
  # Absolute bound: the fixed 25-second run plus ten seconds for grouped
  # drain and orderly worker shutdown, independent of how quickly UE attach
  # and ping complete.
  broker_exit_deadline=$((SECONDS + 35))
  wait_log "${log_dir}/broker.log" 'event=radio_node_resolved id=ue0' "${broker_pid}" 15 || usage_error "broker did not become ready"
  start_group gnb "${log_dir}/gnb-console.log" "${gnb}" -c "${config_dir}/gnb.yaml"
  gnb_pid="${started_pid}"
  wait_log "${log_dir}/gnb-console.log" '==== gNB started ===' "${gnb_pid}" 15 || usage_error "gNB did not start"
  sleep 3
  # Cell identity: band 3 FDD, DL 1842.5 MHz, 106 PRB at 15 kHz. The SSB
  # start subcarrier is offsetToPointA * 12 + k_SSB = 40 * 12 + 6 = 486, read
  # from the audited gNB's own derivation log. -E selects the 3/4 FFT
  # (1536-point) so the UE samples at the gNB's 23.04 MS/s.
  #
  # --CO -95000000 is NOT cosmetic: OAI initialises its 38.211 upconversion
  # phase compensation from the command-line UL carrier (DL + CO) BEFORE SIB1,
  # and never re-derives it. With the default CO=0 the UE compensates UL
  # symbols at the DL frequency while the gNB expects the band-3 UL (DL - 95
  # MHz); the mismatch surfaced as a constant apparent +817 Hz UL CFO and
  # every Msg3 failed at sinr about -7 dB on a clean direct control run.
  #
  # --uecap_file must be given absolutely: the OAI default is the RELATIVE
  # "./uecap_ports1.xml", and without the capability featureset the UE's
  # fallback maxMIMO layers is 0, which is an AssertFatal the moment a DCI
  # 1_1 arrives. ports1 = 1 DL layer, matching this gate's 1T1R scope.
  #
  # setpriv drops CAP_SYS_NICE from the bounding set: inside the rootless
  # userns the capability is visible but namespace-scoped, so OAI's
  # threadCreate() sees it, requests SCHED_FIFO, and the kernel refuses with
  # EPERM -- an AssertFatal abort. With the capability absent, OAI takes its
  # own graceful default-priority path (the same one it takes for any
  # unprivileged user outside a namespace).
  start_group nrue "${log_dir}/nrue.log" nsenter --net="/run/netns/${nested_name}" -- \
    setpriv --bounding-set -sys_nice \
    "${nrue}" -O "${config_dir}/nrue.conf" \
    -E -r 106 --numerology 0 --band 3 -C 1842500000 --ssb 486 --CO -95000000 \
    --ue-fo-compensation \
    --uecap_file "${uecap_file}" \
    --device.name oai_zmqdevif \
    --loader.oai_zmqdevif.shlibpath "${oai_build}" \
    --zmq.'[0]'.tx_channels tcp://10.201.0.2:2101 \
    --zmq.'[0]'.rx_channels tcp://10.201.0.1:2100
  nrue_pid="${started_pid}"

  local deadline=$((SECONDS + 30))
  local rrc=0 pdu=0 ping_ok=0
  while [[ "${SECONDS}" -lt "${deadline}" ]] && process_running "${nrue_pid}"; do
    grep -q 'State = NR_RRC_CONNECTED' "${log_dir}/nrue.log" 2>/dev/null && rrc=1
    grep -q 'Received PDU Session Establishment Accept' "${log_dir}/nrue.log" 2>/dev/null && pdu=1
    [[ "${rrc}" -eq 1 && "${pdu}" -eq 1 ]] && break
    sleep 0.5
  done
  if [[ "${rrc}" -eq 1 && "${pdu}" -eq 1 ]]; then
    # The PDU accept token precedes the TUN configuration; wait for the
    # interface to hold the pinned subscriber address before pinging.
    local tun_deadline=$((SECONDS + 10))
    while [[ "${SECONDS}" -lt "${tun_deadline}" ]]; do
      nsenter --net=/run/netns/ue1 -- ip -4 addr show dev oaitun_ue1 2>/dev/null | grep -q '10\.45\.1\.2' && break
      sleep 0.5
    done
    if nsenter --net=/run/netns/ue1 -- ping -I oaitun_ue1 -c 3 -W 2 10.45.1.1 >"${log_dir}/ue-ping.log" 2>&1; then
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
  local gnb_alive=0 fivegc_alive=0 nrue_alive=0
  process_running "${gnb_pid}" && gnb_alive=1
  process_running "${fivegc_pid}" && fivegc_alive=1
  process_running "${nrue_pid}" && nrue_alive=1
  local stop_line rx_starvations tx_queue_overflows tx_sequence_gaps zmq_errors status
  stop_line="$(grep '^event=stop ' "${log_dir}/broker.log" | tail -n 1 || true)"
  rx_starvations="$(extract_counter rx_starvations "${stop_line}")"
  tx_queue_overflows="$(extract_counter tx_queue_overflows "${stop_line}")"
  tx_sequence_gaps="$(extract_counter tx_sequence_gaps "${stop_line}")"
  zmq_errors="$(extract_counter zmq_errors "${stop_line}")"
  status="passed"
  if [[ "${broker_status}" -ne 0 || "${tx_queue_overflows}" -ne 0 || "${tx_sequence_gaps}" -ne 0 || "${zmq_errors}" -ne 0 ]]; then
    status="broker_failed"
  elif [[ "${gnb_alive}" -ne 1 || "${fivegc_alive}" -ne 1 || "${nrue_alive}" -ne 1 ]]; then
    status="stack_process_exited_before_broker_stop"
  elif [[ "${rrc}" -ne 1 || "${pdu}" -ne 1 ]]; then
    status="ue_stack_blocker_no_attach"
  elif [[ "${ping_ok}" -ne 1 ]]; then
    status="ue_stack_blocker_ping_failed"
  fi
  write_summary "${status}" "${broker_status}" "${rrc}" "${pdu}" "${ping_ok}" \
    "${rx_starvations}" "${tx_queue_overflows}" "${tx_sequence_gaps}" "${zmq_errors}" \
    "${gnb_alive}" "${fivegc_alive}" "${nrue_alive}"
  [[ "${status}" == "passed" ]]
}

if [[ "${mode}" == "probe" ]]; then
  run_probe
else
  run_stack
fi
