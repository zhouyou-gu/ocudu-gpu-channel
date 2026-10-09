#!/usr/bin/env bash
# Native robot-fight gate (ROBOT_FIGHT_MILESTONES.md R5): two cells on TWO
# broker processes sharing one GPU.
#
#   cell a: gnb0 (PCI 1, ports 2000/2001) <-> broker a <-> ue0 (netns ue1, 10.45.1.2)
#   cell b: gnb1 (PCI 2, ports 2010/2011) <-> broker b <-> ue1 (netns ue2, 10.45.1.3)
#
# Each broker is driven by its own Sionna bridge from ONE scene and ONE live
# position feed, so the two radio links are the same physics; what the gate
# varies is how each broker is scheduled (OCUDU_NATIVE_RF_BROKER_{A,B}_SCHED)
# while a contention source shares the GPU (OCUDU_NATIVE_RF_CONTENTION).
#
#   plain      own CUDA context, no pinning, no realtime class, default stream
#   protected  MPS client, runtime.cuda_stream_priority high, pinned to the
#              platform profile's broker cores (OCG_BROKER_SPIN from the profile),
#              SCHED_FIFO (OCUDU_NATIVE_RF_PROTECTED_RT_PRIORITY, 0 = off)
#
# Contention: `busy` starts ocudu-gpu-hog (back-to-back spin kernels,
# OCUDU_NATIVE_RF_HOG_KERNEL_US / _DUTY, an MPS client when _HOG_MPS=1) once
# both UEs have attached (or at once with OCUDU_NATIVE_RF_CONTENTION_START=
# immediate); `cudagnb` runs both gNBs CUDA-accelerated (OCUDU_NATIVE_GNB_
# ACCELERATION, default all); `busy+cudagnb` both. The two Sionna bridges are
# always there (10 Hz solves on the same GPU).
#
# Structurally this is the R4a multi-UE Sionna gate with the multi-gNB gate's
# second cell; the hooks the arena half uses (OCUDU_NATIVE_MUE_UE_EXEC /
# _ROOT_EXEC, OCUDU_NATIVE_SIONNA_POSITION_*) and the receiver noise floor keep
# their names and meaning. Results land under results/{logs,reports,...}/
# ocudu-robot-fight/<ts>.
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/.." && cd .. && pwd)"
native_dir="${repo_root}/scripts/native"
# shellcheck source=../../scripts/native/env.sh
source "${native_dir}/env.sh"

