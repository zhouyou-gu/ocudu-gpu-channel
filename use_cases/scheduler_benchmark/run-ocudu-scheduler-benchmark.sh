#!/usr/bin/env bash
# Native scheduler-benchmark gate: two cells, two UEs each, one channel.
#
#   cell a: gnb0 (PCI 1, ports 2000/2001) <-> broker a <-> ue0, ue1 (netns ue1, ue2)
#   cell b: gnb1 (PCI 2, ports 2010/2011) <-> broker b <-> ue2, ue3 (netns ue3, ue4)
#
# ONE Sionna bridge solves the seeded scenario on a grid timeline and sends
# every batch to both brokers (cell b under gnb0->gnb1, ue0->ue2, ue1->ue3),
# so both cells carry the same channel at the same instant. Each gNB runs a
# different MAC scheduler policy (OCUDU_NATIVE_SB_SCHED_A / _B: rr or qos);
# both brokers are scheduled identically and the CPU placement is symmetric,
# so the policy is the only intended difference between the cells.
#
# Sequence: attach all four UEs while the bridge holds scenario time 0; then,
# at one instant, start the seeded traffic and the grid timeline; measure
# OCUDU_NATIVE_SB_MEASURE_SECONDS of scenario time; drain; analyze. The seed
# (OCUDU_NATIVE_SB_SEED) fixes the UE routes, the PathSolver seed and the
# packet schedules, so a rerun with the same seed reproduces the same channel
# sequence and the same offered traffic (bit-identical channel profiles; the
# report records their SHA-256 per grid point).
#
# Results: results/{logs,reports}/ocudu-scheduler-benchmark/<ts>, configs under
# configs/ocudu-scheduler-benchmark-native/<ts>. The report is
# reports/.../benchmark-report.json; the web UI is use_cases/scheduler_benchmark/server.py.
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
inner="${script_dir}/run-ocudu-scheduler-benchmark-inner.sh"
renderer="${script_dir}/render-scheduler-benchmark-configs.py"
bench_dir="${script_dir}"
sionna_python="${OCUDU_NATIVE_SIONNA_PYTHON:-}"
seed="${OCUDU_NATIVE_SB_SEED:-1}"
sched_a="${OCUDU_NATIVE_SB_SCHED_A:-rr}"
sched_b="${OCUDU_NATIVE_SB_SCHED_B:-qos}"
profile="${OCUDU_NATIVE_SB_PROFILE:-mixed}"
traffic_overrides="${OCUDU_NATIVE_SB_TRAFFIC_OVERRIDES:-}"
measure_seconds="${OCUDU_NATIVE_SB_MEASURE_SECONDS:-120}"
drain_seconds="${OCUDU_NATIVE_SB_DRAIN_SECONDS:-6}"
attach_timeout="${OCUDU_NATIVE_SB_ATTACH_TIMEOUT:-180}"
scenario_override="${OCUDU_NATIVE_SB_SCENARIO:-}"
scenario_base="${OCUDU_NATIVE_SB_SCENARIO_BASE:-${repo_root}/use_cases/configs/sionna/scenarios/robot_ring/robot-ring-walk.json}"
sionna_update_hz="${OCUDU_NATIVE_SIONNA_UPDATE_HZ:-10}"
sionna_awgn_snr_db="${OCUDU_NATIVE_SIONNA_AWGN_SNR_DB:-40}"
sionna_tx_power_dl="${OCUDU_NATIVE_SIONNA_TX_POWER_DL:-}"
sionna_tx_power_ul="${OCUDU_NATIVE_SIONNA_TX_POWER_UL:-}"
web_ui="${OCUDU_NATIVE_SB_WEB_UI:-1}"
web_port="${OCUDU_NATIVE_SB_WEB_PORT:-8090}"
skip_ctest="${OCUDU_NATIVE_SB_SKIP_CTEST:-0}"
gnb_metrics_enabled="${OCUDU_NATIVE_GNB_METRICS:-1}"
audited_ocudu="a1916edcdbcd70ba6e0af47ee87be061dad5a4e4"
audited_srsran="eea87b1d893ae58e0b08bc381730c502024ae71f"
audited_open5gs="d9d3abdd480be96fac3bc8a997e83446648763ca"

usage_error()
{
  printf 'error: %s\n' "$1" >&2
  exit 2
}

