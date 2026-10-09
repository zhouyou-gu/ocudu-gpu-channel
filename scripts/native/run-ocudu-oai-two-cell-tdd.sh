#!/usr/bin/env bash
# Native two-cell TDD gate with OAI nrUEs (X5, CROSSTALK_MILESTONES.md): two
# OCUDU gNB processes on ONE TDD n78 20 MHz / 30 kHz carrier (co-channel
# cells, distinct PCI / gnb_id / addresses / ZMQ ports / PRACH root), one OAI
# nrUE under each, Open5GS, and this tree's broker in Sionna mode, with no
# container runtime anywhere.
#
# It is run-ocudu-oai-multi-ue.sh (X3/X4: TDD cell, OAI nrUE launch, strict
# OAI verdict, wire capture, CPU placement) with the second cell of
# run-ocudu-multi-gnb.sh (two gNBs started together, each UE expected on its
# own cell via the camped PCI). The one new knob is the TDD pattern per cell:
#
#   OCUDU_NATIVE_TDD_PATTERNS=same        both cells 7D/1S/2U (default)
#   OCUDU_NATIVE_TDD_PATTERNS="7D2U;3D6U" cell A 7D/1S/2U, cell B 3D/1S/6U:
#       in slots 3(S)-6 of every 5 ms cell B's UE transmits uplink while cell
#       A's UE receives downlink -- cross-link interference through a
#       ue1->ue0 edge if the scenario has one (two-cell-tdd-10.json).
#   OCUDU_NATIVE_TDD_GNB_CPUS="cpus0;cpus1"  taskset list per gNB. Default on
#       the spark-gb10 profile: "5,6,0,1,2;7,8,10,11,12" (two X925 + three
#       A725 each; the profile's five gNB cores 5-9 cannot hold two gNBs with
#       the broker on 15-17 and the UEs on 18,4 / 19,14; core 9 is left to
#       the Sionna bridge and the gate itself). Empty on other profiles.
#   OCUDU_NATIVE_TDD_STDOUT_METRICS=1     per-UE gNB stdout metrics table
#       (DL/UL HARQ ok/nok, CQI, PUSCH SNR once a second) on the console log.
#   OCUDU_NATIVE_SIONNA_SCENARIO          default use_cases/configs/sionna/scenarios/robot_ring/two-cell-tdd-8.json
# Every other knob is the X3 gate's, same names, same meaning
# (OCUDU_NATIVE_MUE_*, OCUDU_NATIVE_SIONNA_*, OCUDU_NATIVE_OAI_MUE_*).
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/../.." && pwd)"
# shellcheck source=env.sh
source "${script_dir}/env.sh"

native_root="${OCUDU_NATIVE_ROOT}"
physical_gpu="${OCUDU_NATIVE_GPU_DEVICE:-0}"
cuda_compiler="${CUDACXX:-/opt/conda/envs/cuda128/bin/nvcc}"
cuda_arch="${OCUDU_NATIVE_CUDA_ARCH:-120}"
inner="${script_dir}/run-ocudu-oai-two-cell-tdd-inner.sh"
renderer="${script_dir}/render-oai-two-cell-tdd-configs.py"
gnb_binary="${OCUDU_NATIVE_GNB_BINARY:-${native_root}/builds/ocudu-zmq-release/apps/gnb/gnb}"
sionna_scenario="${OCUDU_NATIVE_SIONNA_SCENARIO:-${repo_root}/use_cases/configs/sionna/scenarios/robot_ring/two-cell-tdd-8.json}"
sionna_python="${OCUDU_NATIVE_SIONNA_PYTHON:-}"
sionna_update_hz="${OCUDU_NATIVE_SIONNA_UPDATE_HZ:-10}"
web_port="${OCUDU_NATIVE_WEB_PORT:-0}"
sionna_awgn_snr_db="${OCUDU_NATIVE_SIONNA_AWGN_SNR_DB:-40}"
sionna_tx_power_dl="${OCUDU_NATIVE_SIONNA_TX_POWER_DL:-}"
sionna_tx_power_ul="${OCUDU_NATIVE_SIONNA_TX_POWER_UL:-}"
sionna_ul_power_offset_db="${OCUDU_NATIVE_SIONNA_UL_POWER_OFFSET_DB:-}"
mue_duration_seconds="${OCUDU_NATIVE_MUE_DURATION_SECONDS:-240}"
mue_ue_exec="${OCUDU_NATIVE_MUE_UE_EXEC:-}"
mue_root_exec="${OCUDU_NATIVE_MUE_ROOT_EXEC:-}"
mue_strict_realtime="${OCUDU_NATIVE_MUE_STRICT_REALTIME:-0}"
mue_wire_capture_samples="${OCUDU_NATIVE_MUE_WIRE_CAPTURE_SAMPLES:-0}"
mue_wire_capture_skip_seconds="${OCUDU_NATIVE_MUE_WIRE_CAPTURE_SKIP_SECONDS:-60}"
mue_stagger_seconds="${OCUDU_NATIVE_OAI_MUE_STAGGER_SECONDS:-0}"
mue_ue_cpus="${OCUDU_NATIVE_OAI_MUE_UE_CPUS:-}"
tdd_patterns="${OCUDU_NATIVE_TDD_PATTERNS:-same}"
tdd_gnb_cpus="${OCUDU_NATIVE_TDD_GNB_CPUS:-}"
tdd_stdout_metrics="${OCUDU_NATIVE_TDD_STDOUT_METRICS:-1}"
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
[[ "${tdd_patterns}" == "same" || "${tdd_patterns}" =~ ^[0-9]+D[0-9]+U\;[0-9]+D[0-9]+U$ ]] || \
  usage_error "OCUDU_NATIVE_TDD_PATTERNS must be 'same' or 'A;B' with patterns like 7D2U"
