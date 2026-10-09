#!/usr/bin/env bash
# Native multi-UE attach gate: one OCUDU gNB, two srsUEs, Open5GS, and this
# tree's broker on use_cases/configs/topologies/ocudu_docker/topology.ocudu-docker.multi-ue.cuda.yaml, with no
# container runtime anywhere.
#
# This is the Docker-free counterpart of scripts/remote/ocudu-multi-ue-smoke.sh.
# It exists because nested LXC blocks runc from writing
# net.ipv4.ip_unprivileged_port_start into a fresh network namespace, so no
# bridged container can start here, while `unshare --user --net` can.
#
# Like the 1x1 gate it builds the broker from THIS repository, so the binary
# under test is the working tree's, not a prebuilt artifact.
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/.." && cd .. && pwd)"
# shellcheck source=env.sh
source "${script_dir}/env.sh"

native_root="${OCUDU_NATIVE_ROOT}"
physical_gpu="${OCUDU_NATIVE_GPU_DEVICE:-0}"
cuda_compiler="${CUDACXX:-/opt/conda/envs/cuda128/bin/nvcc}"
inner="${script_dir}/run-ocudu-multi-ue-inner.sh"
# Sionna mode swaps the renderer for the scenario-driven one and adds the
# bridge, the broker control plane and the read-only web UI. Unset, this gate
# runs exactly as it always has, on the checked-in fixed-TDL topology.
channel_mode="${OCUDU_NATIVE_CHANNEL_MODE:-legacy}"
sionna_scenario="${OCUDU_NATIVE_SIONNA_SCENARIO:-}"
sionna_python="${OCUDU_NATIVE_SIONNA_PYTHON:-}"
sionna_update_hz="${OCUDU_NATIVE_SIONNA_UPDATE_HZ:-10}"
# Live node positions for the bridge (an external publisher). The bridge runs inside the
# inner network namespace, so the publisher must bind an ipc:// socket on the
# shared filesystem; a tcp://127.0.0.1 endpoint would not reach it.
sionna_position_endpoint="${OCUDU_NATIVE_SIONNA_POSITION_ENDPOINT:-}"
sionna_position_offset="${OCUDU_NATIVE_SIONNA_POSITION_OFFSET:-}"
sionna_position_timeout_s="${OCUDU_NATIVE_SIONNA_POSITION_TIMEOUT_S:-}"
web_port="${OCUDU_NATIVE_WEB_PORT:-8080}"
# Receiver noise floor for the Sionna links: the SNR a unit-gain link
# sees against an absolute per-node floor, so a shadowed UE really decodes
# worse than one in line of sight. 40 dB puts a line-of-sight UE of the ring
# scene (tap -2.5 dB) at a reported 34 dB and the pillar shadow (-27 dB) at
# ~16 dB, which srsUE keeps in sync; 26.6 (shadow ~0 dB) and 34.6 (~8 dB)
# both break its initial access and sync there. `off` renders the floor-less topology the Sionna mode always had.
# Only the scenario-driven (sionna) renderer honours it; the fixed-TDL
# fixtures carry their own awgn steps.
sionna_awgn_snr_db="${OCUDU_NATIVE_SIONNA_AWGN_SNR_DB:-40}"
sionna_tx_power_dl="${OCUDU_NATIVE_SIONNA_TX_POWER_DL:-}"
sionna_tx_power_ul="${OCUDU_NATIVE_SIONNA_TX_POWER_UL:-}"
# Emitted UE power relative to the gNB (dB), folded into each UE port's
# tx_scale_db by the renderer; default 23 dBm - 30 dBm. See ue_tx_scale_db.
sionna_ul_power_offset_db="${OCUDU_NATIVE_SIONNA_UL_POWER_OFFSET_DB:-}"
# Run length (the broker's --duration), and what runs next to the stack:
# OCUDU_NATIVE_MUE_UE_EXEC starts a command inside each UE's netns once that
# UE has pinged the core (placeholders {ue_id} {ue_ip} {ue_index} {ue_netns}
# {log_dir} {run_dir} {config_dir}), OCUDU_NATIVE_MUE_ROOT_EXEC one command in
# the stack's own namespace as soon as ogstun exists ({ue_ids} {ue_ips}
# {log_dir} {run_dir} {config_dir}). Both are `bash -c` templates, torn down
# by the gate's cleanup, empty by default.
mue_duration_seconds="${OCUDU_NATIVE_MUE_DURATION_SECONDS:-240}"
mue_ue_exec="${OCUDU_NATIVE_MUE_UE_EXEC:-}"
mue_root_exec="${OCUDU_NATIVE_MUE_ROOT_EXEC:-}"
mue_strict_realtime="${OCUDU_NATIVE_MUE_STRICT_REALTIME:-0}"
# Broker wire capture (samples per port per direction, after a skip in
# seconds of run time); 0 = off. Used to measure the transmit level the
# noise floor is sized against.
mue_wire_capture_samples="${OCUDU_NATIVE_MUE_WIRE_CAPTURE_SAMPLES:-0}"
mue_wire_capture_skip_seconds="${OCUDU_NATIVE_MUE_WIRE_CAPTURE_SKIP_SECONDS:-60}"
# X6: the gNB lower-PHY thread profile the renderer writes into gnb.yaml
# (single|dual|triple; empty = fixture untouched). See
# docs/plans/x6-gnb-realtime-traffic.md: on OCUDU a1916edc the zmq driver
# forces the sequential profile after parsing, so this is accepted but inert
# until the gNB source is patched; the summary records the mode it printed.
gnb_lower_phy_profile="${OCUDU_NATIVE_GNB_LOWER_PHY_PROFILE:-}"
# X6: the gNB main worker pool size (expert_execution.threads.main_pool.nof_threads;
# empty = OCUDU default, which the 5-core GB10 pin shrinks to 2 and which
# deadlocks under two-UE traffic -- see the renderer and the plan note).
gnb_main_pool_threads="${OCUDU_NATIVE_GNB_MAIN_POOL_THREADS:-}"
if [[ "${channel_mode}" == "sionna" ]]; then
  renderer="${script_dir}/render-sionna-multi-ue-configs.py"
