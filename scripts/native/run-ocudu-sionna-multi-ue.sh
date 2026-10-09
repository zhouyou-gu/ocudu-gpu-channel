#!/usr/bin/env bash
# Native multi-UE gate driven by Sionna RT: one OCUDU gNB, two real srsUEs,
# Open5GS, this tree's broker, and the read-only Web UI — with the channel
# recomputed from ray tracing instead of a fixed TDL.
#
# The counterpart of run-ocudu-sionna-rank1.sh for the multi-UE gate. Node and
# link shape come from the scenario; render-sionna-multi-ue-configs.py turns it
# into the broker topology, and the inner script starts the bridge before the
# gNB so no UE RACHes into a quiet -100 dB channel.
#
# One gNB with a single ZMQ port pair and one single-port srsUE per record in
# render-multi-ue-configs.py. For a multi-antenna gNB use the rank-1 gate.
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/../.." && pwd)"
scenario="${OCUDU_NATIVE_SIONNA_SCENARIO:-${repo_root}/use_cases/configs/sionna/scenarios/sutd/sionna-multi-ue-sutd.json}"
[[ "${scenario}" == /* && -f "${scenario}" && ! -L "${scenario}" ]] || {
  printf 'error: OCUDU_NATIVE_SIONNA_SCENARIO must be an absolute regular file: %s\n' \
    "${scenario}" >&2
  exit 2
}

export OCUDU_NATIVE_CHANNEL_MODE=sionna
export OCUDU_NATIVE_SIONNA_SCENARIO="${scenario}"
: "${OCUDU_NATIVE_SIONNA_PYTHON:?set OCUDU_NATIVE_SIONNA_PYTHON to the Sionna interpreter}"
export OCUDU_NATIVE_SIONNA_PYTHON

exec "${script_dir}/run-ocudu-multi-ue.sh" "$@"
