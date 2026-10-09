#!/usr/bin/env bash
set -euo pipefail

# M6.3 -- the native OAI nrUE 2x2 SU-MIMO run.
#
# OCUDU gNB (2T2R) <-> CUDA broker with a fixed 2x2 matrix <-> OAI nrUE
# (2 RX / 2 TX), single UE. Same Open5GS / subscriber / cell identity as the
# OAI 1x1 gate; see render-oai-2x2-configs.py for exactly what differs.
#
# Knobs (environment):
#   OAI2X2_PATH           broker (default) | direct (no emulator, identity H)
#   OAI2X2_MAX_RANK       2 (default) | 1
#   OAI2X2_MAX_UE_MCS     optional PDSCH MCS cap
#   OAI2X2_CSI_RS         on (default) | off
#   OAI2X2_TX_BACKOFF_DB  gNB amplitude_control.tx_gain_backoff (OCUDU default 12)
#   OAI2X2_TOPOLOGY       topology fixture (default the 2x2 fixture)
#   OAI2X2_BW_MHZ         cell bandwidth 20 (default) | 30 | 40 | 50 | 100 (100 is
#                         the n78 TDD 30 kHz cell), as in render-1x1-bw-configs.py
#   OAI2X2_CUDA_HOST_MEMORY  broker runtime.cuda_host_memory copy | zero_copy | auto
#                         (unset leaves the topology's default, auto)
#   OAI2X2_BROKER_SECONDS run window (default 90)
#   OAI2X2_IPERF_SECONDS  DL UDP iperf length, 0 disables (default 15)
#   OAI2X2_IPERF_RATE     DL UDP offered rate (default 80M)
#   OAI2X2_UL_MAX_RANK    unset (default: stock UL, 1 layer) | 1 | 2; set, the gNB
#                         gets pusch.max_rank + periodic SRS and the UE a band-3
#                         capability with 2-layer codebook PUSCH (renderer doc)
#   OAI2X2_IPERF_DIR      dl (default) | ul | both -- UDP iperf direction(s)
#   OAI2X2_IPERF_UL_RATE  UL UDP offered rate (default 60M)
#   OAI2X2_GNB_PHY_LOG    info (default) | debug -- diagnosis only
#   OAI2X2_ALLOW_UNHEALTHY  1 keeps exit 0 when the run summary fails its health
#                         bound (NACK > 10%, PUSCH KO > 10%); for runs meant to fail
#   OAI2X2_BROKER_EXTRA   extra broker arguments (WIRECAP expands to the run's
#                         wire-capture directory)
#   OCUDU_NATIVE_OAI2X2_WIRE_CAPTURE_SAMPLES / _SKIP_SECONDS  broker wire capture per
#                         port and direction into logs/<ts>/wire-capture (0 = off /
#                         skip 2 s from each port's first sample), as the OAI 1x1
#                         gate's OCUDU_NATIVE_OAI1X1_* knobs; wire-capture-power.py
#                         prints the per-port levels (X7 measurement)
#   OAI2X2_UE_EXTRA       extra nr-uesoftmodem arguments
#   OCUDU_NATIVE_OAI_UE_CONT_FO_COMP  UE --cont-fo-comp mode, 1 (default) | 0 (off) | 2 | 3
#   OCUDU_NATIVE_OAI_UE   local (default: builds/oai-zmq-local, pinned OAI + the
#                         local UE PHY patches, e.g. the 2-layer MMSE int16 fix)
#                         or stock (builds/oai-zmq-release, the pinned build)
#   OAI2X2_NRUE_DIR       explicit nr-uesoftmodem build directory; overrides
#                         OCUDU_NATIVE_OAI_UE
#   OCUDU_NATIVE_OAI_ZMQ_MODULE  patched (default: builds/oai-zmq-patched, the S9
#                         reply-poll driver) or stock (builds/oai-zmq-release)
#   OCUDU_NATIVE_OAI_SHLIBPATH  explicit ZMQ radio module directory; overrides
#                         OCUDU_NATIVE_OAI_ZMQ_MODULE
#   OCUDU_NATIVE_GNB_BINARY  gNB binary (default the pinned CPU build); a CUDA
#                         gNB runs under MPS by default (OCUDU_NATIVE_MPS)
#   OCUDU_NATIVE_MPS      auto (default: MPS only for a CUDA gNB) | on | off
#   OCUDU_NATIVE_PLATFORM auto (default: platform-profiles.json CPU split) | none
#   OCUDU_NATIVE_CUDA_ARCH  broker CUDA architecture (default 120; GB10 121)
#   OCUDU_NATIVE_CHANNEL_BUILD  broker build directory
#   OCUDU_NATIVE_BROKER_STARTUP_ALLOWANCE_SECONDS  added to the broker window
#   OCUDU_NATIVE_GNB_ACCELERATION  CUDA gNB stage (all, pdsch, ...); renders the
#                         acceleration keys through scripts/cuda/render-cuda-1x1-configs.py
#                         (a CUDA gNB takes ~20 s to start)
#   OAI2X2_LABEL          free-text label stored with the run
#
# Output: results/{logs,reports}/oai-2x2/<timestamp>/, then
# summarize-oai-2x2-run.py prints rank / HARQ / throughput from the logs.

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/../.." && pwd)"
# shellcheck source=env.sh
source "${script_dir}/env.sh"

