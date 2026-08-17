#!/usr/bin/env bash
# Performance sweep: bench every example topology under both CPU and CUDA
# backends, collect per-phase latency, throughput, and memory footprint into a
# single JSON file. Populates the numbers behind docs/index.html
# section "Performance — measured boundaries".
#
# Usage: scripts/remote/perf-sweep.sh
# Output (on remote): ~/ocudu-gpu-channel-workspace/results/perf-sweep/<timestamp>/
#   sweep.json   -- aggregated per-config results, one JSON object per backend per config
#   *.csv        -- raw bench CSV per (config, backend)
#
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${script_dir}/common.sh"

duration_seconds="${PERF_SWEEP_DURATION_SECONDS:-10}"

case "${REMOTE_PROJECT_ROOT}" in
  "~/"*) remote_dest="${REMOTE_PROJECT_ROOT#\~/}" ;;
  *) remote_dest="${REMOTE_PROJECT_ROOT}" ;;
esac

echo "== syncing working tree to ${REMOTE_USER}@${REMOTE_HOST}:${remote_dest} =="
rsync -az --delete \
  --exclude '.git' --exclude 'build*' --exclude '.config' \
  -e "ssh -i ${REMOTE_SSH_KEY} -o BatchMode=yes -o ConnectTimeout=8" \
  "${repo_root}/" "${REMOTE_USER}@${REMOTE_HOST}:${remote_dest}/"

remote_sh bash -s -- "${REMOTE_WORKSPACE}" "${REMOTE_PROJECT_ROOT}" "${REMOTE_BUILDS_ROOT}" "${REMOTE_RESULTS_ROOT}" "${duration_seconds}" <<'REMOTE'
set -uo pipefail

workspace="$1"
project_root="$2"
builds_root="$3"
results_root="$4"
duration_seconds="$5"

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

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
out_dir="${results_root}/perf-sweep/${timestamp}"
mkdir -p "${out_dir}"
cuda_build="${builds_root}/ocudu-gpu-channel/cuda-release"
cpu_build="${builds_root}/ocudu-gpu-channel/cpu-release"

# ---- builds: CPU and CUDA release ----
mkdir -p "${cpu_build}" "${cuda_build}"
echo "== [build/1] CPU release =="
cmake -S "${project_root}" -B "${cpu_build}" \
  -DCMAKE_BUILD_TYPE=Release \
  -DOCUDU_GPU_CHANNEL_ENABLE_CUDA=OFF >/dev/null
cmake --build "${cpu_build}" -j"$(nproc)" --target ocudu-gpu-channel-bench >/dev/null
echo "    CPU bench OK"

echo "== [build/2] CUDA release =="
cmake -S "${project_root}" -B "${cuda_build}" \
  -DCMAKE_BUILD_TYPE=Release \
  -DOCUDU_GPU_CHANNEL_ENABLE_CUDA=ON \
  -DCMAKE_CUDA_COMPILER="${workspace}/tools/cuda-12.8.1/bin/nvcc" \
  -DOCUDU_GPU_CHANNEL_CUDA_ARCHITECTURES=120 >/dev/null
cmake --build "${cuda_build}" -j"$(nproc)" --target ocudu-gpu-channel-bench >/dev/null
echo "    CUDA bench OK"

