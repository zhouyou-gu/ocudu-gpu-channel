#!/usr/bin/env bash
# D8 -- GPU PUSCH SINR over-reported by ~2.4 dB on single-PRB allocations.
#
# Builds pin + C1 + the D8 change (s-d8-c1-gpu-1prb-sinr.patch, one combined
# diff against the pin) in its own checkout, runs the same 14 PHY + OFH suite
# as S2 (regression), then the offline SINR parity check that exposed D8:
# pusch_e2e_pipeline_test at 1 and 2 PRB, input SINR 5/10/20 dB, CPU vs GPU.
# The S2 build (C1 only) is measured the same way as the negative control.
#
#   bash s-d8-validate.sh
set -uo pipefail
WG_COMMIT=5830c9cb7813393b5d518dd5ee2b6641071be4ad
CUDA_ARCH="${CUDA_ARCH:-121}"; MCPU="${MCPU:-neoverse-v2}"; jobs="${S2_BUILD_JOBS:-16}"
repo="$(cd "$(dirname "$0")/../../.." && pwd)"
root="${PLATFORM_ROOT:-/workspace/ocudu-spark}"
src="$root/src/ocudu-cuda-d8"
build="$root/builds/d8-cuda-patched-sm${CUDA_ARCH}"
control="$root/builds/s2-cuda-patched-sm${CUDA_ARCH}"
patch="${D8_PATCH:-$repo/integrations/ocudu/patches/s-c1-d8-d9.patch}"
out="$root/results/d8-$(date -u +%Y%m%dT%H%M%SZ)"
export PATH="/usr/local/cuda/bin:$PATH"
mkdir -p "$out"; exec > >(tee -a "$out/d8.log") 2>&1
die() { echo "FATAL: $*"; exit 1; }
TESTS='^(ofdm_demodulator_cuda_test|ofdm_prach_demodulator_cuda_test|pdxch_baseband_modulator_cuda_test|ldpc_encoder_gpu_cpu_test|ldpc_decoder_gpu_cpu_test|prach_detector_cuda_test|pusch_gpu_cpu_comparison_test|pusch_gpu_cpu_cfo_interpolation_test|pusch_gpu_cpu_sync_sinr_test|pdsch_gpu_e2e_test|pusch_e2e_pipeline_test|pusch_resident_dematch_scramble_test|srs_estimator_gpu_latency_baseline_4x4_n4|srs_estimator_gpu_sensitivity_baseline_4x4_n4)$'

[[ -d "$src/.git" ]] || git clone -q "$root/src/ocudu-cuda" "$src" || die clone
git -C "$src" checkout -q "$WG_COMMIT" && git -C "$src" checkout -q -- . && git -C "$src" apply "$patch" || die "patch does not apply"
echo "patch_sha256=$(sha256sum "$patch" | cut -d' ' -f1) diff_matches=$([[ "$(git -C "$src" -c core.abbrev=7 diff --binary HEAD)" == "$(cat "$patch")" ]] && echo yes || echo NO)"
echo "other_gpu_processes_at_start=$(nvidia-smi --query-compute-apps=pid --format=csv,noheader | wc -l)"

echo "=== configure/build $(date -u +%T) ==="
cmake -S "$src" -B "$build" -G Ninja -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=ON \
  -DENABLE_CUDA=ON "-DCMAKE_CUDA_ARCHITECTURES=${CUDA_ARCH}" "-DMCPU=${MCPU}" -DCMAKE_POSITION_INDEPENDENT_CODE=ON \
  -DENABLE_UHD=OFF -DENABLE_SIDEKIQ=OFF -DENABLE_MKL=OFF -DENABLE_FFTZ=OFF -DENABLE_ARMPL=OFF \
  -DENABLE_DPDK=OFF -DENABLE_LIBNUMA=OFF -DENABLE_PLUGINS=OFF -DENABLE_BACKWARD=OFF \
  -DENABLE_ZEROMQ=ON -DENABLE_FFTW=ON -DENABLE_EXPORT=ON > "$out/configure.log" 2>&1 || die configure
cmake --build "$build" --target ocudu_phy_cuda gnb ofdm_demodulator_cuda_test ofdm_prach_demodulator_cuda_test \
  pdxch_baseband_modulator_cuda_test ldpc_encoder_gpu_cpu_test ldpc_decoder_gpu_cpu_test prach_detector_cuda_test \
  pusch_gpu_cpu_comparison_test pdsch_gpu_e2e_test pusch_e2e_pipeline_test pusch_resident_dematch_scramble_test \
  srs_estimator_gpu_latency_benchmark srs_estimator_gpu_sensitivity_sweep ofh_iq_compression_cuda_test \
  -j"$jobs" > "$out/build.log" 2>&1 || die build
echo "=== build done $(date -u +%T) ==="
sha256sum "$build/apps/gnb/gnb" | tee "$out/gnb-sha256.txt"

ctest --test-dir "$build" --output-on-failure --no-tests=error --timeout 3600 -j1 -R "$TESTS" > "$out/phy-validation.log" 2>&1
echo "phy_ctest_exit=$?"
cp "$build/Testing/Temporary/LastTest.log" "$out/phy-LastTest.log"
grep -E '^ *[0-9]+/[0-9]+ Test|tests passed' "$out/phy-validation.log"
ctest --test-dir "$build" --output-on-failure --timeout 600 -j1 -R '^ofh_iq_compression_cuda/' > "$out/ofh-validation.log" 2>&1
echo "ofh_ctest_exit=$?"; grep -E 'tests passed' "$out/ofh-validation.log"
python3 "$repo/scripts/cuda/verify-phy-log.py" "$out/phy-LastTest.log" | tee "$out/phy-log-verdict.txt"

echo "=== SINR parity, CPU vs GPU (|delta| <= 0.5 dB) $(date -u +%T) ==="
fail=0
for tree in d8:"$build" control-c1:"$control"; do
  name="${tree%%:*}"; b="${tree#*:}/tests/integrationtests/phy/upper/channel_processors/pusch_e2e_pipeline_test"
  for prb in 1 2; do for s in 5 10 20; do
    o=$("$b" --prb "$prb" --sinr "$s" --mcs 4 --mcs-table qam64 --iterations 50 --warmup 3 --layers 1 --ports 1 \
          --dmrs-type type1 --dmrs-symbols 2\,11 --rx-device-grid managed --quiet 2>&1)
    d=$(echo "$o" | awk '/^SINR \(dB\)/{print $5}')
    ok=$(awk -v d="$d" 'BEGIN{print (d != "" && d <= 0.5 && d >= -0.5) ? "ok" : "OUT"}')
    echo "tree=$name prb=$prb sinr_in=$s delta_db=$d $ok decode=$(echo "$o" | grep -cE '^PASS')pass"
    [[ "$name" == d8 && "$ok" != ok ]] && fail=1
  done; done
done | tee "$out/sinr-parity.txt"
grep -q '^tree=d8 .* OUT' "$out/sinr-parity.txt" && echo "d8_sinr_parity=FAIL" || echo "d8_sinr_parity=PASS"
grep -q '^tree=control-c1 prb=1 .* OUT' "$out/sinr-parity.txt" && echo "nc_d8=reproduced" || echo "nc_d8=NOT reproduced"
echo "d8_evidence=$out"; echo D8_DONE