[[ "${tdd_stdout_metrics}" =~ ^[01]$ ]] || usage_error "OCUDU_NATIVE_TDD_STDOUT_METRICS must be 0 or 1"
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
  "${repo_root}/scripts/sionna_rt/run_bridge.py"; do
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
# UE placement: the X3 gate's (one profile core + one A725 core per UE).
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
# gNB placement: two gNBs on the profile's five big gNB cores would halve what
# one gNB had, so each cell gets two X925 cores plus three A725 cores
# (symmetric, so the two cells are the same kind of process for the pattern
# comparison). Any other profile: both gNBs on OCUDU_NATIVE_GNB_CPUS.
if [[ -z "${tdd_gnb_cpus}" ]]; then
  if [[ "${OCUDU_NATIVE_PLATFORM_PROFILE:-none}" == "spark-gb10" && "${OCUDU_NATIVE_GNB_CPUS:-}" == "5-9" ]]; then
    tdd_gnb_cpus="5,6,0,1,2;7,8,10,11,12"
  elif [[ -n "${OCUDU_NATIVE_GNB_CPUS:-}" ]]; then
    tdd_gnb_cpus="${OCUDU_NATIVE_GNB_CPUS};${OCUDU_NATIVE_GNB_CPUS}"
  fi
fi
if [[ -n "${tdd_gnb_cpus}" ]]; then
  [[ "${tdd_gnb_cpus}" =~ ^[0-9,-]+\;[0-9,-]+$ ]] || usage_error "OCUDU_NATIVE_TDD_GNB_CPUS must be two taskset lists separated by ';'"
fi
export OCUDU_NATIVE_TDD_GNB_CPUS="${tdd_gnb_cpus}"
[[ "${OCUDU_NATIVE_ALLOW_HOST_PORTS:-0}" == "1" ]] || \
for port in 2000 2001 2010 2011 2100 2101 2102 2103 27017 38412 7777; do
  ss -H -ltn "sport = :${port}" | grep -q . && usage_error "TCP port ${port} is already listening"
done

exec {lock_fd}<"${BASH_SOURCE[0]}"
flock -n "${lock_fd}" || usage_error "another native OAI two-cell TDD gate is running"
export OCUDU_NATIVE_GATE_LOCK_FD="${lock_fd}"

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
gate_name="ocudu-oai-two-cell-tdd"
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
  --scenario-config "${sionna_scenario}" --tdd-patterns "${tdd_patterns}"
  --stdout-metrics "${tdd_stdout_metrics}"
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
for cell in gnb0 gnb1; do
  "${gnb_binary}" -c "${config_dir}/${cell}.yaml" --dryrun >"${log_dir}/${cell}-dryrun.log" 2>&1 || {
    tail -20 "${log_dir}/${cell}-dryrun.log" >&2; usage_error "${cell} dry run failed"
  }
