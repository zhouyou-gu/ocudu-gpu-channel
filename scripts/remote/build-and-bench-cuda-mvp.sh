#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${script_dir}/common.sh"

remote_sh bash -s -- "${REMOTE_WORKSPACE}" "${REMOTE_PROJECT_ROOT}" "${REMOTE_BUILDS_ROOT}" "${REMOTE_RESULTS_ROOT}" <<'REMOTE'
set -euo pipefail

workspace="$1"
project_root="$2"
builds_root="$3"
results_root="$4"

expand_remote_path() {
  case "$1" in
    "~") printf '%s\n' "${HOME}" ;;
    "~/"*) printf '%s/%s\n' "${HOME}" "${1#~/}" ;;
    *) printf '%s\n' "$1" ;;
  esac
}

workspace="$(expand_remote_path "${workspace}")"
project_root="$(expand_remote_path "${project_root}")"
builds_root="$(expand_remote_path "${builds_root}")"
results_root="$(expand_remote_path "${results_root}")"

if [[ ! -f "${workspace}/tools/env.sh" ]]; then
  echo "missing ${workspace}/tools/env.sh; run scripts/remote/bootstrap-user-tools.sh first" >&2
  exit 1
fi

# shellcheck source=/dev/null
source "${workspace}/tools/env.sh"

cpu_build="${builds_root}/ocudu-gpu-channel/cpu-release"
cuda_build="${builds_root}/ocudu-gpu-channel/cuda-release"
bench_dir="${results_root}/benchmarks"
mkdir -p "${cpu_build}" "${cuda_build}" "${bench_dir}"

cmake -S "${project_root}" -B "${cpu_build}" \
  -DCMAKE_BUILD_TYPE=Release \
  -DOCUDU_GPU_CHANNEL_ENABLE_CUDA=OFF
cmake --build "${cpu_build}" -j"$(nproc)"
ctest --test-dir "${cpu_build}" --output-on-failure

cmake -S "${project_root}" -B "${cuda_build}" \
  -DCMAKE_BUILD_TYPE=Release \
  -DOCUDU_GPU_CHANNEL_ENABLE_CUDA=ON \
  -DCMAKE_CUDA_COMPILER="${workspace}/tools/cuda-12.8.1/bin/nvcc" \
  -DOCUDU_GPU_CHANNEL_CUDA_ARCHITECTURES=120
cmake --build "${cuda_build}" -j"$(nproc)"
ctest --test-dir "${cuda_build}" --output-on-failure

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
"${cpu_build}/ocudu-gpu-channel-bench" \
  --config "${project_root}/examples/topology.local.cpu.yaml" \
  --duration 10s \
  --scs-khz 30 | tee "${bench_dir}/cpu-23p04-${timestamp}.csv"

"${cuda_build}/ocudu-gpu-channel-bench" \
  --config "${project_root}/examples/topology.mvp.cuda.yaml" \
  --duration 10s \
  --scs-khz 30 | tee "${bench_dir}/cuda-mvp-23p04-${timestamp}.csv"

echo "cpu_build=${cpu_build}"
echo "cuda_build=${cuda_build}"
echo "bench_dir=${bench_dir}"
REMOTE
