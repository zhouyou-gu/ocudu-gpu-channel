#!/usr/bin/env bash
# Native multi-UE attach gate: one OCUDU gNB, two srsUEs, Open5GS, and this
# tree's broker on examples/topology.ocudu-docker.multi-ue.cuda.yaml, with no
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
renderer="${script_dir}/render-multi-ue-configs.py"
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
[[ -x "${cuda_compiler}" ]] || usage_error "missing CUDA compiler: ${cuda_compiler}"
for command_name in unshare nsenter ip mount umount flock cmake ctest ss setsid stdbuf; do
  command -v "${command_name}" >/dev/null 2>&1 || usage_error "missing command: ${command_name}"
done
for path in "${inner}" "${renderer}" \
  "${native_root}/builds/ocudu-zmq-release/apps/gnb/gnb" \
  "${native_root}/builds/srsran4g-zmq-release/srsue/src/srsue" \
  "${native_root}/builds/open5gs-v2.7.6/tests/app/5gc" \
  "${native_root}/install/mongodb-6.0.29/bin/mongod" \
  "${repo_root}/examples/topology.ocudu-docker.multi-ue.cuda.yaml"; do
  [[ -e "${path}" ]] || usage_error "missing required path: ${path}"
done
[[ -c /dev/net/tun ]] || usage_error "/dev/net/tun is absent"
[[ "$(git -C "${native_root}/src/ocudu" rev-parse HEAD)" == "${audited_ocudu}" ]] || usage_error "OCUDU revision mismatch"
[[ "$(git -C "${native_root}/src/srsRAN_4G" rev-parse HEAD)" == "${audited_srsran}" ]] || usage_error "srsRAN revision mismatch"
[[ "$(git -C "${native_root}/src/open5gs" rev-parse HEAD)" == "${audited_open5gs}" ]] || usage_error "Open5GS revision mismatch"
"/usr/bin/python3" "${script_dir}/verify-workspace-lock.py" \
  --root "${native_root}" --repo-root "${repo_root}" \
  --lock "${script_dir}/native-workspace.lock.json"
grep -qx 'ENABLE_ZEROMQ:BOOL=ON' "${native_root}/builds/ocudu-zmq-release/CMakeCache.txt" || usage_error "gNB lacks ZMQ"
# Ports for the gNB pair and BOTH UE pairs, plus mongo and the core.
for port in 2000 2001 2100 2101 2102 2103 27017 38412 7777; do
  ss -H -ltn "sport = :${port}" | grep -q . && usage_error "TCP port ${port} is already listening"
done

exec {lock_fd}<"${BASH_SOURCE[0]}"
flock -n "${lock_fd}" || usage_error "another native multi-UE gate is running"

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

"/usr/bin/python3" "${renderer}" --repo-root "${repo_root}" --native-root "${native_root}" \
  --output-dir "${config_dir}" --log-dir "${log_dir}" >"${log_dir}/render.log" 2>&1 || {
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
set +e
unshare --user --map-root-user --net --mount --fork --kill-child --propagation private \
  "${inner}" \
  --repo-root "${repo_root}" --native-root "${native_root}" \
  --config-dir "${config_dir}" --log-dir "${log_dir}" --netns-dir "${netns_dir}" \
  --timestamp "${timestamp}" --physical-gpu "${physical_gpu}" \
  --channel-build "${channel_build}" \
  --parent-netns "${parent_netns}" --parent-mntns "${parent_mntns}" \
  --outer-uid "$(id -u)"
inner_status="$?"
set -e

summary="${report_dir}/attach-summary.json"
if [[ "${inner_status}" -eq 0 && -f "${summary}" ]]; then
  printf 'event=native_multi_ue_attach_gate result=pass summary="%s"\n' "${summary}"
else
  [[ -f "${summary}" ]] && cat "${summary}" >&2
  printf 'event=native_multi_ue_attach_gate result=fail status=%s logs="%s"\n' \
    "${inner_status}" "${log_dir}" >&2
fi
exit "${inner_status}"