# ---- helper: emit one JSON record per bench run ----
# Args: config_id  base_yaml  backend  csv_path
emit_record() {
  local config_id="$1" base_yaml="$2" backend="$3" mode="$4" csv="$5"
  python3 - "$config_id" "$base_yaml" "$backend" "$mode" "$csv" <<'PY'
import csv, json, os, sys
cfg, base, backend, mode, csv_path = sys.argv[1:6]
rec = {
  "config_id": cfg,
  "base_yaml": os.path.basename(base),
  "backend": backend,
  "mode": mode,
  "metrics": {},
}
# Parse the bench CSV. The first non-trivia rows are
# (metric, count, p50, p95, p99, p999, max, slot_us, gate); later rows are
# backend tag, cuda_status, iterations, then the throughput block.
metrics = {}
backend_tag = None
iterations = None
rate_hz = None
bps = None
with open(csv_path) as fh:
    reader = csv.reader(fh)
    section = None
    for row in reader:
        if not row:
            continue
        if row[0] == "metric":
            section = "metric"
            continue
        if row[0] == "backend":
            backend_tag = row[1]
            section = None
            continue
        if row[0] == "iterations":
            iterations = int(row[1])
            section = None
            continue
        if row[0] == "raw_cf32_full_duplex_bits_per_device":
            section = "throughput"
            continue
        if section == "metric" and len(row) >= 9:
            metrics[row[0]] = {
                "count": int(row[1]),
                "p50_us": float(row[2]),
                "p95_us": float(row[3]),
                "p99_us": float(row[4]),
                "p999_us": float(row[5]),
                "max_us": float(row[6]),
                "slot_us": float(row[7]),
                "gate": row[8],
            }
        elif section == "throughput" and len(row) >= 3 and rate_hz is None:
            rate_hz = int(row[1])
            bps = int(row[2])
rec["metrics"] = metrics
rec["backend_tag"] = backend_tag
rec["iterations"] = iterations
rec["sample_rate_hz_per_device"] = rate_hz
rec["raw_cf32_full_duplex_bits_per_sec_per_device"] = bps
print(json.dumps(rec))
PY
}

# ---- topology sweep: bench each config under CPU and CUDA ----
declare -A CONFIGS=(
  [mvp-2-edge]="examples/topology.mvp.cuda.yaml"
  [multi-ue-4-edge]="examples/topology.ocudu-docker.multi-ue.cuda.yaml"
  [graph-6-edge]="examples/topology.graph.cuda.yaml"
  [multi-gnb-8-edge]="examples/topology.multi-gnb.cuda.yaml"
  [stress-16-edge]="examples/topology.stress-16-edge.cuda.yaml"
)
ORDER=(mvp-2-edge multi-ue-4-edge graph-6-edge multi-gnb-8-edge stress-16-edge)

sweep_json="${out_dir}/sweep.json"
: > "${sweep_json}"

for cfg in "${ORDER[@]}"; do
  base_yaml="${project_root}/${CONFIGS[$cfg]}"
  if [[ ! -f "${base_yaml}" ]]; then
    echo "  skip ${cfg}: ${base_yaml} not found"
    continue
  fi

  # Generate per-backend copies in the out_dir so we don't mutate the source
  # examples; bench reads whatever YAML it is pointed at.
  cuda_yaml="${out_dir}/${cfg}.cuda.yaml"
  cpu_yaml="${out_dir}/${cfg}.cpu.yaml"
  awk '/^[[:space:]]*backend:/ {sub(/backend:[[:space:]]*[A-Za-z]+/,"backend: cuda")} {print}' "${base_yaml}" > "${cuda_yaml}"
  awk '/^[[:space:]]*backend:/ {sub(/backend:[[:space:]]*[A-Za-z]+/,"backend: cpu")}  {print}' "${base_yaml}" > "${cpu_yaml}"

  # Run each backend (process_superposition is the only entry point now).
  for backend in cpu cuda; do
    if [[ "${backend}" == "cpu" ]]; then
      bench_bin="${cpu_build}/ocudu-gpu-channel-bench"
      yaml="${cpu_yaml}"
    else
      bench_bin="${cuda_build}/ocudu-gpu-channel-bench"
      yaml="${cuda_yaml}"
    fi
    csv="${out_dir}/${cfg}.${backend}.csv"
    echo "== [bench] ${cfg} / ${backend} =="
    if "${bench_bin}" --config "${yaml}" --duration "${duration_seconds}s" --scs-khz 30 > "${csv}" 2>"${csv}.stderr"; then
      emit_record "${cfg}" "${base_yaml}" "${backend}" "superposition" "${csv}" >> "${sweep_json}"
      echo "    ${backend} OK"
    else
      echo "    ${backend} FAILED (see ${csv}.stderr)"
    fi
  done
done

# ---- device numbers: GPU memory + name from nvidia-smi ----
nvidia-smi --query-gpu=name,memory.total,memory.used,driver_version --format=csv,noheader,nounits > "${out_dir}/gpu-device.csv" 2>/dev/null || true

echo
echo "== sweep done =="
echo "out_dir=${out_dir}"
echo "sweep_json=${sweep_json}"
echo "record_count=$(wc -l < "${sweep_json}")"
REMOTE
