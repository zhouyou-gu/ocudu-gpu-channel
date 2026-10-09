# shellcheck shell=bash
# The local OAI patches this repository carries on top of the pinned OAI, and
# the one selector the OAI gates use for each patched artifact. Sourced by the
# build scripts, the gates and check-oai-local-patches.sh. Upstream reports
# come later; until then these patches are applied at build time and the
# pinned tree stays untouched.
#
# The patch list and the recorded sha256 live in oai-local-patches.lock.json
# (the single source); this file only reads it:
#   OAI_LOCAL_PIN                pinned OAI commit
#   OAI_LOCAL_ZMQ_PATCHES        "name:sha256" built into builds/oai-zmq-patched
#   OAI_LOCAL_UE_PATCHES         "name:sha256" built into builds/oai-zmq-local
#   OAI_LOCAL_OPTIONAL_PATCHES   "name:sha256" kept for measurement, never built

OAI_LOCAL_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OAI_LOCAL_LOCK="${OAI_LOCAL_SCRIPT_DIR}/../../integrations/oai/oai-local-patches.lock.json"
OAI_LOCAL_PIN="$(/usr/bin/python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["oai_commit"])' "${OAI_LOCAL_LOCK}")"
oai_local_list()
{
  # oai_local_list <artifact> <patches|optional>: "name:sha256" per line.
  /usr/bin/python3 - "${OAI_LOCAL_LOCK}" "$1" "$2" <<'PY'
import json, os, sys
lock, artifact, key = sys.argv[1:]
for entry in json.load(open(lock))["artifacts"][artifact][key]:
    print(os.path.basename(entry["path"]) + ":" + entry["sha256"])
PY
}
mapfile -t OAI_LOCAL_ZMQ_PATCHES < <(oai_local_list zmq_module patches)
mapfile -t OAI_LOCAL_UE_PATCHES < <(oai_local_list ue patches)
mapfile -t OAI_LOCAL_OPTIONAL_PATCHES < <(oai_local_list zmq_module optional; oai_local_list ue optional)

# resolve_oai_zmq_module <native_root> picks the nrUE ZMQ radio module for the
# OAI gates and exports OCUDU_NATIVE_OAI_SHLIBPATH (the directory the inner
# scripts pass as --loader.oai_zmqdevif.shlibpath), OAI_ZMQ_MODULE_VARIANT and
# OAI_ZMQ_MODULE_SHA256.
#   OCUDU_NATIVE_OAI_ZMQ_MODULE=patched (default: builds/oai-zmq-patched, the
#   zmq_module patches above, checked by build-oai-zmq-patched.py --verify)
#   | stock (builds/oai-zmq-release, the pinned build). An explicit
#   OCUDU_NATIVE_OAI_SHLIBPATH wins over both.
resolve_oai_zmq_module()
{
  local native_root="$1" dir variant
  if [[ -n "${OCUDU_NATIVE_OAI_SHLIBPATH:-}" ]]; then
    dir="${OCUDU_NATIVE_OAI_SHLIBPATH}"
    variant=custom
  else
    variant="${OCUDU_NATIVE_OAI_ZMQ_MODULE:-patched}"
    case "${variant}" in
      patched)
        dir="${native_root}/builds/oai-zmq-patched"
        if ! OCUDU_NATIVE_ROOT="${native_root}" /usr/bin/python3 \
             "${OAI_LOCAL_SCRIPT_DIR}/build-oai-zmq-patched.py" --verify >/dev/null; then
          printf 'error: %s is missing or stale; run scripts/native/build-oai-zmq-patched.py\n' "${dir}" >&2
          return 1
        fi
        ;;
      stock) dir="${native_root}/builds/oai-zmq-release" ;;
      *)
        printf 'error: OCUDU_NATIVE_OAI_ZMQ_MODULE must be patched or stock, not %s\n' "${variant}" >&2
        return 1
        ;;
    esac
  fi
  if [[ ! -f "${dir}/liboai_zmqdevif.so" ]]; then
    printf 'error: no liboai_zmqdevif.so in %s\n' "${dir}" >&2
    return 1
  fi
  OAI_ZMQ_MODULE_VARIANT="${variant}"
  OAI_ZMQ_MODULE_SHA256="$(sha256sum "${dir}/liboai_zmqdevif.so" | cut -d' ' -f1)"
  export OAI_ZMQ_MODULE_VARIANT OAI_ZMQ_MODULE_SHA256
  export OCUDU_NATIVE_OAI_SHLIBPATH="${dir}"
}

# resolve_oai_ue_build <native_root> picks the nr-uesoftmodem build for the OAI
# 2x2 gate and exports OAI2X2_NRUE_DIR, OAI_UE_VARIANT and OAI_UE_SHA256.
#   OCUDU_NATIVE_OAI_UE=local (default: builds/oai-zmq-local, the UE PHY
#   patches above, checked against its BUILD-MANIFEST.txt) | stock
#   (builds/oai-zmq-release, the pinned build). An explicit OAI2X2_NRUE_DIR
#   wins over both.
resolve_oai_ue_build()
{
  local native_root="$1" dir variant
  if [[ -n "${OAI2X2_NRUE_DIR:-}" ]]; then
    dir="${OAI2X2_NRUE_DIR}"
    variant=custom
  else
    variant="${OCUDU_NATIVE_OAI_UE:-local}"
    case "${variant}" in
      local)
        dir="${native_root}/builds/oai-zmq-local"
        local manifest="${dir}/BUILD-MANIFEST.txt" entry
        if [[ ! -f "${manifest}" ]]; then
          printf 'error: %s is missing; run scripts/native/build-oai-ue-local.sh\n' "${manifest}" >&2
          return 1
        fi
        local ok=1
        grep -qx "oai_pin=${OAI_LOCAL_PIN}" "${manifest}" || ok=0
        for entry in "${OAI_LOCAL_UE_PATCHES[@]}"; do
          grep -qx "patch=${entry%%:*} sha256=${entry##*:}" "${manifest}" || ok=0
        done
        grep -qx "nr_uesoftmodem_sha256=$(sha256sum "${dir}/nr-uesoftmodem" | cut -d' ' -f1)" "${manifest}" || ok=0
        if [[ "${ok}" != 1 ]]; then
          printf 'error: %s does not match the recorded pin/patches/binary; rebuild it\n' "${dir}" >&2
          return 1
        fi
        ;;
      stock) dir="${native_root}/builds/oai-zmq-release" ;;
      *)
        printf 'error: OCUDU_NATIVE_OAI_UE must be local or stock, not %s\n' "${variant}" >&2
        return 1
        ;;
    esac
  fi
  if [[ ! -x "${dir}/nr-uesoftmodem" ]]; then
    printf 'error: no nr-uesoftmodem in %s\n' "${dir}" >&2
    return 1
  fi
  OAI_UE_VARIANT="${variant}"
  OAI_UE_SHA256="$(sha256sum "${dir}/nr-uesoftmodem" | cut -d' ' -f1)"
  export OAI_UE_VARIANT OAI_UE_SHA256
  export OAI2X2_NRUE_DIR="${dir}"
}
