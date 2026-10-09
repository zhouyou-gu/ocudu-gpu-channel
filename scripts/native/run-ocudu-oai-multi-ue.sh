#!/usr/bin/env bash
# Native TDD multi-UE attach gate with OAI nrUEs (X3/X4, CROSSTALK_MILESTONES.md):
# one OCUDU gNB on a TDD n78 20 MHz / 30 kHz cell, two OAI nrUEs, Open5GS, and
# this tree's broker in Sionna mode, with no container runtime anywhere.
#
# It is the OAI sibling of run-ocudu-multi-ue.sh (srsUE, FDD band 3): the same
# lock, port preflight, broker-from-tree build, rootless namespaces, Sionna
# bridge, strict verdict and result layout. What differs is the cell (TDD, so a
# UE<->UE edge is physical and the broker admits it: every port is
# `carrier: n78`), the UE process (OAI nrUE: whole process inside its netns,
# ZMQ over a veth pair, patched ZMQ module, uecap, --cont-fo-comp, rx_gain)
# and the verdict tokens (OAI's). Sionna mode is the only mode: the TDD
# topology is generated from the scenario (render-oai-multi-ue-configs.py).
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/../.." && pwd)"
# shellcheck source=env.sh
source "${script_dir}/env.sh"

native_root="${OCUDU_NATIVE_ROOT}"
physical_gpu="${OCUDU_NATIVE_GPU_DEVICE:-0}"
cuda_compiler="${CUDACXX:-/opt/conda/envs/cuda128/bin/nvcc}"
cuda_arch="${OCUDU_NATIVE_CUDA_ARCH:-120}"
inner="${script_dir}/run-ocudu-oai-multi-ue-inner.sh"
renderer="${script_dir}/render-oai-multi-ue-configs.py"
gnb_binary="${OCUDU_NATIVE_GNB_BINARY:-${native_root}/builds/ocudu-zmq-release/apps/gnb/gnb}"
sionna_scenario="${OCUDU_NATIVE_SIONNA_SCENARIO:-${repo_root}/use_cases/configs/sionna/scenarios/robot_ring/robot-ring-walk-tdd.json}"
sionna_python="${OCUDU_NATIVE_SIONNA_PYTHON:-}"
sionna_update_hz="${OCUDU_NATIVE_SIONNA_UPDATE_HZ:-10}"
# Read-only web UI; 0 (default) = none. The srsUE gate's default 8080 is
# often taken on a shared host.
web_port="${OCUDU_NATIVE_WEB_PORT:-0}"
# Receiver noise floor and transmit levels: the srsUE Sionna gate's knobs with
# the same meaning (render-sionna-multi-ue-configs.py rx_noise_powers). The UL
# level defaults to the renderer's provisional OAI_UE_TX_POWER (first
# calibration capture); OCUDU_NATIVE_SIONNA_TX_POWER_UL sets it per run.
sionna_awgn_snr_db="${OCUDU_NATIVE_SIONNA_AWGN_SNR_DB:-40}"
sionna_tx_power_dl="${OCUDU_NATIVE_SIONNA_TX_POWER_DL:-}"
sionna_tx_power_ul="${OCUDU_NATIVE_SIONNA_TX_POWER_UL:-}"
sionna_ul_power_offset_db="${OCUDU_NATIVE_SIONNA_UL_POWER_OFFSET_DB:-}"
# Run length (broker --duration), side processes, wire capture: the srsUE
# multi-UE gate's knobs, same names, same placeholders.
mue_duration_seconds="${OCUDU_NATIVE_MUE_DURATION_SECONDS:-240}"
mue_ue_exec="${OCUDU_NATIVE_MUE_UE_EXEC:-}"
mue_root_exec="${OCUDU_NATIVE_MUE_ROOT_EXEC:-}"
mue_strict_realtime="${OCUDU_NATIVE_MUE_STRICT_REALTIME:-0}"
mue_wire_capture_samples="${OCUDU_NATIVE_MUE_WIRE_CAPTURE_SAMPLES:-0}"
mue_wire_capture_skip_seconds="${OCUDU_NATIVE_MUE_WIRE_CAPTURE_SKIP_SECONDS:-60}"
# Seconds between the two nrUE starts (0 = together). The gNB hears nothing
# until every UE lane streams (run-ocudu-multi-ue-inner.sh), so a long hold
# only delays everything; a few seconds separates the two cold syncs.
mue_stagger_seconds="${OCUDU_NATIVE_OAI_MUE_STAGGER_SECONDS:-0}"
# Per-UE CPU lists, ';'-separated (taskset -c syntax per UE). Empty = derive
# from the platform profile (see below); OCUDU_NATIVE_PLATFORM=none = unpinned.
mue_ue_cpus="${OCUDU_NATIVE_OAI_MUE_UE_CPUS:-}"
audited_ocudu="a1916edcdbcd70ba6e0af47ee87be061dad5a4e4"
audited_oai="2b69bde6aeafe892cda1531a0f0cbba2e37792cd"
audited_open5gs="d9d3abdd480be96fac3bc8a997e83446648763ca"

