#!/usr/bin/env bash
# Fan-in topology sweep: drives 21 generated configs through the bench under
# the CUDA backend and captures per-config metrics (bench latency, per-phase
# GPU µs, nvidia-smi snapshot during the run, GPU memory delta, PCIe state).
# For each config: also runs a short nsys trace for kernel-level timing.
# Output: one results directory on the remote containing JSON + raw CSVs.
#
# Usage: scripts/remote/perf-fanin-sweep.sh [duration_seconds]
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${script_dir}/common.sh"

duration_seconds="${1:-10}"

case "${REMOTE_PROJECT_ROOT}" in
  "~/"*) remote_dest="${REMOTE_PROJECT_ROOT#\~/}" ;;
  *) remote_dest="${REMOTE_PROJECT_ROOT}" ;;
esac

echo "== syncing working tree =="
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
cuda_build="${builds_root}/ocudu-gpu-channel/cuda-release"

# Ensure bench is built
if [[ ! -x "${cuda_build}/ocudu-gpu-channel-bench" ]]; then
  echo "missing ${cuda_build}/ocudu-gpu-channel-bench; building..."
  mkdir -p "${cuda_build}"
  cmake -S "${project_root}" -B "${cuda_build}" \
    -DCMAKE_BUILD_TYPE=Release \
    -DOCUDU_GPU_CHANNEL_ENABLE_CUDA=ON \
    -DCMAKE_CUDA_COMPILER="${workspace}/tools/cuda-12.8.1/bin/nvcc" \
    -DOCUDU_GPU_CHANNEL_CUDA_ARCHITECTURES=120 >/dev/null
  cmake --build "${cuda_build}" -j"$(nproc)" --target ocudu-gpu-channel-bench >/dev/null
fi

# shellcheck source=/dev/null
source "${workspace}/tools/env.sh"
export PATH="${workspace}/tools/cuda-12.8.1/bin:${PATH}"

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
out_dir="${results_root}/perf-fanin/${timestamp}"
mkdir -p "${out_dir}"
yaml_dir="${out_dir}/yaml"
mkdir -p "${yaml_dir}"
echo "out_dir=${out_dir}"

# ---- Config list (mode, M, N, label) ----
# Set 1: 1-to-N for N in {1,2,4,8,16,32,64}
# Set 2 sym: M=N for M in {2,4,8,16}
# Set 2 many-N (cells few, users many): (2,4),(2,8),(2,16),(4,8),(4,16)
# Set 2 many-M (cells many, users few): (4,2),(8,2),(16,2),(8,4),(16,4)

CONFIGS=(
  "one-to-n 1 1 one-to-n_N1"
  "one-to-n 1 2 one-to-n_N2"
  "one-to-n 1 4 one-to-n_N4"
  "one-to-n 1 8 one-to-n_N8"
  "one-to-n 1 16 one-to-n_N16"
  "one-to-n 1 32 one-to-n_N32"
  "one-to-n 1 64 one-to-n_N64"
  "m-to-n 2 2 m-to-n_M2_N2"
  "m-to-n 4 4 m-to-n_M4_N4"
  "m-to-n 8 8 m-to-n_M8_N8"
  "m-to-n 16 16 m-to-n_M16_N16"
  "m-to-n 2 4 m-to-n_M2_N4"
  "m-to-n 2 8 m-to-n_M2_N8"
  "m-to-n 2 16 m-to-n_M2_N16"
  "m-to-n 4 8 m-to-n_M4_N8"
  "m-to-n 4 16 m-to-n_M4_N16"
  "m-to-n 4 2 m-to-n_M4_N2"
  "m-to-n 8 2 m-to-n_M8_N2"
  "m-to-n 16 2 m-to-n_M16_N2"
  "m-to-n 8 4 m-to-n_M8_N4"
  "m-to-n 16 4 m-to-n_M16_N4"
)

# ---- Per-config: bench + nvidia-smi snapshot during ----
sweep_json="${out_dir}/sweep.json"
: > "${sweep_json}"

# Idle baseline once
idle_mem_used="$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits | head -1)"
echo "idle_gpu_mem_MiB=${idle_mem_used}" > "${out_dir}/idle-baseline.txt"

for cfg in "${CONFIGS[@]}"; do
  read -r mode M N label <<< "${cfg}"
  yaml="${yaml_dir}/${label}.yaml"
  csv="${out_dir}/${label}.csv"
  gpu_snap="${out_dir}/${label}.gpu.csv"
  nsys_rep="${out_dir}/${label}.nsys-rep"

  # Generate YAML
  if [[ "${mode}" == "one-to-n" ]]; then
    python3 "${project_root}/scripts/gen_topology.py" one-to-n "${N}" "${yaml}"
  else
    python3 "${project_root}/scripts/gen_topology.py" m-to-n "${M}" "${N}" "${yaml}"
  fi

  echo "== [bench] ${label} =="
  # Bench in background so we can snapshot nvidia-smi mid-flight
  "${cuda_build}/ocudu-gpu-channel-bench" --config "${yaml}" --duration "${duration_seconds}s" --scs-khz 30 \
    > "${csv}" 2>"${csv}.stderr" &
  bench_pid=$!
  sleep 1.5  # warmup so PCIe link is at full speed
  nvidia-smi --query-gpu=memory.used,pcie.link.gen.current,pcie.link.width.current,utilization.gpu,utilization.memory,clocks.current.graphics,clocks.current.memory,power.draw \
    --format=csv,noheader,nounits > "${gpu_snap}"
  wait "${bench_pid}" || echo "    bench FAILED for ${label}"

  # Brief nsys trace: another short bench run so we capture kernel + memcpy stats
  nsys profile --trace=cuda --duration=2 --stats=false \
    --output="${out_dir}/${label}-nsys" --force-overwrite=true \
    "${cuda_build}/ocudu-gpu-channel-bench" --config "${yaml}" --duration 3s --scs-khz 30 \
    > "${out_dir}/${label}.nsys.log" 2>&1 || echo "    nsys FAILED for ${label}"
  nsys stats --report cuda_gpu_kern_sum --report cuda_gpu_mem_time_sum --report cuda_gpu_mem_size_sum \
    "${out_dir}/${label}-nsys.nsys-rep" > "${out_dir}/${label}.nsys-stats.txt" 2>/dev/null || true

  # Aggregate one JSON line per config
  python3 - "${label}" "${mode}" "${M}" "${N}" "${csv}" "${gpu_snap}" "${out_dir}/${label}.nsys-stats.txt" "${idle_mem_used}" <<'PY' >> "${sweep_json}"
