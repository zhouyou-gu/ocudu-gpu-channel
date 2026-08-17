#!/usr/bin/env bash
# Deep resource profile of one config (default: stress-16): PCIe throughput,
# host + device memory, GPU/SM utilisation, and -- if accessible -- ncu
# kernel-level metrics. Output is one results directory on the remote
# containing logs, dmon traces, and a brief summary text.
#
# Usage: scripts/remote/perf-deep-profile.sh [duration_seconds]
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${script_dir}/common.sh"

duration_seconds="${1:-15}"
target_yaml="${PERF_DEEP_TARGET:-examples/topology.stress-16-edge.cuda.yaml}"

case "${REMOTE_PROJECT_ROOT}" in
  "~/"*) remote_dest="${REMOTE_PROJECT_ROOT#\~/}" ;;
  *) remote_dest="${REMOTE_PROJECT_ROOT}" ;;
esac

echo "== syncing working tree =="
rsync -az --delete \
  --exclude '.git' --exclude 'build*' --exclude '.config' \
  -e "ssh -i ${REMOTE_SSH_KEY} -o BatchMode=yes -o ConnectTimeout=8" \
  "${repo_root}/" "${REMOTE_USER}@${REMOTE_HOST}:${remote_dest}/"

remote_sh bash -s -- "${REMOTE_WORKSPACE}" "${REMOTE_PROJECT_ROOT}" "${REMOTE_BUILDS_ROOT}" "${REMOTE_RESULTS_ROOT}" "${duration_seconds}" "${target_yaml}" <<'REMOTE'
set -uo pipefail

workspace="$1"
project_root="$2"
builds_root="$3"
results_root="$4"
duration_seconds="$5"
target_yaml="$6"

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

if [[ ! -x "${cuda_build}/ocudu-gpu-channel-bench" ]]; then
  echo "missing ${cuda_build}/ocudu-gpu-channel-bench; run scripts/remote/perf-sweep.sh or gpu-test-sequence.sh first" >&2
  exit 1
fi

# shellcheck source=/dev/null
source "${workspace}/tools/env.sh"
export PATH="${workspace}/tools/cuda-12.8.1/bin:${PATH}"

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
out_dir="${results_root}/perf-deep/${timestamp}"
mkdir -p "${out_dir}"
echo "out_dir=${out_dir}"

# ---- Idle snapshot ----
nvidia-smi --query-gpu=name,memory.total,memory.used,memory.free,pcie.link.gen.current,pcie.link.width.current,utilization.gpu,utilization.memory,clocks.current.graphics,clocks.current.memory,power.draw \
  --format=csv,noheader > "${out_dir}/gpu-idle.csv"
free -m > "${out_dir}/host-mem-idle.txt"
echo "    idle snapshot OK"

# ---- nvidia-smi dmon background (1 Hz) ----
nvidia-smi dmon -s pucvmt -i 0 -d 1 > "${out_dir}/dmon.log" 2>&1 &
dmon_pid=$!

# ---- Bench in background ----
cd "${project_root}"
"${cuda_build}/ocudu-gpu-channel-bench" --config "${target_yaml}" --duration "${duration_seconds}s" --scs-khz 30 \
  > "${out_dir}/bench.csv" 2>&1 &
bench_pid=$!

# Brief warmup so the GPU/PCIe link is at full speed before we sample
sleep 2

# ---- Active sample: PCIe state under load + host RSS ----
nvidia-smi --query-gpu=memory.used,pcie.link.gen.current,pcie.link.width.current,utilization.gpu,utilization.memory,clocks.current.graphics,clocks.current.memory,power.draw \
  --format=csv,noheader > "${out_dir}/gpu-active.csv"
grep -E "VmRSS|VmHWM|VmSize" "/proc/${bench_pid}/status" > "${out_dir}/host-rss-active.txt" 2>/dev/null || true

wait "${bench_pid}"
kill "${dmon_pid}" 2>/dev/null || true
wait "${dmon_pid}" 2>/dev/null || true

# ---- Post-run snapshot ----
nvidia-smi --query-gpu=memory.used --format=csv,noheader > "${out_dir}/gpu-postrun.csv"
echo "    bench + dmon trace OK"

# ---- nsys trace: short profile pass ----
echo "== nsys profile (~3s) =="
nsys profile \
  --trace=cuda \
  --duration=3 \
  --stats=true \
  --output="${out_dir}/nsys-stress16" \
  --force-overwrite=true \
  "${cuda_build}/ocudu-gpu-channel-bench" \
  --config "${target_yaml}" \
  --duration 5s --scs-khz 30 > "${out_dir}/nsys.log" 2>&1 || {
    echo "    nsys profile FAILED (see ${out_dir}/nsys.log)"
  }
nsys stats "${out_dir}/nsys-stress16.nsys-rep" > "${out_dir}/nsys-stats.txt" 2>&1 || true
echo "    nsys stats OK"

# ---- ncu kernel deep-dive (best effort; needs profiling perms) ----
echo "== ncu launch-stats + occupancy (best effort) =="
ncu --target-processes all \
  --launch-skip 100 --launch-count 5 \
  --section LaunchStats --section Occupancy \
  --csv \
  "${cuda_build}/ocudu-gpu-channel-bench" \
  --config "${target_yaml}" \
  --duration 4s --scs-khz 30 > "${out_dir}/ncu.csv" 2>"${out_dir}/ncu.log" || {
    echo "    ncu FAILED (likely needs NVreg_RestrictProfilingToAdminUsers=0 or root) -- see ncu.log"
  }

