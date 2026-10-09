#!/usr/bin/env bash
# Native multi-gNB attach gate: two OCUDU gNB processes (two co-channel cells),
# two srsUEs, Open5GS, and this tree's broker on
# use_cases/configs/topologies/ocudu_docker/topology.multi-gnb.cuda.yaml, with no container runtime anywhere.
#
# The Docker-free counterpart of scripts/remote/ocudu-multi-gnb-smoke.sh, built
# on the native multi-UE gate's namespace layout and supervision. Each UE camps
# on its own cell while the other cell's transmission leaks in as interference.
#
#   OCUDU_NATIVE_GNB_ACCELERATION=disabled|...|all  run the CUDA gNB (both cells)
#     with that acceleration stage; unset runs the CPU gNB. The CUDA build is
#     resolved and audited by scripts/cuda/resolve-cuda-gnb.py, whose lock can be
#     switched with OCUDU_CUDA_WORKSPACE_LOCK.
#   OCUDU_NATIVE_CUDA_HOST_MEMORY=copy|zero_copy|auto  the broker's host-memory mode.
#   OCUDU_NATIVE_CUDA_ARCH      broker CUDA architecture (default 120; 121 on GB10).
#   OCUDU_NATIVE_SKIP_WORKSPACE_LOCK=1  skip the x86 native-workspace lock check
#     (it cannot pass on aarch64); the three source revisions are still checked.
#   OCUDU_NATIVE_GNB_START_TIMEOUT_SECONDS  per-gNB start window (default 15, CUDA 120).
#   OCUDU_NATIVE_CHANNEL_MODE=legacy|sionna  sionna runs the Sionna RT bridge against
#     the broker control plane (as the multi-UE gate does) on a sionna_rt topology:
#     OCUDU_NATIVE_SIONNA_SCENARIO (absolute scenario JSON naming gnb0/gnb1/ue0/ue1),
#     OCUDU_NATIVE_SIONNA_PYTHON, OCUDU_NATIVE_SIONNA_UPDATE_HZ (10).
#   OCUDU_NATIVE_MGNB_TOPOLOGY  broker topology (default: the fixed-TDL
#     topology.multi-gnb.cuda.yaml; sionna: topology.sionna-2gnb-2ue.cuda.yaml, 10 links).
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/.." && cd .. && pwd)"
# shellcheck source=env.sh
source "${script_dir}/env.sh"

native_root="${OCUDU_NATIVE_ROOT}"
physical_gpu="${OCUDU_NATIVE_GPU_DEVICE:-0}"
cuda_compiler="${CUDACXX:-/opt/conda/envs/cuda128/bin/nvcc}"
cuda_arch="${OCUDU_NATIVE_CUDA_ARCH:-120}"
acceleration="${OCUDU_NATIVE_GNB_ACCELERATION:-}"
inner="${script_dir}/run-ocudu-multi-gnb-inner.sh"
renderer="${script_dir}/render-multi-gnb-configs.py"
channel_mode="${OCUDU_NATIVE_CHANNEL_MODE:-legacy}"
sionna_scenario="${OCUDU_NATIVE_SIONNA_SCENARIO:-}"
sionna_python="${OCUDU_NATIVE_SIONNA_PYTHON:-}"
sionna_update_hz="${OCUDU_NATIVE_SIONNA_UPDATE_HZ:-10}"
if [[ "${channel_mode}" == "sionna" ]]; then
  topology="${OCUDU_NATIVE_MGNB_TOPOLOGY:-${repo_root}/use_cases/configs/topologies/sionna/topology.sionna-2gnb-2ue.cuda.yaml}"
else
  topology="${OCUDU_NATIVE_MGNB_TOPOLOGY:-${repo_root}/use_cases/configs/topologies/ocudu_docker/topology.multi-gnb.cuda.yaml}"
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
[[ "${physical_gpu}" =~ ^(0|[1-9][0-9]*)$ && "${physical_gpu}" -le 255 ]] || usage_error "invalid GPU device"
[[ "${cuda_arch}" =~ ^[1-9][0-9]*$ ]] || usage_error "invalid OCUDU_NATIVE_CUDA_ARCH"
[[ -x "${cuda_compiler}" ]] || usage_error "missing CUDA compiler: ${cuda_compiler}"
[[ "${channel_mode}" == "legacy" || "${channel_mode}" == "sionna" ]] || \
  usage_error "unsupported OCUDU_NATIVE_CHANNEL_MODE: ${channel_mode}"
