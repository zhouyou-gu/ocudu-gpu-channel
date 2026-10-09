#!/usr/bin/env bash
# J2 -- C1 patch (d2579af2) + the Orin layer (j2-ocudu-cuda-orin.patch, defect D6)
# on Jetson AGX Orin, validated with the same 14 PHY tests + OFH suite as C1/S2.
#
# The checkout src/ocudu-cuda-j2 is built from the clean vendor tree: pin, C1
# patch committed locally as a base, J2 patch applied on top (both hash-checked).
#
# J2_TAG / J2_PATCH select another Orin layer in its own checkout, build and
# evidence prefix (e.g. J2_TAG=j2b J2_PATCH=.../j2b-ocudu-cuda-orin.patch for D7).
set -uo pipefail
WG_COMMIT=5830c9cb7813393b5d518dd5ee2b6641071be4ad
C1_SHA=d2579af289c79377e50b55811d563b0cba2587b42f9409bd1001a1e6d934da0a
repo="$(cd "$(dirname "$0")/../../.." && pwd)"
root="${PLATFORM_ROOT:-/workspace/ocudu-jetson}"
tag="${J2_TAG:-j2}"
src="$root/src/ocudu-cuda-${tag}"
build="$root/builds/${tag}-cuda-patched-sm87"
c1="$repo/integrations/ocudu/patches/c1-ocudu-cuda-discrete-and-config.patch"
j2="${J2_PATCH:-$repo/integrations/ocudu/patches/j2-ocudu-cuda-orin.patch}"
jobs="${J2_BUILD_JOBS:-6}"
out="$root/results/${tag}-$(date -u +%Y%m%dT%H%M%SZ)"
export PATH="/usr/local/cuda/bin:$PATH"
mkdir -p "$out"; exec > >(tee -a "$out/j2.log") 2>&1
die() { echo "FATAL: $*"; exit 1; }
TESTS='^(ofdm_demodulator_cuda_test|ofdm_prach_demodulator_cuda_test|pdxch_baseband_modulator_cuda_test|ldpc_encoder_gpu_cpu_test|ldpc_decoder_gpu_cpu_test|prach_detector_cuda_test|pusch_gpu_cpu_comparison_test|pusch_gpu_cpu_cfo_interpolation_test|pusch_gpu_cpu_sync_sinr_test|pdsch_gpu_e2e_test|pusch_e2e_pipeline_test|pusch_resident_dematch_scramble_test|srs_estimator_gpu_latency_baseline_4x4_n4|srs_estimator_gpu_sensitivity_baseline_4x4_n4)$'

[[ "$(sha256sum "$c1" | cut -d' ' -f1)" == "$C1_SHA" ]] || die "C1 patch hash"
echo "c1_sha256=$C1_SHA j2_sha256=$(sha256sum "$j2" | cut -d' ' -f1)"
if [[ ! -d "$src/.git" ]]; then
  git clone -q "$root/src/ocudu-cuda" "$src" && git -C "$src" checkout -q "$WG_COMMIT" && git -C "$src" apply "$c1" \
    && git -C "$src" -c user.name=j2 -c user.email=j2@local commit -qam "C1 patch d2579af2 (base for the J2 layer)" || die "base"
fi
[[ "$(git -C "$src" rev-parse HEAD~1)" == "$WG_COMMIT" ]] || die "base commit is not pin+C1"
git -C "$src" checkout -q -- . && git -C "$src" apply "$j2" || die "J2 patch does not apply"
echo "source=pin+C1+J2 diff_lines=$(git -C "$src" diff | wc -l)"
bash "$repo/scripts/cuda/jetson/probe-platform.sh" "$out" >/dev/null

echo "=== configure/build $(date -u +%T) ==="
cmake -S "$src" -B "$build" -G Ninja -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=ON \
  -DENABLE_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=87 -DCMAKE_POSITION_INDEPENDENT_CODE=ON \
  -DENABLE_UHD=OFF -DENABLE_SIDEKIQ=OFF -DENABLE_MKL=OFF -DENABLE_FFTZ=OFF -DENABLE_ARMPL=OFF \
  -DENABLE_DPDK=OFF -DENABLE_LIBNUMA=OFF -DENABLE_PLUGINS=OFF -DENABLE_BACKWARD=OFF \
  -DENABLE_ZEROMQ=ON -DENABLE_FFTW=ON -DENABLE_EXPORT=ON > "$out/configure.log" 2>&1 || die configure
cmake --build "$build" --target ocudu_phy_cuda gnb ofdm_demodulator_cuda_test ofdm_prach_demodulator_cuda_test \
  pdxch_baseband_modulator_cuda_test ldpc_encoder_gpu_cpu_test ldpc_decoder_gpu_cpu_test prach_detector_cuda_test \
  pusch_gpu_cpu_comparison_test pdsch_gpu_e2e_test pusch_e2e_pipeline_test pusch_resident_dematch_scramble_test \
  srs_estimator_gpu_latency_benchmark srs_estimator_gpu_sensitivity_sweep ofh_iq_compression_cuda_test \
  -j"$jobs" > "$out/build.log" 2>&1 || die build
echo "=== build done $(date -u +%T) ==="

ctest --test-dir "$build" --output-on-failure --no-tests=error --timeout 7200 -j1 -R "$TESTS" > "$out/phy-validation.log" 2>&1
echo "phy_ctest_exit=$?"
cp "$build/Testing/Temporary/LastTest.log" "$out/phy-LastTest.log"
grep -E '^ *[0-9]+/[0-9]+ Test|tests passed|Total Test time' "$out/phy-validation.log"
ctest --test-dir "$build" --output-on-failure --timeout 600 -j1 -R '^ofh_iq_compression_cuda/' > "$out/ofh-validation.log" 2>&1
echo "ofh_ctest_exit=$?"; grep -E 'tests passed' "$out/ofh-validation.log"
python3 "$repo/scripts/cuda/verify-phy-log.py" "$out/phy-LastTest.log" | tee "$out/phy-log-verdict.txt"
echo "${tag}_evidence=$out"; echo J2_DONE