[[ "$#" -eq 0 ]] || usage_error "usage: $0 (configure with OCUDU_NATIVE_SB_* variables)"
[[ -x "${sionna_python}" ]] || usage_error "OCUDU_NATIVE_SIONNA_PYTHON must point at the Sionna interpreter"
[[ "${seed}" =~ ^(0|[1-9][0-9]{0,8})$ ]] || usage_error "OCUDU_NATIVE_SB_SEED must be a non-negative integer"
for value in "${sched_a}" "${sched_b}"; do
  [[ "${value}" =~ ^(rr|qos)$ ]] || usage_error "OCUDU_NATIVE_SB_SCHED_{A,B} must be rr or qos"
done
[[ "${profile}" =~ ^[a-z_]+$ ]] || usage_error "invalid OCUDU_NATIVE_SB_PROFILE"
[[ "${measure_seconds}" =~ ^[1-9][0-9]*$ && "${measure_seconds}" -ge 10 ]] || \
  usage_error "OCUDU_NATIVE_SB_MEASURE_SECONDS must be an integer >= 10"
[[ "${drain_seconds}" =~ ^[1-9][0-9]?$ ]] || usage_error "OCUDU_NATIVE_SB_DRAIN_SECONDS must be 1..99"
[[ "${attach_timeout}" =~ ^[1-9][0-9]*$ && "${attach_timeout}" -ge 30 ]] || \
  usage_error "OCUDU_NATIVE_SB_ATTACH_TIMEOUT must be an integer >= 30"
[[ "${sionna_update_hz}" =~ ^[0-9]+(\.[0-9]+)?$ ]] || usage_error "invalid OCUDU_NATIVE_SIONNA_UPDATE_HZ"
number_re='^-?[0-9]+([.][0-9]+)?([eE][-+]?[0-9]+)?$'
[[ "${sionna_awgn_snr_db}" == "off" || "${sionna_awgn_snr_db}" =~ ${number_re} ]] || \
  usage_error "OCUDU_NATIVE_SIONNA_AWGN_SNR_DB must be a number or off"
[[ -z "${sionna_tx_power_dl}" || "${sionna_tx_power_dl}" =~ ${number_re} ]] || usage_error "invalid TX_POWER_DL"
[[ -z "${sionna_tx_power_ul}" || "${sionna_tx_power_ul}" =~ ${number_re} ]] || usage_error "invalid TX_POWER_UL"
[[ "${web_ui}" =~ ^[01]$ && "${skip_ctest}" =~ ^[01]$ && "${gnb_metrics_enabled}" =~ ^[01]$ ]] || \
  usage_error "OCUDU_NATIVE_SB_{WEB_UI,SKIP_CTEST} and OCUDU_NATIVE_GNB_METRICS must be 0 or 1"
