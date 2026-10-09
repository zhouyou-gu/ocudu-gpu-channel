#!/usr/bin/env bash
# Native OCUDU rank-1 MISO/SIMO live stack with an EXTERNAL channel source.
#
# Identical to run-ocudu-sionna-rank1.sh except that this gate does not start
# Sionna RT. It brings up the radio stack, opens the Broker's control plane on
# ipc://.../control.sock, and leaves the channel matrices to whatever process
# you attach. The Broker keeps serving the scenario's static TDL until the
# first external batch commits, so the link never goes dark while you connect.
#
# The scenario JSON is still required: the config renderer reads it for the
# port geometry (tx_array/rx_array -> gNB and Broker port counts). Nothing in
# this path imports Sionna.
#
# Verdict is attach + PDU + ping, exactly as the legacy gate. No claim is made
# about Sionna RT, and source-evidence.json records
# claim_boundary.external_channel_source = true.
#
# Attach a feeder to ${OCUDU_NATIVE_ROOT}/run/s1-<timestamp>/control.sock:
#   {"type":"batch_begin","id":"ext-1"}
#   {"type":"matrix_profile_swap","link_id":"gnb0>ue0:sionna_rt",
#    "nt":4,"nr":1,"lanes":[{"rx_port":0,"tx_port":T,
#      "taps":[{"delay_samples":0.0,"gain_db":G,"phase_rad":P}]} for T in 0..3]}
#   {"type":"matrix_profile_swap","link_id":"ue0>gnb0:sionna_rt","nt":1,"nr":4,...}
#   {"type":"batch_commit","id":"ext-1"}
# Use a batch-id prefix of your own; ids are a shared namespace.
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/../.." && pwd)"
scenario="${OCUDU_NATIVE_SIONNA_SCENARIO:-${repo_root}/use_cases/configs/sionna/scenarios/simple_street/ocudu-rank1.json}"
[[ "${scenario}" == /* && -f "${scenario}" && ! -L "${scenario}" ]] || {
  printf 'error: OCUDU_NATIVE_SIONNA_SCENARIO must be an absolute regular file: %s\n' \
    "${scenario}" >&2
  exit 2
}

export OCUDU_NATIVE_CHANNEL_MODE=external
export OCUDU_NATIVE_SIONNA_SCENARIO="${scenario}"
export OCUDU_NATIVE_CONFIG_RENDERER="${script_dir}/render-sionna-rank1-configs.py"
export OCUDU_NATIVE_RENDERER_USES_SCENARIO=1
export OCUDU_NATIVE_SIONNA_RESULT_FAMILY=ocudu-external-rank1
export OCUDU_NATIVE_SIONNA_RUN_FAMILY=ocudu-external-rank1-native
export OCUDU_NATIVE_SIONNA_EVENT_FAMILY=native_external_rank1
export OCUDU_NATIVE_EXECUTION_PROFILE=rank1
export OCUDU_NATIVE_BROKER_READY_DEVICE=ue0_p0

exec "${script_dir}/run-ocudu-legacy-1x1.sh" "$@"
