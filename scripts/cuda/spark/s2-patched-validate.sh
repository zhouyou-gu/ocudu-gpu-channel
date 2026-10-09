#!/usr/bin/env bash
# S2 -- the C1 patch (d2579af2) on the DGX Spark, validated the way C1 was on the 5090.
#
# The Spark counterpart of c1-build.sh + c1-validate.sh + c1-negative-controls.sh:
# same pinned source, same patch (hash-checked), same 14 PHY tests (12 vendor +
# the two blind-spot regressions the patch adds), same OFH suite, same printed-
# verdict grading, same NC-D1..D4 controls. NC-D5 needs a live gNB and is left to
# the live stages. The patched tree is a separate checkout, so the unpatched
# vendor tree used by S1 stays clean.
#
# Run inside ocudu-minwoo on the Spark:
#   bash s2-patched-validate.sh
set -uo pipefail

WG_COMMIT=5830c9cb7813393b5d518dd5ee2b6641071be4ad
PATCH_SHA256=d2579af289c79377e50b55811d563b0cba2587b42f9409bd1001a1e6d934da0a
CUDA_ARCH="${CUDA_ARCH:-121}"
MCPU="${MCPU:-neoverse-v2}"   # WG doc's DGX Spark setting (see SPARK_MILESTONES.md S1)
jobs="${S2_BUILD_JOBS:-16}"

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/../../.." && pwd)"
root="${PLATFORM_ROOT:-/workspace/ocudu-spark}"
vendor_src="${root}/src/ocudu-cuda"
src="${root}/src/ocudu-cuda-c1"
build="${root}/builds/s2-cuda-patched-sm${CUDA_ARCH}"
patch_file="${repo_root}/integrations/ocudu/patches/c1-ocudu-cuda-discrete-and-config.patch"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
out="${root}/results/s2-${stamp}"
export PATH="/usr/local/cuda/bin:${PATH}"
mkdir -p "$out"
exec > >(tee -a "$out/s2.log") 2>&1

TESTS='^(ofdm_demodulator_cuda_test|ofdm_prach_demodulator_cuda_test|pdxch_baseband_modulator_cuda_test|ldpc_encoder_gpu_cpu_test|ldpc_decoder_gpu_cpu_test|prach_detector_cuda_test|pusch_gpu_cpu_comparison_test|pusch_gpu_cpu_cfo_interpolation_test|pusch_gpu_cpu_sync_sinr_test|pdsch_gpu_e2e_test|pusch_e2e_pipeline_test|pusch_resident_dematch_scramble_test|srs_estimator_gpu_latency_baseline_4x4_n4|srs_estimator_gpu_sensitivity_baseline_4x4_n4)$'
OFH='^ofh_iq_compression_cuda/'

die() { echo "FATAL: $*"; exit 1; }
[[ "$(sha256sum "$patch_file" | cut -d' ' -f1)" == "$PATCH_SHA256" ]] || die "patch hash mismatch"
echo "patch_sha256=${PATCH_SHA256}"

# Patched checkout: a clone of the clean vendor tree at the pin, patch applied.
if [[ ! -d "$src/.git" ]]; then
  git clone -q "$vendor_src" "$src" || die "clone"
fi
restore() { git -C "$src" checkout -q -- . && git -C "$src" apply "$patch_file"; }
git -C "$src" checkout -q "$WG_COMMIT" || die "checkout"
restore || die "patch does not apply"
echo "s2_source=${WG_COMMIT} patched=yes diff_sha256=$(git -C "$src" diff HEAD | sha256sum | cut -c1-16)"

bash "${repo_root}/scripts/cuda/jetson/probe-platform.sh" "$out" >/dev/null
nvidia-smi --query-compute-apps=pid,name --format=csv,noheader > "$out/gpu-apps-at-start.txt"
echo "other_gpu_processes_at_start=$(wc -l < "$out/gpu-apps-at-start.txt")"