[[ "${topology}" == /* && -f "${topology}" ]] || usage_error "OCUDU_NATIVE_MGNB_TOPOLOGY must be an absolute regular file"
if [[ "${channel_mode}" == "sionna" ]]; then
  [[ "${sionna_scenario}" == /* && -f "${sionna_scenario}" && ! -L "${sionna_scenario}" ]] || \
    usage_error "OCUDU_NATIVE_SIONNA_SCENARIO must be an absolute regular file"
  [[ -x "${sionna_python}" ]] || usage_error "OCUDU_NATIVE_SIONNA_PYTHON must point at the Sionna interpreter"
  [[ "${sionna_update_hz}" =~ ^[0-9]+(\.[0-9]+)?$ ]] || usage_error "invalid OCUDU_NATIVE_SIONNA_UPDATE_HZ"
fi
for command_name in unshare nsenter ip mount umount flock cmake ctest ss setsid stdbuf; do
  command -v "${command_name}" >/dev/null 2>&1 || usage_error "missing command: ${command_name}"
done

if [[ -n "${acceleration}" ]]; then
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
  gnb_binary="${native_root}/builds/ocudu-zmq-release/apps/gnb/gnb"
  gnb_commit="${audited_ocudu}"
  gnb_kind="cpu"
  gnb_start_timeout="${OCUDU_NATIVE_GNB_START_TIMEOUT_SECONDS:-15}"
fi
[[ "${gnb_start_timeout}" =~ ^[1-9][0-9]*$ ]] || usage_error "invalid OCUDU_NATIVE_GNB_START_TIMEOUT_SECONDS"

for path in "${inner}" "${renderer}" "${gnb_binary}" \
  "${native_root}/builds/srsran4g-zmq-release/srsue/src/srsue" \
  "${native_root}/builds/open5gs-v2.7.6/tests/app/5gc" \
  "${native_root}/install/mongodb-6.0.29/bin/mongod" \
  "${topology}"; do
  [[ -e "${path}" ]] || usage_error "missing required path: ${path}"
done
[[ -c /dev/net/tun ]] || usage_error "/dev/net/tun is absent"
[[ "$(git -C "${native_root}/src/ocudu" rev-parse HEAD)" == "${audited_ocudu}" ]] || usage_error "OCUDU revision mismatch"
[[ "$(git -C "${native_root}/src/srsRAN_4G" rev-parse HEAD)" == "${audited_srsran}" ]] || usage_error "srsRAN revision mismatch"
[[ "$(git -C "${native_root}/src/open5gs" rev-parse HEAD)" == "${audited_open5gs}" ]] || usage_error "Open5GS revision mismatch"
if [[ "${OCUDU_NATIVE_SKIP_WORKSPACE_LOCK:-0}" == "1" ]]; then
  echo "event=skip x86_native_workspace_lock"
else
  "/usr/bin/python3" "${script_dir}/verify-workspace-lock.py" \
    --root "${native_root}" --repo-root "${repo_root}" \
    --lock "${script_dir}/native-workspace.lock.json"
fi
# Both gNB pairs, both UE pairs, mongo and the core.
for port in 2000 2001 2010 2011 2100 2101 2102 2103 27017 38412 7777; do
  ss -H -ltn "sport = :${port}" | grep -q . && usage_error "TCP port ${port} is already listening"
done

exec {lock_fd}<"${BASH_SOURCE[0]}"
flock -n "${lock_fd}" || usage_error "another native multi-gNB gate is running"
export OCUDU_NATIVE_GATE_LOCK_FD="${lock_fd}"

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
log_dir="${native_root}/results/logs/ocudu-multi-gnb/${timestamp}"
report_dir="${native_root}/results/reports/ocudu-multi-gnb/${timestamp}"
config_dir="${native_root}/configs/ocudu-multi-gnb-native/${timestamp}"
data_dir="${native_root}/data/ocudu-multi-gnb-native/${timestamp}"
netns_dir="${native_root}/run/ocudu-multi-gnb-native/${timestamp}/netns"
for path in "${log_dir}" "${report_dir}" "${config_dir}" "${data_dir}" "${netns_dir}"; do
  [[ ! -e "${path}" && ! -L "${path}" ]] || usage_error "run path already exists: ${path}"
done
mkdir -p "${log_dir}" "${report_dir}" "${config_dir}" "${data_dir}" "${netns_dir}"
printf 'event=native_multi_gnb_launch gnb=%s kind=%s commit=%s acceleration=%s host_memory=%s channel_mode=%s topology=%s scenario=%s\n' \
  "${gnb_binary}" "${gnb_kind}" "${gnb_commit:0:10}" "${acceleration:-none}" \
  "${OCUDU_NATIVE_CUDA_HOST_MEMORY:-default}" "${channel_mode}" "${topology}" "${sionna_scenario:-none}" | tee "${log_dir}/launch.log"

"/usr/bin/python3" "${renderer}" --repo-root "${repo_root}" --native-root "${native_root}" \
  --output-dir "${config_dir}" --log-dir "${log_dir}" --topology "${topology}" \
  --channel-mode "${channel_mode}" >"${log_dir}/render.log" 2>&1 || {
  cat "${log_dir}/render.log" >&2; usage_error "config rendering failed"
}
for cell in gnb0 gnb1; do
  "${gnb_binary}" -c "${config_dir}/${cell}.yaml" --dryrun >"${log_dir}/${cell}-dryrun.log" 2>&1 \
    || { tail -20 "${log_dir}/${cell}-dryrun.log" >&2; usage_error "${cell} dry run failed"; }
done

# Build the broker from THIS tree, so the gate tests the working copy.
channel_build="${OCUDU_NATIVE_CHANNEL_BUILD:-${native_root}/builds/ocudu-gpu-channel-cuda-release}"
cmake -S "${repo_root}" -B "${channel_build}" -DCMAKE_BUILD_TYPE=Release \
  -DOCUDU_GPU_CHANNEL_ENABLE_CUDA=ON -DCMAKE_CUDA_COMPILER="${cuda_compiler}" \
  -DCMAKE_CUDA_ARCHITECTURES="${cuda_arch}" -DOCUDU_GPU_CHANNEL_CUDA_ARCHITECTURES="${cuda_arch}" \
  >"${log_dir}/cmake-configure.log" 2>&1 \
  || { tail -20 "${log_dir}/cmake-configure.log" >&2; usage_error "cmake configure"; }
cmake --build "${channel_build}" -j"$(nproc)" >"${log_dir}/cmake-build.log" 2>&1 \
  || { tail -20 "${log_dir}/cmake-build.log" >&2; usage_error "cmake build"; }
CUDA_VISIBLE_DEVICES="${physical_gpu}" \
  ctest --test-dir "${channel_build}" --output-on-failure >"${log_dir}/ctest.log" 2>&1 \
  || { tail -20 "${log_dir}/ctest.log" >&2; usage_error "ctest"; }

sionna_args=()
if [[ "${channel_mode}" == "sionna" ]]; then
  # ipc:// under the run directory: the control and telemetry sockets live on
  # the shared filesystem, which is what crosses the inner network namespace.
  run_dir="${native_root}/run/ocudu-multi-gnb-native/${timestamp}"
  sionna_args=(
    --channel-mode sionna
    --control-endpoint "ipc://${run_dir}/control.sock"
    --telemetry-endpoint "ipc://${run_dir}/telemetry.sock"
    --sionna-python "${sionna_python}"
    --sionna-bridge "${repo_root}/apps/sionna_bridge/run_bridge.py"
    --sionna-scenario-config "${sionna_scenario}"
    --sionna-status-jsonl "${log_dir}/sionna-status.jsonl"
    --sionna-update-hz "${sionna_update_hz}"
    --sionna-ready-seconds 180
  )
fi

parent_netns="$(readlink /proc/self/ns/net)"
parent_mntns="$(readlink /proc/self/ns/mnt)"
set +e
unshare --user --map-root-user --net --mount --fork --kill-child --propagation private \
  "${inner}" \
  --repo-root "${repo_root}" --native-root "${native_root}" \
  --config-dir "${config_dir}" --log-dir "${log_dir}" --netns-dir "${netns_dir}" \
  --timestamp "${timestamp}" --physical-gpu "${physical_gpu}" \
  --channel-build "${channel_build}" --gnb-binary "${gnb_binary}" \
  --gnb-start-timeout "${gnb_start_timeout}" \
  --parent-netns "${parent_netns}" --parent-mntns "${parent_mntns}" \
  --outer-uid "$(id -u)" ${sionna_args[@]+"${sionna_args[@]}"}
inner_status="$?"
set -e

summary="${report_dir}/attach-summary.json"
if [[ "${inner_status}" -eq 0 && -f "${summary}" ]]; then
  printf 'event=native_multi_gnb_attach_gate result=pass kind=%s summary="%s"\n' "${gnb_kind}" "${summary}"
else
  [[ -f "${summary}" ]] && cat "${summary}" >&2
  printf 'event=native_multi_gnb_attach_gate result=fail kind=%s status=%s logs="%s"\n' \
    "${gnb_kind}" "${inner_status}" "${log_dir}"
  exit 1
fi
