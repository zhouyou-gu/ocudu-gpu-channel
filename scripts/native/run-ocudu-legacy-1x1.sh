#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/../.." && pwd)"
# shellcheck source=env.sh
source "${script_dir}/env.sh"

native_root="${OCUDU_NATIVE_ROOT}"
channel_mode="${OCUDU_NATIVE_CHANNEL_MODE:-legacy}"
if [[ "${channel_mode}" != "legacy" ]]; then
  duration_seconds="${OCUDU_NATIVE_SIONNA_DURATION_SECONDS:-0}"
else
  duration_seconds="${OCUDU_NATIVE_LEGACY_DURATION_SECONDS:-15}"
fi
physical_gpu="${OCUDU_NATIVE_GPU_DEVICE:-0}"
cuda_compiler="${CUDACXX:-/opt/conda/envs/cuda128/bin/nvcc}"
inner="${script_dir}/run-ocudu-legacy-1x1-inner.sh"
renderer="${OCUDU_NATIVE_CONFIG_RENDERER:-${script_dir}/render-legacy-1x1-configs.py}"
renderer_uses_scenario="${OCUDU_NATIVE_RENDERER_USES_SCENARIO:-0}"
verifier="${script_dir}/verify-legacy-1x1-artifacts.py"
sionna_python="${OCUDU_NATIVE_SIONNA_PYTHON:-${repo_root}/../venvs/sionna/bin/python}"
sionna_bridge="${repo_root}/apps/sionna_bridge/run_bridge.py"
sionna_scenario="${OCUDU_NATIVE_SIONNA_SCENARIO:-${repo_root}/use_cases/configs/sionna/scenarios/simple_street/ocudu-docker.json}"
web_server="${repo_root}/apps/dashboard/server.py"
web_index="${repo_root}/apps/dashboard/index.html"
sionna_update_hz="${OCUDU_NATIVE_SIONNA_UPDATE_HZ:-10}"
sionna_ready_seconds="${OCUDU_NATIVE_SIONNA_READY_SECONDS:-120}"
# Live node positions for the bridge (an external publisher). The bridge runs inside the
# inner network namespace, so a tcp://127.0.0.1 publisher on the host is not
# reachable from it: the endpoint has to be an ipc:// socket the publisher
# binds on the shared filesystem, e.g. ipc://${native_root}/run/arena/positions.sock.
sionna_position_endpoint="${OCUDU_NATIVE_SIONNA_POSITION_ENDPOINT:-}"
sionna_position_offset="${OCUDU_NATIVE_SIONNA_POSITION_OFFSET:-}"
sionna_position_timeout_s="${OCUDU_NATIVE_SIONNA_POSITION_TIMEOUT_S:-}"
# The same switch that makes the renderer emit the gNB metrics block also
# points the Web UI at the gNB's remote-control WebSocket, so one variable
# turns the whole RAN KPI path on. Unset, neither side is touched.
gnb_metrics_enabled="${OCUDU_NATIVE_GNB_METRICS:-}"
gnb_metrics_port="${OCUDU_NATIVE_GNB_METRICS_PORT:-8001}"
# Keepalive ping interval for the unbounded live demo, in seconds; 0 disables
# it. Fractional values are accepted and are what you want: the scheduler sums
# its KPIs per report period (1 s as rendered), so an interval at or above that
# period leaves most rows a truthful but useless zero. 0.2 puts five packets in
# every report. Pair it with OCUDU_NATIVE_UE_INACTIVITY_SECONDS, which the
# config renderer reads: the keepalive keeps the KPI table populated, and the
# timer decides how long a genuinely idle UE survives.
ue_keepalive_seconds="${OCUDU_NATIVE_UE_KEEPALIVE_SECONDS:-0}"
web_bind="${OCUDU_NATIVE_WEB_BIND:-127.0.0.1}"
web_port="${OCUDU_NATIVE_WEB_PORT:-8080}"
sionna_result_family="${OCUDU_NATIVE_SIONNA_RESULT_FAMILY:-ocudu-sionna-1x1}"
sionna_run_family="${OCUDU_NATIVE_SIONNA_RUN_FAMILY:-ocudu-sionna-1x1-native}"
sionna_event_family="${OCUDU_NATIVE_SIONNA_EVENT_FAMILY:-native_sionna_1x1}"
execution_profile="${OCUDU_NATIVE_EXECUTION_PROFILE:-1x1}"
broker_ready_device="${OCUDU_NATIVE_BROKER_READY_DEVICE:-ue0}"
audited_ocudu="a1916edcdbcd70ba6e0af47ee87be061dad5a4e4"
# Optional gNB binary override for an alternative build (e.g. the CUDA gNB).
# It must declare the revision it reports, so the identity check below stays a
# real check rather than being skipped. Both unset, this file behaves exactly
# as it did before: gnb_binary is the audited CPU build and the version grep
# still demands a1916ed.
gnb_binary="${OCUDU_NATIVE_GNB_BINARY:-${native_root}/builds/ocudu-zmq-release/apps/gnb/gnb}"
audited_gnb_commit="${OCUDU_NATIVE_GNB_COMMIT:-a1916ed}"
gnb_start_timeout_seconds="${OCUDU_NATIVE_GNB_START_TIMEOUT_SECONDS:-15}"
broker_startup_allowance_seconds="${OCUDU_NATIVE_BROKER_STARTUP_ALLOWANCE_SECONDS:-0}"
[[ "${gnb_binary}" == /* && -x "${gnb_binary}" ]] || usage_error "gNB binary is not an executable absolute path: ${gnb_binary}"
[[ "${audited_gnb_commit}" =~ ^[0-9a-f]{7,40}$ ]] || usage_error "invalid OCUDU_NATIVE_GNB_COMMIT"
[[ "${gnb_start_timeout_seconds}" =~ ^[1-9][0-9]{0,2}$ ]] || usage_error "invalid OCUDU_NATIVE_GNB_START_TIMEOUT_SECONDS"
[[ "${broker_startup_allowance_seconds}" =~ ^(0|[1-9][0-9]{0,2})$ ]] || usage_error "invalid OCUDU_NATIVE_BROKER_STARTUP_ALLOWANCE_SECONDS"
audited_srsran="eea87b1d893ae58e0b08bc381730c502024ae71f"
audited_open5gs="d9d3abdd480be96fac3bc8a997e83446648763ca"

usage_error()
{
  printf 'error: %s\n' "$1" >&2
  exit 2
}

write_channel_manifest()
{
  local output_path="$1"
  /usr/bin/python3 - "${repo_root}" "${output_path}" <<'PY'
import hashlib
import os
import subprocess
import sys

root, output_path = sys.argv[1:]
raw_paths = subprocess.check_output(
    ["git", "-C", root, "ls-files", "--cached", "--others", "--exclude-standard", "-z"]
)
paths = sorted(os.fsdecode(value) for value in raw_paths.split(b"\0") if value)
with open(output_path, "x", encoding="utf-8") as output:
    output.write("sha256\tpath\n")
    for relative in paths:
        path = os.path.join(root, relative)
        if not os.path.isfile(path):
            raise SystemExit(f"manifest input is not a regular file: {relative}")
        digest = hashlib.sha256()
        with open(path, "rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        output.write(f"{digest.hexdigest()}\t{relative}\n")
PY
}

[[ "$#" -eq 0 ]] || usage_error "usage: $0"
[[ "${channel_mode}" == "legacy" || "${channel_mode}" == "sionna" || \
   "${channel_mode}" == "external" ]] || \
  usage_error "OCUDU_NATIVE_CHANNEL_MODE must be legacy, sionna or external"
[[ "${renderer_uses_scenario}" == "0" || "${renderer_uses_scenario}" == "1" ]] || \
  usage_error "OCUDU_NATIVE_RENDERER_USES_SCENARIO must be 0 or 1"
if [[ "${channel_mode}" == "legacy" ]]; then
  [[ "${duration_seconds}" == "15" ]] || usage_error "legacy duration is fixed to 15 seconds"
else
  [[ "${duration_seconds}" =~ ^(0|[1-9][0-9]*)$ ]] || usage_error "invalid Sionna duration"
  [[ "${sionna_update_hz}" =~ ^[0-9]+([.][0-9]+)?$ ]] || usage_error "invalid Sionna update rate"
  [[ "${sionna_ready_seconds}" =~ ^[1-9][0-9]*$ ]] || usage_error "invalid Sionna ready timeout"
  if [[ -n "${sionna_position_endpoint}" ]]; then
    [[ "${sionna_position_endpoint}" == ipc:///* ]] || \
      usage_error "OCUDU_NATIVE_SIONNA_POSITION_ENDPOINT must be an absolute ipc:// socket (the bridge runs in its own network namespace)"
    [[ -z "${sionna_position_offset}" || "${sionna_position_offset}" =~ ^-?[0-9.]+,-?[0-9.]+,-?[0-9.]+$ ]] || \
      usage_error "OCUDU_NATIVE_SIONNA_POSITION_OFFSET must be x,y,z"
    [[ -z "${sionna_position_timeout_s}" || "${sionna_position_timeout_s}" =~ ^[0-9]+([.][0-9]+)?$ ]] || \
      usage_error "OCUDU_NATIVE_SIONNA_POSITION_TIMEOUT_S must be a number"
  fi
  [[ "${web_port}" =~ ^[1-9][0-9]*$ && "${web_port}" -le 65535 ]] || usage_error "invalid Web UI port"
  [[ "${web_bind}" == "127.0.0.1" || "${web_bind}" == "localhost" ]] || \
    usage_error "rootless native Web UI bind is restricted to loopback"
  [[ "${sionna_result_family}" =~ ^[a-z0-9][a-z0-9-]*$ ]] || usage_error "invalid Sionna result family"
  [[ "${sionna_run_family}" =~ ^[a-z0-9][a-z0-9-]*$ ]] || usage_error "invalid Sionna run family"
  [[ "${sionna_event_family}" =~ ^[a-z0-9_]+$ ]] || usage_error "invalid Sionna event family"
  [[ "${execution_profile}" == "1x1" || "${execution_profile}" == "rank1" ]] || \
    usage_error "OCUDU_NATIVE_EXECUTION_PROFILE must be 1x1 or rank1"
fi
[[ "${physical_gpu}" =~ ^(0|[1-9][0-9]*)$ && "${physical_gpu}" -le 255 ]] || usage_error "invalid GPU device"
[[ "${broker_ready_device}" =~ ^[A-Za-z0-9_-]+$ ]] || usage_error "invalid Broker ready device"
[[ "${native_root}" == /* && "${native_root}" != "/" && "${native_root}" != "/home/ubuntu" ]] || usage_error "invalid native root"
[[ -x "${cuda_compiler}" ]] || usage_error "missing CUDA compiler: ${cuda_compiler}"
for command_name in unshare nsenter ip mount umount flock cmake ctest setsid stdbuf; do
  command -v "${command_name}" >/dev/null 2>&1 || usage_error "missing command: ${command_name}"
done
[[ -x /usr/bin/python3 ]] || usage_error "missing /usr/bin/python3"
for path in "${inner}" "${renderer}" "${verifier}" \
  "${gnb_binary}" \
  "${native_root}/builds/srsran4g-zmq-release/srsue/src/srsue" \
  "${native_root}/builds/open5gs-v2.7.6/tests/app/5gc" \
  "${native_root}/install/mongodb-6.0.29/bin/mongod" \
  "${repo_root}/use_cases/configs/topologies/ocudu_docker/topology.ocudu-docker.cuda.yaml"; do
  [[ -e "${path}" ]] || usage_error "missing required path: ${path}"
done
if [[ "${channel_mode}" != "legacy" ]]; then
  # The Web UI runs in both live modes and needs an interpreter with pyzmq.
  # The scenario is read by the config renderer, not by Sionna, so external
  # runs need it too whenever the renderer consumes one.
  for path in "${sionna_python}" "${sionna_scenario}" "${web_server}" "${web_index}"; do
    [[ -e "${path}" ]] || usage_error "missing Web UI path: ${path}"
  done
  [[ -x "${sionna_python}" ]] || usage_error "Web UI Python is not executable: ${sionna_python}"
fi
if [[ "${channel_mode}" == "external" ]]; then
  # The sionna branch below proves pyzmq as part of its full RT import; an
  # external run still needs it for the Web UI telemetry subscriber, so check
  # it here rather than failing later inside the web server.
  "${sionna_python}" -c 'import zmq' >/dev/null 2>&1 ||     usage_error "pyzmq import failed in ${sionna_python}"
fi
if [[ "${channel_mode}" == "sionna" ]]; then
  [[ -e "${sionna_bridge}" ]] || usage_error "missing Sionna bridge: ${sionna_bridge}"
  if [[ -z "${DRJIT_LIBOPTIX_PATH:-}" ]]; then
    optix_library="$(find "${repo_root}/../local" -type f -name 'libnvoptix.so*' -print -quit 2>/dev/null || true)"
    [[ -n "${optix_library}" ]] || usage_error "libnvoptix was not found under ${repo_root}/../local"
    export DRJIT_LIBOPTIX_PATH="${optix_library}"
  fi
  optix_dir="$(dirname "${DRJIT_LIBOPTIX_PATH}")"
  export LD_LIBRARY_PATH="${optix_dir}:${LD_LIBRARY_PATH:-}"
  CUDA_VISIBLE_DEVICES="${physical_gpu}" "${sionna_python}" -c \
    'import sionna, zmq; from sionna import rt' >/dev/null 2>&1 || \
    usage_error "Sionna RT/pyzmq import failed in ${sionna_python}"
fi
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
for binary in \
  "${gnb_binary}" \
  "${native_root}/builds/srsran4g-zmq-release/srsue/src/srsue" \
  "${native_root}/builds/open5gs-v2.7.6/tests/app/5gc" \
  "${native_root}/install/mongodb-6.0.29/bin/mongod"; do
  ldd_report="$(mktemp /tmp/ocudu-native-legacy-ldd.XXXXXX)"
  if ! ldd "${binary}" >"${ldd_report}" 2>&1; then
    cat "${ldd_report}" >&2
    rm -f "${ldd_report}"
    usage_error "ldd inspection failed: ${binary}"
  fi
  if grep -q 'not found' "${ldd_report}"; then
    cat "${ldd_report}" >&2
    rm -f "${ldd_report}"
    usage_error "unresolved shared library: ${binary}"
  fi
  rm -f "${ldd_report}"
done
# Keep the lock in this supervisor only. A fixed descriptor lets every
# long-lived child close it explicitly, so an interrupted supervisor cannot
# leave the native 1x1 lock pinned by an orphaned radio/core process.
exec 9<"${BASH_SOURCE[0]}"
flock -n 9 || usage_error "another native 1x1 run is active"
export OCUDU_NATIVE_GATE_LOCK_FD="9"
parent_netns="$(readlink /proc/self/ns/net)"
parent_mntns="$(readlink /proc/self/ns/mnt)"

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
results_root="${native_root}/results"
if [[ "${channel_mode}" != "legacy" ]]; then
  result_family="${sionna_result_family}"
  run_family="${sionna_run_family}"
else
  result_family="ocudu-interop"
  run_family="ocudu-legacy-1x1-native"
fi
log_dir="${results_root}/logs/${result_family}/${timestamp}"
report_dir="${results_root}/reports/${result_family}/${timestamp}"
config_dir="${native_root}/configs/${run_family}/${timestamp}"
data_dir="${native_root}/data/${run_family}/${timestamp}"
netns_dir="${native_root}/run/${run_family}/${timestamp}/netns"
ipc_dir="${native_root}/run/s1-${timestamp}"
for path in "${log_dir}" "${report_dir}" "${config_dir}" "${data_dir}" "${netns_dir}" "${ipc_dir}"; do
  [[ ! -e "${path}" && ! -L "${path}" ]] || usage_error "run path already exists: ${path}"
done
mkdir -p "${log_dir}" "${report_dir}" "${config_dir}" "${data_dir}" "${netns_dir}" "${ipc_dir}"
control_endpoint="ipc://${ipc_dir}/control.sock"
telemetry_endpoint="ipc://${ipc_dir}/telemetry.sock"
# The gNB's remote-control WebSocket is bound inside the isolated network
# namespace, so it is relayed out through this socket file the same way the
# ZMQ planes cross the boundary. Only created when metrics are enabled.
gnb_metrics_socket="${ipc_dir}/gnb-metrics.sock"
sionna_status_jsonl="${log_dir}/sionna-status.jsonl"
live_ready_path="${report_dir}/live-ready.json"
web_status_path="${report_dir}/web-ui-status.json"

gnb_metrics_relay_arg=""
case "${gnb_metrics_enabled,,}" in
  1|true|yes|on) gnb_metrics_relay_arg="${gnb_metrics_socket}" ;;
esac

source_manifest="${report_dir}/channel-source-manifest.tsv"
source_manifest_after="${report_dir}/channel-source-manifest.after-build.tsv"
source_evidence="${report_dir}/source-evidence.json"
preserved_configs="${report_dir}/configs"
write_channel_manifest "${source_manifest}"
channel_head="$(git -C "${repo_root}" rev-parse HEAD)"
channel_diff_sha256="$(git -C "${repo_root}" diff --binary -- . | sha256sum | awk '{print $1}')"

renderer_args=(
  "${renderer}" --repo-root "${repo_root}" --native-root "${native_root}"
  --output-dir "${config_dir}" --log-dir "${log_dir}"
)
if [[ "${channel_mode}" != "legacy" && "${renderer_uses_scenario}" == "1" ]]; then
  renderer_args+=(--scenario-config "${sionna_scenario}")
fi
"/usr/bin/python3" "${renderer_args[@]}" >"${log_dir}/render.log" 2>&1
"${gnb_binary}" -c "${config_dir}/gnb.yaml" --dryrun \
  >"${log_dir}/gnb-dryrun.log" 2>&1

channel_build="${OCUDU_NATIVE_CHANNEL_BUILD:-${native_root}/builds/ocudu-gpu-channel-cuda-release}"
cmake -S "${repo_root}" -B "${channel_build}" -DCMAKE_BUILD_TYPE=Release \
  -DOCUDU_GPU_CHANNEL_ENABLE_CUDA=ON -DCMAKE_CUDA_COMPILER="${cuda_compiler}" \
  -DCMAKE_CUDA_ARCHITECTURES="${OCUDU_NATIVE_CUDA_ARCH:-120}" \
  -DOCUDU_GPU_CHANNEL_CUDA_ARCHITECTURES="${OCUDU_NATIVE_CUDA_ARCH:-120}" >"${log_dir}/cmake-configure.log" 2>&1
cmake --build "${channel_build}" -j"$(nproc)" >"${log_dir}/cmake-build.log" 2>&1
CUDA_VISIBLE_DEVICES="${physical_gpu}" \
  ctest --test-dir "${channel_build}" --output-on-failure >"${log_dir}/ctest.log" 2>&1
write_channel_manifest "${source_manifest_after}"
cmp -s "${source_manifest}" "${source_manifest_after}" || \
  usage_error "channel source changed while the native legacy gate was building"

mkdir "${preserved_configs}"
cp "${config_dir}/gnb.yaml" "${config_dir}/topology.yaml" \
  "${config_dir}/open5gs.yaml" "${config_dir}/srsue.conf" \
  "${config_dir}/subscriber.csv" "${preserved_configs}/"
if [[ "${channel_mode}" != "legacy" && "${execution_profile}" == "rank1" ]]; then
  cp "${config_dir}/sionna-rank1-shape.json" "${preserved_configs}/"
  cp "${sionna_scenario}" "${preserved_configs}/sionna-scenario.json"
fi
"${gnb_binary}" --version \
  >"${report_dir}/gnb-version.txt" 2>&1
grep -Eq "OCUDU 5G gNB version .*\(${audited_gnb_commit}|OCUDU gNB \(commit ${audited_gnb_commit:0:7}[0-9a-f]*\)" "${report_dir}/gnb-version.txt" || \
  usage_error "native gNB binary does not identify the audited revision"
"/usr/bin/python3" - "${source_evidence}" "${native_root}" "${channel_build}" \
  "${source_manifest}" "${preserved_configs}" "${channel_head}" \
  "${channel_diff_sha256}" "${audited_ocudu}" "${audited_srsran}" \
  "${audited_open5gs}" "${channel_mode}" "${execution_profile}" "${gnb_binary}" <<'PY'
import hashlib
import json
import pathlib
import sys

(output_path, native_root, channel_build, manifest_path, config_root,
 channel_head, channel_diff_sha256, ocudu_commit, srsran_commit,
 open5gs_commit, channel_mode, execution_profile, gnb_binary) = sys.argv[1:]

def digest(path):
    value = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()

native = pathlib.Path(native_root)
build = pathlib.Path(channel_build)
configs = pathlib.Path(config_root)
binary_paths = {
    "gnb": pathlib.Path(gnb_binary),
    "srsue": native / "builds/srsran4g-zmq-release/srsue/src/srsue",
    "open5gs_5gc": native / "builds/open5gs-v2.7.6/tests/app/5gc",
    "mongod": native / "install/mongodb-6.0.29/bin/mongod",
    "broker": build / "ocudu-gpu-channel",
}
config_paths = {
    name: configs / name
    for name in ("gnb.yaml", "topology.yaml", "open5gs.yaml", "srsue.conf", "subscriber.csv")
}
if channel_mode != "legacy" and execution_profile == "rank1":
    config_paths["sionna-rank1-shape.json"] = configs / "sionna-rank1-shape.json"
    config_paths["sionna-scenario.json"] = configs / "sionna-scenario.json"
profile_label = "1x1" if channel_mode == "legacy" else execution_profile
data = {
    "schema": f"ocudu-native-{channel_mode}-{profile_label}-source-evidence/v1",
    "docker_used": False,
    "channel_head": channel_head,
    "channel_tracked_diff_sha256": channel_diff_sha256,
    "channel_source_manifest_sha256": digest(manifest_path),
    "source_commits": {
        "ocudu": ocudu_commit,
        "srsran4g": srsran_commit,
        "open5gs": open5gs_commit,
    },
    "binary_sha256": {name: digest(path) for name, path in binary_paths.items()},
    "config_sha256": {name: digest(path) for name, path in config_paths.items()},
    "claim_boundary": {
        "legacy_attach": True,
        "pdu_session": True,
        "gateway_ping": True,
        "rank2": False,
        "strict_realtime": False,
    },
}
if channel_mode == "sionna":
    data["claim_boundary"]["sionna_rt"] = True
if channel_mode == "external":
    # The channel matrices came from a process this gate did not start and
    # cannot vouch for. Nothing about Sionna RT is claimed.
    data["claim_boundary"]["sionna_rt"] = False
    data["claim_boundary"]["external_channel_source"] = True
if channel_mode != "legacy" and execution_profile == "rank1":
    data["claim_boundary"]["rank1_dynamic_arrays"] = True
with open(output_path, "x", encoding="utf-8") as output:
    json.dump(data, output, indent=2, sort_keys=True)
    output.write("\n")
PY

# Probe the freshly built direct primitive before MongoDB, TUN, netns, or
# any live radio/core process is created for the acceptance run.
postbuild_probe="$(mktemp -d /tmp/ocudu-native-postbuild-probe.XXXXXX)"
mkdir "${postbuild_probe}/run-netns"
if ! unshare --user --map-root-user --net --mount --fork --kill-child=TERM --propagation private \
  "${inner}" --mode probe --parent-netns "${parent_netns}" --parent-mntns "${parent_mntns}" \
  --outer-uid "$(id -u)" --netns-dir "${postbuild_probe}/run-netns" \
  --physical-gpu "${physical_gpu}" --hardware-probe "${channel_build}/test_hardware_probe" \
  --probe-broker "${channel_build}/ocudu-gpu-channel" \
  --probe-config "${repo_root}/use_cases/configs/topologies/ocudu_docker/topology.ocudu-docker.cuda.yaml" \
  >"${log_dir}/postbuild-primitive-probe.log" 2>&1 9<&-; then
  rmdir "${postbuild_probe}/run-netns" "${postbuild_probe}" >/dev/null 2>&1 || true
  usage_error "fresh broker rootless/CUDA primitive probe failed; see ${log_dir}/postbuild-primitive-probe.log"
fi
rmdir "${postbuild_probe}/run-netns" "${postbuild_probe}"

summary_path="${report_dir}/attach-summary.json"
common_inner_args=(
  "${inner}" --mode run --parent-netns "${parent_netns}" --parent-mntns "${parent_mntns}"
  --outer-uid "$(id -u)" --netns-dir "${netns_dir}" --physical-gpu "${physical_gpu}"
  --hardware-probe "${channel_build}/test_hardware_probe"
  --probe-broker "${channel_build}/ocudu-gpu-channel"
  --broker "${channel_build}/ocudu-gpu-channel"
  --probe-config "${repo_root}/use_cases/configs/topologies/ocudu_docker/topology.ocudu-docker.cuda.yaml"
  --native-root "${native_root}" --repo-root "${repo_root}"
  --config-dir "${config_dir}" --log-dir "${log_dir}"
  --report-dir "${report_dir}" --timestamp "${timestamp}"
  --channel-mode "${channel_mode}" --run-duration-seconds "${duration_seconds}"
  --run-family "${run_family}"
  --broker-ready-device "${broker_ready_device}"
  --gnb-binary "${gnb_binary}"
  --gnb-start-timeout-seconds "${gnb_start_timeout_seconds}"
  --broker-startup-allowance-seconds "${broker_startup_allowance_seconds}"
  --ue-keepalive-seconds "${ue_keepalive_seconds}"
)

if [[ "${channel_mode}" == "legacy" ]]; then
  set +e
  unshare --user --map-root-user --net --mount --fork --kill-child=TERM --propagation private \
    "${common_inner_args[@]}" 9<&-
  run_status="$?"
  set -e
  if [[ "${run_status}" -ne 0 ]]; then
    printf 'event=native_legacy_1x1_attach_gate result=fail child_status=%s summary="%s"\n' \
      "${run_status}" "${summary_path}" >&2
    exit "${run_status}"
  fi
  "/usr/bin/python3" "${verifier}" --results-root "${results_root}" --summary "${summary_path}"
  printf 'summary=%s\n' "${summary_path}"
  exit 0
fi

runtime_pid=""
runtime_child_pid=""
web_pid=""
process_running()
{
  local pid="$1"
  local state
  state="$(ps -o stat= -p "${pid}" 2>/dev/null | awk '{print $1}')"
  [[ -n "${state}" && "${state:0:1}" != "Z" ]]
}

stop_pid()
{
  local pid="$1"
  local signal deadline
  [[ "${pid}" =~ ^[1-9][0-9]*$ ]] || return 0
  process_running "${pid}" || return 0
  for signal in TERM KILL; do
    kill -s "${signal}" "${pid}" >/dev/null 2>&1 || true
    deadline=$((SECONDS + 5))
    while process_running "${pid}" && [[ "${SECONDS}" -lt "${deadline}" ]]; do
      sleep 0.1
    done
    process_running "${pid}" || break
  done
  wait "${pid}" >/dev/null 2>&1 || true
}

runtime_child()
{
  local supervisor_pid="$1"
  local child_pid=""
  [[ "${supervisor_pid}" =~ ^[1-9][0-9]*$ ]] || return 1
  if [[ -r "/proc/${supervisor_pid}/task/${supervisor_pid}/children" ]]; then
    read -r child_pid _ <"/proc/${supervisor_pid}/task/${supervisor_pid}/children" || true
  fi
  [[ "${child_pid}" =~ ^[1-9][0-9]*$ ]] || return 1
  printf '%s\n' "${child_pid}"
}

stop_runtime()
{
  local supervisor_pid="$1"
  local child_pid="$2"
  local deadline
  if [[ "${child_pid}" =~ ^[1-9][0-9]*$ ]] && process_running "${child_pid}"; then
    # Signal the inner runner first so its EXIT trap can stop every setsid
    # process group and remove the disposable network/mount state.
    kill -s TERM "${child_pid}" >/dev/null 2>&1 || true
    deadline=$((SECONDS + 30))
    while process_running "${child_pid}" && [[ "${SECONDS}" -lt "${deadline}" ]]; do
      sleep 0.1
    done
  fi
  stop_pid "${supervisor_pid}"
}

cleanup_live_run()
{
  local original_status="$?"
  set +e
  trap - EXIT INT TERM HUP
  stop_pid "${web_pid}"
  stop_runtime "${runtime_pid}" "${runtime_child_pid}"
  exit "${original_status}"
}
trap cleanup_live_run EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

unshare --user --map-root-user --net --mount --fork --kill-child=TERM --propagation private \
  "${common_inner_args[@]}" \
  --control-endpoint "${control_endpoint}" --telemetry-endpoint "${telemetry_endpoint}" \
  --gnb-metrics-socket "${gnb_metrics_relay_arg}" \
  --sionna-python "${sionna_python}" --sionna-bridge "${sionna_bridge}" \
  --sionna-scenario-config "${sionna_scenario}" \
  --sionna-status-jsonl "${sionna_status_jsonl}" --sionna-update-hz "${sionna_update_hz}" \
  --sionna-ready-seconds "${sionna_ready_seconds}" --live-ready-path "${live_ready_path}" \
  --sionna-position-endpoint "${sionna_position_endpoint}" \
  --sionna-position-offset "${sionna_position_offset}" \
  --sionna-position-timeout-s "${sionna_position_timeout_s}" \
  --live-ready-event "${sionna_event_family}_live_ready" \
  >"${log_dir}/native-runtime-console.log" 2>&1 9<&- &
runtime_pid="$!"
runtime_child_deadline=$((SECONDS + 5))
while [[ "${SECONDS}" -lt "${runtime_child_deadline}" ]]; do
  runtime_child_pid="$(runtime_child "${runtime_pid}" || true)"
  [[ -n "${runtime_child_pid}" ]] && break
  process_running "${runtime_pid}" || break
  sleep 0.05
done
[[ -n "${runtime_child_pid}" ]] || usage_error "native runtime supervisor did not start its child"

web_metrics_args=()
if [[ -n "${gnb_metrics_relay_arg}" ]]; then
  web_metrics_args=(--gnb-metrics-endpoint "ws+unix://${gnb_metrics_socket}")
fi
"${sionna_python}" "${web_server}" --bind "${web_bind}" --port "${web_port}" \
  --telemetry-endpoint "${telemetry_endpoint}" --status-jsonl "${sionna_status_jsonl}" \
  --index "${web_index}" "${web_metrics_args[@]}" \
  >"${log_dir}/web-ui.log" 2>&1 9<&- &
web_pid="$!"
web_url="http://${web_bind}:${web_port}"

health_deadline=$((SECONDS + 20))
while [[ "${SECONDS}" -lt "${health_deadline}" ]]; do
  if ! process_running "${web_pid}"; then
    usage_error "Web UI exited; see ${log_dir}/web-ui.log"
  fi
  if /usr/bin/python3 - "${web_url}/healthz" <<'PY' >/dev/null 2>&1
import sys
import urllib.request
with urllib.request.urlopen(sys.argv[1], timeout=1) as response:
    raise SystemExit(0 if response.status == 200 else 1)
PY
  then
    break
  fi
  sleep 0.25
done
[[ "${SECONDS}" -lt "${health_deadline}" ]] || usage_error "Web UI health check timed out"
printf 'event=%s_start web_ui="%s" runtime_pid=%s web_pid=%s\n' \
  "${sionna_event_family}" "${web_url}" "${runtime_pid}" "${web_pid}"

ready_deadline=$((SECONDS + sionna_ready_seconds + 60))
require_sionna_feed=0
[[ "${channel_mode}" == "sionna" ]] && require_sionna_feed=1
feeds_ready=0
while [[ "${SECONDS}" -lt "${ready_deadline}" ]]; do
  if ! process_running "${runtime_pid}"; then
    break
  fi
  if [[ -f "${live_ready_path}" ]] && \
    /usr/bin/python3 - "${web_url}/api/status?history_ms=1000" "${web_status_path}" \
      "${require_sionna_feed}" <<'PY' >/dev/null 2>&1
import json
import pathlib
import sys
import urllib.request

with urllib.request.urlopen(sys.argv[1], timeout=1) as response:
    data = json.load(response)
feeds = data.get("feeds", {})
if not feeds.get("telemetry_connected"):
    raise SystemExit(1)
# An external channel source never writes the Sionna status JSONL, so that
# feed is only evidence when this gate started the bridge itself.
if sys.argv[3] == "1" and not feeds.get("sionna_connected"):
    raise SystemExit(1)
path = pathlib.Path(sys.argv[2])
with path.open("x", encoding="utf-8") as output:
    json.dump(data, output, indent=2, sort_keys=True)
    output.write("\n")
PY
  then
    feeds_ready=1
    break
  fi
  sleep 0.25
done
if [[ "${feeds_ready}" -ne 1 ]]; then
  printf 'error: live attach/Sionna/Web UI readiness failed; logs=%s\n' "${log_dir}" >&2
  exit 1
fi
printf 'event=%s_live_ready web_ui="%s" marker="%s"\n' \
  "${sionna_event_family}" "${web_url}" "${live_ready_path}"
printf 'Web UI: %s\n' "${web_url}"
if [[ "${duration_seconds}" -eq 0 ]]; then
  printf 'The live demo keeps running until Ctrl-C.\n'
fi

set +e
wait "${runtime_pid}"
run_status="$?"
set -e
runtime_pid=""
runtime_child_pid=""
stop_pid "${web_pid}"
web_pid=""
if [[ "${run_status}" -ne 0 ]]; then
  printf 'event=%s_attach_gate result=fail child_status=%s summary="%s"\n' \
    "${sionna_event_family}" "${run_status}" "${summary_path}" >&2
  exit "${run_status}"
fi

"/usr/bin/python3" - "${summary_path}" "${live_ready_path}" "${web_status_path}" \
  "${sionna_event_family}_live_ready" "${sionna_event_family}_attach_gate" \
  "${channel_mode}" <<'PY'
import json
import pathlib
import sys

summary_path, live_path, web_path = map(pathlib.Path, sys.argv[1:4])
expected_live_event, gate_event, channel_mode = sys.argv[4:]
with summary_path.open(encoding="utf-8") as source:
    summary = json.load(source)
with live_path.open(encoding="utf-8") as source:
    live = json.load(source)
with web_path.open(encoding="utf-8") as source:
    web = json.load(source)
required = {
    "status": "passed",
    "channel_mode": channel_mode,
    "docker_used": False,
    "rrc_connected": 1,
    "pdu_session_established": 1,
    "ping_ok": 1,
}
for key, expected in required.items():
    if summary.get(key) != expected:
        raise SystemExit(f"invalid Sionna summary {key}: {summary.get(key)!r}")
counters = ("sionna_updates", "telemetry_frames", "control_batches_committed")
# External runs make no claim about who drove the control plane, so their
# counters are recorded but not required to be non-zero.
for key in counters if channel_mode == "sionna" else ():
    if not isinstance(summary.get(key), int) or summary[key] < 1:
        raise SystemExit(f"missing Sionna evidence counter: {key}")
if live.get("event") != expected_live_event:
    raise SystemExit("live readiness marker is invalid")
feeds = web.get("feeds", {})
if not feeds.get("telemetry_connected"):
    raise SystemExit("Web UI did not receive the telemetry feed")
# The Sionna feed is the status JSONL this gate's own bridge writes; an
# external channel source never produces one, so it is not evidence here.
if channel_mode == "sionna" and not feeds.get("sionna_connected"):
    raise SystemExit("Web UI did not receive the Sionna feed")
print(f"event={gate_event} result=pass")
PY
printf 'summary=%s\n' "${summary_path}"