native_root="${OCUDU_NATIVE_ROOT}"
physical_gpu="${OCUDU_NATIVE_GPU_DEVICE:-0}"
cuda_compiler="${CUDACXX:-/opt/conda/envs/cuda128/bin/nvcc}"
cuda_arch="${OCUDU_NATIVE_CUDA_ARCH:-120}"
inner="${script_dir}/run-ocudu-robot-fight-inner.sh"
renderer="${script_dir}/render-robot-fight-configs.py"
sionna_scenario="${OCUDU_NATIVE_SIONNA_SCENARIO:-${repo_root}/use_cases/configs/sionna/scenarios/robot_ring/robot-ring-fight.json}"
sionna_python="${OCUDU_NATIVE_SIONNA_PYTHON:-}"
sionna_update_hz="${OCUDU_NATIVE_SIONNA_UPDATE_HZ:-10}"
sionna_position_endpoint="${OCUDU_NATIVE_SIONNA_POSITION_ENDPOINT:-}"
sionna_position_offset="${OCUDU_NATIVE_SIONNA_POSITION_OFFSET:-}"
sionna_position_timeout_s="${OCUDU_NATIVE_SIONNA_POSITION_TIMEOUT_S:-}"
sionna_awgn_snr_db="${OCUDU_NATIVE_SIONNA_AWGN_SNR_DB:-40}"
sionna_tx_power_dl="${OCUDU_NATIVE_SIONNA_TX_POWER_DL:-}"
sionna_tx_power_ul="${OCUDU_NATIVE_SIONNA_TX_POWER_UL:-}"
# The run is the attach window (150 s) plus the time under contention.
rf_duration_seconds="${OCUDU_NATIVE_RF_DURATION_SECONDS:-300}"
sched_a="${OCUDU_NATIVE_RF_BROKER_A_SCHED:-plain}"
sched_b="${OCUDU_NATIVE_RF_BROKER_B_SCHED:-plain}"
contention="${OCUDU_NATIVE_RF_CONTENTION:-none}"
contention_start="${OCUDU_NATIVE_RF_CONTENTION_START:-after-attach}"
hog_kernel_us="${OCUDU_NATIVE_RF_HOG_KERNEL_US:-2000}"
hog_duty="${OCUDU_NATIVE_RF_HOG_DUTY:-1.0}"
hog_mps="${OCUDU_NATIVE_RF_HOG_MPS:-1}"
# Kernels kept queued by the hog: S streams x K deep (R5b). A default-priority
# kernel from another client of the same MPS context lines up behind all of
# them; a high-priority stream only waits for the running blocks.
hog_streams="${OCUDU_NATIVE_RF_HOG_STREAMS:-1}"
hog_queue_depth="${OCUDU_NATIVE_RF_HOG_QUEUE_DEPTH:-1}"
# SM share of the hog when it is an MPS client (CUDA_MPS_ACTIVE_THREAD_PERCENTAGE,
# the one partitioning knob MPS offers); empty = the server default (all SMs).
hog_sm_percent="${OCUDU_NATIVE_RF_HOG_SM_PERCENT:-}"
# The Sionna bridges as MPS clients (default): a solve in its own context
# time-slices every other context for its length, and the broker cannot
# defend against that from inside; in the MPS context the protected broker's
# high-priority stream keeps its slot p99 at ~0.2 ms while a same-GPU
# tenant's own context sees 1.5 ms (R5a t3). Plain brokers lose nothing.
sionna_mps="${OCUDU_NATIVE_RF_SIONNA_MPS:-1}"
# SCHED_FIFO for the protected broker: off by default. With the profile's
# OCG_BROKER_SPIN=1 the broker's ~8 spinning threads on 3 cores starve each
# other under FIFO (no time slicing between equal-priority FIFO threads):
# ping RTT 69 -> 111 ms in R5a run 20260929T142527Z.
protected_rt_priority="${OCUDU_NATIVE_RF_PROTECTED_RT_PRIORITY:-0}"
protected_stream_priority="${OCUDU_NATIVE_RF_PROTECTED_STREAM_PRIORITY:-high}"
mps_choice="${OCUDU_NATIVE_RF_MPS:-auto}"
acceleration="${OCUDU_NATIVE_GNB_ACCELERATION:-}"
strict_realtime="${OCUDU_NATIVE_MUE_STRICT_REALTIME:-0}"
ue_exec="${OCUDU_NATIVE_MUE_UE_EXEC:-}"
root_exec="${OCUDU_NATIVE_MUE_ROOT_EXEC:-}"
skip_ctest="${OCUDU_NATIVE_RF_SKIP_CTEST:-0}"
web_port="${OCUDU_NATIVE_WEB_PORT:-8080}"
web_ui="${OCUDU_NATIVE_RF_WEB_UI:-0}"
# gNB KPI feed for the Web UI (the same switch as the legacy 1x1 gate). ON by
# default here: this is the demo gate and the dashboard's KPI panel is empty
# without it. Each gNB gets its own remote-control port on the stack loopback
# and its own relay socket in the run dir (gnb-metrics-{a,b}.sock). 0 leaves
# the gNB configs and the process list exactly as before.
gnb_metrics_enabled="${OCUDU_NATIVE_GNB_METRICS:-1}"
gnb_metrics_port_a="${OCUDU_NATIVE_GNB_METRICS_PORT_A:-8001}"
gnb_metrics_port_b="${OCUDU_NATIVE_GNB_METRICS_PORT_B:-8002}"
# RAN latency knobs for BOTH gNBs (R7-prep): `pusch.min_k2=6,pucch.sr_period_ms=40,...`
# (keys listed by the renderer's --help). Empty = the fixture's defaults.
gnb_cell_overrides="${OCUDU_NATIVE_RF_GNB_CELL_OVERRIDES:-}"
audited_ocudu="a1916edcdbcd70ba6e0af47ee87be061dad5a4e4"
audited_srsran="eea87b1d893ae58e0b08bc381730c502024ae71f"
audited_open5gs="d9d3abdd480be96fac3bc8a997e83446648763ca"

usage_error()
{
  printf 'error: %s\n' "$1" >&2
  exit 2
}

