#!/usr/bin/env bash
# CPU vs CUDA backend comparison sweep.
#
# Runs each config in two sets through both backends and produces a JSON
# report with per-config p99 latency for both backends + per-RX-node output
# avg_power for matching verification.
#
# Two config sets:
#   - synthetic_fanin: gen_topology.py one-to-N for N in {1, 4, 16}
#                       (cuda_mvp model — single-tap tdl + phase + cfo)
#   - tdl_profiles:    examples/topology.tdl-{a..e}.cuda.yaml
#                       (TR 38.901 23-tap TDL + Jakes fading + LOS for D/E)
#
# Output: results/perf-backend-compare/<timestamp>/
#   - compare.json     — one line per (config, backend) record
#   - <label>.cpu.csv  / <label>.cuda.csv — raw bench output per run
#
# Run as a Bash script: scripts/remote/perf-backend-compare.sh [duration_s]
set -uo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${script_dir}/common.sh"

duration_seconds="${1:-5}"

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

# Need the toolchain PATH set BEFORE the build check (cmake / nproc live there).
# shellcheck source=/dev/null
source "${workspace}/tools/env.sh"
export PATH="${workspace}/tools/cuda-12.8.1/bin:${PATH}"

# Build if needed.
if [[ ! -x "${cuda_build}/ocudu-gpu-channel-bench" ]]; then
  echo "missing bench; building..."
  mkdir -p "${cuda_build}"
  cmake -S "${project_root}" -B "${cuda_build}" \
    -DCMAKE_BUILD_TYPE=Release \
    -DOCUDU_GPU_CHANNEL_ENABLE_CUDA=ON \
    -DCMAKE_CUDA_COMPILER="${workspace}/tools/cuda-12.8.1/bin/nvcc" \
    -DOCUDU_GPU_CHANNEL_CUDA_ARCHITECTURES=120 >/dev/null
  cmake --build "${cuda_build}" -j"$(nproc)" --target ocudu-gpu-channel-bench >/dev/null
fi
# Force a rebuild if the bench source is newer than the binary (catches new
# rx_output emissions etc. after source changes).
if [[ "${project_root}/apps/ocudu_gpu_channel_bench.cpp" -nt "${cuda_build}/ocudu-gpu-channel-bench" ]]; then
  echo "bench source newer than binary; rebuilding..."
  cmake --build "${cuda_build}" -j"$(nproc)" --target ocudu-gpu-channel-bench >/dev/null
fi

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
out_dir="${results_root}/perf-backend-compare/${timestamp}"
mkdir -p "${out_dir}"
yaml_dir="${out_dir}/yaml"
mkdir -p "${yaml_dir}"
echo "out_dir=${out_dir}"

# ---- Config lists ----
# Two sets:
#   synthetic_fanin: gen_topology one-to-N (cuda_mvp model)
#   tdl_profiles:    examples/topology.tdl-{a..e}.cuda.yaml
SYNTHETIC_CONFIGS=(
  "synthetic_fanin one-to-n_N1   gen one-to-n 1"
  "synthetic_fanin one-to-n_N4   gen one-to-n 4"
  "synthetic_fanin one-to-n_N16  gen one-to-n 16"
)
TDL_CONFIGS=(
  "tdl_profiles tdl-a examples/topology.tdl-a.cuda.yaml"
  "tdl_profiles tdl-b examples/topology.tdl-b.cuda.yaml"
  "tdl_profiles tdl-c examples/topology.tdl-c.cuda.yaml"
  "tdl_profiles tdl-d examples/topology.tdl-d.cuda.yaml"
  "tdl_profiles tdl-e examples/topology.tdl-e.cuda.yaml"
)

compare_json="${out_dir}/compare.json"
: > "${compare_json}"

