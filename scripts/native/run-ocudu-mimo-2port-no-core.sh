#!/usr/bin/env bash
set -euo pipefail

# Docker-free actual-OCUDU Split-8 transport gate. This exercises the native
# pinned gNB, the production CUDA broker and the repository's two-port peer.
# It is intentionally labelled transport-only and makes no UE/rank-2 claim.

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/../.." && pwd)"
native_root="${OCUDU_NATIVE_ROOT:-/home/ubuntu/ocudu-native-workspace}"
duration_seconds="${OCUDU_NATIVE_2PORT_DURATION_SECONDS:-20}"
physical_gpu="${OCUDU_NATIVE_GPU_DEVICE:-0}"
cuda_compiler="${CUDACXX:-/opt/conda/envs/cuda128/bin/nvcc}"
audited_ocudu_commit="a1916edcdbcd70ba6e0af47ee87be061dad5a4e4"
# M5.3: only the TOPOLOGY pin moved. The gNB fixture is an OCUDU config, needed
# no adaptation, and still hashes to the audited value -- so the pin below is
# the same one the superseded attempt ran against.
#
# The topology was adapted to the post-M1 schema. The pin exists so a run cannot
# silently use a different mapping than the audited one, so what changed is
# recorded rather than just re-hashed: `role:` removed from the radio_nodes
# entries (a node is an id and its ordered port lists), and `rx_ports`/
# `tx_ports` removed from the fixed_mimo blocks (dimensions are stated once, in
# the node declaration). Coefficients, endpoints, antenna counts and port order
# are unchanged.
#   previous topology pin: 5bdf3ecada9c07d651bae3f52efc9bcffb2562de79b1025af92deb13d0be2a6e
audited_gnb_fixture_sha256="6e0378b9969c3e12a3105ed58726b9aaa92236b2d8ce8247d9e25df9536c80b9"
audited_topology_sha256="ced8f0250c0a724ec015f5a2c1c31164325f0dcbafeb9b556605ea044e0eb6f5"

usage_error()
{
  printf 'error: %s\n' "$1" >&2
  exit 2
}