usage_error()
{
  printf 'error: %s\n' "$1" >&2
  exit 2
}

[[ "$#" -eq 0 ]] || usage_error "usage: $0 (knobs are environment variables)"
[[ "${sionna_scenario}" == /* && -f "${sionna_scenario}" && ! -L "${sionna_scenario}" ]] || \
  usage_error "OCUDU_NATIVE_SIONNA_SCENARIO must be an absolute regular file"
[[ -x "${sionna_python}" ]] || usage_error "OCUDU_NATIVE_SIONNA_PYTHON must point at the Sionna interpreter"
[[ "${web_port}" =~ ^(0|[1-9][0-9]*)$ && "${web_port}" -le 65535 ]] || usage_error "invalid OCUDU_NATIVE_WEB_PORT: ${web_port}"
[[ "${physical_gpu}" =~ ^(0|[1-9][0-9]*)$ && "${physical_gpu}" -le 255 ]] || usage_error "invalid GPU device"
[[ -x "${cuda_compiler}" ]] || usage_error "missing CUDA compiler: ${cuda_compiler}"
number_re='^-?[0-9]+([.][0-9]+)?([eE][-+]?[0-9]+)?$'
[[ "${sionna_awgn_snr_db}" == "off" || "${sionna_awgn_snr_db}" =~ ${number_re} ]] || \
  usage_error "OCUDU_NATIVE_SIONNA_AWGN_SNR_DB must be a number or off"
for knob in sionna_tx_power_dl sionna_tx_power_ul sionna_ul_power_offset_db; do
  [[ -z "${!knob}" || "${!knob}" =~ ${number_re} ]] || usage_error "${knob} must be a number"
done
[[ "${mue_duration_seconds}" =~ ^[1-9][0-9]*$ && "${mue_duration_seconds}" -ge 160 ]] || \
  usage_error "OCUDU_NATIVE_MUE_DURATION_SECONDS must be an integer >= 160"
[[ "${mue_strict_realtime}" =~ ^[01]$ ]] || usage_error "OCUDU_NATIVE_MUE_STRICT_REALTIME must be 0 or 1"
[[ "${mue_wire_capture_samples}" =~ ^(0|[1-9][0-9]*)$ ]] || usage_error "OCUDU_NATIVE_MUE_WIRE_CAPTURE_SAMPLES must be a non-negative integer"
[[ "${mue_wire_capture_skip_seconds}" =~ ^(0|[1-9][0-9]*)$ ]] || usage_error "OCUDU_NATIVE_MUE_WIRE_CAPTURE_SKIP_SECONDS must be a non-negative integer"
[[ "${mue_stagger_seconds}" =~ ^(0|[1-9][0-9]*)$ && "${mue_stagger_seconds}" -le 60 ]] || \
  usage_error "OCUDU_NATIVE_OAI_MUE_STAGGER_SECONDS must be an integer in [0, 60]"
for command_name in unshare nsenter ip mount umount flock cmake ctest ss setsid stdbuf taskset setpriv; do
  command -v "${command_name}" >/dev/null 2>&1 || usage_error "missing command: ${command_name}"
done
for path in "${inner}" "${renderer}" "${gnb_binary}" \
  "${native_root}/builds/oai-zmq-release/nr-uesoftmodem" \
  "${native_root}/builds/oai-zmq-release/liboai_zmqdevif.so" \
  "${native_root}/builds/open5gs-v2.7.6/tests/app/5gc" \
  "${native_root}/install/mongodb-6.0.29/bin/mongod" \
  "${native_root}/src/oai/targets/PROJECTS/GENERIC-NR-5GC/CONF/uecap_ports1.xml" \
  "${repo_root}/use_cases/configs/ran/oai/nrue_zmq_multi_ue.conf.in" \
  "${repo_root}/use_cases/configs/ran/open5gs/subscriber-multi-ue.csv" \
  "${repo_root}/apps/sionna_bridge/run_bridge.py"; do
  [[ -e "${path}" ]] || usage_error "missing required path: ${path}"
done
[[ -c /dev/net/tun ]] || usage_error "/dev/net/tun is absent"
[[ "$(git -C "${native_root}/src/ocudu" rev-parse HEAD)" == "${audited_ocudu}" ]] || usage_error "OCUDU revision mismatch"
[[ "$(git -C "${native_root}/src/oai" rev-parse HEAD)" == "${audited_oai}" ]] || usage_error "OAI revision mismatch"
[[ "$(git -C "${native_root}/src/open5gs" rev-parse HEAD)" == "${audited_open5gs}" ]] || usage_error "Open5GS revision mismatch"
if [[ "${OCUDU_NATIVE_SKIP_WORKSPACE_LOCK:-0}" == "1" ]]; then
  echo "event=skip x86_native_workspace_lock"
else
  "/usr/bin/python3" "${script_dir}/verify-workspace-lock.py" \
    --root "${native_root}" --repo-root "${repo_root}" --lock "${script_dir}/native-workspace.lock.json"
fi
grep -qx 'ENABLE_ZEROMQ:BOOL=ON' "${native_root}/builds/ocudu-zmq-release/CMakeCache.txt" || usage_error "gNB lacks ZMQ"

# OAI gate defaults (oai-gate-defaults.sh / oai-local-patches.sh): the patched
# ZMQ radio module, the UE RX gain, continuous FO compensation and the platform
# CPU placement. No MPS: this gate runs the CPU gNB (OCUDU_NATIVE_GNB_BINARY
# may point at another, but a CUDA gNB is then without MPS -- say so).
# shellcheck source=oai-gate-defaults.sh
source "${script_dir}/oai-gate-defaults.sh"
# shellcheck source=oai-local-patches.sh
source "${script_dir}/oai-local-patches.sh"
resolve_oai_zmq_module "${native_root}" || usage_error "OAI ZMQ module selection failed"
oai_gate_ue_rx_gain || usage_error "UE RX gain selection failed"
oai_gate_ue_fo_comp || usage_error "UE FO compensation selection failed"
oai_gate_platform || usage_error "platform profile resolution failed"
if ldd "${gnb_binary}" 2>/dev/null | grep -Eq 'libcudart|libcuda\.so'; then
  echo "event=warning gnb_uses_cuda=1 mps=off (this gate does not start MPS; see run-ocudu-oai-1x1.sh)"
fi
# UE CPU placement. The spark-gb10 profile sizes `nrue` (18,19) for ONE OAI
# nrUE; two of them get one profile core each plus one of the host's A725
# cores (4 and 14; the ten X925 cores 5-9,15-19 are all taken by gNB, broker
# and the two UE anchors). Symmetric on purpose, so the two UEs are the same
# kind of process for the UE<->UE comparison. Any other profile or an env
# OCUDU_NATIVE_NRUE_CPUS: both UEs share that set. OCUDU_NATIVE_OAI_MUE_UE_CPUS
# ("cpus0;cpus1") overrides everything.
if [[ -z "${mue_ue_cpus}" ]]; then
  if [[ "${OCUDU_NATIVE_PLATFORM_PROFILE:-none}" == "spark-gb10" && "${OCUDU_NATIVE_NRUE_CPUS:-}" == "18,19" ]]; then
    mue_ue_cpus="18,4;19,14"
  elif [[ -n "${OCUDU_NATIVE_NRUE_CPUS:-}" ]]; then
    mue_ue_cpus="${OCUDU_NATIVE_NRUE_CPUS};${OCUDU_NATIVE_NRUE_CPUS}"
  fi
fi
if [[ -n "${mue_ue_cpus}" ]]; then
  [[ "${mue_ue_cpus}" =~ ^[0-9,-]+\;[0-9,-]+$ ]] || usage_error "OCUDU_NATIVE_OAI_MUE_UE_CPUS must be two taskset lists separated by ';'"
fi
export OCUDU_NATIVE_OAI_MUE_UE_CPUS="${mue_ue_cpus}"
# The stack runs in its own network namespace; host listeners cannot collide.
# The check stays on unless OCUDU_NATIVE_ALLOW_HOST_PORTS=1.
[[ "${OCUDU_NATIVE_ALLOW_HOST_PORTS:-0}" == "1" ]] || \
for port in 2000 2001 2100 2101 2102 2103 27017 38412 7777; do
  ss -H -ltn "sport = :${port}" | grep -q . && usage_error "TCP port ${port} is already listening"
done

exec {lock_fd}<"${BASH_SOURCE[0]}"
flock -n "${lock_fd}" || usage_error "another native OAI multi-UE gate is running"
export OCUDU_NATIVE_GATE_LOCK_FD="${lock_fd}"

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
gate_name="ocudu-oai-multi-ue"
log_dir="${native_root}/results/logs/${gate_name}/${timestamp}"
report_dir="${native_root}/results/reports/${gate_name}/${timestamp}"
config_dir="${native_root}/configs/${gate_name}-native/${timestamp}"
data_dir="${native_root}/data/${gate_name}-native/${timestamp}"
run_dir="${native_root}/run/${gate_name}-native/${timestamp}"
netns_dir="${run_dir}/netns"
for path in "${log_dir}" "${report_dir}" "${config_dir}" "${data_dir}" "${run_dir}"; do
  [[ ! -e "${path}" && ! -L "${path}" ]] || usage_error "run path already exists: ${path}"
done
mkdir -p "${log_dir}" "${report_dir}" "${config_dir}" "${data_dir}" "${netns_dir}"

declare -a renderer_args=(
  --repo-root "${repo_root}" --native-root "${native_root}"
  --output-dir "${config_dir}" --log-dir "${log_dir}"
  --scenario-config "${sionna_scenario}"
)
if [[ "${sionna_awgn_snr_db}" != "off" ]]; then
  renderer_args+=(--awgn-snr-db "${sionna_awgn_snr_db}")
fi
[[ -z "${sionna_tx_power_dl}" ]] || renderer_args+=(--tx-power-dl "${sionna_tx_power_dl}")
[[ -z "${sionna_tx_power_ul}" ]] || renderer_args+=(--tx-power-ul "${sionna_tx_power_ul}")
[[ -z "${sionna_ul_power_offset_db}" ]] || renderer_args+=(--ul-power-offset-db "${sionna_ul_power_offset_db}")
"/usr/bin/python3" "${renderer}" "${renderer_args[@]}" >"${log_dir}/render.log" 2>&1 || {
  cat "${log_dir}/render.log" >&2; usage_error "config rendering failed"
}
cat "${log_dir}/render.log"
"${gnb_binary}" -c "${config_dir}/gnb.yaml" --dryrun >"${log_dir}/gnb-dryrun.log" 2>&1 || {
  tail -20 "${log_dir}/gnb-dryrun.log" >&2; usage_error "gNB dry run failed"
}

# Build the broker from THIS tree (OCUDU_NATIVE_CHANNEL_BUILD selects the
# build directory, one per source tree).
channel_build="${OCUDU_NATIVE_CHANNEL_BUILD:-${native_root}/builds/ocudu-gpu-channel-cuda-release}"
cmake -S "${repo_root}" -B "${channel_build}" -DCMAKE_BUILD_TYPE=Release \
  -DOCUDU_GPU_CHANNEL_ENABLE_CUDA=ON -DCMAKE_CUDA_COMPILER="${cuda_compiler}" \
  -DCMAKE_CUDA_ARCHITECTURES="${cuda_arch}" -DOCUDU_GPU_CHANNEL_CUDA_ARCHITECTURES="${cuda_arch}" \
  >"${log_dir}/cmake-configure.log" 2>&1 || { tail -20 "${log_dir}/cmake-configure.log" >&2; usage_error "cmake configure"; }
cmake --build "${channel_build}" -j"$(nproc)" >"${log_dir}/cmake-build.log" 2>&1 \
  || { tail -20 "${log_dir}/cmake-build.log" >&2; usage_error "cmake build"; }
CUDA_VISIBLE_DEVICES="${physical_gpu}" ctest --test-dir "${channel_build}" --output-on-failure \
  >"${log_dir}/ctest.log" 2>&1 || { tail -20 "${log_dir}/ctest.log" >&2; usage_error "ctest"; }

parent_netns="$(readlink /proc/self/ns/net)"
parent_mntns="$(readlink /proc/self/ns/mnt)"
control_endpoint="ipc://${run_dir}/control.sock"
telemetry_endpoint="ipc://${run_dir}/telemetry.sock"
sionna_status_jsonl="${log_dir}/sionna-status.jsonl"
declare -a inner_args=(
  --repo-root "${repo_root}" --native-root "${native_root}"
  --config-dir "${config_dir}" --log-dir "${log_dir}" --report-dir "${report_dir}"
  --netns-dir "${netns_dir}" --timestamp "${timestamp}" --physical-gpu "${physical_gpu}"
  --channel-build "${channel_build}" --gnb-binary "${gnb_binary}"
  --parent-netns "${parent_netns}" --parent-mntns "${parent_mntns}" --outer-uid "$(id -u)"
  --run-duration-seconds "${mue_duration_seconds}" --strict-realtime "${mue_strict_realtime}"
  --ue-exec "${mue_ue_exec}" --root-exec "${mue_root_exec}"
  --wire-capture-samples "${mue_wire_capture_samples}"
  --wire-capture-skip-seconds "${mue_wire_capture_skip_seconds}"
  --stagger-seconds "${mue_stagger_seconds}"
  --control-endpoint "${control_endpoint}" --telemetry-endpoint "${telemetry_endpoint}"
  --sionna-python "${sionna_python}" --sionna-bridge "${repo_root}/apps/sionna_bridge/run_bridge.py"
  --sionna-scenario-config "${sionna_scenario}" --sionna-status-jsonl "${sionna_status_jsonl}"
  --sionna-update-hz "${sionna_update_hz}"
)
/usr/bin/python3 - "${report_dir}/run-parameters.json" <<'PY' \
  "${mue_duration_seconds}" "${mue_strict_realtime}" "${sionna_awgn_snr_db}" \
  "${sionna_tx_power_dl}" "${sionna_tx_power_ul}" "${sionna_ul_power_offset_db}" \
  "${OCUDU_NATIVE_PLATFORM_PROFILE:-none}" "${OCUDU_NATIVE_GNB_CPUS:-}" "${OCUDU_NATIVE_BROKER_CPUS:-}" \
  "${mue_ue_cpus}" "${OCUDU_NATIVE_BROKER_ENV:-}" "${mue_ue_exec}" "${mue_root_exec}" \
  "${mue_wire_capture_samples}" "${mue_wire_capture_skip_seconds}" "${sionna_scenario}" \
  "${mue_stagger_seconds}" "${OAI_ZMQ_MODULE_VARIANT}" "${OCUDU_NATIVE_OAI_SHLIBPATH}" \
  "${OAI_GATE_UE_RX_GAIN_DB:-}" "${OAI_GATE_UE_CONT_FO_COMP:-}" "${gnb_binary}" "${config_dir}/oai-multi-ue-shape.json"
import json, sys
(out, duration, strict, awgn, tx_dl, tx_ul, ul_offset, profile, gnb_cpus, broker_cpus, ue_cpus,
 broker_env, ue_exec, root_exec, cap_samples, cap_skip, scenario, stagger, zmq_variant, zmq_dir,
 rx_gain, fo_comp, gnb_binary, shape_path) = sys.argv[1:]
shape = json.load(open(shape_path))
json.dump({
    "gate": "ocudu-oai-multi-ue", "channel_mode": "sionna", "ue": "oai-nrue", "ue_count": 2,
    "run_duration_seconds": int(duration), "strict_realtime": int(strict),
    "sionna_awgn_snr_db": None if awgn == "off" else float(awgn),
    "sionna_tx_power_dl": float(tx_dl) if tx_dl else None,
    "sionna_tx_power_ul": float(tx_ul) if tx_ul else None,
    "sionna_ul_power_offset_db": float(ul_offset) if ul_offset else None,
    "ue_tx_scale_db": shape["ue_tx_scale_db"], "ue_tx_power_source": shape["ue_tx_power_source"],
    "cell": shape["cell"], "nrue_radio_args": shape["nrue_radio_args"],
    "platform_profile": profile,
    "cpus": {"gnb": gnb_cpus, "broker": broker_cpus, "ue": ue_cpus.split(";") if ue_cpus else []},
    "broker_env": broker_env, "ue_exec": ue_exec, "root_exec": root_exec,
    "wire_capture": {"samples": int(cap_samples), "skip_seconds": int(cap_skip)},
    "sionna_scenario": scenario, "stagger_seconds": int(stagger),
    "oai_zmq_module": {"variant": zmq_variant, "dir": zmq_dir},
    "ue_rx_gain_db": rx_gain or None, "ue_cont_fo_comp": fo_comp or None, "gnb_binary": gnb_binary,
}, open(out, "w"), indent=2, sort_keys=True)
PY
oai_gate_defaults_line | tee "${log_dir}/gate-defaults.log"
printf 'event=native_oai_multi_ue_gate_parameters duration=%ss cell=tdd-n78-20mhz-30khz scenario=%s ue_cpus="%s" stagger=%ss awgn_snr_db=%s wire_capture=%s\n' \
  "${mue_duration_seconds}" "${sionna_scenario##*/}" "${mue_ue_cpus}" "${mue_stagger_seconds}" \
  "${sionna_awgn_snr_db}" "${mue_wire_capture_samples}"

web_pid=""
if [[ "${web_port}" != 0 ]]; then
  "${sionna_python}" "${repo_root}/apps/dashboard/server.py" \
    --port "${web_port}" --telemetry-endpoint "${telemetry_endpoint}" \
    --status-jsonl "${sionna_status_jsonl}" --index "${repo_root}/apps/dashboard/index.html" \
    >"${log_dir}/web-ui.log" 2>&1 &
  web_pid="$!"
  printf 'Web UI: http://127.0.0.1:%s\n' "${web_port}"
fi

set +e
unshare --user --map-root-user --net --mount --fork --kill-child --propagation private \
  "${inner}" "${inner_args[@]}"
inner_status="$?"
set -e
if [[ -n "${web_pid}" ]]; then
  kill "${web_pid}" >/dev/null 2>&1 || true
  wait "${web_pid}" >/dev/null 2>&1 || true
fi

summary="${report_dir}/attach-summary.json"
if [[ "${inner_status}" -eq 0 && -f "${summary}" ]]; then
  printf 'event=native_oai_multi_ue_attach_gate result=pass summary="%s"\n' "${summary}"
else
  [[ -f "${summary}" ]] && cat "${summary}" >&2
  printf 'event=native_oai_multi_ue_attach_gate result=fail status=%s logs="%s"\n' "${inner_status}" "${log_dir}" >&2
fi
exit "${inner_status}"