native_root="${OCUDU_NATIVE_ROOT}"
# shellcheck source=oai-local-patches.sh
source "${script_dir}/oai-local-patches.sh"
# shellcheck source=oai-gate-defaults.sh
source "${script_dir}/oai-gate-defaults.sh"
gnb_binary="${OCUDU_NATIVE_GNB_BINARY:-${native_root}/builds/ocudu-zmq-release/apps/gnb/gnb}"
export OCUDU_NATIVE_GNB_BINARY="${gnb_binary}"
cuda_arch="${OCUDU_NATIVE_CUDA_ARCH:-120}"
physical_gpu="${OCUDU_NATIVE_GPU_DEVICE:-0}"
cuda_compiler="${CUDACXX:-/usr/local/cuda/bin/nvcc}"
inner="${script_dir}/run-ocudu-oai-2x2-inner.sh"
renderer="${script_dir}/render-oai-2x2-configs.py"
# A CUDA gNB needs its acceleration keys (expert_phy, ru_sdr.expert_cfg); the
# CUDA renderer runs the 2x2 renderer unchanged and appends them, as it does
# for the 1x1 profiles. OCUDU_NATIVE_GNB_ACCELERATION=<stage> selects it.
if [[ -n "${OCUDU_NATIVE_GNB_ACCELERATION:-}" ]]; then
  renderer="${repo_root}/scripts/cuda/render-cuda-1x1-configs.py"
  export OCUDU_NATIVE_CUDA_BASE_RENDERER=render-oai-2x2-configs.py
fi
summarizer="${script_dir}/summarize-oai-2x2-run.py"
audited_ocudu="a1916edcdbcd70ba6e0af47ee87be061dad5a4e4"
audited_oai="2b69bde6aeafe892cda1531a0f0cbba2e37792cd"

export OAI2X2_PATH="${OAI2X2_PATH:-broker}"
max_rank="${OAI2X2_MAX_RANK:-2}"
csi_rs="${OAI2X2_CSI_RS:-on}"
topology="${OAI2X2_TOPOLOGY:-${repo_root}/use_cases/configs/topologies/ocudu_native/topology.ocudu.oai-2x2.cuda.yaml}"

usage_error()
{
  printf 'error: %s\n' "$1" >&2
  exit 2
}

