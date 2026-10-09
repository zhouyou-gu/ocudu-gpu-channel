#!/usr/bin/env bash
set -euo pipefail

# Inner runner for the native OAI nrUE 2x2 SU-MIMO gate (M6.3). Runs inside a
# rootless user+net+mount namespace created by run-ocudu-oai-2x2.sh.
#
# Same namespace layout as the OAI 1x1 gate (nested ue1 namespace, veth pair
# 10.201.0.1 <-> 10.201.0.2, Open5GS + MongoDB in the gate namespace). What
# changes:
#   - the UE runs 2 RX / 2 TX antennas with the ports2 capability file and two
#     ZMQ channels per direction;
#   - OAI2X2_PATH=broker puts the CUDA broker (2x2 topology) between gNB and
#     UE; OAI2X2_PATH=direct wires the gNB straight to the UE over the veth
#     (identity channel, no emulator);
#   - after attach it runs a ping and a downlink iperf3 (UDP) so rank and
#     throughput can be read from the same run.

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
  # The outer gate holds its flock on OCUDU_NATIVE_GATE_LOCK_FD. Close it in
  # the child: a process that outlives teardown must not keep the lock (a
  # stray open5gs-scpd once refused seven later runs, S15).
  local gate_lock_fd="${OCUDU_NATIVE_GATE_LOCK_FD:-}"
  if [[ "${gate_lock_fd}" =~ ^[0-9]+$ && "${gate_lock_fd}" -gt 2 ]]; then
    setsid stdbuf -oL -eL "$@" >"${output}" 2>&1 {gate_lock_fd}>&- &
  else
    setsid stdbuf -oL -eL "$@" >"${output}" 2>&1 &
  fi
  local pid="$!"
  local pgid
  # setsid runs in the background child; poll until it has made the group.
  for _ in $(seq 1 50); do
    pgid="$(ps -o pgid= -p "${pid}" | tr -d '[:space:]')"
    [[ "${pgid}" == "${pid}" ]] && break
    sleep 0.02
  done
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
  local path_mode="${OAI2X2_PATH:?}"
  local broker_seconds="${OAI2X2_BROKER_SECONDS:-90}"
  local attach_seconds="${OAI2X2_ATTACH_SECONDS:-45}"
  local iperf_seconds="${OAI2X2_IPERF_SECONDS:-15}"
  local iperf_rate="${OAI2X2_IPERF_RATE:-80M}"
  local iperf_dir="${OAI2X2_IPERF_DIR:-dl}"
  local iperf_ul_rate="${OAI2X2_IPERF_UL_RATE:-60M}"
  [[ "${iperf_dir}" == dl || "${iperf_dir}" == ul || "${iperf_dir}" == both ]] || usage_error "OAI2X2_IPERF_DIR must be dl, ul or both"
  local oai_build="${OAI2X2_NRUE_DIR:-${native_root}/builds/oai-zmq-release}"
  local gnb="${OCUDU_NATIVE_GNB_BINARY:-${native_root}/builds/ocudu-zmq-release/apps/gnb/gnb}"
  # Platform CPU placement and profiler wrappers, as in the OAI 1x1 gate: the
  # outer script resolves OCUDU_NATIVE_{GNB,BROKER,NRUE}_CPUS (empty on a host
  # without a profile, which leaves the scheduler alone); the *_WRAPPER knobs
  # prefix a command with a script that ends in `exec ... "$@"`.
  local broker_pin=() gnb_pin=() nrue_pin=()
  [[ -n "${OCUDU_NATIVE_BROKER_CPUS:-}" ]] && broker_pin=(taskset -c "${OCUDU_NATIVE_BROKER_CPUS}")
  [[ -n "${OCUDU_NATIVE_GNB_CPUS:-}" ]] && gnb_pin=(taskset -c "${OCUDU_NATIVE_GNB_CPUS}")
  [[ -n "${OCUDU_NATIVE_NRUE_CPUS:-}" ]] && nrue_pin=(taskset -c "${OCUDU_NATIVE_NRUE_CPUS}")
  # The broker's --duration clock starts with the broker; a CUDA gNB's device
  # initialisation (~20 s) is added on top of the window.
  local startup_allowance="${OCUDU_NATIVE_BROKER_STARTUP_ALLOWANCE_SECONDS:-0}"
  local nrue="${oai_build}/nr-uesoftmodem"
  local fivegc="${native_root}/builds/open5gs-v2.7.6/tests/app/5gc"
  local mongod="${native_root}/install/mongodb-6.0.29/bin/mongod"
  local broker="${OAI2X2_BROKER_BIN:?}"
  local add_users="${native_root}/src/ocudu/docker/open5gs/add_users.py"
  local subscriber_verify="${repo_root}/scripts/native/verify-open5gs-subscriber.py"
  for binary in "${gnb}" "${nrue}" "${fivegc}" "${mongod}" "${broker}"; do
    [[ -x "${binary}" ]] || usage_error "missing executable: ${binary}"
  done
  # The outer script resolves the ZMQ radio module (oai-local-patches.sh; the S9
  # reply-poll patched build by default) and exports OCUDU_NATIVE_OAI_SHLIBPATH;
  # the nrUE binary stays ${oai_build}'s.
  local zmq_module_dir="${OCUDU_NATIVE_OAI_SHLIBPATH:-${oai_build}}"
  [[ -f "${zmq_module_dir}/liboai_zmqdevif.so" ]] || usage_error "missing OAI ZMQ radio module"
  # ports2 = the UE advertises 2 DL MIMO layers (maxMIMO-Layers 2) and 2-port
  # SRS/PUSCH; OAI's own 2x2 ZMQ CI job uses this same file.
  local uecap_file="${native_root}/src/oai/targets/PROJECTS/GENERIC-NR-5GC/CONF/uecap_ports2.xml"
  # OAI2X2_UL_MAX_RANK: the renderer wrote a band-3, 2-layer-PUSCH derivative.
  [[ -f "${config_dir}/uecap.xml" ]] && uecap_file="${config_dir}/uecap.xml"
  [[ -f "${uecap_file}" ]] || usage_error "missing OAI UE capability file: ${uecap_file}"

  prepare_namespace
  root_tun="ogstun"
  ip tuntap add dev "${root_tun}" mode tun
  ip addr add 10.45.0.1/24 dev "${root_tun}"
  ip addr add 10.45.1.1/24 dev "${root_tun}"
  ip link set "${root_tun}" up
  nested_name="ue1"
  ip netns add "${nested_name}"
  nsenter --net="/run/netns/${nested_name}" -- ip link set lo up
  root_veth="vethoai"
  ip link add "${root_veth}" type veth peer name vethoaip
  ip link set vethoaip netns "${nested_name}"
  ip addr add 10.201.0.1/30 dev "${root_veth}"
  ip link set "${root_veth}" up
  nsenter --net="/run/netns/${nested_name}" -- ip addr add 10.201.0.2/30 dev vethoaip
  nsenter --net="/run/netns/${nested_name}" -- ip link set vethoaip up

  local data_dir="${native_root}/data/ocudu-oai-2x2-native/${timestamp}"

  local mongod_pid fivegc_pid broker_pid="" gnb_pid nrue_pid broker_index=-1
  start_group mongod "${log_dir}/mongod-console.log" "${mongod}" --dbpath "${data_dir}" --bind_ip 127.0.0.1 --port 27017 --logpath "${log_dir}/mongod.log"
  mongod_pid="${started_pid}"
  /usr/bin/python3 - <<'PY'
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
  PYTHONPATH="${native_root}/src/open5gs:${PYTHONPATH:-}" /usr/bin/python3 "${add_users}" \
    --mongodb 127.0.0.1 --mongodb_port 27017 --subscriber_data "${config_dir}/subscriber.csv" \
    >"${log_dir}/subscriber-insert.log" 2>&1
  /usr/bin/python3 "${subscriber_verify}" --subscriber-csv "${config_dir}/subscriber.csv" \
    >"${log_dir}/subscriber-verify.log" 2>&1

  start_group open5gs "${log_dir}/open5gs.log" "${fivegc}" -c "${config_dir}/open5gs.yaml"
  fivegc_pid="${started_pid}"
  /usr/bin/python3 - <<'PY'
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
  local window_deadline
  if [[ "${path_mode}" == "broker" ]]; then
    # WIRECAP in the extra broker arguments stands for this run's capture dir.
    local broker_extra="${OAI2X2_BROKER_EXTRA:-}"
    if [[ "${broker_extra}" == *WIRECAP* ]]; then
      mkdir -p "${log_dir}/wire-capture"
      broker_extra="${broker_extra//WIRECAP/${log_dir}/wire-capture}"
    fi
    # Optional broker wire capture, the OAI 1x1 gate's knobs with the 2x2
    # prefix: OCUDU_NATIVE_OAI2X2_WIRE_CAPTURE_SAMPLES (0 = off) / _SKIP_SECONDS
    # (default 2, counted from each port's first sample), into
    # <log_dir>/wire-capture (one <port>.{tx_in,rx_out}.cf32 per gNB/UE port);
    # wire-capture-power.py gives the per-port levels. X7 measured the UE's
    # port 0 at the 1x1 per-channel level and port 1 silent (1-layer UL), see
    # docs/reports/experiments/x7-oai-levels-prach/README.md; a 2 s skip here lands in the
    # connected-mode PUCCH / early DL-iperf phase, so the traffic-weighted
    # level is not comparable with the 1x1 calibration window without a
    # per-burst breakdown.
    local wire_capture_samples="${OCUDU_NATIVE_OAI2X2_WIRE_CAPTURE_SAMPLES:-0}"
    local wire_capture_skip_seconds="${OCUDU_NATIVE_OAI2X2_WIRE_CAPTURE_SKIP_SECONDS:-2}"
    [[ "${wire_capture_samples}" =~ ^(0|[1-9][0-9]*)$ && "${wire_capture_skip_seconds}" =~ ^(0|[1-9][0-9]*)$ ]] || \
      usage_error "OCUDU_NATIVE_OAI2X2_WIRE_CAPTURE_SAMPLES/_SKIP_SECONDS must be non-negative integers"
    local -a wire_capture_args=()
    if [[ "${wire_capture_samples}" -gt 0 ]]; then
      [[ "${broker_extra}" == *--wire-capture* ]] && usage_error "wire capture requested twice (OAI2X2_BROKER_EXTRA and OCUDU_NATIVE_OAI2X2_WIRE_CAPTURE_SAMPLES)"
      mkdir -p "${log_dir}/wire-capture"
      wire_capture_args=(
        --wire-capture-dir "${log_dir}/wire-capture"
        --wire-capture-samples "${wire_capture_samples}"
        --wire-capture-skip "$((wire_capture_skip_seconds * 23040000))"
      )
      printf 'event=wire_capture_requested samples=%s skip_seconds=%s dir="%s"\n' \
        "${wire_capture_samples}" "${wire_capture_skip_seconds}" "${log_dir}/wire-capture"
    fi
    # shellcheck disable=SC2086
    start_group broker "${log_dir}/broker.log" env CUDA_VISIBLE_DEVICES="${physical_gpu}" ${OCUDU_NATIVE_BROKER_ENV:-} \
      "${broker_pin[@]}" ${OCUDU_NATIVE_BROKER_WRAPPER:-} \
      "${broker}" --config "${config_dir}/topology.yaml" --duration "$((broker_seconds + startup_allowance))s" \
      "${wire_capture_args[@]}" ${broker_extra}
    broker_pid="${started_pid}"
    broker_index=$((${#process_pids[@]} - 1))
    wait_log "${log_dir}/broker.log" 'event=radio_node_resolved id=ue0' "${broker_pid}" 15 || usage_error "broker did not become ready"
  fi
  window_deadline=$((SECONDS + broker_seconds + startup_allowance))
  start_group gnb "${log_dir}/gnb-console.log" "${gnb_pin[@]}" ${OCUDU_NATIVE_GNB_WRAPPER:-} "${gnb}" -c "${config_dir}/gnb.yaml"
  gnb_pid="${started_pid}"
  # A CUDA gNB initialises its GPU pipelines before the banner and needs well
  # over 20 s (two D10 runs on the 5090 hit the 20 s limit, 2026-09-29).
  local gnb_start_default=20
  [[ "${OAI_GATE_GNB_USES_CUDA:-0}" == "1" ]] && gnb_start_default=90
  wait_log "${log_dir}/gnb-console.log" '==== gNB started ===' "${gnb_pid}" "${OCUDU_NATIVE_GNB_START_TIMEOUT_SECONDS:-${gnb_start_default}}" || usage_error "gNB did not start"
  sleep 3
  # Cell identity and the three M6.2 fixes (setpriv, --CO, absolute uecap) are
  # the 1x1 gate's; see run-ocudu-oai-1x1-inner.sh for why each is needed.
  # 2x2: --ue-nb-ant-rx/tx 2 and a comma list of two ZMQ channels per
  # direction (the syntax of OAI's ci-scripts/yaml_files/5g_zmq_radio_2x2).
  # 20 MHz band n3 FDD by default; a wider cell's renderer writes the radio
  # arguments (and, where the stock capability lacks the bandwidth, a
  # capability file) next to the configs, as for the 1x1 gate.
  local -a oai_radio=(-E -r 106 --numerology 0 --band 3 -C 1842500000 --ssb 486 --CO -95000000)
  [[ -f "${config_dir}/nrue-radio.args" ]] && read -r -a oai_radio <"${config_dir}/nrue-radio.args"
  [[ -f "${config_dir}/uecap.xml" ]] && uecap_file="${config_dir}/uecap.xml"
  printf 'event=nrue_radio_args %s uecap=%s\n' "${oai_radio[*]}" "${uecap_file}" >"${log_dir}/nrue-radio.log"
  pushd "${log_dir}" >/dev/null
  # shellcheck disable=SC2086
  # The gate's UE RX gain (oai-gate-defaults.sh oai_gate_ue_rx_gain), if any.
  local -a ue_rx_gain=()
  [[ -n "${OAI_GATE_UE_RX_GAIN_DB:-}" ]] && ue_rx_gain=(--zmq.'[0]'.rx_gain_db "${OAI_GATE_UE_RX_GAIN_DB}")
  # The gate's continuous FO compensation (oai-gate-defaults.sh oai_gate_ue_fo_comp).
  local -a ue_fo=()
  [[ -n "${OAI_GATE_UE_CONT_FO_COMP:-}" ]] && ue_fo=(--cont-fo-comp "${OAI_GATE_UE_CONT_FO_COMP}")
  printf 'event=nrue_fo_comp cont_fo_comp=%s\n' "${OAI_GATE_UE_CONT_FO_COMP:-off}" >>"${log_dir}/nrue-radio.log"
  start_group nrue "${log_dir}/nrue.log" nsenter --net="/run/netns/${nested_name}" -- \
    setpriv --bounding-set -sys_nice \
    "${nrue_pin[@]}" ${OCUDU_NATIVE_NRUE_WRAPPER:-} "${nrue}" -O "${config_dir}/nrue.conf" \
    "${oai_radio[@]}" \
    --ue-fo-compensation \
    --ue-nb-ant-rx 2 --ue-nb-ant-tx 2 \
    --uecap_file "${uecap_file}" \
    --device.name oai_zmqdevif \
    --loader.oai_zmqdevif.shlibpath "${zmq_module_dir}" \
    --zmq.'[0]'.tx_channels tcp://10.201.0.2:2101,tcp://10.201.0.2:2103 \
    --zmq.'[0]'.rx_channels tcp://10.201.0.1:2100,tcp://10.201.0.1:2102 \
    "${ue_rx_gain[@]}" "${ue_fo[@]}" \
    ${OAI2X2_UE_EXTRA:-}
  popd >/dev/null
  nrue_pid="${started_pid}"

  local deadline=$((SECONDS + attach_seconds))
  local rrc=0 pdu=0 ping_ok=0 iperf_ok=0
  while [[ "${SECONDS}" -lt "${deadline}" ]] && process_running "${nrue_pid}"; do
    grep -q 'State = NR_RRC_CONNECTED' "${log_dir}/nrue.log" 2>/dev/null && rrc=1
    grep -q 'Received PDU Session Establishment Accept' "${log_dir}/nrue.log" 2>/dev/null && pdu=1
    [[ "${rrc}" -eq 1 && "${pdu}" -eq 1 ]] && break
    sleep 0.5
  done
  local attach_elapsed="${SECONDS}"
  if [[ "${rrc}" -eq 1 && "${pdu}" -eq 1 ]]; then
    local tun_deadline=$((SECONDS + 10))
    while [[ "${SECONDS}" -lt "${tun_deadline}" ]]; do
      nsenter --net=/run/netns/ue1 -- ip -4 addr show dev oaitun_ue1 2>/dev/null | grep -q '10\.45\.1\.2' && break
      sleep 0.5
    done
    nsenter --net=/run/netns/ue1 -- ip route replace 10.45.1.1/32 dev oaitun_ue1 >/dev/null 2>&1 || true
    if nsenter --net=/run/netns/ue1 -- ping -I oaitun_ue1 -c 10 -i 0.2 -W 2 10.45.1.1 >"${log_dir}/ue-ping.log" 2>&1; then
      ping_ok=1
    fi
    if [[ "${iperf_seconds}" -gt 0 ]]; then
      local iperf_dl_ok=1 iperf_ul_ok=1
      if [[ "${iperf_dir}" == dl || "${iperf_dir}" == both ]]; then
        start_group iperf3 "${log_dir}/iperf3-server.log" iperf3 -s -B 10.45.1.1 -p 5201 -1
        sleep 0.5
        # Downlink: -R makes the server (core side) send to the UE.
        timeout $((iperf_seconds + 20)) nsenter --net=/run/netns/ue1 -- \
            iperf3 -c 10.45.1.1 -B 10.45.1.2 -p 5201 -R -u -b "${iperf_rate}" -t "${iperf_seconds}" -l 1300 --json \
            >"${log_dir}/iperf3-dl.json" 2>"${log_dir}/iperf3-dl.err" || iperf_dl_ok=0
      fi
      if [[ "${iperf_dir}" == ul || "${iperf_dir}" == both ]]; then
        start_group iperf3ul "${log_dir}/iperf3-server-ul.log" iperf3 -s -B 10.45.1.1 -p 5202 -1
        sleep 0.5
        # Uplink: the UE sends to the core side.
        timeout $((iperf_seconds + 20)) nsenter --net=/run/netns/ue1 -- \
            iperf3 -c 10.45.1.1 -B 10.45.1.2 -p 5202 -u -b "${iperf_ul_rate}" -t "${iperf_seconds}" -l 1300 --json \
            >"${log_dir}/iperf3-ul.json" 2>"${log_dir}/iperf3-ul.err" || iperf_ul_ok=0
      fi
      [[ "${iperf_dl_ok}" -eq 1 && "${iperf_ul_ok}" -eq 1 ]] && iperf_ok=1
    fi
  fi
  # Hold the window open until the configured end so the metrics cover it.
  if [[ "${path_mode}" == "broker" ]]; then
    local broker_exit_deadline=$((window_deadline + 15))
    while process_running "${broker_pid}" && [[ "${SECONDS}" -lt "${broker_exit_deadline}" ]]; do
      sleep 0.2
    done
  fi
  local gnb_alive=0 fivegc_alive=0 nrue_alive=0
  process_running "${gnb_pid}" && gnb_alive=1
  process_running "${fivegc_pid}" && fivegc_alive=1
  process_running "${nrue_pid}" && nrue_alive=1
  local broker_status=0
  if [[ "${path_mode}" == "broker" ]]; then
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
  fi
  local stop_line=""
  [[ -f "${log_dir}/broker.log" ]] && stop_line="$(grep '^event=stop ' "${log_dir}/broker.log" | tail -n 1 || true)"
  local status="passed"
  if [[ "${broker_status}" -ne 0 ]] || [[ "$(extract_counter tx_queue_overflows "${stop_line}")" -ne 0 ]] || \
     [[ "$(extract_counter tx_sequence_gaps "${stop_line}")" -ne 0 ]] || [[ "$(extract_counter zmq_errors "${stop_line}")" -ne 0 ]]; then
    status="broker_failed"
  elif [[ "${rrc}" -ne 1 || "${pdu}" -ne 1 ]]; then
    status="ue_stack_blocker_no_attach"
  elif [[ "${ping_ok}" -ne 1 ]]; then
    status="ue_stack_blocker_ping_failed"
  elif [[ "${gnb_alive}" -ne 1 || "${fivegc_alive}" -ne 1 || "${nrue_alive}" -ne 1 ]]; then
    status="stack_process_exited_before_window_end"
  fi
  /usr/bin/python3 - "${report_dir}/run-summary.json" <<PY
import json, sys
data = {
    "timestamp": "${timestamp}", "status": "${status}", "path": "${path_mode}",
    "broker_status": ${broker_status}, "rrc_connected": ${rrc}, "pdu_session_established": ${pdu},
    "ping_ok": ${ping_ok}, "iperf_ok": ${iperf_ok}, "attach_elapsed_s": ${attach_elapsed},
    "gnb_alive": ${gnb_alive}, "open5gs_alive": ${fivegc_alive}, "nrue_alive": ${nrue_alive},
    "broker_stop_line": """${stop_line}""", "log_dir": "${log_dir}", "report_dir": "${report_dir}",
}
with open(sys.argv[1], "x") as out:
    json.dump(data, out, indent=2, sort_keys=True)
    out.write("\n")
PY
  [[ "${status}" == "passed" ]]
}

if [[ "${mode}" == "probe" ]]; then
  run_probe
else
  run_stack
fi
