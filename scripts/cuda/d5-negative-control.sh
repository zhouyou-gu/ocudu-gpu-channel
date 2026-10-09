#!/usr/bin/env bash
# Negative control for D5 -- the missing PDSCH codeblock executor in OCUDU's
# ZMQ sequential PHY configuration.
#
# D5 cannot be controlled the way D1..D4 are. The vendor's CUDA test suite
# builds its own processor factories and never reaches
# create_downlink_processor_factory_sw() with a ZMQ radio, so no ctest can
# observe the defect. The control that observes it is the live gate: with the
# hook removed and pdsch_acceleration_mode enabled, upper-PHY construction must
# abort and the gNB must never start.
#
# The gate is invoked through run-ocudu-legacy-1x1.sh rather than
# run-ocudu-cuda-1x1.sh on purpose. The CUDA launcher's first act is to audit
# the source against the locked patch, and this control deliberately makes the
# source differ from that patch -- so the launcher rejects the run before the
# gNB is ever built into a stack, and a control that stopped there would be
# scoring the audit, not the defect. The environment below is exactly what the
# launcher exports, minus that audit.
#
# A nonzero exit is necessary but NOT sufficient: the run must also fail for
# the right reason, so the gNB console is required to carry the fatal error
# that the missing executor produces.
set -uo pipefail
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/../.." && pwd)"
source "${repo_root}/scripts/native/env.sh"
src="${OCUDU_NATIVE_ROOT}/src/ocudu-cuda"
build="${OCUDU_NATIVE_ROOT}/builds/c1-cuda-patched"
patch_file="${repo_root}/integrations/ocudu/patches/c1-ocudu-cuda-discrete-and-config.patch"
runs="${OCUDU_NATIVE_ROOT}/results/logs/ocudu-interop"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
out="${OCUDU_NATIVE_ROOT}/results/cuda-rebuild/d5-nc-${stamp}"
expected_error='Accelerated PDSCH block processor requested but no block-processor capable PDSCH configuration is active'
mkdir -p "$out"

restore() { git -C "$src" checkout -- . ; git -C "$src" apply "$patch_file"; }
rebuild() { cmake --build "$build" --target gnb -j"${OCUDU_NATIVE_BUILD_JOBS:-12}" >/dev/null 2>&1; }

echo "=== NC-D5: PDSCH codeblock executor hook removed (vendor behaviour) ==="
restore
python3 - "$src" <<'PY'
import sys, pathlib
p = pathlib.Path(sys.argv[1]) / 'lib/phy/upper/upper_phy_factories.cpp'
s = p.read_text()
anchor = '  if (pdsch_acceleration_requested && !pdsch_codeblock_executor.is_valid() &&'
assert s.count(anchor) == 1, 'NC-D5 anchor not found'
# Keep the declarations so the translation unit still compiles; disable only
# the substitution, which is exactly the defect.
p.write_text(s.replace(anchor, '  if (false /* NEGATIVE CONTROL */ && pdsch_acceleration_requested && !pdsch_codeblock_executor.is_valid() &&'))
PY
rebuild

before="$(ls -1 "$runs" 2>/dev/null | tail -1)"
OCUDU_NATIVE_GNB_BINARY="${build}/apps/gnb/gnb" \
OCUDU_NATIVE_GNB_COMMIT=5830c9c \
OCUDU_NATIVE_GNB_START_TIMEOUT_SECONDS=120 \
OCUDU_NATIVE_BROKER_STARTUP_ALLOWANCE_SECONDS=30 \
OCUDU_NATIVE_GNB_ACCELERATION=all \
OCUDU_NATIVE_CONFIG_RENDERER="${script_dir}/render-cuda-1x1-configs.py" \
  bash "${repo_root}/scripts/native/run-ocudu-legacy-1x1.sh" >"$out/nc-d5.log" 2>&1
status="$?"

after="$(ls -1 "$runs" 2>/dev/null | tail -1)"
console=""
if [[ -n "$after" && "$after" != "$before" ]]; then
  console="${runs}/${after}/gnb-console.log"
  cp -f "$console" "$out/gnb-console.log" 2>/dev/null || true
fi
mechanism=0
if [[ -n "$console" ]] && grep -qF "$expected_error" "$console" 2>/dev/null; then
  mechanism=1
fi

{
  printf '%-28s gate=%-30s exit=%s mechanism_observed=%s\n' \
    "NC-D5 pdsch-cb-executor" "legacy_1x1 acceleration=all" "$status" "$mechanism"
  printf 'expected_error=%s\n' "$expected_error"
  printf 'gnb_console=%s\n' "${console:-<no new run directory>}"
  if [[ "$status" -ne 0 && "$mechanism" -eq 1 ]]; then
    echo 'CONTROL OK (failed, and for the defect being controlled)'
  elif [[ "$status" -eq 0 ]]; then
    echo '*** CONTROL BROKEN: defect restored but the gate passed ***'
  else
    echo '*** CONTROL VOID: the run failed, but not with the controlled defect ***'
  fi
} | tee -a "$out/summary.txt"

echo "=== restoring the C1 patch and rebuilding ==="
restore
rebuild
git -C "$src" diff HEAD | sha256sum | sed 's/^/restored_patch_sha256=/' | tee -a "$out/summary.txt"
echo "d5_nc_evidence=${out}"