[[ "$#" -eq 0 ]] || usage_error "usage: $0"
[[ "${sionna_scenario}" == /* && -f "${sionna_scenario}" && ! -L "${sionna_scenario}" ]] || \
  usage_error "OCUDU_NATIVE_SIONNA_SCENARIO must be an absolute regular file"
[[ -x "${sionna_python}" ]] || usage_error "OCUDU_NATIVE_SIONNA_PYTHON must point at the Sionna interpreter"
[[ "${physical_gpu}" =~ ^(0|[1-9][0-9]*)$ && "${physical_gpu}" -le 255 ]] || usage_error "invalid GPU device"
[[ "${cuda_arch}" =~ ^[1-9][0-9]*$ ]] || usage_error "invalid OCUDU_NATIVE_CUDA_ARCH"
[[ -x "${cuda_compiler}" ]] || usage_error "missing CUDA compiler: ${cuda_compiler}"
number_re='^-?[0-9]+([.][0-9]+)?([eE][-+]?[0-9]+)?$'
[[ "${sionna_awgn_snr_db}" == "off" || "${sionna_awgn_snr_db}" =~ ${number_re} ]] || \
  usage_error "OCUDU_NATIVE_SIONNA_AWGN_SNR_DB must be a number or off"
[[ -z "${sionna_tx_power_dl}" || "${sionna_tx_power_dl}" =~ ${number_re} ]] || \
  usage_error "OCUDU_NATIVE_SIONNA_TX_POWER_DL must be a number"
[[ -z "${sionna_tx_power_ul}" || "${sionna_tx_power_ul}" =~ ${number_re} ]] || \
  usage_error "OCUDU_NATIVE_SIONNA_TX_POWER_UL must be a number"
[[ "${sionna_update_hz}" =~ ^[0-9]+(\.[0-9]+)?$ ]] || usage_error "invalid OCUDU_NATIVE_SIONNA_UPDATE_HZ"
if [[ -n "${sionna_position_endpoint}" ]]; then
  [[ "${sionna_position_endpoint}" == ipc:///* ]] || \
    usage_error "OCUDU_NATIVE_SIONNA_POSITION_ENDPOINT must be an absolute ipc:// socket"
  [[ -z "${sionna_position_offset}" || "${sionna_position_offset}" =~ ^-?[0-9.]+,-?[0-9.]+,-?[0-9.]+$ ]] || \
    usage_error "OCUDU_NATIVE_SIONNA_POSITION_OFFSET must be x,y,z"
  [[ -z "${sionna_position_timeout_s}" || "${sionna_position_timeout_s}" =~ ^[0-9]+([.][0-9]+)?$ ]] || \
    usage_error "OCUDU_NATIVE_SIONNA_POSITION_TIMEOUT_S must be a number"
fi
[[ "${rf_duration_seconds}" =~ ^[1-9][0-9]*$ && "${rf_duration_seconds}" -ge 160 ]] || \
  usage_error "OCUDU_NATIVE_RF_DURATION_SECONDS must be an integer >= 160"
for value in "${sched_a}" "${sched_b}"; do
  [[ "${value}" =~ ^(plain|protected)$ ]] || usage_error "OCUDU_NATIVE_RF_BROKER_{A,B}_SCHED must be plain or protected"
done
[[ "${contention}" =~ ^(none|busy|cudagnb|busy\+cudagnb)$ ]] || \
  usage_error "OCUDU_NATIVE_RF_CONTENTION must be none, busy, cudagnb or busy+cudagnb"
[[ "${contention_start}" =~ ^(after-attach|immediate)$ ]] || \
  usage_error "OCUDU_NATIVE_RF_CONTENTION_START must be after-attach or immediate"
[[ "${hog_kernel_us}" =~ ^[1-9][0-9]*$ ]] || usage_error "OCUDU_NATIVE_RF_HOG_KERNEL_US must be a positive integer"
[[ "${hog_duty}" =~ ^(0\.[0-9]+|1(\.0+)?)$ ]] || usage_error "OCUDU_NATIVE_RF_HOG_DUTY must be in (0, 1]"
[[ "${hog_streams}" =~ ^[1-9][0-9]?$ ]] || usage_error "OCUDU_NATIVE_RF_HOG_STREAMS must be 1..64"
[[ "${hog_queue_depth}" =~ ^[1-9][0-9]{0,3}$ ]] || usage_error "OCUDU_NATIVE_RF_HOG_QUEUE_DEPTH must be 1..1024"
[[ "${hog_mps}" =~ ^[01]$ && "${sionna_mps}" =~ ^[01]$ ]] || usage_error "OCUDU_NATIVE_RF_{HOG,SIONNA}_MPS must be 0 or 1"
[[ -z "${hog_sm_percent}" || ( "${hog_sm_percent}" =~ ^[1-9][0-9]?$|^100$ ) ]] || \
  usage_error "OCUDU_NATIVE_RF_HOG_SM_PERCENT must be 1..100"
[[ "${protected_rt_priority}" =~ ^(0|[1-9][0-9]?)$ ]] || usage_error "OCUDU_NATIVE_RF_PROTECTED_RT_PRIORITY must be 0..99"
[[ "${protected_stream_priority}" =~ ^(default|high|low)$ ]] || \
  usage_error "OCUDU_NATIVE_RF_PROTECTED_STREAM_PRIORITY must be default, high or low"
[[ "${mps_choice}" =~ ^(auto|on|off)$ ]] || usage_error "OCUDU_NATIVE_RF_MPS must be auto, on or off"
[[ "${strict_realtime}" =~ ^[01]$ ]] || usage_error "OCUDU_NATIVE_MUE_STRICT_REALTIME must be 0 or 1"
[[ "${skip_ctest}" =~ ^[01]$ && "${web_ui}" =~ ^[01]$ ]] || usage_error "OCUDU_NATIVE_RF_{SKIP_CTEST,WEB_UI} must be 0 or 1"
[[ "${web_port}" =~ ^[1-9][0-9]*$ && "${web_port}" -le 65535 ]] || usage_error "invalid OCUDU_NATIVE_WEB_PORT"
case "${gnb_metrics_enabled,,}" in
  1|true|yes|on) gnb_metrics_enabled=1 ;;
  0|false|no|off|"") gnb_metrics_enabled=0 ;;
  *) usage_error "OCUDU_NATIVE_GNB_METRICS must be 0 or 1" ;;
esac
for value in "${gnb_metrics_port_a}" "${gnb_metrics_port_b}"; do
  [[ "${value}" =~ ^[1-9][0-9]*$ && "${value}" -le 65535 ]] || usage_error "invalid OCUDU_NATIVE_GNB_METRICS_PORT_{A,B}"
done
[[ "${gnb_metrics_port_a}" != "${gnb_metrics_port_b}" ]] || usage_error "OCUDU_NATIVE_GNB_METRICS_PORT_{A,B} must differ"
[[ -z "${gnb_cell_overrides}" || "${gnb_cell_overrides}" =~ ^[a-z_]+\.[a-z_0-9]+=[0-9]+(,[a-z_]+\.[a-z_0-9]+=[0-9]+)*$ ]] || \
  usage_error "OCUDU_NATIVE_RF_GNB_CELL_OVERRIDES must be key=int[,key=int] (e.g. pusch.min_k2=6)"

# gNB: the audited CPU build, or the CUDA build when contention asks for it.
gnb_kind="cpu"
gnb_binary="${native_root}/builds/ocudu-zmq-release/apps/gnb/gnb"
gnb_commit="${audited_ocudu}"
gnb_start_timeout="${OCUDU_NATIVE_GNB_START_TIMEOUT_SECONDS:-15}"
if [[ "${contention}" == *cudagnb* ]]; then
  acceleration="${acceleration:-all}"
  case "${acceleration}" in
    disabled|low-phy-rx|low-phy-tx|pusch|pdsch|prach|all) ;;
    *) usage_error "invalid OCUDU_NATIVE_GNB_ACCELERATION: ${acceleration}" ;;
  esac
  selection="$(/usr/bin/python3 "${repo_root}/scripts/cuda/resolve-cuda-gnb.py" --root "${native_root}" --fields)" \
    || usage_error "CUDA gNB build did not pass its audit"
  IFS=$'\t' read -r gnb_binary _ _ gnb_commit <<<"${selection}"
  gnb_kind="cuda"
  gnb_start_timeout="${OCUDU_NATIVE_GNB_START_TIMEOUT_SECONDS:-120}"
else
  acceleration=""
fi
[[ "${gnb_start_timeout}" =~ ^[1-9][0-9]*$ ]] || usage_error "invalid OCUDU_NATIVE_GNB_START_TIMEOUT_SECONDS"

# MPS: one server for the whole gate when anything is meant to be an MPS
# client; each process then keeps or drops CUDA_MPS_PIPE_DIRECTORY (inner).
# with-cuda-mps.py re-executes this gate under the server and shuts it down.
mps_wanted=0
if [[ "${mps_choice}" == "on" ]]; then
  mps_wanted=1
elif [[ "${mps_choice}" == "auto" ]]; then
  [[ "${sched_a}" == "protected" || "${sched_b}" == "protected" ]] && mps_wanted=1
  [[ "${contention}" == *busy* && "${hog_mps}" == "1" ]] && mps_wanted=1
  [[ "${sionna_mps}" == "1" ]] && mps_wanted=1
fi
if [[ "${mps_wanted}" -eq 1 && -z "${CUDA_MPS_PIPE_DIRECTORY:-}" ]]; then
  export OCUDU_NATIVE_MPS_REASON="robot-fight:${mps_choice}:a=${sched_a},b=${sched_b},contention=${contention}"
  exec /usr/bin/python3 "${repo_root}/scripts/cuda/with-cuda-mps.py" -- bash "${BASH_SOURCE[0]}"
fi
mps_active=0
[[ -n "${CUDA_MPS_PIPE_DIRECTORY:-}" ]] && mps_active=1

# CPU placement (platform-profiles.json). The protected broker takes the
# profile's broker cores and broker_env; the plain one is left to the
# scheduler unless OCUDU_NATIVE_RF_PLAIN_CPUS says otherwise.
# shellcheck source=oai-gate-defaults.sh
source "${native_dir}/oai-gate-defaults.sh"
oai_gate_platform || usage_error "platform profile resolution failed"
protected_cpus="${OCUDU_NATIVE_RF_PROTECTED_CPUS:-${OCUDU_NATIVE_BROKER_CPUS:-}}"
plain_cpus="${OCUDU_NATIVE_RF_PLAIN_CPUS:-}"
# SCHED_FIFO inside the user namespace needs the rlimit, not a capability;
# raise the soft limit here (root via sudo has the hard limit) so the inner
# script's chrt can succeed. Best effort: the inner logs what it got.
if [[ "${protected_rt_priority}" -gt 0 ]]; then
  ulimit -r "${protected_rt_priority}" 2>/dev/null || echo "event=rt_limit_unavailable requested=${protected_rt_priority}"
fi

for command_name in unshare nsenter ip mount umount flock cmake ctest ss setsid stdbuf taskset chrt; do
  command -v "${command_name}" >/dev/null 2>&1 || usage_error "missing command: ${command_name}"
done
for path in "${inner}" "${renderer}" "${gnb_binary}" \
  "${native_root}/builds/srsran4g-zmq-release/srsue/src/srsue" \
  "${native_root}/builds/open5gs-v2.7.6/tests/app/5gc" \
  "${native_root}/install/mongodb-6.0.29/bin/mongod" \
  "${repo_root}/scripts/sionna_rt/run_bridge.py"; do
  [[ -e "${path}" ]] || usage_error "missing required path: ${path}"
done
[[ -c /dev/net/tun ]] || usage_error "/dev/net/tun is absent"
[[ "$(git -C "${native_root}/src/ocudu" rev-parse HEAD)" == "${audited_ocudu}" ]] || usage_error "OCUDU revision mismatch"
[[ "$(git -C "${native_root}/src/srsRAN_4G" rev-parse HEAD)" == "${audited_srsran}" ]] || usage_error "srsRAN revision mismatch"
[[ "$(git -C "${native_root}/src/open5gs" rev-parse HEAD)" == "${audited_open5gs}" ]] || usage_error "Open5GS revision mismatch"
if [[ "${OCUDU_NATIVE_SKIP_WORKSPACE_LOCK:-0}" == "1" ]]; then
  echo "event=skip x86_native_workspace_lock"
else
  "/usr/bin/python3" "${native_dir}/verify-workspace-lock.py" \
    --root "${native_root}" --repo-root "${repo_root}" \
    --lock "${native_dir}/native-workspace.lock.json"
fi
grep -qx 'ENABLE_ZEROMQ:BOOL=ON' "${native_root}/builds/ocudu-zmq-release/CMakeCache.txt" || usage_error "gNB lacks ZMQ"
# Both gNB pairs, both UE pairs, mongo and the core.
for port in 2000 2001 2010 2011 2100 2101 2102 2103 27017 38412 7777; do
  ss -H -ltn "sport = :${port}" | grep -q . && usage_error "TCP port ${port} is already listening"
done

exec {lock_fd}<"${BASH_SOURCE[0]}"
flock -n "${lock_fd}" || usage_error "another native robot-fight gate is running"
export OCUDU_NATIVE_GATE_LOCK_FD="${lock_fd}"

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
log_dir="${native_root}/results/logs/ocudu-robot-fight/${timestamp}"
report_dir="${native_root}/results/reports/ocudu-robot-fight/${timestamp}"
config_dir="${native_root}/configs/ocudu-robot-fight-native/${timestamp}"
data_dir="${native_root}/data/ocudu-robot-fight-native/${timestamp}"
run_dir="${native_root}/run/ocudu-robot-fight-native/${timestamp}"
netns_dir="${run_dir}/netns"
for path in "${log_dir}" "${report_dir}" "${config_dir}" "${data_dir}" "${run_dir}"; do
  [[ ! -e "${path}" && ! -L "${path}" ]] || usage_error "run path already exists: ${path}"
done
mkdir -p "${log_dir}" "${report_dir}" "${config_dir}" "${data_dir}" "${netns_dir}"

priority_a="default"; priority_b="default"
[[ "${sched_a}" == "protected" ]] && priority_a="${protected_stream_priority}"
[[ "${sched_b}" == "protected" ]] && priority_b="${protected_stream_priority}"
declare -a renderer_args=(
  --repo-root "${repo_root}" --native-root "${native_root}"
  --output-dir "${config_dir}" --log-dir "${log_dir}"
  --scenario-config "${sionna_scenario}"
  --stream-priority-a "${priority_a}" --stream-priority-b "${priority_b}"
)
if [[ "${sionna_awgn_snr_db}" != "off" ]]; then
  renderer_args+=(--awgn-snr-db "${sionna_awgn_snr_db}")
  [[ -z "${sionna_tx_power_dl}" ]] || renderer_args+=(--tx-power-dl "${sionna_tx_power_dl}")
  [[ -z "${sionna_tx_power_ul}" ]] || renderer_args+=(--tx-power-ul "${sionna_tx_power_ul}")
fi
[[ -z "${acceleration}" ]] || renderer_args+=(--gnb-acceleration "${acceleration}")
[[ -z "${gnb_cell_overrides}" ]] || renderer_args+=(--gnb-cell-overrides "${gnb_cell_overrides}")
gnb_metrics_socket_a=""; gnb_metrics_socket_b=""
if [[ "${gnb_metrics_enabled}" == "1" ]]; then
  renderer_args+=(--gnb-metrics-ports "${gnb_metrics_port_a},${gnb_metrics_port_b}")
  gnb_metrics_socket_a="${run_dir}/gnb-metrics-a.sock"
  gnb_metrics_socket_b="${run_dir}/gnb-metrics-b.sock"
fi
"/usr/bin/python3" "${renderer}" "${renderer_args[@]}" >"${log_dir}/render.log" 2>&1 || {
  cat "${log_dir}/render.log" >&2; usage_error "config rendering failed"
}
for cell in gnb0 gnb1; do
  "${gnb_binary}" -c "${config_dir}/${cell}.yaml" --dryrun >"${log_dir}/${cell}-dryrun.log" 2>&1 || {
    tail -20 "${log_dir}/${cell}-dryrun.log" >&2; usage_error "${cell} dry run failed"
  }
done

# The brokers and the hog come from THIS tree.
channel_build="${OCUDU_NATIVE_CHANNEL_BUILD:-${native_root}/builds/ocudu-gpu-channel-cuda-release}"
cmake -S "${repo_root}" -B "${channel_build}" -DCMAKE_BUILD_TYPE=Release \
  -DOCUDU_GPU_CHANNEL_ENABLE_CUDA=ON -DCMAKE_CUDA_COMPILER="${cuda_compiler}" \
  -DOCUDU_GPU_CHANNEL_CUDA_ARCHITECTURES="${cuda_arch}" >"${log_dir}/cmake-configure.log" 2>&1 \
  || { tail -20 "${log_dir}/cmake-configure.log" >&2; usage_error "cmake configure"; }
cmake --build "${channel_build}" -j"$(nproc)" >"${log_dir}/cmake-build.log" 2>&1 \
  || { tail -20 "${log_dir}/cmake-build.log" >&2; usage_error "cmake build"; }
if [[ "${skip_ctest}" == "1" ]]; then
  echo "event=skip ctest reason=OCUDU_NATIVE_RF_SKIP_CTEST"
else
  CUDA_VISIBLE_DEVICES="${physical_gpu}" \
    ctest --test-dir "${channel_build}" --output-on-failure >"${log_dir}/ctest.log" 2>&1 \
    || { tail -20 "${log_dir}/ctest.log" >&2; usage_error "ctest"; }
fi
[[ -x "${channel_build}/ocudu-gpu-hog" ]] || usage_error "missing ocudu-gpu-hog in ${channel_build}"

parent_netns="$(readlink /proc/self/ns/net)"
parent_mntns="$(readlink /proc/self/ns/mnt)"

/usr/bin/python3 - "${report_dir}/run-parameters.json" <<'PY' \
  "${rf_duration_seconds}" "${strict_realtime}" "${sched_a}" "${sched_b}" "${contention}" "${contention_start}" \
  "${hog_kernel_us}" "${hog_duty}" "${hog_mps}" "${sionna_mps}" "${protected_rt_priority}" \
  "${protected_stream_priority}" "${mps_active}" "${OCUDU_NATIVE_PLATFORM_PROFILE:-none}" "${hog_sm_percent}" \
  "${protected_cpus}" "${plain_cpus}" "${OCUDU_NATIVE_GNB_CPUS:-}" "${OCUDU_NATIVE_NRUE_CPUS:-}" \
  "${OCUDU_NATIVE_BROKER_ENV:-}" "${gnb_kind}" "${gnb_commit}" "${acceleration}" \
  "${sionna_awgn_snr_db}" "${sionna_scenario}" "${sionna_position_endpoint}" "${ue_exec}" "${root_exec}" \
  "${gnb_metrics_socket_a}" "${gnb_metrics_port_a}" "${gnb_metrics_socket_b}" "${gnb_metrics_port_b}"
import json, sys
(out, duration, strict, sched_a, sched_b, contention, contention_start, hog_kernel_us, hog_duty, hog_mps,
 sionna_mps, rt_priority, stream_priority, mps_active, profile, hog_sm_percent, protected_cpus, plain_cpus, gnb_cpus,
 ue_cpus, broker_env, gnb_kind, gnb_commit, acceleration, awgn, scenario, position_endpoint,
 ue_exec, root_exec, metrics_sock_a, metrics_port_a, metrics_sock_b, metrics_port_b) = sys.argv[1:]
json.dump({
    "gnb_metrics": None if not metrics_sock_a else {
        "a": {"gnb": "gnb0", "socket": metrics_sock_a, "port": int(metrics_port_a),
              "endpoint": "ws+unix://" + metrics_sock_a},
        "b": {"gnb": "gnb1", "socket": metrics_sock_b, "port": int(metrics_port_b),
              "endpoint": "ws+unix://" + metrics_sock_b}},
    "run_duration_seconds": int(duration), "strict_realtime": int(strict),
    "broker_sched": {"a": sched_a, "b": sched_b},
    "protected": {"rt_priority": int(rt_priority), "stream_priority": stream_priority,
                  "cpus": protected_cpus, "broker_env": broker_env, "mps_client": True},
    "plain": {"cpus": plain_cpus},
    "contention": {"kind": contention, "start": contention_start,
                   "hog": {"kernel_us": int(hog_kernel_us), "duty": float(hog_duty), "mps_client": int(hog_mps),
                           "sm_percent": int(hog_sm_percent) if hog_sm_percent else None},
                   "sionna_mps_client": int(sionna_mps)},
    "mps_server": int(mps_active), "platform_profile": profile,
    "cpus": {"gnb": gnb_cpus, "ue": ue_cpus},
    "gnb": {"kind": gnb_kind, "commit": gnb_commit, "acceleration": acceleration or None},
    "sionna_awgn_snr_db": None if awgn == "off" else float(awgn),
    "sionna_scenario": scenario, "sionna_position_endpoint": position_endpoint or None,
    "ue_exec": ue_exec, "root_exec": root_exec,
}, open(out, "w"), indent=2, sort_keys=True)
PY
printf 'event=native_robot_fight_gate_parameters duration=%ss a=%s b=%s contention=%s start=%s mps=%s rt=%s protected_cpus=%s gnb=%s awgn=%s\n' \
  "${rf_duration_seconds}" "${sched_a}" "${sched_b}" "${contention}" "${contention_start}" "${mps_active}" \
  "${protected_rt_priority}" "${protected_cpus:-any}" "${gnb_kind}" "${sionna_awgn_snr_db}"

declare -a inner_args=(
  --repo-root "${repo_root}" --native-root "${native_root}"
  --config-dir "${config_dir}" --log-dir "${log_dir}" --netns-dir "${netns_dir}"
  --timestamp "${timestamp}" --physical-gpu "${physical_gpu}"
  --channel-build "${channel_build}"
  --parent-netns "${parent_netns}" --parent-mntns "${parent_mntns}"
  --outer-uid "$(id -u)"
  --gnb-binary "${gnb_binary}" --gnb-start-timeout "${gnb_start_timeout}"
  --run-duration-seconds "${rf_duration_seconds}" --strict-realtime "${strict_realtime}"
  --sched-a "${sched_a}" --sched-b "${sched_b}"
  --contention "${contention}" --contention-start "${contention_start}"
  --hog-kernel-us "${hog_kernel_us}" --hog-duty "${hog_duty}" --hog-mps "${hog_mps}"
  --hog-sm-percent "${hog_sm_percent}" --hog-streams "${hog_streams}" --hog-queue-depth "${hog_queue_depth}"
  --sionna-mps "${sionna_mps}" --rt-priority "${protected_rt_priority}"
  --protected-cpus "${protected_cpus}" --plain-cpus "${plain_cpus}"
  --control-endpoint-a "ipc://${run_dir}/control-a.sock" --telemetry-endpoint-a "ipc://${run_dir}/telemetry-a.sock"
  --control-endpoint-b "ipc://${run_dir}/control-b.sock" --telemetry-endpoint-b "ipc://${run_dir}/telemetry-b.sock"
  --sionna-python "${sionna_python}" --sionna-bridge "${repo_root}/scripts/sionna_rt/run_bridge.py"
  --sionna-update-hz "${sionna_update_hz}"
  --sionna-position-endpoint "${sionna_position_endpoint}"
  --sionna-position-offset "${sionna_position_offset}"
  --sionna-position-timeout-s "${sionna_position_timeout_s}"
  --ue-exec "${ue_exec}" --root-exec "${root_exec}"
  --gnb-metrics-socket-a "${gnb_metrics_socket_a}" --gnb-metrics-port-a "${gnb_metrics_port_a}"
  --gnb-metrics-socket-b "${gnb_metrics_socket_b}" --gnb-metrics-port-b "${gnb_metrics_port_b}"
)
web_pid=""
if [[ "${web_ui}" == "1" ]]; then
  # Read-only observer of cell a only (one telemetry socket per web server).
  web_metrics_args=()
  [[ -z "${gnb_metrics_socket_a}" ]] || web_metrics_args=(--gnb-metrics-endpoint "ws+unix://${gnb_metrics_socket_a}")
  "${sionna_python}" "${repo_root}/scripts/web_ui/server.py" \
    --port "${web_port}" --telemetry-endpoint "ipc://${run_dir}/telemetry-a.sock" \
    --status-jsonl "${log_dir}/sionna-status-a.jsonl" \
    --index "${repo_root}/scripts/web_ui/index.html" "${web_metrics_args[@]}" \
    >"${log_dir}/web-ui.log" 2>&1 &
  web_pid="$!"
  printf 'Web UI (cell a): http://127.0.0.1:%s\n' "${web_port}"
fi

set +e
unshare --user --map-root-user --net --mount --fork --kill-child --propagation private \
  "${inner}" "${inner_args[@]}"
inner_status="$?"
set -e
[[ -n "${web_pid}" ]] && kill "${web_pid}" >/dev/null 2>&1
[[ -n "${web_pid}" ]] && wait "${web_pid}" >/dev/null 2>&1

summary="${report_dir}/attach-summary.json"
if [[ "${inner_status}" -eq 0 && -f "${summary}" ]]; then
  printf 'event=native_robot_fight_gate result=pass summary="%s"\n' "${summary}"
else
  [[ -f "${summary}" ]] && cat "${summary}" >&2
  printf 'event=native_robot_fight_gate result=fail status=%s logs="%s"\n' "${inner_status}" "${log_dir}" >&2
fi
exit "${inner_status}"