echo "=== configure $(date -u +%T) ==="
cmake -S "$src" -B "$build" -G Ninja \
  -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=ON \
  -DENABLE_CUDA=ON "-DCMAKE_CUDA_ARCHITECTURES=${CUDA_ARCH}" "-DMCPU=${MCPU}" \
  -DCMAKE_POSITION_INDEPENDENT_CODE=ON \
  -DENABLE_UHD=OFF -DENABLE_SIDEKIQ=OFF -DENABLE_MKL=OFF -DENABLE_FFTZ=OFF \
  -DENABLE_ARMPL=OFF -DENABLE_DPDK=OFF -DENABLE_LIBNUMA=OFF \
  -DENABLE_PLUGINS=OFF -DENABLE_BACKWARD=OFF -DENABLE_ZEROMQ=ON \
  -DENABLE_FFTW=ON -DENABLE_EXPORT=ON > "$out/configure.log" 2>&1 || die "configure (see configure.log)"
grep -E '^(CMAKE_CUDA_ARCHITECTURES|ENABLE_CUDA|CMAKE_BUILD_TYPE|MCPU)' "$build/CMakeCache.txt" | tee "$out/cache.txt"

targets=(ocudu_phy_cuda gnb
  ofdm_demodulator_cuda_test ofdm_prach_demodulator_cuda_test
  pdxch_baseband_modulator_cuda_test ldpc_encoder_gpu_cpu_test
  ldpc_decoder_gpu_cpu_test prach_detector_cuda_test
  pusch_gpu_cpu_comparison_test pdsch_gpu_e2e_test pusch_e2e_pipeline_test
  pusch_resident_dematch_scramble_test srs_estimator_gpu_latency_benchmark
  srs_estimator_gpu_sensitivity_sweep ofh_iq_compression_cuda_test)
echo "=== build $(date -u +%T) ==="
cmake --build "$build" --target "${targets[@]}" -j"$jobs" > "$out/build.log" 2>&1 || die "build (see build.log)"
echo "=== build done $(date -u +%T) ==="
sha256sum "$build/apps/gnb/gnb" | tee "$out/gnb-sha256.txt"

echo "=== PHY validation (14) $(date -u +%T) ==="
ctest --test-dir "$build" -N -R "$TESTS" | grep -c "Test *#" | sed 's/^/phy_tests_registered=/'
ctest --test-dir "$build" --output-on-failure --no-tests=error --timeout 3600 -j1 \
  -R "$TESTS" > "$out/phy-validation.log" 2>&1
echo "phy_ctest_exit=$?"
cp "$build/Testing/Temporary/LastTest.log" "$out/phy-LastTest.log"
grep -E '^ *[0-9]+/[0-9]+ Test|tests passed|Total Test time' "$out/phy-validation.log"

echo "=== OFH validation $(date -u +%T) ==="
ctest --test-dir "$build" --output-on-failure --timeout 600 -j1 -R "$OFH" > "$out/ofh-validation.log" 2>&1
echo "ofh_ctest_exit=$?"
grep -E 'tests passed' "$out/ofh-validation.log"

python3 "${repo_root}/scripts/cuda/verify-phy-log.py" "$out/phy-LastTest.log" | tee "$out/phy-log-verdict.txt"
for marker in 'CFO interpolation regression: PASS' 'Synchronous SINR regression: PASS'; do
  grep -q "$marker" "$out/phy-LastTest.log" && echo "marker_ok=\"$marker\"" || echo "marker_MISSING=\"$marker\""
done

# ---- negative controls: each restores one defect and must make its test FAIL.
rebuild() { cmake --build "$build" --target "$@" -j"$jobs" >/dev/null 2>&1; }
record() { # name test exit log mechanism_regex
  local mech="absent"
  grep -qE "$5" "$4" && mech="present"
  local verdict='*** CONTROL BROKEN ***'
  if [[ "$3" -ne 0 && "$mech" == present ]]; then verdict='CONTROL OK'; fi
  if [[ "$3" -ne 0 && "$mech" != present ]]; then verdict='*** failed, but not by the restored mechanism ***'; fi
  printf '%-26s test=%-44s exit=%-3s mechanism=%-7s %s\n' "$1" "$2" "$3" "$mech" "$verdict" | tee -a "$out/nc-summary.txt"
}