[[ "${web_port}" =~ ^[1-9][0-9]*$ && "${web_port}" -le 65535 ]] || usage_error "invalid OCUDU_NATIVE_SB_WEB_PORT"
[[ -z "${scenario_override}" || ( "${scenario_override}" == /* && -f "${scenario_override}" ) ]] || \
  usage_error "OCUDU_NATIVE_SB_SCENARIO must be an absolute regular file"
[[ -f "${scenario_base}" ]] || usage_error "missing scenario base ${scenario_base}"

gnb_binary="${native_root}/builds/ocudu-zmq-release/apps/gnb/gnb"
gnb_start_timeout="${OCUDU_NATIVE_GNB_START_TIMEOUT_SECONDS:-20}"

# CPU placement. Neither cell may be favoured, so each cell's process gets
# the same kind of core set as its counterpart: the two gNBs share one pool,
# the four UEs share another, and each broker has its own core of the same
# type. On spark-gb10 (big X925 5-9,15-19; little A725 0-4,10-14):
#   UEs      15-19  srsUE decoding was the bottleneck: four UEs on 17-19 held
#                   those cores at 97% and the radios ran at 0.49x real time
#                   (run 20261001T081347Z)
#   gNBs     5-9,10-12  a gNB must see >= 5 CPUs: OCUDU sizes its main worker
#                   pool as min(needed, CPUs - 3) (apps/services/worker_manager/
#                   worker_manager.cpp), and with one worker it stalls at MAC
#                   cell activation (two-core pinning, run 20261001T080553Z)
#   brokers  13 / 14  plain brokers used 37% of a big core each
#   others   0-4    Sionna, core, traffic, metrics recorders
# Unknown hosts are left to the kernel unless the variables are set.
# shellcheck source=oai-gate-defaults.sh
source "${native_dir}/oai-gate-defaults.sh"
oai_gate_platform || usage_error "platform profile resolution failed"
if [[ "${OCUDU_NATIVE_PLATFORM_PROFILE:-none}" == "spark-gb10" ]]; then
  default_cpus=(5-12 5-12 13 14 15-19 15-19 15-19 15-19 0-4)
else
  default_cpus=("" "" "" "" "" "" "" "" "")
fi
cpus_gnb_a="${OCUDU_NATIVE_SB_CPUS_GNB_A:-${default_cpus[0]}}"
cpus_gnb_b="${OCUDU_NATIVE_SB_CPUS_GNB_B:-${default_cpus[1]}}"
cpus_broker_a="${OCUDU_NATIVE_SB_CPUS_BROKER_A:-${default_cpus[2]}}"
cpus_broker_b="${OCUDU_NATIVE_SB_CPUS_BROKER_B:-${default_cpus[3]}}"
cpus_ue0="${OCUDU_NATIVE_SB_CPUS_UE0:-${default_cpus[4]}}"
cpus_ue1="${OCUDU_NATIVE_SB_CPUS_UE1:-${default_cpus[5]}}"
cpus_ue2="${OCUDU_NATIVE_SB_CPUS_UE2:-${default_cpus[6]}}"
cpus_ue3="${OCUDU_NATIVE_SB_CPUS_UE3:-${default_cpus[7]}}"
cpus_aux="${OCUDU_NATIVE_SB_CPUS_AUX:-${default_cpus[8]}}"
for value in "${cpus_gnb_a}" "${cpus_gnb_b}" "${cpus_broker_a}" "${cpus_broker_b}" \
             "${cpus_ue0}" "${cpus_ue1}" "${cpus_ue2}" "${cpus_ue3}" "${cpus_aux}"; do
  [[ -z "${value}" || "${value}" =~ ^[0-9]+(-[0-9]+)?(,[0-9]+(-[0-9]+)?)*$ ]] || usage_error "invalid CPU list: ${value}"
done
for value in "${cpus_gnb_a}" "${cpus_gnb_b}"; do
  [[ -z "${value}" ]] && continue
  count="$(/usr/bin/python3 -c 'import sys
n = 0
for part in sys.argv[1].split(","):
    lo, _, hi = part.partition("-")
    n += int(hi or lo) - int(lo) + 1
print(n)' "${value}")"
  [[ "${count}" -ge 5 ]] || usage_error "a gNB needs at least 5 CPUs (got ${value}); OCUDU's main pool deadlocks with one worker"
done

for command_name in unshare nsenter ip mount umount flock cmake ctest ss setsid stdbuf taskset; do
  command -v "${command_name}" >/dev/null 2>&1 || usage_error "missing command: ${command_name}"
done
for path in "${inner}" "${renderer}" "${gnb_binary}" \
  "${native_root}/builds/open5gs-v2.7.6/tests/app/5gc" \
  "${native_root}/install/mongodb-6.0.29/bin/mongod" \
  "${repo_root}/apps/sionna_bridge/run_bridge.py" "${bench_dir}/traffic.py" "${bench_dir}/analyze.py" \
  "${bench_dir}/make_scenario.py" "${bench_dir}/gnb_metrics_recorder.py"; do
  [[ -e "${path}" ]] || usage_error "missing required path: ${path}"
done
[[ -c /dev/net/tun ]] || usage_error "/dev/net/tun is absent"
[[ "$(git -C "${native_root}/src/ocudu" rev-parse HEAD)" == "${audited_ocudu}" ]] || usage_error "OCUDU revision mismatch"
[[ "$(git -C "${native_root}/src/srsRAN_4G" rev-parse HEAD)" == "${audited_srsran}" ]] || usage_error "srsRAN revision mismatch"
[[ "$(git -C "${native_root}/src/open5gs" rev-parse HEAD)" == "${audited_open5gs}" ]] || usage_error "Open5GS revision mismatch"
if [[ "${OCUDU_NATIVE_SKIP_WORKSPACE_LOCK:-0}" == "1" ]]; then
  echo "event=skip native_workspace_lock"
else
  "/usr/bin/python3" "${native_dir}/verify-workspace-lock.py" \
    --root "${native_root}" --repo-root "${repo_root}" \
    --lock "${native_dir}/native-workspace.lock.json"
fi
grep -qx 'ENABLE_ZEROMQ:BOOL=ON' "${native_root}/builds/ocudu-zmq-release/CMakeCache.txt" || usage_error "gNB lacks ZMQ"
for port in 2000 2001 2010 2011 2100 2101 2102 2103 2104 2105 2106 2107 27017 38412 7777 "${web_port}"; do
  ss -H -ltn "sport = :${port}" | grep -q . && usage_error "TCP port ${port} is already listening"
done

exec {lock_fd}<"${BASH_SOURCE[0]}"
flock -n "${lock_fd}" || usage_error "another native scheduler-benchmark gate is running"
export OCUDU_NATIVE_GATE_LOCK_FD="${lock_fd}"

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
log_dir="${native_root}/results/logs/ocudu-scheduler-benchmark/${timestamp}"
report_dir="${native_root}/results/reports/ocudu-scheduler-benchmark/${timestamp}"
config_dir="${native_root}/configs/ocudu-scheduler-benchmark-native/${timestamp}"
data_dir="${native_root}/data/ocudu-scheduler-benchmark-native/${timestamp}"
run_dir="${native_root}/run/ocudu-scheduler-benchmark-native/${timestamp}"
netns_dir="${run_dir}/netns"
for path in "${log_dir}" "${report_dir}" "${config_dir}" "${data_dir}" "${run_dir}"; do
  [[ ! -e "${path}" && ! -L "${path}" ]] || usage_error "run path already exists: ${path}"
done
mkdir -p "${log_dir}" "${report_dir}" "${config_dir}" "${data_dir}" "${netns_dir}"

# --- seeded scenario ----------------------------------------------------------
scenario="${config_dir}/scenario-source.json"
if [[ -n "${scenario_override}" ]]; then
  cp "${scenario_override}" "${scenario}"
else
  # Routes cover the attach window as well: scenario time starts at the
  # measurement, but a long route costs nothing.
  "/usr/bin/python3" "${bench_dir}/make_scenario.py" --seed "${seed}" --base "${scenario_base}" \
    --duration-s "$((measure_seconds + 60))" --out "${scenario}" >"${log_dir}/scenario.log" 2>&1 || {
    cat "${log_dir}/scenario.log" >&2; usage_error "scenario generation failed"
  }
fi

declare -a renderer_args=(
  --repo-root "${repo_root}" --native-root "${native_root}"
  --output-dir "${config_dir}" --log-dir "${log_dir}"
  --scenario-config "${scenario}"
  --sched-a "${sched_a}" --sched-b "${sched_b}"
  --traffic-profile "${profile}" --seed "${seed}" --measure-seconds "${measure_seconds}"
)
[[ -z "${traffic_overrides}" ]] || renderer_args+=(--traffic-overrides "${traffic_overrides}")
if [[ "${sionna_awgn_snr_db}" != "off" ]]; then
  renderer_args+=(--awgn-snr-db "${sionna_awgn_snr_db}")
  [[ -z "${sionna_tx_power_dl}" ]] || renderer_args+=(--tx-power-dl "${sionna_tx_power_dl}")
  [[ -z "${sionna_tx_power_ul}" ]] || renderer_args+=(--tx-power-ul "${sionna_tx_power_ul}")
fi
[[ "${gnb_metrics_enabled}" == "1" ]] && renderer_args+=(--gnb-metrics)
"/usr/bin/python3" "${renderer}" "${renderer_args[@]}" >"${log_dir}/render.log" 2>&1 || {
  cat "${log_dir}/render.log" >&2; usage_error "config rendering failed"
}
# Optional gNB ZMQ receive gain (dB, <= 0). The OCUDU ZMQ radio scales its
# float input by this gain and then converts it to int16 with saturation at
# +-1.0; srsUE (tx_gain 50) puts ~+42 dB on the wire, so at 0 dB nearly every
# UL sample saturates. -55 brings the UL back to the gNB's own TX level.
gnb_rx_gain="${OCUDU_NATIVE_SB_GNB_RX_GAIN_DB:-}"
if [[ -n "${gnb_rx_gain}" ]]; then
  [[ "${gnb_rx_gain}" =~ ^(0|-[0-9]+([.][0-9]+)?)$ ]] || usage_error "OCUDU_NATIVE_SB_GNB_RX_GAIN_DB must be <= 0"
  for gnb_id in gnb0 gnb1; do
    grep -qx '  rx_gain: 0' "${config_dir}/${gnb_id}.yaml" || usage_error "${gnb_id}.yaml has no 'rx_gain: 0' line"
    sed -i "s/^  rx_gain: 0\$/  rx_gain: ${gnb_rx_gain}/" "${config_dir}/${gnb_id}.yaml"
  done
fi
for gnb_id in gnb0 gnb1; do
  "${gnb_binary}" -c "${config_dir}/${gnb_id}.yaml" --dryrun >"${log_dir}/${gnb_id}-dryrun.log" 2>&1 || {
    tail -20 "${log_dir}/${gnb_id}-dryrun.log" >&2; usage_error "${gnb_id} dry run failed"
  }
done

# --- broker build ---------------------------------------------------------------
channel_build="${OCUDU_NATIVE_CHANNEL_BUILD:-${native_root}/builds/ocudu-gpu-channel-cuda-release}"
cmake -S "${repo_root}" -B "${channel_build}" -DCMAKE_BUILD_TYPE=Release \
  -DOCUDU_GPU_CHANNEL_ENABLE_CUDA=ON -DCMAKE_CUDA_COMPILER="${cuda_compiler}" \
  -DOCUDU_GPU_CHANNEL_CUDA_ARCHITECTURES="${cuda_arch}" >"${log_dir}/cmake-configure.log" 2>&1 \
  || { tail -20 "${log_dir}/cmake-configure.log" >&2; usage_error "cmake configure"; }
cmake --build "${channel_build}" -j"$(nproc)" >"${log_dir}/cmake-build.log" 2>&1 \
  || { tail -20 "${log_dir}/cmake-build.log" >&2; usage_error "cmake build"; }
if [[ "${skip_ctest}" == "1" ]]; then
  echo "event=skip ctest reason=OCUDU_NATIVE_SB_SKIP_CTEST"
else
  CUDA_VISIBLE_DEVICES="${physical_gpu}" \
    ctest --test-dir "${channel_build}" --output-on-failure >"${log_dir}/ctest.log" 2>&1 \
    || { tail -20 "${log_dir}/ctest.log" >&2; usage_error "ctest"; }
fi

parent_netns="$(readlink /proc/self/ns/net)"
parent_mntns="$(readlink /proc/self/ns/mnt)"

/usr/bin/python3 - "${report_dir}/run-parameters.json" <<'PY' \
  "${seed}" "${sched_a}" "${sched_b}" "${profile}" "${traffic_overrides}" "${measure_seconds}" "${drain_seconds}" \
  "${attach_timeout}" "${sionna_update_hz}" "${sionna_awgn_snr_db}" "${sionna_tx_power_dl}" "${sionna_tx_power_ul}" \
  "${scenario}" "${scenario_override}" "${OCUDU_NATIVE_PLATFORM_PROFILE:-none}" \
  "${cpus_gnb_a}" "${cpus_gnb_b}" "${cpus_broker_a}" "${cpus_broker_b}" \
  "${cpus_ue0}" "${cpus_ue1}" "${cpus_ue2}" "${cpus_ue3}" "${cpus_aux}" "${gnb_metrics_enabled}" "${timestamp}"
import json, sys
(out, seed, sched_a, sched_b, profile, overrides, measure, drain, attach_timeout, hz, awgn, tx_dl, tx_ul,
 scenario, scenario_override, platform, gnb_a, gnb_b, broker_a, broker_b, ue0, ue1, ue2, ue3, aux,
 metrics, timestamp) = sys.argv[1:]
json.dump({
    "timestamp": timestamp, "seed": int(seed), "schedulers": {"a": sched_a, "b": sched_b},
    "traffic_profile": profile, "traffic_overrides": overrides or None,
    "measure_seconds": int(measure), "drain_seconds": int(drain), "attach_timeout_seconds": int(attach_timeout),
    "sionna": {"update_hz": float(hz), "awgn_snr_db": None if awgn == "off" else float(awgn),
               "tx_power_dl": float(tx_dl) if tx_dl else None, "tx_power_ul": float(tx_ul) if tx_ul else None,
               "timeline": "grid", "fanout": True},
    "scenario": scenario, "scenario_override": scenario_override or None,
    "platform_profile": platform,
    "cpus": {"gnb": {"a": gnb_a, "b": gnb_b}, "broker": {"a": broker_a, "b": broker_b},
             "ue": {"ue0": ue0, "ue1": ue1, "ue2": ue2, "ue3": ue3}, "aux": aux},
    "brokers": "both plain: own CUDA context, default stream, no realtime class",
    "gnb_metrics": int(metrics),
}, open(out, "w"), indent=2, sort_keys=True)
PY
printf 'event=native_scheduler_benchmark_parameters seed=%s a=%s b=%s profile=%s measure=%ss platform=%s\n' \
  "${seed}" "${sched_a}" "${sched_b}" "${profile}" "${measure_seconds}" "${OCUDU_NATIVE_PLATFORM_PROFILE:-none}"

declare -a inner_args=(
  --repo-root "${repo_root}" --native-root "${native_root}"
  --config-dir "${config_dir}" --log-dir "${log_dir}" --netns-dir "${netns_dir}"
  --timestamp "${timestamp}" --physical-gpu "${physical_gpu}"
  --channel-build "${channel_build}"
  --parent-netns "${parent_netns}" --parent-mntns "${parent_mntns}"
  --outer-uid "$(id -u)"
  --gnb-binary "${gnb_binary}" --gnb-start-timeout "${gnb_start_timeout}"
  --measure-seconds "${measure_seconds}" --drain-seconds "${drain_seconds}" --attach-timeout "${attach_timeout}"
  --sched-a "${sched_a}" --sched-b "${sched_b}"
  --control-endpoint-a "ipc://${run_dir}/control-a.sock" --telemetry-endpoint-a "ipc://${run_dir}/telemetry-a.sock"
  --control-endpoint-b "ipc://${run_dir}/control-b.sock" --telemetry-endpoint-b "ipc://${run_dir}/telemetry-b.sock"
  --sionna-python "${sionna_python}" --sionna-update-hz "${sionna_update_hz}"
  --gnb-metrics "${gnb_metrics_enabled}"
  --cpus-gnb-a "${cpus_gnb_a}" --cpus-gnb-b "${cpus_gnb_b}"
  --cpus-broker-a "${cpus_broker_a}" --cpus-broker-b "${cpus_broker_b}"
  --cpus-ues "${cpus_ue0}|${cpus_ue1}|${cpus_ue2}|${cpus_ue3}" --cpus-aux "${cpus_aux}"
)

web_pid=""
if [[ "${web_ui}" == "1" ]]; then
  # The server must not inherit the gate lock: an orphaned server would
  # otherwise block every later gate.
  "${sionna_python}" "${bench_dir}/server.py" --port "${web_port}" \
    --runs-root "${native_root}" --follow-latest >"${log_dir}/web-ui.log" 2>&1 {lock_fd}<&- &
  web_pid="$!"
  printf 'Scheduler benchmark UI: http://127.0.0.1:%s\n' "${web_port}"
fi

set +e
unshare --user --map-root-user --net --mount --fork --kill-child --propagation private \
  "${inner}" "${inner_args[@]}"
inner_status="$?"
set -e

# The final report from the complete logs (the live UI used partial ones).
/usr/bin/python3 "${bench_dir}/analyze.py" --log-dir "${log_dir}" --config-dir "${config_dir}" \
  --out "${report_dir}/benchmark-report.json" 2>"${log_dir}/analyze.log" || \
  echo "event=analyze_failed log=${log_dir}/analyze.log" >&2
cat "${log_dir}/analyze.log" 2>/dev/null || true

if [[ -n "${web_pid}" ]]; then
  if [[ "${OCUDU_NATIVE_SB_WEB_LINGER_SECONDS:-0}" =~ ^[1-9][0-9]*$ ]]; then
    printf 'event=web_ui_linger seconds=%s url=http://127.0.0.1:%s\n' "${OCUDU_NATIVE_SB_WEB_LINGER_SECONDS}" "${web_port}"
    sleep "${OCUDU_NATIVE_SB_WEB_LINGER_SECONDS}"
  fi
  # The server exits 143 on SIGTERM; that is not the gate's status.
  kill "${web_pid}" >/dev/null 2>&1 || true
  wait "${web_pid}" >/dev/null 2>&1 || true
fi

summary="${report_dir}/gate-summary.json"
if [[ "${inner_status}" -eq 0 && -f "${summary}" ]]; then
  printf 'event=native_scheduler_benchmark_gate result=pass summary="%s" report="%s"\n' \
    "${summary}" "${report_dir}/benchmark-report.json"
else
  [[ -f "${summary}" ]] && cat "${summary}" >&2
  printf 'event=native_scheduler_benchmark_gate result=fail status=%s logs="%s"\n' "${inner_status}" "${log_dir}" >&2
fi
exit "${inner_status}"
