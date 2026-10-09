#!/usr/bin/env bash
# C6: the rank-1 Sionna RT live demo, driven by the CUDA gNB.
#
# Three GPU clients share one RTX 5090 -- the broker's channel kernels, Sionna
# RT's OptiX ray tracing, and the CUDA gNB's PHY. The point of the milestone is
# whether they coexist inside the broker's slot budget, not whether any of them
# got faster.
#
# `run-ocudu-sionna-rank1.sh` is not modified. Its profile settings are read
# out of it rather than copied here, so the two cannot drift: only its literal
# `export OCUDU_NATIVE_*=` lines are taken, and its `exec` line is not run. The
# one setting overridden afterwards is the config renderer, which composes the
# CUDA acceleration block over the Sionna render instead of replacing it.
#
#   OCUDU_NATIVE_GNB_ACCELERATION  stage, default all
#   OCUDU_CUDA_C6_DURATION         seconds, default 60 (0 = run until SIGINT)
set -uo pipefail
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/../.." && pwd)"
native_dir="${repo_root}/scripts/native"
sionna_runner="${native_dir}/run-ocudu-sionna-rank1.sh"
source "${native_dir}/env.sh"

acceleration="${OCUDU_NATIVE_GNB_ACCELERATION:-all}"
case "${acceleration}" in
  disabled|low-phy-rx|low-phy-tx|pusch|pdsch|prach|all) ;;
  *) echo "invalid OCUDU_NATIVE_GNB_ACCELERATION: ${acceleration}" >&2; exit 2 ;;
esac

# Audits the pinned source, the reviewed patch, the build settings and the
# binary revision. Same gate as the 1x1 CUDA runner.
selection="$(/usr/bin/python3 "${script_dir}/resolve-cuda-gnb.py" \
  --root "${OCUDU_NATIVE_ROOT}" --fields)" || exit 1
IFS=$'\t' read -r gnb_binary gnb_source gnb_build gnb_commit <<<"${selection}"

# Take the Sionna profile's own settings from its runner. The variables it
# interpolates are bound first, with the same meaning that file gives them.
script_dir_saved="${script_dir}"
script_dir="${native_dir}"
scenario="${OCUDU_NATIVE_SIONNA_SCENARIO:-${repo_root}/use_cases/configs/sionna/scenarios/simple_street/ocudu-rank1.json}"
[[ "${scenario}" == /* && -f "${scenario}" && ! -L "${scenario}" ]] || {
  printf 'error: OCUDU_NATIVE_SIONNA_SCENARIO must be an absolute regular file: %s\n' "${scenario}" >&2
  exit 2
}
taken=0
while IFS= read -r line; do
  eval "${line}"
  taken=$((taken + 1))
done < <(grep -E '^export OCUDU_NATIVE_[A-Z_]+=' "${sionna_runner}")
script_dir="${script_dir_saved}"
# If that runner stops declaring its profile this way, fail rather than run a
# half-configured Sionna profile that looks like a CUDA result.
[[ "${taken}" -ge 8 ]] || {
  printf 'error: took only %s settings from %s; its profile declaration changed\n' \
    "${taken}" "${sionna_runner}" >&2
  exit 2
}

export OCUDU_NATIVE_GNB_BINARY="${gnb_binary}"
export OCUDU_NATIVE_GNB_COMMIT="${gnb_commit:0:7}"
export OCUDU_NATIVE_GNB_ACCELERATION="${acceleration}"
export OCUDU_NATIVE_GNB_START_TIMEOUT_SECONDS="${OCUDU_NATIVE_GNB_START_TIMEOUT_SECONDS:-120}"
export OCUDU_NATIVE_SIONNA_DURATION_SECONDS="${OCUDU_CUDA_C6_DURATION:-60}"
# Compose over the Sionna renderer the profile just selected, rather than
# replacing it: the CUDA block is appended to its output.
export OCUDU_NATIVE_CUDA_BASE_RENDERER="$(basename "${OCUDU_NATIVE_CONFIG_RENDERER}")"
export OCUDU_NATIVE_CONFIG_RENDERER="${script_dir}/render-cuda-1x1-configs.py"

printf 'event=native_cuda_sionna_rank1_launch gnb=%s commit=%s acceleration=%s base_renderer=%s duration=%s\n' \
  "${gnb_binary}" "${OCUDU_NATIVE_GNB_COMMIT}" "${acceleration}" \
  "${OCUDU_NATIVE_CUDA_BASE_RENDERER}" "${OCUDU_NATIVE_SIONNA_DURATION_SECONDS}"

exec "${native_dir}/run-ocudu-legacy-1x1.sh" "$@"
