#!/usr/bin/env bash
# C1 discipline: every fix must have a control that FAILS when the defect is
# restored. A test that passes after a change only shows the change is
# compatible with passing; the control shows it is the reason.
#
# Each control restores exactly one defect, rebuilds, runs only the test that
# should catch it, and requires a NONZERO exit. Exit status is checked here
# because some of these programs print a failure verdict and still return 0.
set -uo pipefail
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/../.." && pwd)"
source "${repo_root}/scripts/native/env.sh"
src="${OCUDU_NATIVE_ROOT}/src/ocudu-cuda"
build="${OCUDU_NATIVE_ROOT}/builds/c1-cuda-patched"
patch_file="${repo_root}/integrations/ocudu/patches/c1-ocudu-cuda-discrete-and-config.patch"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
out="${OCUDU_NATIVE_ROOT}/results/cuda-rebuild/c1-nc-${stamp}"
mkdir -p "$out"

restore() { git -C "$src" checkout -- . ; git -C "$src" apply "$patch_file"; }
rebuild() { cmake --build "$build" --target "$@" -j"${OCUDU_NATIVE_BUILD_JOBS:-12}" >/dev/null 2>&1; }

record() { # name expected_test exit_code logfile
  printf '%-28s test=%-38s exit=%s %s\n' "$1" "$2" "$3" "$([ "$3" -ne 0 ] && echo 'CONTROL OK (failed as required)' || echo '*** CONTROL BROKEN: defect restored but test passed ***')" \
    | tee -a "$out/summary.txt"
}

echo "=== NC-D1: SRS discrete/host-grid staging removed ==="
restore; git -C "$src" checkout -- lib/phy/upper/signal_processors/srs/srs_estimator_cuda_impl.cpp
rebuild srs_estimator_gpu_latency_benchmark srs_estimator_gpu_sensitivity_sweep
ctest --test-dir "$build" -j1 --timeout 600 -R '^srs_estimator_gpu_latency_baseline_4x4_n4$' >"$out/nc-d1.log" 2>&1
record "NC-D1 srs-host-grid" "srs_estimator_gpu_latency_baseline_4x4_n4" "$?"

echo "=== NC-D2: OFH host-copy fallback removed ==="
restore; git -C "$src" checkout -- lib/ofh/compression/iq_compression_cuda.cpp
rebuild pdsch_gpu_e2e_test
ctest --test-dir "$build" -j1 --timeout 600 -R '^pdsch_gpu_e2e_test$' >"$out/nc-d2.log" 2>&1
record "NC-D2 ofh-host-copy" "pdsch_gpu_e2e_test" "$?"

echo "=== NC-D3: configured time interpolation not applied on the GPU path ==="
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
ctest --test-dir "$build" -j1 --timeout 900 -R '^pusch_gpu_cpu_cfo_interpolation_test$' >"$out/nc-d3.log" 2>&1
record "NC-D3 pusch-interpolation" "pusch_gpu_cpu_cfo_interpolation_test" "$?"

echo "=== NC-D4: EVM substituted for post-equalization SINR on the synchronous path ==="
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
ctest --test-dir "$build" -j1 --timeout 900 -R '^pusch_gpu_cpu_sync_sinr_test$' >"$out/nc-d4.log" 2>&1
record "NC-D4 sync-sinr-evm" "pusch_gpu_cpu_sync_sinr_test" "$?"

echo "=== restoring the C1 patch and rebuilding ==="
restore
rebuild srs_estimator_gpu_latency_benchmark srs_estimator_gpu_sensitivity_sweep pdsch_gpu_e2e_test pusch_gpu_cpu_comparison_test
git -C "$src" diff HEAD | sha256sum | sed 's/^/restored_patch_sha256=/' | tee -a "$out/summary.txt"
echo "c1_nc_evidence=${out}"