[[ "${native_root}" == /* && "${native_root}" != "/" && \
   "${native_root}" != "/home" && "${native_root}" != "/home/ubuntu" ]] || \
  usage_error "OCUDU_NATIVE_ROOT must be an absolute dedicated workspace"
[[ "${duration_seconds}" == "20" ]] || \
  usage_error "OCUDU_NATIVE_2PORT_DURATION_SECONDS is fixed to 20 for this gate"
[[ "${physical_gpu}" =~ ^(0|[1-9][0-9]*)$ ]] || \
  usage_error "OCUDU_NATIVE_GPU_DEVICE must be a non-negative integer"
[[ "${physical_gpu}" -le 255 ]] || \
  usage_error "OCUDU_NATIVE_GPU_DEVICE exceeds the supported CLI range"
[[ -x "${cuda_compiler}" ]] || usage_error "CUDA compiler is missing: ${cuda_compiler}"
command -v cmake >/dev/null 2>&1 || usage_error "cmake is required"
command -v python3 >/dev/null 2>&1 || usage_error "python3 is required"
command -v taskset >/dev/null 2>&1 || usage_error "taskset is required"
command -v ss >/dev/null 2>&1 || usage_error "ss is required"
command -v stdbuf >/dev/null 2>&1 || usage_error "stdbuf is required"
command -v flock >/dev/null 2>&1 || usage_error "flock is required"
broker_cpus="0-3"
peer_cpus="4-5"
gnb_cpus="6-23"
for cpu_set in "${broker_cpus}" "${peer_cpus}" "${gnb_cpus}"; do
  taskset -c "${cpu_set}" true >/dev/null 2>&1 || \
    usage_error "required CPU set is outside the current affinity: ${cpu_set}"
done

lock_path="${native_root}/results/.ocudu-mimo-2port-native.lock"
mkdir -p "$(dirname "${lock_path}")"
exec 9>"${lock_path}"
flock -n 9 || usage_error "another native two-port gate is already running"

ocudu_root="${native_root}/src/ocudu"
gnb_binary="${native_root}/builds/ocudu-zmq-release/apps/gnb/gnb"
channel_build="${native_root}/builds/ocudu-gpu-channel-cuda-release"
results_root="${native_root}/results"
gnb_fixture="${repo_root}/examples/native/ocudu/gnb_zmq_b210_fdd_2port_no_core.yaml"
topology="${repo_root}/examples/native/topology.ocudu.mimo-2port-transport.cuda.yaml"
validator="${script_dir}/validate-mimo-2port-transport.py"

for path in "${ocudu_root}/.git" "${gnb_binary}" "${gnb_fixture}" \
            "${topology}" "${validator}"; do
  [[ -e "${path}" ]] || usage_error "missing required native artifact: ${path}"
done
gnb_fixture_sha256="$(sha256sum "${gnb_fixture}" | awk '{print $1}')"
topology_sha256="$(sha256sum "${topology}" | awk '{print $1}')"
[[ "${gnb_fixture_sha256}" == "${audited_gnb_fixture_sha256}" ]] || \
  usage_error "native gNB fixture differs from the audited mapping"
[[ "${topology_sha256}" == "${audited_topology_sha256}" ]] || \
  usage_error "native channel topology differs from the audited mapping"
actual_ocudu_commit="$(git -C "${ocudu_root}" rev-parse HEAD)"
[[ "${actual_ocudu_commit}" == "${audited_ocudu_commit}" ]] || \
  usage_error "OCUDU revision ${actual_ocudu_commit} is not audited ${audited_ocudu_commit}"
[[ -z "$(git -C "${ocudu_root}" status --porcelain --untracked-files=no)" ]] || \
  usage_error "tracked OCUDU source files are modified"
gnb_cache="${native_root}/builds/ocudu-zmq-release/CMakeCache.txt"
[[ -f "${gnb_cache}" ]] || usage_error "missing OCUDU CMake cache: ${gnb_cache}"
grep -qx 'ENABLE_ZEROMQ:BOOL=ON' "${gnb_cache}" || \
  usage_error "native OCUDU build was not configured with ENABLE_ZEROMQ=ON"

ss -H -ltn >/dev/null || usage_error "unable to query listening TCP ports"
for port in 2000 2001 2002 2003 2100 2101 2102 2103; do
  if ss -H -ltn "sport = :${port}" | grep -q .; then
    usage_error "TCP port ${port} is already listening"
  fi
done

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
log_dir="${results_root}/logs/ocudu-mimo-2port-native/${timestamp}"
report_dir="${results_root}/reports/ocudu-mimo-2port-native/${timestamp}"
config_dir="${native_root}/configs/ocudu-mimo-2port-native/${timestamp}"
mkdir -p "${log_dir}" "${report_dir}" "${config_dir}" "${channel_build}"

summary_path="${report_dir}/transport-summary.json"
peer_summary="${report_dir}/peer-summary.json"
peer_selftest="${report_dir}/peer-selftest-summary.json"
source_evidence="${report_dir}/source-evidence.json"
endpoint_map="${report_dir}/endpoint-map.json"
channel_source_manifest="${report_dir}/channel-source-manifest.tsv"
channel_source_manifest_after="${report_dir}/channel-source-manifest.after-build.tsv"
resolved_gnb_config="${config_dir}/gnb.yaml"
preserved_gnb_fixture="${config_dir}/gnb.fixture.yaml"
preserved_topology="${config_dir}/topology.yaml"
gnb_version_log="${report_dir}/gnb-version.txt"
gnb_sha256="$(sha256sum "${gnb_binary}" | awk '{print $1}')"
channel_head="$(git -C "${repo_root}" rev-parse HEAD)"
channel_tracked_diff_sha256="$(git -C "${repo_root}" diff --binary -- . | sha256sum | awk '{print $1}')"

write_channel_manifest()
{
  local output_path="$1"
  python3 - "${repo_root}" "${output_path}" <<'PY'
import hashlib
import os
import subprocess
import sys

root, output_path = sys.argv[1:]
raw_paths = subprocess.check_output(
    ["git", "-C", root, "ls-files", "--cached", "--others", "--exclude-standard", "-z"]
)
paths = sorted(os.fsdecode(value) for value in raw_paths.split(b"\0") if value)
with open(output_path, "w", encoding="utf-8") as output:
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

write_channel_manifest "${channel_source_manifest}"
channel_source_manifest_sha256="$(sha256sum "${channel_source_manifest}" | awk '{print $1}')"

"${gnb_binary}" --version >"${gnb_version_log}" 2>&1
grep -Eq 'OCUDU 5G gNB version .*\(a1916edcd\)' "${gnb_version_log}" || \
  usage_error "native gNB binary does not identify the audited revision"

awk -v log_path="${log_dir}/gnb-internal.log" '
  /^[[:space:]]*filename: \/tmp\/ocudu-native-2port-gnb\.log$/ {
    print "  filename: " log_path
    next
  }
  { print }
' "${gnb_fixture}" >"${resolved_gnb_config}"
cp "${gnb_fixture}" "${preserved_gnb_fixture}"
cp "${topology}" "${preserved_topology}"
resolved_gnb_sha256="$(sha256sum "${resolved_gnb_config}" | awk '{print $1}')"

python3 - "${source_evidence}" "${audited_ocudu_commit}" \
  "${actual_ocudu_commit}" "${gnb_sha256}" "${gnb_fixture_sha256}" \
  "${topology_sha256}" "${resolved_gnb_sha256}" "${channel_head}" \
  "${channel_tracked_diff_sha256}" "${channel_source_manifest_sha256}" <<'PY'
import json
import sys

(
    path,
    audited,
    actual,
    gnb_sha256,
    gnb_fixture_sha256,
    topology_sha256,
    resolved_gnb_sha256,
    channel_head,
    channel_tracked_diff_sha256,
    channel_source_manifest_sha256,
) = sys.argv[1:]
data = {
    "schema": "ocudu-mimo-2port-source-evidence/v1",
    "audited_ocudu_commit": audited,
    "actual_ocudu_commit": actual,
    "audit_revision_match": audited == actual,
    "docker_used": False,
    "core_mode": "ocudu_no_core",
    "gnb_sha256": gnb_sha256,
    "gnb_fixture_sha256": gnb_fixture_sha256,
    "topology_sha256": topology_sha256,
    "resolved_gnb_sha256": resolved_gnb_sha256,
    "channel_head": channel_head,
    "channel_tracked_diff_sha256": channel_tracked_diff_sha256,
    "channel_source_manifest_sha256": channel_source_manifest_sha256,
    "source_paths": [
        "apps/units/flexible_o_du/split_8/helpers/ru_sdr_config_translator.cpp",
        "apps/units/o_cu_cp/o_cu_cp_builder.cpp",
        "apps/units/o_cu_up/o_cu_up_builder.cpp",
        "lib/radio/zmq/radio_session_zmq_impl.cpp",
        "lib/radio/zmq/radio_zmq_tx_channel.cpp",
        "lib/radio/zmq/radio_zmq_rx_channel.cpp",
    ],
    "claim_boundary": {
        "transport_only": True,
        "ue_decode": False,
        "rank2": False,
        "spatial_multiplexing": False,
        "attach_or_user_plane": False,
    },
}
with open(path, "w", encoding="utf-8") as output:
    json.dump(data, output, indent=2, sort_keys=True)
    output.write("\n")
PY

python3 - "${endpoint_map}" <<'PY'
import json
import sys

data = {
    "schema": "ocudu-mimo-2port-endpoints/v1",
    "network_namespace": "native_loopback",
    "ordered_ports": [0, 1],
    "ports": [
        {"node": "gnb0", "port": 0, "peer_tx_rep": "tcp://127.0.0.1:2000", "broker_tx_req": "tcp://127.0.0.1:2000", "broker_rx_rep": "tcp://127.0.0.1:2001", "peer_rx_req": "tcp://127.0.0.1:2001"},
        {"node": "gnb0", "port": 1, "peer_tx_rep": "tcp://127.0.0.1:2002", "broker_tx_req": "tcp://127.0.0.1:2002", "broker_rx_rep": "tcp://127.0.0.1:2003", "peer_rx_req": "tcp://127.0.0.1:2003"},
        {"node": "peer0", "port": 0, "peer_tx_rep": "tcp://127.0.0.1:2101", "broker_tx_req": "tcp://127.0.0.1:2101", "broker_rx_rep": "tcp://127.0.0.1:2100", "peer_rx_req": "tcp://127.0.0.1:2100"},
        {"node": "peer0", "port": 1, "peer_tx_rep": "tcp://127.0.0.1:2103", "broker_tx_req": "tcp://127.0.0.1:2103", "broker_rx_rep": "tcp://127.0.0.1:2102", "peer_rx_req": "tcp://127.0.0.1:2102"},
    ],
    "transport_only": True,
    "rank2_claim": False,
}
with open(sys.argv[1], "w", encoding="utf-8") as output:
    json.dump(data, output, indent=2, sort_keys=True)
    output.write("\n")
PY

export CUDA_VISIBLE_DEVICES="${physical_gpu}"
cmake -S "${repo_root}" -B "${channel_build}" \
  -DCMAKE_BUILD_TYPE=Release \
  -DOCUDU_GPU_CHANNEL_ENABLE_CUDA=ON \
  -DCMAKE_CUDA_COMPILER="${cuda_compiler}" \
  -DOCUDU_GPU_CHANNEL_CUDA_ARCHITECTURES=120 \
  >"${log_dir}/cmake-configure.log" 2>&1
cmake --build "${channel_build}" -j"$(nproc)" \
  >"${log_dir}/cmake-build.log" 2>&1
ctest --test-dir "${channel_build}" --output-on-failure -j1 \
  >"${log_dir}/ctest.log" 2>&1
write_channel_manifest "${channel_source_manifest_after}"
cmp -s "${channel_source_manifest}" "${channel_source_manifest_after}" || \
  usage_error "channel source changed while the gate was building"

peer_binary="${channel_build}/ocudu-mimo-transport-peer"
broker_binary="${channel_build}/ocudu-gpu-channel"
[[ -x "${peer_binary}" ]] || usage_error "missing peer binary: ${peer_binary}"
[[ -x "${broker_binary}" ]] || usage_error "missing broker binary: ${broker_binary}"
broker_sha256="$(sha256sum "${broker_binary}" | awk '{print $1}')"
python3 - "${source_evidence}" "${broker_sha256}" <<'PY'
import json
import sys

path, broker_sha256 = sys.argv[1:]
with open(path, "r", encoding="utf-8") as source:
    data = json.load(source)
data["broker_sha256"] = broker_sha256
with open(path, "w", encoding="utf-8") as output:
    json.dump(data, output, indent=2, sort_keys=True)
    output.write("\n")
PY

"${gnb_binary}" -c "${resolved_gnb_config}" --dryrun \
  >"${log_dir}/gnb-dryrun.log" 2>&1
"${peer_binary}" --self-test --summary-json "${peer_selftest}" \
  >"${log_dir}/peer-selftest.log" 2>&1

gnb_pid=""
peer_pid=""
broker_pid=""

process_running()
{
  local pid="$1"
  local state
  kill -0 "${pid}" >/dev/null 2>&1 || return 1
  state="$(ps -o stat= -p "${pid}" 2>/dev/null | tr -d '[:space:]')"
  [[ -n "${state}" && "${state}" != Z* ]]
}

stop_process()
{
  local pid="$1"
  local initial_signal="$2"
  local result_name="$3"
  local label="$4"
  local status=0

  if process_running "${pid}"; then
    kill -s "${initial_signal}" "${pid}" >/dev/null 2>&1 || true
  fi
  for _ in $(seq 1 100); do
    process_running "${pid}" || break
    sleep 0.1
  done
  if process_running "${pid}"; then
    echo "${label} did not stop after ${initial_signal}; sending TERM" >&2
    kill -TERM "${pid}" >/dev/null 2>&1 || true
    for _ in $(seq 1 30); do
      process_running "${pid}" || break
      sleep 0.1
    done
  fi
  if process_running "${pid}"; then
    echo "${label} did not stop after TERM; sending KILL" >&2
    kill -KILL "${pid}" >/dev/null 2>&1 || true
    for _ in $(seq 1 30); do
      process_running "${pid}" || break
      sleep 0.1
    done
  fi
  if process_running "${pid}"; then
    echo "${label} remains uninterruptible after KILL" >&2
    status=124
  elif wait "${pid}"; then
    status=0
  else
    status="$?"
  fi
  printf -v "${result_name}" '%s' "${status}"
}

cleanup()
{
  set +e
  if [[ -n "${broker_pid}" ]]; then
    stop_process "${broker_pid}" TERM ignored_status broker
  fi
  if [[ -n "${gnb_pid}" ]]; then
    stop_process "${gnb_pid}" INT ignored_status gnb
  fi
  if [[ -n "${peer_pid}" ]]; then
    stop_process "${peer_pid}" INT ignored_status peer
  fi
}
trap cleanup EXIT

peer_duration_seconds=$((duration_seconds + 60))
taskset -c "${peer_cpus}" stdbuf -oL -eL "${peer_binary}" \
  --tx0-endpoint tcp://127.0.0.1:2101 \
  --tx1-endpoint tcp://127.0.0.1:2103 \
  --rx0-endpoint tcp://127.0.0.1:2100 \
  --rx1-endpoint tcp://127.0.0.1:2102 \
  --tx0-chunk-samples 23040 \
  --tx1-chunk-samples 23040 \
  --tx0-reply-delay-us 0 \
  --tx1-reply-delay-us 73 \
  --rx0-request-offset-us 109 \
  --rx1-request-offset-us 0 \
  --duration "${peer_duration_seconds}s" \
  --summary-json "${peer_summary}" >"${log_dir}/peer.log" 2>&1 &
peer_pid="$!"
for _ in $(seq 1 100); do
  grep -q '^event=ready transport_only=1 tx_ports=2 rx_ports=2$' \
    "${log_dir}/peer.log" 2>/dev/null && break
  kill -0 "${peer_pid}" >/dev/null 2>&1 || {
    echo "synthetic peer exited before readiness" >&2
    exit 1
  }
  sleep 0.1
done
grep -q '^event=ready transport_only=1 tx_ports=2 rx_ports=2$' \
  "${log_dir}/peer.log" || usage_error "synthetic peer did not become ready"

taskset -c "${broker_cpus}" stdbuf -oL -eL "${broker_binary}" --config "${topology}" \
  --strict-realtime \
  >"${log_dir}/broker.log" 2>&1 &
broker_pid="$!"
broker_reached_running=0
for _ in $(seq 1 300); do
  process_running "${broker_pid}" || {
    echo "broker exited before transport readiness" >&2
    exit 1
  }
  socket_count="$(grep -c '^event=socket_ready ' "${log_dir}/broker.log" 2>/dev/null || true)"
  radio_count="$(grep -c '^event=radio_node_resolved ' "${log_dir}/broker.log" 2>/dev/null || true)"
  if [[ "${socket_count}" == "4" && "${radio_count}" == "2" ]] && \
      grep -q '^event=hardware_probe ok=true device=0 ' "${log_dir}/broker.log"; then
    broker_reached_running=1
    break
  fi
  sleep 0.1
done
[[ "${broker_reached_running}" == "1" ]] || \
  usage_error "broker did not expose the complete CUDA/four-port overlay"

taskset -c "${gnb_cpus}" stdbuf -oL -eL "${gnb_binary}" -c "${resolved_gnb_config}" \
  >"${log_dir}/gnb-console.log" 2>&1 &
gnb_pid="$!"
gnb_reached_running=0
for _ in $(seq 1 600); do
  process_running "${gnb_pid}" || {
    echo "OCUDU gNB exited during startup" >&2
    exit 1
  }
  if grep -q '^==== gNB started ===$' "${log_dir}/gnb-console.log" 2>/dev/null; then
    gnb_reached_running=1
    break
  fi
  sleep 0.1
done
[[ "${gnb_reached_running}" == "1" ]] || \
  usage_error "OCUDU gNB did not emit its bounded readiness token"

for _ in $(seq 1 $((duration_seconds * 10))); do
  process_running "${gnb_pid}" || usage_error "gNB exited during measured transport"
  process_running "${peer_pid}" || usage_error "peer exited during measured transport"
  process_running "${broker_pid}" || usage_error "broker exited during measured transport"
  sleep 0.1
done
gnb_alive_before_shutdown=0
process_running "${gnb_pid}" && gnb_alive_before_shutdown=1

set +e
# Stop grouped admission first while all four requesters remain alive, so any
# already-dispatched generation can drain. As soon as the broker exits, signal
# both peers before the native radio can run without its relay.
kill -TERM "${broker_pid}" >/dev/null 2>&1 || true
for _ in $(seq 1 500); do
  process_running "${broker_pid}" || break
  sleep 0.02
done
kill -INT "${gnb_pid}" >/dev/null 2>&1 || true
kill -INT "${peer_pid}" >/dev/null 2>&1 || true
stop_process "${broker_pid}" TERM broker_status broker
broker_pid=""
stop_process "${gnb_pid}" INT gnb_status gnb
gnb_pid=""
stop_process "${peer_pid}" INT peer_status peer
peer_pid=""
set -e

[[ -s "${log_dir}/gnb-internal.log" ]] || \
  usage_error "gNB internal log is missing or empty"

set +e
python3 "${validator}" \
  --summary "${summary_path}" \
  --broker-log "${log_dir}/broker.log" \
  --peer-summary "${peer_summary}" \
  --peer-selftest "${peer_selftest}" \
  --gnb-console-log "${log_dir}/gnb-console.log" \
  --gnb-internal-log "${log_dir}/gnb-internal.log" \
  --gnb-version-log "${gnb_version_log}" \
  --broker-status "${broker_status}" \
  --peer-status "${peer_status}" \
  --gnb-status "${gnb_status}" \
  --gnb-reached-running "${gnb_reached_running}" \
  --gnb-alive-before-shutdown "${gnb_alive_before_shutdown}" \
  --duration "${duration_seconds}" \
  --audited-commit "${audited_ocudu_commit}" \
  --actual-commit "${actual_ocudu_commit}" \
  --source-evidence "${source_evidence}" \
  --endpoint-map "${endpoint_map}" \
  --channel-source-manifest "${channel_source_manifest}" \
  --gnb-fixture "${preserved_gnb_fixture}" \
  --resolved-gnb-config "${resolved_gnb_config}" \
  --topology "${preserved_topology}" \
  --log-dir "${log_dir}" \
  --report-dir "${report_dir}"
verdict_status="$?"
set -e
exit "${verdict_status}"