# ---- Summary ----
python3 - "${out_dir}" "${duration_seconds}" <<'PY'
import csv, json, sys, os, statistics, re
out, duration = sys.argv[1], int(sys.argv[2])

def grab(path):
    try: return open(os.path.join(out, path)).read().strip()
    except FileNotFoundError: return ""

idle = grab("gpu-idle.csv").split(",")
active = grab("gpu-active.csv").split(",")
postrun = grab("gpu-postrun.csv").split(",")

# dmon: columns (after header) include sm util %, mem util %, ...
# parse for sm util range while bench was active
sm_util, mem_util, mem_used = [], [], []
for line in open(os.path.join(out, "dmon.log")):
    if line.startswith('#') or not line.strip(): continue
    parts = line.split()
    # dmon -s pucvmt: gpu pwr gtemp mtemp sm mem enc dec mclk pclk pviol tviol
    try:
        sm_util.append(int(parts[4]))
        mem_util.append(int(parts[5]))
    except (IndexError, ValueError):
        pass

# parse bench CSV
bench = {}
try:
    with open(os.path.join(out, "bench.csv")) as fh:
        reader = csv.reader(fh)
        for row in reader:
            if len(row) >= 5 and row[0] in ("model_mix_latency","h2d_us","kernel_us","d2h_us","gpu_process_us"):
                bench[row[0]] = {"p50": float(row[2]), "p99": float(row[4])}
except Exception: pass

# parse host rss
rss_kb = None
for line in open(os.path.join(out, "host-rss-active.txt")):
    m = re.match(r"VmRSS:\s+(\d+)\s+kB", line)
    if m: rss_kb = int(m.group(1))

# Compute achieved PCIe BW from bench h2d_us + known staged size
# stress-16 sink has 8 incoming edges * 23040 samples * 8 bytes = 1.40 MiB per H2D
N = 8
batch = 23040
h2d_bytes = N * batch * 8
d2h_bytes = batch * 8
# bits / (us * 1e3) = bits / ns = Gbits/s
achieved_h2d_gbps = (h2d_bytes * 8) / (bench.get("h2d_us", {}).get("p99", 1) * 1e3) if bench.get("h2d_us") else None
achieved_d2h_gbps = (d2h_bytes * 8) / (bench.get("d2h_us", {}).get("p99", 1) * 1e3) if bench.get("d2h_us") else None

summary = {
  "config_yaml": os.path.basename(grab("../bench.csv").split('=')[-1] if False else ""),
  "duration_s": duration,
  "gpu": {
    "name": idle[0] if idle else "",
    "memory_total_MiB": int(idle[1].split()[0]) if len(idle) > 1 else None,
    "memory_used_idle_MiB": int(idle[2].split()[0]) if len(idle) > 2 else None,
    "memory_used_active_MiB": int(active[0].split()[0]) if active else None,
    "memory_used_postrun_MiB": int(postrun[0].split()[0]) if postrun and postrun[0] else None,
    "pcie_gen_idle": idle[4].strip() if len(idle) > 4 else None,
    "pcie_width_idle": idle[5].strip() if len(idle) > 5 else None,
    "pcie_gen_active": active[1].strip() if len(active) > 1 else None,
    "pcie_width_active": active[2].strip() if len(active) > 2 else None,
    "util_gpu_active_pct": active[3].strip() if len(active) > 3 else None,
    "util_mem_active_pct": active[4].strip() if len(active) > 4 else None,
    "graphics_clock_active_MHz": active[5].strip() if len(active) > 5 else None,
    "mem_clock_active_MHz": active[6].strip() if len(active) > 6 else None,
    "power_active": active[7].strip() if len(active) > 7 else None,
  },
  "dmon_during_bench": {
    "samples": len(sm_util),
    "sm_util_pct_median": statistics.median(sm_util) if sm_util else None,
    "sm_util_pct_p95": (statistics.quantiles(sm_util, n=20)[-1] if len(sm_util) >= 20 else max(sm_util) if sm_util else None),
    "mem_util_pct_median": statistics.median(mem_util) if mem_util else None,
  },
  "host_memory_active": {
    "rss_kb": rss_kb,
    "rss_MiB": rss_kb // 1024 if rss_kb else None,
  },
  "pcie_achieved": {
    "h2d_p99_us": bench.get("h2d_us", {}).get("p99"),
    "d2h_p99_us": bench.get("d2h_us", {}).get("p99"),
    "h2d_staged_bytes_per_call": h2d_bytes,
    "d2h_bytes_per_call": d2h_bytes,
    "h2d_achieved_Gbps_p99": achieved_h2d_gbps,
    "d2h_achieved_Gbps_p99": achieved_d2h_gbps,
    "pcie_5_x16_theoretical_Gbps": 504,
  },
  "bench_p99_us": {k: v["p99"] for k, v in bench.items()},
}
with open(os.path.join(out, "summary.json"), "w") as fh:
    json.dump(summary, fh, indent=2)
print(json.dumps(summary, indent=2))
PY
echo
echo "out_dir=${out_dir}"
REMOTE