[[ "$#" -eq 0 ]] || usage_error "usage: $0 (configure with OAI2X2_* variables)"
[[ "${OAI2X2_PATH}" == broker || "${OAI2X2_PATH}" == direct ]] || usage_error "OAI2X2_PATH must be broker or direct"
[[ "${native_root}" == /* && "${native_root}" != "/" ]] || usage_error "invalid native root"
[[ -x "${cuda_compiler}" ]] || usage_error "missing CUDA compiler: ${cuda_compiler}"
for command_name in unshare nsenter ip mount umount flock cmake ss setsid stdbuf iperf3; do
  command -v "${command_name}" >/dev/null 2>&1 || usage_error "missing command: ${command_name}"
done
[[ "$(git -C "${native_root}/src/ocudu" rev-parse HEAD)" == "${audited_ocudu}" ]] || usage_error "OCUDU revision mismatch"
[[ "$(git -C "${native_root}/src/oai" rev-parse HEAD)" == "${audited_oai}" ]] || usage_error "OAI revision mismatch"
[[ -x "${gnb_binary}" ]] || usage_error "missing gNB binary: ${gnb_binary}"
# The S9-S11 real-time defaults, as in the OAI 1x1 gate (oai-gate-defaults.sh).
# MPS first: it re-executes this script under one MPS server for a CUDA gNB.
oai_gate_mps_reexec "${gnb_binary}" "${BASH_SOURCE[0]}" || usage_error "MPS selection failed"
oai_gate_platform || usage_error "platform profile resolution failed"
# Exports OCUDU_NATIVE_OAI_SHLIBPATH for the inner script (patched by default).
resolve_oai_zmq_module "${native_root}" || usage_error "OAI ZMQ module selection failed"
oai_gate_ue_rx_gain || usage_error "UE RX gain selection failed"
oai_gate_ue_fo_comp || usage_error "UE FO compensation selection failed"
printf 'oai zmq module: %s %s\n' "${OAI_ZMQ_MODULE_VARIANT}" "${OCUDU_NATIVE_OAI_SHLIBPATH}"
# Exports OAI2X2_NRUE_DIR for the inner script (the locally patched UE by default).
resolve_oai_ue_build "${native_root}" || usage_error "OAI UE build selection failed"
printf 'oai ue: %s %s\n' "${OAI_UE_VARIANT}" "${OAI2X2_NRUE_DIR}"

exec {lock_fd}<"${script_dir}/run-ocudu-oai-1x1.sh"
flock -n "${lock_fd}" || usage_error "another native OAI gate is running"
export OCUDU_NATIVE_GATE_LOCK_FD="${lock_fd}"

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
results_root="${native_root}/results"
log_dir="${results_root}/logs/oai-2x2/${timestamp}"
report_dir="${results_root}/reports/oai-2x2/${timestamp}"
config_dir="${native_root}/configs/ocudu-oai-2x2-native/${timestamp}"
netns_dir="${native_root}/run/ocudu-oai-2x2-native/${timestamp}/netns"
data_dir="${native_root}/data/ocudu-oai-2x2-native/${timestamp}"
mkdir -p "${log_dir}" "${report_dir}" "${config_dir}" "${netns_dir}" "${data_dir}"

render_args=(--repo-root "${repo_root}" --native-root "${native_root}" --output-dir "${config_dir}"
  --log-dir "${log_dir}" --path "${OAI2X2_PATH}" --max-rank "${max_rank}" --csi-rs "${csi_rs}"
  --topology "${topology}")
[[ -n "${OAI2X2_MAX_UE_MCS:-}" ]] && render_args+=(--max-ue-mcs "${OAI2X2_MAX_UE_MCS}")
[[ -n "${OAI2X2_TX_BACKOFF_DB:-}" ]] && render_args+=(--tx-backoff-db "${OAI2X2_TX_BACKOFF_DB}")
[[ -n "${OAI2X2_BW_MHZ:-}" ]] && render_args+=(--bw-mhz "${OAI2X2_BW_MHZ}")
[[ -n "${OAI2X2_CUDA_HOST_MEMORY:-}" ]] && render_args+=(--cuda-host-memory "${OAI2X2_CUDA_HOST_MEMORY}")
[[ -n "${OAI2X2_UL_MAX_RANK:-}" ]] && render_args+=(--ul-max-rank "${OAI2X2_UL_MAX_RANK}")
[[ -n "${OAI2X2_GNB_PHY_LOG:-}" ]] && render_args+=(--gnb-phy-log "${OAI2X2_GNB_PHY_LOG}")
/usr/bin/python3 "${renderer}" "${render_args[@]}" >"${log_dir}/render.log" 2>&1 || {
  cat "${log_dir}/render.log" >&2; usage_error "render failed"; }
"${gnb_binary}" -c "${config_dir}/gnb.yaml" --dryrun \
  >"${log_dir}/gnb-dryrun.log" 2>&1 || { tail -20 "${log_dir}/gnb-dryrun.log" >&2; usage_error "gNB dry run failed"; }

channel_build="${OCUDU_NATIVE_CHANNEL_BUILD:-${native_root}/builds/ocudu-gpu-channel-oai2x2-cuda-release}"
cmake -S "${repo_root}" -B "${channel_build}" -DCMAKE_BUILD_TYPE=Release \
  -DOCUDU_GPU_CHANNEL_ENABLE_CUDA=ON -DCMAKE_CUDA_COMPILER="${cuda_compiler}" \
  -DCMAKE_CUDA_ARCHITECTURES="${cuda_arch}" \
  -DOCUDU_GPU_CHANNEL_CUDA_ARCHITECTURES="${cuda_arch}" >"${log_dir}/cmake-configure.log" 2>&1
cmake --build "${channel_build}" -j"$(nproc)" >"${log_dir}/cmake-build.log" 2>&1
export OAI2X2_BROKER_BIN="${channel_build}/ocudu-gpu-channel"

cp "${config_dir}"/* "${report_dir}/"
{
  printf 'timestamp=%s\npath=%s\nmax_rank=%s\ncsi_rs=%s\nmax_ue_mcs=%s\ntx_backoff_db=%s\n' \
    "${timestamp}" "${OAI2X2_PATH}" "${max_rank}" "${csi_rs}" "${OAI2X2_MAX_UE_MCS:-}" "${OAI2X2_TX_BACKOFF_DB:-}"
  printf 'ul_max_rank=%s\niperf_dir=%s\n' "${OAI2X2_UL_MAX_RANK:-}" "${OAI2X2_IPERF_DIR:-dl}"
  printf 'topology=%s\nlabel=%s\nue_extra=%s\nbroker_extra=%s\nnrue_dir=%s\nbw_mhz=%s\ncuda_host_memory=%s\n' \
    "${topology}" "${OAI2X2_LABEL:-}" "${OAI2X2_UE_EXTRA:-}" "${OAI2X2_BROKER_EXTRA:-}" "${OAI2X2_NRUE_DIR:-}" \
    "${OAI2X2_BW_MHZ:-20}" "${OAI2X2_CUDA_HOST_MEMORY:-default}"
  printf 'oai_ue=%s\noai_ue_sha256=%s\n' "${OAI_UE_VARIANT}" "${OAI_UE_SHA256}"
  printf 'gnb_acceleration=%s\n' "${OCUDU_NATIVE_GNB_ACCELERATION:-none}"
  printf 'ue_rx_gain_db=%s\nue_cont_fo_comp=%s\n' "${OAI_GATE_UE_RX_GAIN_DB:-none}" "${OAI_GATE_UE_CONT_FO_COMP:-off}"
  printf 'wire_capture_samples=%s\nwire_capture_skip_seconds=%s\n' \
    "${OCUDU_NATIVE_OAI2X2_WIRE_CAPTURE_SAMPLES:-0}" "${OCUDU_NATIVE_OAI2X2_WIRE_CAPTURE_SKIP_SECONDS:-2}"
  printf 'oai_zmq_module=%s\noai_shlibpath=%s\noai_zmq_module_sha256=%s\n' \
    "${OAI_ZMQ_MODULE_VARIANT}" "${OCUDU_NATIVE_OAI_SHLIBPATH}" "${OAI_ZMQ_MODULE_SHA256}"
  printf 'gnb_binary=%s\ngnb_uses_cuda=%s\nmps=%s\nplatform=%s\ngnb_cpus=%s\nbroker_cpus=%s\nnrue_cpus=%s\n' \
    "${gnb_binary}" "${OAI_GATE_GNB_USES_CUDA}" "$([[ -n "${CUDA_MPS_PIPE_DIRECTORY:-}" ]] && echo on || echo off)" \
    "${OCUDU_NATIVE_PLATFORM_PROFILE}" "${OCUDU_NATIVE_GNB_CPUS:-any}" "${OCUDU_NATIVE_BROKER_CPUS:-any}" \
    "${OCUDU_NATIVE_NRUE_CPUS:-any}"
  printf 'broker_env=%s\n' "${OCUDU_NATIVE_BROKER_ENV:-none}"
  printf 'channel_head=%s\nchannel_dirty=%s\n' "$(git -C "${repo_root}" rev-parse HEAD)" \
    "$(git -C "${repo_root}" status --porcelain | wc -l)"
} >"${report_dir}/run-params.txt"
oai_gate_defaults_line | tee "${log_dir}/gate-defaults.log"

parent_netns="$(readlink /proc/self/ns/net)"
parent_mntns="$(readlink /proc/self/ns/mnt)"
set +e
unshare --user --map-root-user --net --mount --fork --kill-child --propagation private \
  "${inner}" --mode run --parent-netns "${parent_netns}" --parent-mntns "${parent_mntns}" \
  --outer-uid "$(id -u)" --netns-dir "${netns_dir}" --physical-gpu "${physical_gpu}" \
  --native-root "${native_root}" --repo-root "${repo_root}" --config-dir "${config_dir}" \
  --log-dir "${log_dir}" --report-dir "${report_dir}" --timestamp "${timestamp}"
run_status="$?"
set -e
set +e
/usr/bin/python3 "${summarizer}" --log-dir "${log_dir}" --report-dir "${report_dir}"
summary_status="$?"
set -e
# A run that attaches but NACKs most PDSCH (S12) is not a pass. Diagnosis runs
# that are meant to fail set OAI2X2_ALLOW_UNHEALTHY=1.
if [[ "${run_status}" == 0 && "${summary_status}" != 0 && "${OAI2X2_ALLOW_UNHEALTHY:-0}" != 1 ]]; then
  run_status=3
fi
printf 'event=native_oai_2x2_run status=%s timestamp=%s path=%s max_rank=%s summary_status=%s report=%s\n' \
  "${run_status}" "${timestamp}" "${OAI2X2_PATH}" "${max_rank}" "${summary_status}" "${report_dir}"
exit "${run_status}"