import csv, json, os, re, sys
label, mode, M, N, bench_csv, gpu_snap, nsys_stats, idle_mem_used = sys.argv[1:9]
rec = {"label": label, "mode": mode, "M": int(M), "N": int(N), "metrics": {}}

# parse bench CSV
try:
    with open(bench_csv) as fh:
        for row in csv.reader(fh):
            if len(row) >= 9 and row[0] in ("model_mix_latency","h2d_us","kernel_us","d2h_us","gpu_process_us"):
                rec["metrics"][row[0]] = {
                    "p50_us": float(row[2]), "p95_us": float(row[3]),
                    "p99_us": float(row[4]), "max_us": float(row[6]),
                    "gate": row[8],
                }
            elif row and row[0] == "backend":
                rec["backend"] = row[1]
            elif row and row[0] == "iterations":
                rec["iterations"] = int(row[1])
except Exception as e:
    rec["bench_error"] = str(e)

# parse gpu snapshot
try:
    parts = open(gpu_snap).read().strip().split(",")
    rec["gpu_snap"] = {
        "mem_used_MiB": int(parts[0].strip()),
        "mem_delta_MiB": int(parts[0].strip()) - int(idle_mem_used),
        "pcie_gen": parts[1].strip(),
        "pcie_width": parts[2].strip(),
        "util_gpu_pct": int(parts[3].strip()),
        "util_mem_pct": int(parts[4].strip()),
        "graphics_clock_MHz": int(parts[5].strip()),
        "mem_clock_MHz": int(parts[6].strip()),
        "power_W": float(parts[7].strip()),
    }
except Exception as e:
    rec["gpu_snap_error"] = str(e)

# parse nsys stats: kernel + memcpy avg/median time
try:
    text = open(nsys_stats).read()
    m = re.search(r"superpose_kernel.*?\n", text)
    # cuda_gpu_kern_sum columns: Time(%) TotalTime Instances Avg Med Min Max StdDev Name
    krow = None
    for line in text.splitlines():
        if "superpose_kernel" in line:
            krow = line.split()
            break
    if krow:
        rec["kernel_ns"] = {
            "instances": int(krow[2].replace(",","")),
            "avg_ns": int(krow[3].replace(",","")),
            "med_ns": int(krow[4].replace(",","")),
            "max_ns": int(krow[6].replace(",","")),
        }
    # parse cuda_gpu_mem_time_sum: H2D / D2H total time
    h2d_total_ns, h2d_count = None, None
    d2h_total_ns, d2h_count = None, None
    h2d_total_MB = None
    d2h_total_MB = None
    in_time = False
    in_size = False
    for line in text.splitlines():
        if "cuda_gpu_mem_time_sum" in line: in_time, in_size = True, False
        elif "cuda_gpu_mem_size_sum" in line: in_size, in_time = True, False
        if in_time and "[CUDA memcpy Host-to-Device]" in line:
            cols = line.split()
            h2d_total_ns = int(cols[1].replace(",",""))
            h2d_count = int(cols[2].replace(",",""))
        if in_time and "[CUDA memcpy Device-to-Host]" in line:
            cols = line.split()
            d2h_total_ns = int(cols[1].replace(",",""))
            d2h_count = int(cols[2].replace(",",""))
        if in_size and "[CUDA memcpy Host-to-Device]" in line:
            cols = line.split()
            h2d_total_MB = float(cols[0].replace(",",""))
        if in_size and "[CUDA memcpy Device-to-Host]" in line:
            cols = line.split()
            d2h_total_MB = float(cols[0].replace(",",""))
    if h2d_total_ns and h2d_total_MB:
        rec["pcie_h2d"] = {
            "total_ns_on_bus": h2d_total_ns,
            "count": h2d_count,
            "total_MB": h2d_total_MB,
            "sustained_GBps": h2d_total_MB / (h2d_total_ns / 1e9 * 1024),
        }
    if d2h_total_ns and d2h_total_MB:
        rec["pcie_d2h"] = {
            "total_ns_on_bus": d2h_total_ns,
            "count": d2h_count,
            "total_MB": d2h_total_MB,
            "sustained_GBps": d2h_total_MB / (d2h_total_ns / 1e9 * 1024),
        }
except Exception as e:
    rec["nsys_error"] = str(e)

print(json.dumps(rec))
PY
  rm -f "${out_dir}/${label}-nsys.sqlite"   # keep .nsys-rep, drop the giant sqlite
done

echo "== sweep done =="
echo "out_dir=${out_dir}"
echo "configs=$(wc -l < "${sweep_json}")"
REMOTE