echo "=== NC-D1: SRS host-grid staging removed $(date -u +%T) ==="
restore; git -C "$src" checkout -q -- lib/phy/upper/signal_processors/srs/srs_estimator_cuda_impl.cpp
rebuild srs_estimator_gpu_latency_benchmark srs_estimator_gpu_sensitivity_sweep
ctest --test-dir "$build" -j1 --timeout 600 --output-on-failure -R '^srs_estimator_gpu_latency_baseline_4x4_n4$' > "$out/nc-d1.log" 2>&1
record "NC-D1 srs-host-grid" srs_estimator_gpu_latency_baseline_4x4_n4 "$?" "$out/nc-d1.log" "get_nof_rx_ports\(\) == candidate"

echo "=== NC-D2: OFH host-copy fallback removed $(date -u +%T) ==="
restore; git -C "$src" checkout -q -- lib/ofh/compression/iq_compression_cuda.cpp
rebuild pdsch_gpu_e2e_test
ctest --test-dir "$build" -j1 --timeout 600 --output-on-failure -R '^pdsch_gpu_e2e_test$' > "$out/nc-d2.log" 2>&1
record "NC-D2 ofh-host-copy" pdsch_gpu_e2e_test "$?" "$out/nc-d2.log" "compress_device_symbol|nof_mismatches"  # (a) live read refused, no fallback -- 5090; (b) live read hold never released -> next host read refused -- GB10

echo "=== NC-D3: configured time interpolation dropped $(date -u +%T) ==="
restore
python3 - "$src" <<'PY'
import sys, pathlib
p = pathlib.Path(sys.argv[1]) / 'lib/phy/upper/channel_processors/pusch/pusch_demodulator_gpu_impl.cpp'
s = p.read_text()
line = '  time_interp_mode_ = td_interpolation_strategy == port_channel_estimator_td_interpolation_strategy::interpolate ? 1 : 0;'
assert s.count(line) == 1, 'NC-D3 anchor not found'
p.write_text(s.replace(line, '  (void)td_interpolation_strategy;  // NEGATIVE CONTROL: vendor behaviour, always average'))
PY
rebuild pusch_gpu_cpu_comparison_test
ctest --test-dir "$build" -j1 --timeout 900 --output-on-failure -R '^pusch_gpu_cpu_cfo_interpolation_test$' > "$out/nc-d3.log" 2>&1
record "NC-D3 pusch-interpolation" pusch_gpu_cpu_cfo_interpolation_test "$?" "$out/nc-d3.log" "CFO regression CRC or payload mismatch"

echo "=== NC-D4: EVM substituted for sync-path SINR $(date -u +%T) ==="
restore
python3 - "$src" <<'PY'
import sys, pathlib
p = pathlib.Path(sys.argv[1]) / 'lib/phy/upper/channel_processors/pusch/pusch_demodulator_gpu_impl.cpp'
s = p.read_text()
fixed = '''  // Use the same post-equalization noise-variance SINR as the CPU and
  // deferred resident path. EVM is a separate metric; substituting it here
  // inflates scheduler SINR for synchronous/UCI-bearing transmissions.
  if (std::isfinite(gpu_sinr_db)) {'''
vendor = '''  // NEGATIVE CONTROL: vendor behaviour restored.
  if (std::isfinite(gpu_evm) && gpu_evm > 0.0f) {
    stats.sinr_dB.emplace(-20.0f * std::log10(gpu_evm));
  } else if (std::isfinite(gpu_sinr_db)) {'''
assert s.count(fixed) == 1, 'NC-D4 anchor not found'
p.write_text(s.replace(fixed, vendor))
PY
rebuild pusch_gpu_cpu_comparison_test
ctest --test-dir "$build" -j1 --timeout 900 --output-on-failure -R '^pusch_gpu_cpu_sync_sinr_test$' > "$out/nc-d4.log" 2>&1
record "NC-D4 sync-sinr-evm" pusch_gpu_cpu_sync_sinr_test "$?" "$out/nc-d4.log" "Synchronous GPU post-equalization SINR disagrees with CPU"

echo "=== restore the patch and rebuild $(date -u +%T) ==="
restore
rebuild "${targets[@]}"
echo "restored_diff_sha256=$(git -C "$src" diff HEAD | sha256sum | cut -c1-16)"
echo "s2_evidence=${out}"
echo S2_DONE
