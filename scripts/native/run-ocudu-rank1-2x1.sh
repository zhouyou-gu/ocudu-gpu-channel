#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/../.." && pwd)"
# shellcheck source=env.sh
source "${script_dir}/env.sh"

native_root="${OCUDU_NATIVE_ROOT}"
duration_seconds="${OCUDU_NATIVE_RANK1_DURATION_SECONDS:-20}"
physical_gpu="${OCUDU_NATIVE_GPU_DEVICE:-0}"
cuda_compiler="${CUDACXX:-/opt/conda/envs/cuda128/bin/nvcc}"
inner="${script_dir}/run-ocudu-rank1-2x1-inner.sh"
renderer="${script_dir}/render-rank1-2x1-configs.py"
verifier="${script_dir}/verify-rank1-2x1-artifacts.py"
audited_ocudu="a1916edcdbcd70ba6e0af47ee87be061dad5a4e4"
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
[[ "${duration_seconds}" == "20" ]] || usage_error "duration is fixed to 20 seconds"
[[ "${physical_gpu}" =~ ^(0|[1-9][0-9]*)$ && "${physical_gpu}" -le 255 ]] || usage_error "invalid GPU device"
[[ "${native_root}" == /* && "${native_root}" != "/" && "${native_root}" != "/home/ubuntu" ]] || usage_error "invalid native root"
[[ -x "${cuda_compiler}" ]] || usage_error "missing CUDA compiler: ${cuda_compiler}"
for command_name in unshare nsenter ip mount umount flock cmake ctest ss setsid stdbuf; do
  command -v "${command_name}" >/dev/null 2>&1 || usage_error "missing command: ${command_name}"
done
[[ -x /usr/bin/python3 ]] || usage_error "missing /usr/bin/python3"
for path in "${inner}" "${renderer}" "${verifier}" \
  "${native_root}/builds/ocudu-zmq-release/apps/gnb/gnb" \
  "${native_root}/builds/srsran4g-zmq-release/srsue/src/srsue" \
  "${native_root}/builds/open5gs-v2.7.6/tests/app/5gc" \
  "${native_root}/install/mongodb-6.0.29/bin/mongod" \
  "${native_root}/builds/ocudu-gpu-channel-rank1-cuda-release/test_hardware_probe" \
  "${native_root}/builds/ocudu-gpu-channel-rank1-cuda-release/ocudu-gpu-channel" \
  "${repo_root}/examples/topology.ocudu-docker.cuda.yaml" \
  "${repo_root}/examples/native/topology.ocudu.rank1-2x1.cuda.yaml" \
  "${repo_root}/examples/native/ocudu/gnb_zmq_b210_fdd_2t2r_rank1_srsue.yaml"; do
  [[ -e "${path}" ]] || usage_error "missing required path: ${path}"
done
[[ -c /dev/net/tun ]] || usage_error "/dev/net/tun is absent"
[[ "$(git -C "${native_root}/src/ocudu" rev-parse HEAD)" == "${audited_ocudu}" ]] || usage_error "OCUDU revision mismatch"
[[ "$(git -C "${native_root}/src/srsRAN_4G" rev-parse HEAD)" == "${audited_srsran}" ]] || usage_error "srsRAN revision mismatch"
[[ "$(git -C "${native_root}/src/open5gs" rev-parse HEAD)" == "${audited_open5gs}" ]] || usage_error "Open5GS revision mismatch"
"/usr/bin/python3" "${script_dir}/verify-workspace-lock.py" \
  --root "${native_root}" --repo-root "${repo_root}" \
  --lock "${script_dir}/native-workspace.lock.json"
git -C "${repo_root}" diff --quiet bc88865 -- \
  examples/topology.ocudu-docker.cuda.yaml \
  examples/ocudu/gnb_zmq_b210_fdd_srsue.yaml \
  scripts/remote/ocudu-attach-smoke.sh scripts/remote/common.sh || \
  usage_error "pre-MIMO legacy fixture or driver changed"
grep -qx 'ENABLE_ZEROMQ:BOOL=ON' "${native_root}/builds/ocudu-zmq-release/CMakeCache.txt" || usage_error "gNB lacks ZMQ"
for binary in \
  "${native_root}/builds/ocudu-zmq-release/apps/gnb/gnb" \
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
for port in 2000 2001 2002 2003 2100 2101 27017 38412 7777; do
  ss -H -ltn "sport = :${port}" | grep -q . && usage_error "TCP port ${port} is already listening"
done

exec {lock_fd}<"${BASH_SOURCE[0]}"
flock -n "${lock_fd}" || usage_error "another native rank1 gate is running"
parent_netns="$(readlink /proc/self/ns/net)"
parent_mntns="$(readlink /proc/self/ns/mnt)"
probe_dir="$(mktemp -d /tmp/ocudu-native-userns-probe.XXXXXX)"
cleanup_probe()
{
  rmdir "${probe_dir}/run-netns" >/dev/null 2>&1 || true
  rmdir "${probe_dir}" >/dev/null 2>&1 || true
}
trap cleanup_probe EXIT
mkdir "${probe_dir}/run-netns"
unshare --user --map-root-user --net --mount --fork --kill-child --propagation private \
  "${inner}" --mode probe --parent-netns "${parent_netns}" --parent-mntns "${parent_mntns}" \
  --outer-uid "$(id -u)" --netns-dir "${probe_dir}/run-netns" \
  --physical-gpu "${physical_gpu}" \
  --hardware-probe "${native_root}/builds/ocudu-gpu-channel-rank1-cuda-release/test_hardware_probe" \
  --probe-broker "${native_root}/builds/ocudu-gpu-channel-rank1-cuda-release/ocudu-gpu-channel" \
  --probe-config "${repo_root}/examples/topology.ocudu-docker.cuda.yaml"
cleanup_probe
trap - EXIT

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
results_root="${native_root}/results"
log_dir="${results_root}/logs/rank1-2x1/${timestamp}"
report_dir="${results_root}/reports/rank1-2x1/${timestamp}"
config_dir="${native_root}/configs/ocudu-rank1-2x1-native/${timestamp}"
data_dir="${native_root}/data/ocudu-rank1-2x1-native/${timestamp}"
netns_dir="${native_root}/run/ocudu-rank1-2x1-native/${timestamp}/netns"
for path in "${log_dir}" "${report_dir}" "${config_dir}" "${data_dir}" "${netns_dir}"; do
  [[ ! -e "${path}" && ! -L "${path}" ]] || usage_error "run path already exists: ${path}"
done
mkdir -p "${log_dir}" "${report_dir}" "${config_dir}" "${data_dir}" "${netns_dir}"

source_manifest="${report_dir}/channel-source-manifest.tsv"
source_manifest_after="${report_dir}/channel-source-manifest.after-build.tsv"
source_evidence="${report_dir}/source-evidence.json"
preserved_configs="${report_dir}/configs"
write_channel_manifest "${source_manifest}"
channel_head="$(git -C "${repo_root}" rev-parse HEAD)"
channel_diff_sha256="$(git -C "${repo_root}" diff --binary -- . | sha256sum | awk '{print $1}')"

"/usr/bin/python3" "${renderer}" --repo-root "${repo_root}" --native-root "${native_root}" \
  --output-dir "${config_dir}" --log-dir "${log_dir}" >"${log_dir}/render.log" 2>&1
"${native_root}/builds/ocudu-zmq-release/apps/gnb/gnb" -c "${config_dir}/gnb.yaml" --dryrun \
  >"${log_dir}/gnb-dryrun.log" 2>&1

channel_build="${native_root}/builds/ocudu-gpu-channel-rank1-cuda-release"
cmake -S "${repo_root}" -B "${channel_build}" -DCMAKE_BUILD_TYPE=Release \
  -DOCUDU_GPU_CHANNEL_ENABLE_CUDA=ON -DCMAKE_CUDA_COMPILER="${cuda_compiler}" \
  -DOCUDU_GPU_CHANNEL_CUDA_ARCHITECTURES=120 >"${log_dir}/cmake-configure.log" 2>&1
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
"${native_root}/builds/ocudu-zmq-release/apps/gnb/gnb" --version \
  >"${report_dir}/gnb-version.txt" 2>&1
grep -Eq 'OCUDU 5G gNB version .*\(a1916edcd\)' "${report_dir}/gnb-version.txt" || \
  usage_error "native gNB binary does not identify the audited revision"
"/usr/bin/python3" - "${source_evidence}" "${native_root}" "${channel_build}" \
  "${source_manifest}" "${preserved_configs}" "${channel_head}" \
  "${channel_diff_sha256}" "${audited_ocudu}" "${audited_srsran}" \
  "${audited_open5gs}" <<'PY'
import hashlib
import json
import pathlib
import sys

(output_path, native_root, channel_build, manifest_path, config_root,
 channel_head, channel_diff_sha256, ocudu_commit, srsran_commit,
 open5gs_commit) = sys.argv[1:]

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
    "gnb": native / "builds/ocudu-zmq-release/apps/gnb/gnb",
    "srsue": native / "builds/srsran4g-zmq-release/srsue/src/srsue",
    "open5gs_5gc": native / "builds/open5gs-v2.7.6/tests/app/5gc",
    "mongod": native / "install/mongodb-6.0.29/bin/mongod",
    "broker": build / "ocudu-gpu-channel",
}
config_paths = {
    name: configs / name
    for name in ("gnb.yaml", "topology.yaml", "open5gs.yaml", "srsue.conf", "subscriber.csv")
}
data = {
    "schema": "ocudu-native-rank1-2x1-source-evidence/v1",
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
        "rank1_2x1_attach": True,
        "miso_simo": True,
        "pdu_session": True,
        "gateway_ping": True,
        "rank2": False,
        "strict_realtime": False,
    },
}
with open(output_path, "x", encoding="utf-8") as output:
    json.dump(data, output, indent=2, sort_keys=True)
    output.write("\n")
PY

# Re-probe the freshly built direct primitive before MongoDB, TUN, netns, or
# any live radio/core process is created for the acceptance run.
postbuild_probe="$(mktemp -d /tmp/ocudu-native-postbuild-probe.XXXXXX)"
mkdir "${postbuild_probe}/run-netns"
unshare --user --map-root-user --net --mount --fork --kill-child --propagation private \
  "${inner}" --mode probe --parent-netns "${parent_netns}" --parent-mntns "${parent_mntns}" \
  --outer-uid "$(id -u)" --netns-dir "${postbuild_probe}/run-netns" \
  --physical-gpu "${physical_gpu}" --hardware-probe "${channel_build}/test_hardware_probe" \
  --probe-broker "${channel_build}/ocudu-gpu-channel" \
  --probe-config "${repo_root}/examples/topology.ocudu-docker.cuda.yaml" \
  >"${log_dir}/postbuild-primitive-probe.log" 2>&1
rmdir "${postbuild_probe}/run-netns" "${postbuild_probe}"

set +e
unshare --user --map-root-user --net --mount --fork --kill-child --propagation private \
  "${inner}" --mode run --parent-netns "${parent_netns}" --parent-mntns "${parent_mntns}" \
  --outer-uid "$(id -u)" --netns-dir "${netns_dir}" --physical-gpu "${physical_gpu}" \
  --hardware-probe "${channel_build}/test_hardware_probe" \
  --probe-broker "${channel_build}/ocudu-gpu-channel" \
  --probe-config "${repo_root}/examples/topology.ocudu-docker.cuda.yaml" --native-root "${native_root}" \
  --repo-root "${repo_root}" --config-dir "${config_dir}" --log-dir "${log_dir}" \
  --report-dir "${report_dir}" --timestamp "${timestamp}"
run_status="$?"
set -e
summary_path="${report_dir}/attach-summary.json"
if [[ "${run_status}" -ne 0 ]]; then
  printf 'event=native_rank1_2x1_attach_gate result=fail child_status=%s summary="%s"\n' \
    "${run_status}" "${summary_path}" >&2
  exit "${run_status}"
fi
"/usr/bin/python3" "${verifier}" --results-root "${results_root}" --summary "${summary_path}"
printf 'summary=%s\n' "${summary_path}"
