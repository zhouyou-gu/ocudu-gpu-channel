#!/usr/bin/env bash
# Robot-fight glue for the native multi-UE gate's OCUDU_NATIVE_MUE_UE_EXEC hook (R4b).
#
# The gate runs this inside the UE's network namespace right after that UE's
# ping to the gateway passed, i.e. once tun_srsue carries its address. It
# starts the robot's modem: a UDP endpoint on the UE address for the brain,
# relayed to the arena over a filesystem unix datagram socket (the filesystem
# is shared with the stack namespace; only the network namespace differs).
# modem.py is stdlib-only, so the system python3 is enough here.
#
# Usage (as the gate expands it):
#   bash ue_exec.sh {ue_id} {ue_ip} {ue_index} {run_dir} {log_dir}
# Knobs: RF_PORT_BASE (6000), RF_MODEM_PYTHON (/usr/bin/python3).
set -euo pipefail
ue_id="$1"; ue_ip="$2"; ue_index="$3"; run_dir="$4"; log_dir="$5"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
pkg_dir="$(dirname "${script_dir}")"
port=$(( ${RF_PORT_BASE:-6000} + ue_index ))
arena_dir="${run_dir}/arena"
mkdir -p "${arena_dir}"
printf '%s event=modem_start ue=%s bind=%s:%s arena_socket=%s\n' "$(date -u +%s%3N)" \
  "${ue_id}" "${ue_ip}" "${port}" "${arena_dir}/${ue_id}.sock"
exec "${RF_MODEM_PYTHON:-/usr/bin/python3}" "${pkg_dir}/modem.py" --robot "${ue_id}" \
  --bind "${ue_ip}:${port}" --arena-socket "${arena_dir}/${ue_id}.sock" \
  --status-file "${log_dir}/modem-${ue_id}.jsonl"