done

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
  --sionna-python "${sionna_python}" --sionna-bridge "${repo_root}/scripts/sionna_rt/run_bridge.py"
  --sionna-scenario-config "${sionna_scenario}" --sionna-status-jsonl "${sionna_status_jsonl}"
  --sionna-update-hz "${sionna_update_hz}"
)
/usr/bin/python3 - "${report_dir}/run-parameters.json" <<'PY' \
  "${mue_duration_seconds}" "${mue_strict_realtime}" "${sionna_awgn_snr_db}" \
  "${sionna_tx_power_dl}" "${sionna_tx_power_ul}" "${sionna_ul_power_offset_db}" \
  "${OCUDU_NATIVE_PLATFORM_PROFILE:-none}" "${tdd_gnb_cpus}" "${OCUDU_NATIVE_BROKER_CPUS:-}" \
  "${mue_ue_cpus}" "${OCUDU_NATIVE_BROKER_ENV:-}" "${mue_ue_exec}" "${mue_root_exec}" \
  "${mue_wire_capture_samples}" "${mue_wire_capture_skip_seconds}" "${sionna_scenario}" \
  "${mue_stagger_seconds}" "${OAI_ZMQ_MODULE_VARIANT}" "${OCUDU_NATIVE_OAI_SHLIBPATH}" \
  "${OAI_GATE_UE_RX_GAIN_DB:-}" "${OAI_GATE_UE_CONT_FO_COMP:-}" "${gnb_binary}" "${config_dir}/two-cell-tdd-shape.json" \
  "${tdd_patterns}"
import json, sys
(out, duration, strict, awgn, tx_dl, tx_ul, ul_offset, profile, gnb_cpus, broker_cpus, ue_cpus,
 broker_env, ue_exec, root_exec, cap_samples, cap_skip, scenario, stagger, zmq_variant, zmq_dir,
 rx_gain, fo_comp, gnb_binary, shape_path, patterns) = sys.argv[1:]
shape = json.load(open(shape_path))
json.dump({
    "gate": "ocudu-oai-two-cell-tdd", "channel_mode": "sionna", "ue": "oai-nrue", "ue_count": 2, "cell_count": 2,
    "run_duration_seconds": int(duration), "strict_realtime": int(strict),
    "sionna_awgn_snr_db": None if awgn == "off" else float(awgn),
    "sionna_tx_power_dl": float(tx_dl) if tx_dl else None,
    "sionna_tx_power_ul": float(tx_ul) if tx_ul else None,
    "sionna_ul_power_offset_db": float(ul_offset) if ul_offset else None,
    "ue_tx_scale_db": shape["ue_tx_scale_db"], "ue_tx_power_source": shape["ue_tx_power_source"],
    "cell": shape["cell"], "cells": shape["cells"], "tdd_patterns": patterns, "tdd_mode": shape["tdd_mode"],
    "cli_slots_ue1_into_ue0": shape["cli_slots_ue1_into_ue0"], "cli_slots_ue0_into_ue1": shape["cli_slots_ue0_into_ue1"],
    "nrue_radio_args": shape["nrue_radio_args"], "links": shape["links"],
    "platform_profile": profile,
    "cpus": {"gnb": gnb_cpus.split(";") if gnb_cpus else [], "broker": broker_cpus,
             "ue": ue_cpus.split(";") if ue_cpus else []},
    "broker_env": broker_env, "ue_exec": ue_exec, "root_exec": root_exec,
    "wire_capture": {"samples": int(cap_samples), "skip_seconds": int(cap_skip)},
    "sionna_scenario": scenario, "stagger_seconds": int(stagger),
    "oai_zmq_module": {"variant": zmq_variant, "dir": zmq_dir},
    "ue_rx_gain_db": rx_gain or None, "ue_cont_fo_comp": fo_comp or None, "gnb_binary": gnb_binary,
}, open(out, "w"), indent=2, sort_keys=True)
PY
oai_gate_defaults_line | tee "${log_dir}/gate-defaults.log"
printf 'event=native_oai_two_cell_tdd_gate_parameters duration=%ss cell=tdd-n78-20mhz-30khz patterns=%s scenario=%s gnb_cpus="%s" ue_cpus="%s" stagger=%ss awgn_snr_db=%s wire_capture=%s\n' \
  "${mue_duration_seconds}" "${tdd_patterns}" "${sionna_scenario##*/}" "${tdd_gnb_cpus}" "${mue_ue_cpus}" \
  "${mue_stagger_seconds}" "${sionna_awgn_snr_db}" "${mue_wire_capture_samples}"

web_pid=""
if [[ "${web_port}" != 0 ]]; then
  "${sionna_python}" "${repo_root}/scripts/web_ui/server.py" \
    --port "${web_port}" --telemetry-endpoint "${telemetry_endpoint}" \
    --status-jsonl "${sionna_status_jsonl}" --index "${repo_root}/scripts/web_ui/index.html" \
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
  printf 'event=native_oai_two_cell_tdd_attach_gate result=pass summary="%s"\n' "${summary}"
else
  [[ -f "${summary}" ]] && cat "${summary}" >&2
  printf 'event=native_oai_two_cell_tdd_attach_gate result=fail status=%s logs="%s"\n' "${inner_status}" "${log_dir}" >&2
fi
exit "${inner_status}"