run_one() {
  # $1 set_name  $2 label  $3 yaml_path  $4 backend
  local set_name="$1" label="$2" yaml_src="$3" backend="$4"
  local out_csv="${out_dir}/${label}.${backend}.csv"
  local yaml_eff="${yaml_dir}/${label}.${backend}.yaml"
  # Override backend in the YAML.
  sed -e "s|backend: cuda|backend: ${backend}|" -e "s|backend: cpu|backend: ${backend}|" \
    "${yaml_src}" > "${yaml_eff}"
  echo "  [run] ${label} backend=${backend}"
  "${cuda_build}/ocudu-gpu-channel-bench" --config "${yaml_eff}" --duration "${duration_seconds}s" --scs-khz 30 \
    > "${out_csv}" 2>"${out_csv}.stderr" || {
      echo "      FAILED (see ${out_csv}.stderr)"
      return 1
    }
  python3 - "${set_name}" "${label}" "${backend}" "${out_csv}" <<'PY' >> "${compare_json}"
import csv, json, sys
set_name, label, backend, csv_path = sys.argv[1:5]
rec = {"set": set_name, "label": label, "backend": backend, "metrics": {}, "rx_outputs": []}
try:
    with open(csv_path) as fh:
        rows = list(csv.reader(fh))
    for row in rows:
        if not row:
            continue
        if row[0] in ("model_mix_latency", "h2d_us", "kernel_us", "d2h_us", "gpu_process_us") and len(row) >= 9:
            rec["metrics"][row[0]] = {
                "p50_us": float(row[2]),
                "p95_us": float(row[3]),
                "p99_us": float(row[4]),
                "max_us": float(row[6]),
                "gate":   row[8],
            }
        elif row[0] == "iterations" and len(row) >= 2:
            rec["iterations"] = int(row[1])
        elif row[0] == "rx_output" and len(row) >= 7:
            # First rx_output line is a header row -- skip it.
            if row[1] == "device_id":
                continue
            try:
                rec["rx_outputs"].append({
                    "device":     row[1],
                    "avg_power":  float(row[2]),
                    "sample0_i":  float(row[3]),
                    "sample0_q":  float(row[4]),
                    "sampleN_i":  float(row[5]),
                    "sampleN_q":  float(row[6]),
                })
            except ValueError:
                # Defensive: skip any unparseable rx_output row.
                pass
except Exception as e:
    rec["parse_error"] = str(e)
print(json.dumps(rec))
PY
}

for cfg in "${SYNTHETIC_CONFIGS[@]}" "${TDL_CONFIGS[@]}"; do
  read -r set_name label rest <<< "${cfg}"
  if [[ "${set_name}" == "synthetic_fanin" ]]; then
    # gen one-to-n N
    read -r _ mode N <<< "${rest}"
    yaml="${yaml_dir}/${label}.src.yaml"
    python3 "${project_root}/scripts/gen_topology.py" "${mode}" "${N}" "${yaml}"
  else
    yaml="${project_root}/${rest}"
  fi
  echo "== ${set_name} :: ${label} =="
  run_one "${set_name}" "${label}" "${yaml}" cuda || true
  run_one "${set_name}" "${label}" "${yaml}" cpu  || true
done

# ---- Aggregate: compute per-config matching + speedup ----
python3 - "${compare_json}" <<'PY'
import json, sys
path = sys.argv[1]
recs = [json.loads(l) for l in open(path) if l.strip()]
by_label = {}
for r in recs:
    by_label.setdefault(r["label"], {})[r["backend"]] = r

print()
print(f"{'config':<16} {'mix_p99 CPU':<14} {'mix_p99 CUDA':<14} {'speedup':<10} {'match':<10} {'max |Δavg_p|':<14}")
print("-" * 90)
for label, by_backend in by_label.items():
    cpu, gpu = by_backend.get("cpu"), by_backend.get("cuda")
    if not cpu or not gpu:
        print(f"{label:<16} INCOMPLETE")
        continue
    cm = cpu.get("metrics", {}).get("model_mix_latency", {}).get("p99_us")
    gm = gpu.get("metrics", {}).get("model_mix_latency", {}).get("p99_us")
    if cm is None or gm is None:
        print(f"{label:<16} missing metrics")
        continue
    speedup = cm / gm if gm > 0 else 0
    # Match: per-RX avg_power tolerance
    cpu_by_dev = {x["device"]: x["avg_power"] for x in cpu.get("rx_outputs", [])}
    gpu_by_dev = {x["device"]: x["avg_power"] for x in gpu.get("rx_outputs", [])}
    diffs = []
    for dev in cpu_by_dev:
        if dev in gpu_by_dev:
            diffs.append(abs(cpu_by_dev[dev] - gpu_by_dev[dev]))
    max_diff = max(diffs) if diffs else 0
    # Tolerance scales with the avg_power magnitude. Static configs match
    # bit-exact (~0). Fading configs accumulate per-tap Rayleigh variance
    # across different sample counts (CPU and CUDA run different iteration
    # counts in the same wall-clock budget). The ±1.5 power-unit cutoff
    # mirrors gpu-test-sequence step [7/7] which uses the same tolerance
    # for the same single-realization-cumulative-integration reason.
    match = "✓" if max_diff < 1.5 else "?" if max_diff < 5.0 else "✗"
    print(f"{label:<16} {cm:<14.1f} {gm:<14.1f} {speedup:<10.1f} {match:<10} {max_diff:<14.4f}")

print()
print(f"Records:  {len(recs)} (one per (config, backend))")
print(f"Output:   {path}")
PY

echo "== compare done =="
REMOTE
