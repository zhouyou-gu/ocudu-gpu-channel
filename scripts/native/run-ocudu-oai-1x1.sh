#!/usr/bin/env bash
set -euo pipefail

# M6.2 -- the native OAI nrUE 1x1 attach gate.
#
# OCUDU gNB (1T1R) <-> CUDA broker <-> OAI nrUE (1R), same immutable gNB /
# Open5GS / subscriber / channel fixtures as the srsUE legacy gate, so the UE
# process is the ONLY variable this gate changes. The srsUE gate stays in
# place as the regression net; this gate is additive.

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/../.." && pwd)"
# shellcheck source=env.sh
source "${script_dir}/env.sh"

native_root="${OCUDU_NATIVE_ROOT}"
duration_seconds="${OCUDU_NATIVE_OAI_DURATION_SECONDS:-25}"
physical_gpu="${OCUDU_NATIVE_GPU_DEVICE:-0}"
cuda_compiler="${CUDACXX:-/opt/conda/envs/cuda128/bin/nvcc}"
channel_build="${OCUDU_NATIVE_CHANNEL_BUILD:-${native_root}/builds/ocudu-gpu-channel-rank1-cuda-release}"
cuda_arch="${OCUDU_NATIVE_CUDA_ARCH:-120}"
# The CUDA gNB (scripts/cuda/resolve-cuda-gnb.py) can stand in for the CPU one,
# as in the srsUE legacy gate.
gnb_binary="${OCUDU_NATIVE_GNB_BINARY:-${native_root}/builds/ocudu-zmq-release/apps/gnb/gnb}"
audited_gnb_commit="${OCUDU_NATIVE_GNB_COMMIT:-a1916edcd}"
inner="${script_dir}/run-ocudu-oai-1x1-inner.sh"
# S8 (SPARK_MILESTONES.md): the bandwidth renderer composes over this one.
renderer="${OCUDU_NATIVE_CONFIG_RENDERER:-${script_dir}/render-oai-1x1-configs.py}"
verifier="${script_dir}/verify-oai-1x1-artifacts.py"
audited_ocudu="a1916edcdbcd70ba6e0af47ee87be061dad5a4e4"
audited_oai="2b69bde6aeafe892cda1531a0f0cbba2e37792cd"
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
[[ "${duration_seconds}" == "25" ]] || usage_error "duration is fixed to 25 seconds"
[[ "${physical_gpu}" =~ ^(0|[1-9][0-9]*)$ && "${physical_gpu}" -le 255 ]] || usage_error "invalid GPU device"
[[ "${native_root}" == /* && "${native_root}" != "/" && "${native_root}" != "/home/ubuntu" ]] || usage_error "invalid native root"
[[ -x "${cuda_compiler}" ]] || usage_error "missing CUDA compiler: ${cuda_compiler}"
for command_name in unshare nsenter ip mount umount flock cmake ctest ss setsid stdbuf; do
  command -v "${command_name}" >/dev/null 2>&1 || usage_error "missing command: ${command_name}"
done
[[ -x /usr/bin/python3 ]] || usage_error "missing /usr/bin/python3"
for path in "${inner}" "${renderer}" "${verifier}" \
  "${gnb_binary}" \
  "${native_root}/builds/oai-zmq-release/nr-uesoftmodem" \
  "${native_root}/builds/oai-zmq-release/liboai_zmqdevif.so" \
  "${native_root}/builds/oai-zmq-release/libparams_libconfig.so" \
  "${native_root}/builds/oai-zmq-release/libcoding.so" \
  "${native_root}/builds/oai-zmq-release/libldpc.so" \
  "${native_root}/builds/oai-zmq-release/libdfts.so" \
  "${native_root}/builds/open5gs-v2.7.6/tests/app/5gc" \
  "${native_root}/install/mongodb-6.0.29/bin/mongod" \
  "${repo_root}/use_cases/configs/topologies/ocudu_docker/topology.ocudu-docker.cuda.yaml" \
  "${repo_root}/use_cases/configs/ran/oai/nrue_zmq_1x1.conf"; do
  [[ -e "${path}" ]] || usage_error "missing required path: ${path}"
done
[[ -c /dev/net/tun ]] || usage_error "/dev/net/tun is absent"
[[ "$(git -C "${native_root}/src/ocudu" rev-parse HEAD)" == "${audited_ocudu}" ]] || usage_error "OCUDU revision mismatch"
[[ "$(git -C "${native_root}/src/oai" rev-parse HEAD)" == "${audited_oai}" ]] || usage_error "OAI revision mismatch"
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
# The gNB-side fixtures must stay byte-identical to the pre-MIMO baseline
# (commit 0c13a1a): the UE process is the only variable this gate is allowed
# to change. Pinned by content, so a snapshot checkout without that commit in
# its history (the Spark validation trees) is held to the same bytes.
printf '%s  %s\n' \
  7560250a7eff4ee125999a9eb15c386064a7a1de2cce276721b7ee866ab1cd67 "${repo_root}/use_cases/configs/topologies/ocudu_docker/topology.ocudu-docker.cuda.yaml" \
  720fac823f216db74b8c17d8a6bc92242a462a9be50e9316aa0c7b103fd7699f "${repo_root}/use_cases/configs/ran/ocudu/docker/gnb_zmq_b210_fdd_srsue.yaml" | \
  sha256sum --check --quiet --strict - >/dev/null 2>&1 || \
  usage_error "shared legacy fixture changed"
grep -qx 'ENABLE_ZEROMQ:BOOL=ON' "${native_root}/builds/ocudu-zmq-release/CMakeCache.txt" || usage_error "gNB lacks ZMQ"
for binary in \
  "${gnb_binary}" \
  "${native_root}/builds/oai-zmq-release/nr-uesoftmodem" \
  "${native_root}/builds/open5gs-v2.7.6/tests/app/5gc" \
  "${native_root}/install/mongodb-6.0.29/bin/mongod"; do
  ldd_report="$(mktemp /tmp/ocudu-native-oai-ldd.XXXXXX)"
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
# Gate defaults that keep a run at real time (SPARK_MILESTONES.md S9-S11,
# oai-gate-defaults.sh): MPS for a CUDA gNB (re-executes this gate), the
# patched OAI ZMQ radio module (oai-local-patches.lock.json), and the platform
# CPU placement. Each has an opt-out, and each choice goes to run-params.json.
# shellcheck source=oai-gate-defaults.sh
source "${script_dir}/oai-gate-defaults.sh"
# shellcheck source=oai-local-patches.sh
source "${script_dir}/oai-local-patches.sh"
oai_gate_mps_reexec "${gnb_binary}" "${BASH_SOURCE[0]}" || usage_error "MPS selection failed"
gnb_uses_cuda="${OAI_GATE_GNB_USES_CUDA}"
# Exports OCUDU_NATIVE_OAI_SHLIBPATH, which the inner script reads.
resolve_oai_zmq_module "${native_root}" || usage_error "OAI ZMQ module selection failed"
oai_gate_ue_rx_gain || usage_error "UE RX gain selection failed"
oai_gate_ue_fo_comp || usage_error "UE FO compensation selection failed"
oai_zmq_choice="${OAI_ZMQ_MODULE_VARIANT}"
printf 'oai zmq module: %s %s (sha256 %s)\n' "${OAI_ZMQ_MODULE_VARIANT}" \
  "${OCUDU_NATIVE_OAI_SHLIBPATH}" "${OAI_ZMQ_MODULE_SHA256}"
oai_gate_platform || usage_error "platform profile resolution failed"
# The stack runs in its own network namespace, so a host listener on these
# ports cannot collide with it. The check stays on by default; a host that runs
# its own Open5GS/MongoDB (this workstation does) sets
# OCUDU_NATIVE_ALLOW_HOST_PORTS=1.
[[ "${OCUDU_NATIVE_ALLOW_HOST_PORTS:-0}" == "1" ]] || \
for port in 2000 2001 2100 2101 27017 38412 7777; do
  ss -H -ltn "sport = :${port}" | grep -q . && usage_error "TCP port ${port} is already listening"
done

exec {lock_fd}<"${BASH_SOURCE[0]}"
flock -n "${lock_fd}" || usage_error "another native OAI gate is running"
export OCUDU_NATIVE_GATE_LOCK_FD="${lock_fd}"
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
  --hardware-probe "${channel_build}/test_hardware_probe" \
  --probe-broker "${channel_build}/ocudu-gpu-channel" \
  --probe-config "${repo_root}/use_cases/configs/topologies/ocudu_docker/topology.ocudu-docker.cuda.yaml"
cleanup_probe
trap - EXIT

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
results_root="${native_root}/results"
log_dir="${results_root}/logs/oai-1x1/${timestamp}"
report_dir="${results_root}/reports/oai-1x1/${timestamp}"
config_dir="${native_root}/configs/ocudu-oai-1x1-native/${timestamp}"
data_dir="${native_root}/data/ocudu-oai-1x1-native/${timestamp}"
netns_dir="${native_root}/run/ocudu-oai-1x1-native/${timestamp}/netns"
for path in "${log_dir}" "${report_dir}" "${config_dir}" "${data_dir}" "${netns_dir}"; do
  [[ ! -e "${path}" && ! -L "${path}" ]] || usage_error "run path already exists: ${path}"
done
mkdir -p "${log_dir}" "${report_dir}" "${config_dir}" "${data_dir}" "${netns_dir}"

source_manifest="${report_dir}/channel-source-manifest.tsv"
source_manifest_after="${report_dir}/channel-source-manifest.after-build.tsv"
source_evidence="${report_dir}/source-evidence.json"
preserved_configs="${report_dir}/configs"
write_channel_manifest "${source_manifest}"
# What this run actually used for the S9-S11 defaults.
/usr/bin/python3 - "${report_dir}/run-params.json" "${oai_zmq_choice}" "${OCUDU_NATIVE_OAI_SHLIBPATH}" \
  "${gnb_uses_cuda}" "${script_dir}/platform-profile.py" <<'PY'
import hashlib
import json
import os
import subprocess
import sys

path, choice, shlib, gnb_uses_cuda, profile_script = sys.argv[1:]
module = os.path.join(shlib, "liboai_zmqdevif.so")
manifest = os.path.join(shlib, "manifest.json")
data = {
    "schema": "ocudu-native-oai-1x1-run-params/v1",
    "oai_zmq_module": {
        "choice": choice,
        "path": module,
        "sha256": hashlib.sha256(open(module, "rb").read()).hexdigest(),
        "manifest": json.load(open(manifest)) if os.path.isfile(manifest) else None,
    },
    "platform": json.loads(subprocess.check_output(["/usr/bin/python3", profile_script, "--json"], text=True)),
    "cpus_applied": {role: os.environ.get(f"OCUDU_NATIVE_{role.upper()}_CPUS", "") for role in ("gnb", "broker", "nrue")},
    "ue_rx_gain_db": os.environ.get("OAI_GATE_UE_RX_GAIN_DB") or None,
    "ue_cont_fo_comp": os.environ.get("OAI_GATE_UE_CONT_FO_COMP") or None,
    "mps": {
        "gnb_uses_cuda": gnb_uses_cuda == "1",
        "requested": os.environ.get("OCUDU_NATIVE_MPS", "auto"),
        "active": bool(os.environ.get("CUDA_MPS_PIPE_DIRECTORY")),
        "evidence": os.environ.get("OCUDU_NATIVE_MPS_EVIDENCE"),
    },
}
with open(path, "x", encoding="utf-8") as output:
    json.dump(data, output, indent=2, sort_keys=True)
    output.write("\n")
PY
oai_gate_defaults_line | tee "${log_dir}/gate-defaults.log"
channel_head="$(git -C "${repo_root}" rev-parse HEAD)"
channel_diff_sha256="$(git -C "${repo_root}" diff --binary -- . | sha256sum | awk '{print $1}')"

"/usr/bin/python3" "${renderer}" --repo-root "${repo_root}" --native-root "${native_root}" \
  --output-dir "${config_dir}" --log-dir "${log_dir}" >"${log_dir}/render.log" 2>&1
"${gnb_binary}" -c "${config_dir}/gnb.yaml" --dryrun \
  >"${log_dir}/gnb-dryrun.log" 2>&1

cmake -S "${repo_root}" -B "${channel_build}" -DCMAKE_BUILD_TYPE=Release \
  -DOCUDU_GPU_CHANNEL_ENABLE_CUDA=ON -DCMAKE_CUDA_COMPILER="${cuda_compiler}" \
  -DCMAKE_CUDA_ARCHITECTURES="${cuda_arch}" -DOCUDU_GPU_CHANNEL_CUDA_ARCHITECTURES="${cuda_arch}" >"${log_dir}/cmake-configure.log" 2>&1
cmake --build "${channel_build}" -j"$(nproc)" >"${log_dir}/cmake-build.log" 2>&1
CUDA_VISIBLE_DEVICES="${physical_gpu}" \
  ctest --test-dir "${channel_build}" --output-on-failure >"${log_dir}/ctest.log" 2>&1
write_channel_manifest "${source_manifest_after}"
cmp -s "${source_manifest}" "${source_manifest_after}" || \
  usage_error "channel source changed while the native OAI gate was building"

mkdir "${preserved_configs}"
cp "${config_dir}/gnb.yaml" "${config_dir}/topology.yaml" \
  "${config_dir}/open5gs.yaml" "${config_dir}/nrue.conf" \
  "${config_dir}/subscriber.csv" "${preserved_configs}/"
for optional in nrue-radio.args uecap.xml; do
  if [[ -f "${config_dir}/${optional}" ]]; then cp "${config_dir}/${optional}" "${preserved_configs}/"; fi
done
"${gnb_binary}" --version \
  >"${report_dir}/gnb-version.txt" 2>&1
grep -Eq "OCUDU 5G gNB version .*\(${audited_gnb_commit}|OCUDU gNB \(commit ${audited_gnb_commit:0:7}[0-9a-f]*\)" "${report_dir}/gnb-version.txt" || \
  usage_error "native gNB binary does not identify the audited revision"
"/usr/bin/python3" - "${source_evidence}" "${native_root}" "${channel_build}" \
  "${source_manifest}" "${preserved_configs}" "${channel_head}" \
  "${channel_diff_sha256}" "${audited_ocudu}" "${audited_oai}" \
  "${audited_open5gs}" "${gnb_binary}" <<'PY'
import hashlib
import json
import os
import pathlib
import sys

(output_path, native_root, channel_build, manifest_path, config_root,
 channel_head, channel_diff_sha256, ocudu_commit, oai_commit,
 open5gs_commit, gnb_binary) = sys.argv[1:]

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
    "nrue": native / "builds/oai-zmq-release/nr-uesoftmodem",
    "oai_zmq_module": pathlib.Path(os.environ["OCUDU_NATIVE_OAI_SHLIBPATH"]) / "liboai_zmqdevif.so",
    "open5gs_5gc": native / "builds/open5gs-v2.7.6/tests/app/5gc",
    "mongod": native / "install/mongodb-6.0.29/bin/mongod",
    "broker": build / "ocudu-gpu-channel",
}
config_paths = {
    name: configs / name
    for name in ("gnb.yaml", "topology.yaml", "open5gs.yaml", "nrue.conf", "subscriber.csv")
}
data = {
    "schema": "ocudu-native-oai-1x1-source-evidence/v1",
    "docker_used": False,
    "channel_head": channel_head,
    "channel_tracked_diff_sha256": channel_diff_sha256,
    "channel_source_manifest_sha256": digest(manifest_path),
    "source_commits": {
        "ocudu": ocudu_commit,
        "oai": oai_commit,
        "open5gs": open5gs_commit,
    },
    "oai_zmq_module_variant": os.environ["OAI_ZMQ_MODULE_VARIANT"],
    "oai_zmq_module_dir": os.environ["OCUDU_NATIVE_OAI_SHLIBPATH"],
    "binary_sha256": {name: digest(path) for name, path in binary_paths.items()},
    "config_sha256": {name: digest(path) for name, path in config_paths.items()},
    "claim_boundary": {
        "oai_1x1_attach": True,
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
  --probe-config "${repo_root}/use_cases/configs/topologies/ocudu_docker/topology.ocudu-docker.cuda.yaml" \
  >"${log_dir}/postbuild-primitive-probe.log" 2>&1
rmdir "${postbuild_probe}/run-netns" "${postbuild_probe}"

set +e
unshare --user --map-root-user --net --mount --fork --kill-child --propagation private \
  "${inner}" --mode run --parent-netns "${parent_netns}" --parent-mntns "${parent_mntns}" \
  --outer-uid "$(id -u)" --netns-dir "${netns_dir}" --physical-gpu "${physical_gpu}" \
  --hardware-probe "${channel_build}/test_hardware_probe" \
  --probe-broker "${channel_build}/ocudu-gpu-channel" \
  --probe-config "${repo_root}/use_cases/configs/topologies/ocudu_docker/topology.ocudu-docker.cuda.yaml" --native-root "${native_root}" \
  --repo-root "${repo_root}" --config-dir "${config_dir}" --log-dir "${log_dir}" \
  --report-dir "${report_dir}" --timestamp "${timestamp}"
run_status="$?"
set -e
summary_path="${report_dir}/attach-summary.json"
if [[ "${run_status}" -ne 0 ]]; then
  printf 'event=native_oai_1x1_attach_gate result=fail child_status=%s summary="%s"\n' \
    "${run_status}" "${summary_path}" >&2
  exit "${run_status}"
fi
"/usr/bin/python3" "${verifier}" --results-root "${results_root}" --summary "${summary_path}"
printf 'summary=%s\n' "${summary_path}"