else
  renderer="${script_dir}/render-multi-ue-configs.py"
fi
audited_ocudu="a1916edcdbcd70ba6e0af47ee87be061dad5a4e4"
audited_srsran="eea87b1d893ae58e0b08bc381730c502024ae71f"
audited_open5gs="d9d3abdd480be96fac3bc8a997e83446648763ca"

usage_error()
{
  printf 'error: %s\n' "$1" >&2
  exit 2
}

[[ "$#" -eq 0 ]] || usage_error "usage: $0"
[[ "${channel_mode}" == "legacy" || "${channel_mode}" == "sionna" ]] || \
  usage_error "unsupported OCUDU_NATIVE_CHANNEL_MODE: ${channel_mode}"
if [[ "${channel_mode}" == "sionna" ]]; then
  [[ "${sionna_scenario}" == /* && -f "${sionna_scenario}" && ! -L "${sionna_scenario}" ]] || \
    usage_error "OCUDU_NATIVE_SIONNA_SCENARIO must be an absolute regular file"
  [[ -x "${sionna_python}" ]] || \
    usage_error "OCUDU_NATIVE_SIONNA_PYTHON must point at the Sionna interpreter"
  [[ "${web_port}" =~ ^[1-9][0-9]*$ && "${web_port}" -le 65535 ]] || \
    usage_error "invalid OCUDU_NATIVE_WEB_PORT: ${web_port}"
  if [[ -n "${sionna_position_endpoint}" ]]; then
    [[ "${sionna_position_endpoint}" == ipc:///* ]] || \
      usage_error "OCUDU_NATIVE_SIONNA_POSITION_ENDPOINT must be an absolute ipc:// socket (the bridge runs in its own network namespace)"
    [[ -z "${sionna_position_offset}" || "${sionna_position_offset}" =~ ^-?[0-9.]+,-?[0-9.]+,-?[0-9.]+$ ]] || \
      usage_error "OCUDU_NATIVE_SIONNA_POSITION_OFFSET must be x,y,z"
    [[ -z "${sionna_position_timeout_s}" || "${sionna_position_timeout_s}" =~ ^[0-9]+([.][0-9]+)?$ ]] || \
      usage_error "OCUDU_NATIVE_SIONNA_POSITION_TIMEOUT_S must be a number"
  fi
fi
[[ "${physical_gpu}" =~ ^(0|[1-9][0-9]*)$ && "${physical_gpu}" -le 255 ]] || usage_error "invalid GPU device"
[[ -x "${cuda_compiler}" ]] || usage_error "missing CUDA compiler: ${cuda_compiler}"
number_re='^-?[0-9]+([.][0-9]+)?([eE][-+]?[0-9]+)?$'
[[ "${sionna_awgn_snr_db}" == "off" || "${sionna_awgn_snr_db}" =~ ${number_re} ]] || \
  usage_error "OCUDU_NATIVE_SIONNA_AWGN_SNR_DB must be a number or off"
[[ -z "${sionna_tx_power_dl}" || "${sionna_tx_power_dl}" =~ ${number_re} ]] || \
  usage_error "OCUDU_NATIVE_SIONNA_TX_POWER_DL must be a number"
[[ -z "${sionna_tx_power_ul}" || "${sionna_tx_power_ul}" =~ ${number_re} ]] || \
  usage_error "OCUDU_NATIVE_SIONNA_TX_POWER_UL must be a number"
[[ -z "${sionna_ul_power_offset_db}" || "${sionna_ul_power_offset_db}" =~ ${number_re} ]] || \
  usage_error "OCUDU_NATIVE_SIONNA_UL_POWER_OFFSET_DB must be a number"
# The attach window is 150 s of the run; anything shorter cannot verify.
[[ "${mue_duration_seconds}" =~ ^[1-9][0-9]*$ && "${mue_duration_seconds}" -ge 160 ]] || \
  usage_error "OCUDU_NATIVE_MUE_DURATION_SECONDS must be an integer >= 160"
[[ "${mue_strict_realtime}" =~ ^[01]$ ]] || usage_error "OCUDU_NATIVE_MUE_STRICT_REALTIME must be 0 or 1"
[[ "${mue_wire_capture_samples}" =~ ^(0|[1-9][0-9]*)$ ]] || \
  usage_error "OCUDU_NATIVE_MUE_WIRE_CAPTURE_SAMPLES must be a non-negative integer"
[[ "${mue_wire_capture_skip_seconds}" =~ ^(0|[1-9][0-9]*)$ ]] || \
  usage_error "OCUDU_NATIVE_MUE_WIRE_CAPTURE_SKIP_SECONDS must be a non-negative integer"
[[ -z "${gnb_lower_phy_profile}" || "${gnb_lower_phy_profile}" =~ ^(single|dual|triple)$ ]] || \
  usage_error "OCUDU_NATIVE_GNB_LOWER_PHY_PROFILE must be single, dual, triple or empty"
[[ -z "${gnb_main_pool_threads}" || "${gnb_main_pool_threads}" =~ ^[1-9][0-9]?$ ]] || \
  usage_error "OCUDU_NATIVE_GNB_MAIN_POOL_THREADS must be a small positive integer or empty"
# CPU placement from platform-profiles.json (spark-gb10: gNB 5-9, broker
# 15-17, UEs 18,19; a no-op on an unknown host; OCUDU_NATIVE_PLATFORM=none
# turns it off). The same resolver the OAI gates use, so the pinned runs
# here and there mean the same thing.
# shellcheck source=oai-gate-defaults.sh
source "${script_dir}/oai-gate-defaults.sh"
oai_gate_platform || usage_error "platform profile resolution failed"
for command_name in unshare nsenter ip mount umount flock cmake ctest ss setsid stdbuf; do
  command -v "${command_name}" >/dev/null 2>&1 || usage_error "missing command: ${command_name}"
done
for path in "${inner}" "${renderer}" \
  "${native_root}/builds/ocudu-zmq-release/apps/gnb/gnb" \
  "${native_root}/builds/srsran4g-zmq-release/srsue/src/srsue" \
  "${native_root}/builds/open5gs-v2.7.6/tests/app/5gc" \
  "${native_root}/install/mongodb-6.0.29/bin/mongod" \
  "${repo_root}/use_cases/configs/topologies/ocudu_docker/topology.ocudu-docker.multi-ue.cuda.yaml"; do
  [[ -e "${path}" ]] || usage_error "missing required path: ${path}"
done
[[ -c /dev/net/tun ]] || usage_error "/dev/net/tun is absent"
[[ "$(git -C "${native_root}/src/ocudu" rev-parse HEAD)" == "${audited_ocudu}" ]] || usage_error "OCUDU revision mismatch"
[[ "$(git -C "${native_root}/src/srsRAN_4G" rev-parse HEAD)" == "${audited_srsran}" ]] || usage_error "srsRAN revision mismatch"
[[ "$(git -C "${native_root}/src/open5gs" rev-parse HEAD)" == "${audited_open5gs}" ]] || usage_error "Open5GS revision mismatch"
if [[ "${OCUDU_NATIVE_SKIP_WORKSPACE_LOCK:-0}" == "1" ]]; then
  # The lock pins an x86_64 host; an aarch64 host (DGX Spark, Jetson) cannot
  # satisfy it. The source pins above are still checked.
  echo "event=skip x86_native_workspace_lock"
else
  "/usr/bin/python3" "${script_dir}/verify-workspace-lock.py" \
    --root "${native_root}" --repo-root "${repo_root}" \
    --lock "${script_dir}/native-workspace.lock.json"
fi
grep -qx 'ENABLE_ZEROMQ:BOOL=ON' "${native_root}/builds/ocudu-zmq-release/CMakeCache.txt" || usage_error "gNB lacks ZMQ"
# Ports for the gNB pair and BOTH UE pairs, plus mongo and the core.
for port in 2000 2001 2100 2101 2102 2103 27017 38412 7777; do
  ss -H -ltn "sport = :${port}" | grep -q . && usage_error "TCP port ${port} is already listening"
done

exec {lock_fd}<"${BASH_SOURCE[0]}"
flock -n "${lock_fd}" || usage_error "another native multi-UE gate is running"
export OCUDU_NATIVE_GATE_LOCK_FD="${lock_fd}"

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
log_dir="${native_root}/results/logs/ocudu-multi-ue/${timestamp}"
report_dir="${native_root}/results/reports/ocudu-multi-ue/${timestamp}"
config_dir="${native_root}/configs/ocudu-multi-ue-native/${timestamp}"
data_dir="${native_root}/data/ocudu-multi-ue-native/${timestamp}"
netns_dir="${native_root}/run/ocudu-multi-ue-native/${timestamp}/netns"
for path in "${log_dir}" "${report_dir}" "${config_dir}" "${data_dir}" "${netns_dir}"; do
  [[ ! -e "${path}" && ! -L "${path}" ]] || usage_error "run path already exists: ${path}"
done
mkdir -p "${log_dir}" "${report_dir}" "${config_dir}" "${data_dir}" "${netns_dir}"

declare -a renderer_args=(
  --repo-root "${repo_root}" --native-root "${native_root}"
  --output-dir "${config_dir}" --log-dir "${log_dir}"
)
[[ "${channel_mode}" == "sionna" ]] && renderer_args+=(--scenario-config "${sionna_scenario}")
# UE count: 2 (default) or 4 (topology.ocudu-docker.multi-ue-quad). Both
# renderers take it; the Sionna one checks the scenario names that many UEs.
ue_count="${OCUDU_NATIVE_MUE_UE_COUNT:-2}"
[[ "${ue_count}" =~ ^(2|4)$ ]] || usage_error "OCUDU_NATIVE_MUE_UE_COUNT must be 2 or 4"
[[ "${ue_count}" == 2 ]] || renderer_args+=(--ue-count "${ue_count}")
export OCUDU_NATIVE_MUE_UE_COUNT="${ue_count}"
if [[ "${channel_mode}" == "sionna" && "${sionna_awgn_snr_db}" != "off" ]]; then
  renderer_args+=(--awgn-snr-db "${sionna_awgn_snr_db}")
  [[ -z "${sionna_tx_power_dl}" ]] || renderer_args+=(--tx-power-dl "${sionna_tx_power_dl}")
  [[ -z "${sionna_tx_power_ul}" ]] || renderer_args+=(--tx-power-ul "${sionna_tx_power_ul}")
fi
if [[ "${channel_mode}" == "sionna" ]]; then
  [[ -z "${sionna_ul_power_offset_db}" ]] || renderer_args+=(--ul-power-offset-db "${sionna_ul_power_offset_db}")
fi
"/usr/bin/python3" "${renderer}" "${renderer_args[@]}" >"${log_dir}/render.log" 2>&1 || {
  cat "${log_dir}/render.log" >&2; usage_error "config rendering failed"
}
"${native_root}/builds/ocudu-zmq-release/apps/gnb/gnb" -c "${config_dir}/gnb.yaml" --dryrun \
  >"${log_dir}/gnb-dryrun.log" 2>&1 || { tail -20 "${log_dir}/gnb-dryrun.log" >&2; usage_error "gNB dry run failed"; }

# Build the broker from THIS tree, so the gate tests the working copy.
#
# OCUDU_NATIVE_CHANNEL_BUILD exists so the same gate can be pointed at a second
# build directory and run against another revision -- e.g. checking out the
# pre-MIMO baseline in a worktree and re-running to establish whether a live
# failure predates the change under test. CMake refuses to reuse a cache built
# from a different source dir, so each revision needs its own.
channel_build="${OCUDU_NATIVE_CHANNEL_BUILD:-${native_root}/builds/ocudu-gpu-channel-cuda-release}"
cmake -S "${repo_root}" -B "${channel_build}" -DCMAKE_BUILD_TYPE=Release \
  -DOCUDU_GPU_CHANNEL_ENABLE_CUDA=ON -DCMAKE_CUDA_COMPILER="${cuda_compiler}" \
  -DOCUDU_GPU_CHANNEL_CUDA_ARCHITECTURES=120 >"${log_dir}/cmake-configure.log" 2>&1 \
  || { tail -20 "${log_dir}/cmake-configure.log" >&2; usage_error "cmake configure"; }
cmake --build "${channel_build}" -j"$(nproc)" >"${log_dir}/cmake-build.log" 2>&1 \
  || { tail -20 "${log_dir}/cmake-build.log" >&2; usage_error "cmake build"; }
CUDA_VISIBLE_DEVICES="${physical_gpu}" \
  ctest --test-dir "${channel_build}" --output-on-failure >"${log_dir}/ctest.log" 2>&1 \
  || { tail -20 "${log_dir}/ctest.log" >&2; usage_error "ctest"; }

parent_netns="$(readlink /proc/self/ns/net)"
parent_mntns="$(readlink /proc/self/ns/mnt)"

declare -a inner_args=(
  --repo-root "${repo_root}" --native-root "${native_root}"
  --config-dir "${config_dir}" --log-dir "${log_dir}" --netns-dir "${netns_dir}"
  --timestamp "${timestamp}" --physical-gpu "${physical_gpu}"
  --channel-build "${channel_build}"
  --parent-netns "${parent_netns}" --parent-mntns "${parent_mntns}"
  --outer-uid "$(id -u)"
  --run-duration-seconds "${mue_duration_seconds}"
  --strict-realtime "${mue_strict_realtime}"
  --ue-exec "${mue_ue_exec}" --root-exec "${mue_root_exec}"
  --wire-capture-samples "${mue_wire_capture_samples}"
  --wire-capture-skip-seconds "${mue_wire_capture_skip_seconds}"
)
# What this run applied, next to the verdict it will produce.
/usr/bin/python3 - "${report_dir}/run-parameters.json" <<'PY' \
  "${channel_mode}" "${ue_count}" "${mue_duration_seconds}" "${mue_strict_realtime}" \
  "${sionna_awgn_snr_db}" "${sionna_tx_power_dl}" "${sionna_tx_power_ul}" \
  "${OCUDU_NATIVE_PLATFORM_PROFILE:-none}" "${OCUDU_NATIVE_GNB_CPUS:-}" \
  "${OCUDU_NATIVE_BROKER_CPUS:-}" "${OCUDU_NATIVE_NRUE_CPUS:-}" "${OCUDU_NATIVE_BROKER_ENV:-}" \
  "${mue_ue_exec}" "${mue_root_exec}" "${mue_wire_capture_samples}" "${mue_wire_capture_skip_seconds}" \
  "${sionna_scenario}" "${sionna_position_endpoint}" "${gnb_lower_phy_profile}" "${gnb_main_pool_threads}"
import json, sys
(out, channel_mode, ue_count, duration, strict, awgn, tx_dl, tx_ul, profile, gnb_cpus,
 broker_cpus, ue_cpus, broker_env, ue_exec, root_exec, cap_samples, cap_skip,
 scenario, position_endpoint, gnb_lower_phy_profile, gnb_main_pool_threads) = sys.argv[1:]
json.dump({
    "channel_mode": channel_mode, "ue_count": int(ue_count),
    "run_duration_seconds": int(duration), "strict_realtime": int(strict),
    "sionna_awgn_snr_db": None if awgn == "off" or channel_mode != "sionna" else float(awgn),
    "sionna_tx_power_dl": float(tx_dl) if tx_dl else None,
    "sionna_tx_power_ul": float(tx_ul) if tx_ul else None,
    "platform_profile": profile,
    "cpus": {"gnb": gnb_cpus, "broker": broker_cpus, "ue": ue_cpus},
    "broker_env": broker_env, "ue_exec": ue_exec, "root_exec": root_exec,
    "wire_capture": {"samples": int(cap_samples), "skip_seconds": int(cap_skip)},
    "sionna_scenario": scenario or None, "sionna_position_endpoint": position_endpoint or None,
    "gnb_lower_phy_profile": gnb_lower_phy_profile or None,
    "gnb_main_pool_threads": int(gnb_main_pool_threads) if gnb_main_pool_threads else None,
}, open(out, "w"), indent=2, sort_keys=True)
PY
printf 'event=native_multi_ue_gate_parameters duration=%ss strict_realtime=%s platform=%s gnb_cpus=%s broker_cpus=%s ue_cpus=%s awgn_snr_db=%s gnb_lower_phy_profile=%s gnb_main_pool_threads=%s\n' \
  "${mue_duration_seconds}" "${mue_strict_realtime}" "${OCUDU_NATIVE_PLATFORM_PROFILE:-none}" \
  "${OCUDU_NATIVE_GNB_CPUS:-}" "${OCUDU_NATIVE_BROKER_CPUS:-}" "${OCUDU_NATIVE_NRUE_CPUS:-}" \
  "${sionna_awgn_snr_db}" "${gnb_lower_phy_profile:-default}" "${gnb_main_pool_threads:-default}"
web_pid=""
if [[ "${channel_mode}" == "sionna" ]]; then
  # ipc:// under the run directory, so the control and telemetry sockets live
  # in the namespace the inner script owns and are not reachable from the host.
  run_dir="${native_root}/run/ocudu-multi-ue-native/${timestamp}"
  mkdir -p "${run_dir}"
  control_endpoint="ipc://${run_dir}/control.sock"
  telemetry_endpoint="ipc://${run_dir}/telemetry.sock"
  sionna_status_jsonl="${log_dir}/sionna-status.jsonl"
  inner_args+=(
    --channel-mode sionna
    --control-endpoint "${control_endpoint}"
    --telemetry-endpoint "${telemetry_endpoint}"
    --sionna-python "${sionna_python}"
    --sionna-bridge "${repo_root}/scripts/sionna_rt/run_bridge.py"
    --sionna-scenario-config "${sionna_scenario}"
    --sionna-status-jsonl "${sionna_status_jsonl}"
    --sionna-update-hz "${sionna_update_hz}"
    --sionna-position-endpoint "${sionna_position_endpoint}"
    --sionna-position-offset "${sionna_position_offset}"
    --sionna-position-timeout-s "${sionna_position_timeout_s}"
  )
  # Read-only observer. It tails the status JSONL the bridge writes and
  # subscribes to broker telemetry; it never touches the control socket.
  "${sionna_python}" "${repo_root}/scripts/web_ui/server.py" \
    --port "${web_port}" --telemetry-endpoint "${telemetry_endpoint}" \
    --status-jsonl "${sionna_status_jsonl}" \
    --index "${repo_root}/scripts/web_ui/index.html" \
    >"${log_dir}/web-ui.log" 2>&1 &
  web_pid="$!"
  printf 'Web UI: http://127.0.0.1:%s\n' "${web_port}"
fi

set +e
unshare --user --map-root-user --net --mount --fork --kill-child --propagation private \
  "${inner}" "${inner_args[@]}"
inner_status="$?"
set -e
# `|| true`: under set -e the command after the final && is NOT exempt, and
# `wait` returns the killed Web UI's 143 -- which used to end the gate here,
# before the result line, with exit 143 even for a passing Sionna run.
if [[ -n "${web_pid}" ]]; then
  kill "${web_pid}" >/dev/null 2>&1 || true
  wait "${web_pid}" >/dev/null 2>&1 || true
fi

summary="${report_dir}/attach-summary.json"
if [[ "${inner_status}" -eq 0 && -f "${summary}" ]]; then
  printf 'event=native_multi_ue_attach_gate result=pass summary="%s"\n' "${summary}"
else
  [[ -f "${summary}" ]] && cat "${summary}" >&2
  printf 'event=native_multi_ue_attach_gate result=fail status=%s logs="%s"\n' \
    "${inner_status}" "${log_dir}" >&2
fi
exit "${inner_status}"
