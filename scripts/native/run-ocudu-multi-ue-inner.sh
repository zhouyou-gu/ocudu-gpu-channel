#!/usr/bin/env bash
# Inner half of the native multi-UE attach gate. Runs INSIDE the user + network
# + mount namespace created by run-ocudu-multi-ue.sh; never invoke it directly.
#
# Structurally the multi-UE sibling of run-ocudu-legacy-1x1-inner.sh: same
# rootless namespace layout, same process supervision, same bounded shutdown.
# The differences are all UE multiplicity -- one nested netns and one srsUE per
# UE, a two-record subscriber database, a three-device broker topology, and a
# ping from every UE namespace.
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
    *) usage_error "unexpected argument: $1" ;;
  esac
done

for value in "${repo_root}" "${native_root}" "${config_dir}" "${log_dir}" \
             "${netns_dir}" "${timestamp}" "${parent_netns}" "${parent_mntns}" "${outer_uid}"; do
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

# Must agree with scripts/native/render-multi-ue-configs.py UES.
ue_ids=(ue0 ue1)
ue_netns=(ue1 ue2)
ue_ipv4=(10.45.1.2 10.45.1.3)
ue_gateway="10.45.1.1"

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
  setsid stdbuf -oL -eL "$@" >"${output}" 2>&1 &
  started_pid="$!"
  process_names+=("${name}")
  process_pids+=("${started_pid}")
  process_pgids+=("${started_pid}")
}

stop_group()
{
  local index="$1"
  local pid="${process_pids[index]}" pgid="${process_pgids[index]}"
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
    printf 'error: %s pid=%s remains alive after bounded KILL\n' "${process_names[index]}" "${pid}" >&2
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
  # Stop broker admission first while both radio requesters are still alive,
  # then the radio peers, then their core/database dependencies.
  for wanted in broker srsue gnb open5gs mongod; do
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

gnb="${native_root}/builds/ocudu-zmq-release/apps/gnb/gnb"
srsue="${native_root}/builds/srsran4g-zmq-release/srsue/src/srsue"
fivegc="${native_root}/builds/open5gs-v2.7.6/tests/app/5gc"
mongod="${native_root}/install/mongodb-6.0.29/bin/mongod"
broker="${channel_build:-${native_root}/builds/ocudu-gpu-channel-cuda-release}/ocudu-gpu-channel"
add_users="${native_root}/src/ocudu/docker/open5gs/add_users.py"
subscriber_verify="${repo_root}/scripts/native/verify-open5gs-subscribers-multi.py"
data_dir="${native_root}/data/ocudu-multi-ue-native/${timestamp}"

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
start_group broker "${log_dir}/broker.log" \
  env CUDA_VISIBLE_DEVICES="${physical_gpu}" "${broker}" --config "${config_dir}/topology.yaml" --duration 240s
broker_pid="${started_pid}"
broker_index=$((${#process_pids[@]} - 1))
# The fixed 240 s run plus ten seconds for grouped drain and orderly shutdown.
# It must exceed staggered attach + per-UE ping, or a ping races the broker exit.
broker_exit_deadline=$((SECONDS + 250))
# Every UE row must resolve before the gNB is admitted, otherwise a UE could
# attach against a broker that has not finished standing its node up.
# event=socket_ready is used rather than event=radio_node_resolved because the
# latter only exists from M0.5 onward, and this gate has to be runnable against
# an older revision to tell a live regression apart from pre-existing behaviour.
# Both lines mean the same thing here: that node's transport is bound.
for id in "${ue_ids[@]}"; do
  wait_log "${log_dir}/broker.log" "event=socket_ready device=${id}" "${broker_pid}" 15 \
    || usage_error "broker did not bind ${id}"
done

start_group gnb "${log_dir}/gnb-console.log" "${gnb}" -c "${config_dir}/gnb.yaml"
gnb_pid="${started_pid}"
wait_log "${log_dir}/gnb-console.log" '==== gNB started ===' "${gnb_pid}" 15 || usage_error "gNB did not start"
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
  if [[ "${index}" -gt 0 ]]; then
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
  start_group "srsue-${id}" "${log_dir}/srsue-${id}.log" "${srsue}" "${config_dir}/srsue-${id}.conf"
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

gnb_alive=0; process_running "${gnb_pid}" && gnb_alive=1
srsue_alive=1
for pid in "${srsue_pids[@]}"; do process_running "${pid}" || srsue_alive=0; done

/usr/bin/python3 - \
  "${log_dir}" "${native_root}/results/reports/ocudu-multi-ue/${timestamp}/attach-summary.json" \
  "${timestamp}" "${broker_status}" "${gnb_alive}" "${srsue_alive}" \
  "${rrc[*]}" "${pdu[*]}" "${ping_ok[*]}" "${ue_ids[*]}" <<'PY'
import json, pathlib, re, sys

(log_dir, out_path, timestamp, broker_status, gnb_alive, srsue_alive,
 rrc, pdu, ping_ok, ue_ids) = sys.argv[1:]

log_dir = pathlib.Path(log_dir)
stop = ""
for line in (log_dir / "broker.log").read_text(encoding="utf-8", errors="replace").splitlines():
    if line.startswith("event=stop "):
        stop = line
counters = {k: int(v) for k, v in re.findall(r"(\w+)=(\d+)", stop)}

ids = ue_ids.split()
per_ue = {
    ue: {
        "rrc_connected": int(r),
        "pdu_session_established": int(p),
        "ping_ok": int(q),
    }
    for ue, r, p, q in zip(ids, rrc.split(), pdu.split(), ping_ok.split())
}
all_attached = all(v["rrc_connected"] and v["pdu_session_established"] and v["ping_ok"]
                   for v in per_ue.values())
strict_clean = all(counters.get(k, 1) == 0
                   for k in ("tx_queue_overflows", "tx_sequence_gaps", "zmq_errors"))
summary = {
    "timestamp": timestamp,
    "docker_used": False,
    "runtime_mode": "rootless_user_net_mount_namespace",
    "ue_count": len(ids),
    "per_ue": per_ue,
    "broker_status": int(broker_status),
    "gnb_alive_at_broker_stop": int(gnb_alive),
    "srsue_alive_at_broker_stop": int(srsue_alive),
    "log_dir": str(log_dir),
    "status": "passed" if (all_attached and strict_clean and int(broker_status) == 0
                           and int(gnb_alive) == 1 and int(srsue_alive) == 1) else "failed",
}
summary.update({k: counters.get(k, -1) for k in
                ("tx_pulls", "rx_requests", "rx_starvations",
                 "tx_queue_overflows", "tx_sequence_gaps", "zmq_errors")})
out = pathlib.Path(out_path)
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print(f"summary={out}")
raise SystemExit(0 if summary["status"] == "passed" else 1)
PY
