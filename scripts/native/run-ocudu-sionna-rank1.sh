#!/usr/bin/env bash
# Native OCUDU rank-1 MISO/SIMO + Sionna RT + Web UI launcher.
# Sionna tx_array/rx_array values drive the gNB and Broker port dimensions.
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/../.." && pwd)"
# SUTD campus (OpenStreetMap geometry) by default. The canyon scenario is
# still there: OCUDU_NATIVE_SIONNA_SCENARIO=<repo>/use_cases/configs/sionna/scenarios/simple_street/ocudu-rank1.json
scenario="${OCUDU_NATIVE_SIONNA_SCENARIO:-${repo_root}/use_cases/configs/sionna/scenarios/sutd/ocudu-rank1-sutd.json}"
[[ "${scenario}" == /* && -f "${scenario}" && ! -L "${scenario}" ]] || {
  printf 'error: OCUDU_NATIVE_SIONNA_SCENARIO must be an absolute regular file: %s\n' \
    "${scenario}" >&2
  exit 2
}

export OCUDU_NATIVE_CHANNEL_MODE=sionna
export OCUDU_NATIVE_SIONNA_SCENARIO="${scenario}"
export OCUDU_NATIVE_CONFIG_RENDERER="${script_dir}/render-sionna-rank1-configs.py"
export OCUDU_NATIVE_RENDERER_USES_SCENARIO=1
export OCUDU_NATIVE_SIONNA_RESULT_FAMILY=ocudu-sionna-rank1
export OCUDU_NATIVE_SIONNA_RUN_FAMILY=ocudu-sionna-rank1-native
export OCUDU_NATIVE_SIONNA_EVENT_FAMILY=native_sionna_rank1
export OCUDU_NATIVE_EXECUTION_PROFILE=rank1
export OCUDU_NATIVE_BROKER_READY_DEVICE=ue0_p0

exec "${script_dir}/run-ocudu-legacy-1x1.sh" "$@"
