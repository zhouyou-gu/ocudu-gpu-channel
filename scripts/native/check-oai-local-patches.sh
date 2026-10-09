#!/usr/bin/env bash
set -uo pipefail

# Recurring check for the local OAI patches (oai-local-patches.lock.json, read
# through oai-local-patches.sh).
#
# Static part (seconds, no GPU):
#   - each patch file, default and optional, matches its recorded sha256
#   - each patch still applies to the pinned OAI tree (patch --dry-run)
#   - the built artifacts match this pin and these patches
#     (builds/oai-zmq-patched via build-oai-zmq-patched.py --verify,
#      builds/oai-zmq-local/BUILD-MANIFEST.txt)
#
# --probe (about 12 minutes, uses the GPU; run it alone on the device):
#   one short OAI 2x2 run per side, and each patch must show its defect without
#   it and the fix with it:
#     ZMQ reply-poll  stock module -> real-time factor < 0.5; patched -> > 0.7
#                     (the probe's 200M 2x2 load keeps a 2-core UE on the Spark
#                     at ~0.89 even when patched; stock sits at ~0.28)
#     MMSE scale      stock UE at 20 dB back-off -> rank-2 NACK > 20%;
#                     patched UE -> NACK < 5% (both at UE RX gain 0)
#     ZMQ RX gain     gNB at its default back-off, UE RX gain 0 -> rank-2
#                     NACK > 20%; the gate default -12 dB -> NACK < 5%
#     CSI RI init     50 MHz full-band load at 24 dB back-off: stock UE ->
#                     rank-2 share of new PDSCH < 50% (it reports RI=1 from an
#                     uninitialised accumulator); patched UE -> > 90%
#
# Usage (as container root, like the gates):
#   OCUDU_NATIVE_ROOT=... bash scripts/native/check-oai-local-patches.sh [--probe]
# Exit status 0 only if every check passes.

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/../.." && pwd)"
# shellcheck source=oai-local-patches.sh
source "${script_dir}/oai-local-patches.sh"

native_root="${OCUDU_NATIVE_ROOT:?set OCUDU_NATIVE_ROOT}"
pinned="${native_root}/src/oai"
probe=0
[[ "${1:-}" == "--probe" ]] && probe=1
failures=0

report()
{
  local status="$1" what="$2" detail="$3"
  printf '%-4s %-44s %s\n' "${status}" "${what}" "${detail}"
  [[ "${status}" == PASS ]] || failures=$((failures + 1))
}

check_patch()
{
  local entry="$1" kind="${2:-}" name want got file
  name="${entry%%:*}"
  want="${entry##*:}"
  file="${script_dir}/../../integrations/oai/patches/${name}"
  got="$(sha256sum "${file}" 2>/dev/null | cut -d' ' -f1)"
  if [[ "${got}" == "${want}" ]]; then
    report PASS "${name} sha256${kind}" "${got:0:12}"
  else
    report FAIL "${name} sha256${kind}" "have ${got:-missing}, recorded ${want:0:12}"
  fi
  if patch -d "${pinned}" -p1 --dry-run -s <"${file}" >/dev/null 2>&1; then
    report PASS "${name} applies to pin${kind}" "${OAI_LOCAL_PIN:0:8}"
  else
    report FAIL "${name} applies to pin${kind}" "patch --dry-run failed against ${pinned}"
  fi
}

pin_now="$(git -C "${pinned}" rev-parse HEAD 2>/dev/null)"
if [[ "${pin_now}" == "${OAI_LOCAL_PIN}" ]] && git -C "${pinned}" diff --quiet; then
  report PASS "pinned tree" "${OAI_LOCAL_PIN:0:8}, clean"
else
  report FAIL "pinned tree" "HEAD ${pin_now:-?}, expected ${OAI_LOCAL_PIN:0:8} and no local changes"
fi
for entry in "${OAI_LOCAL_ZMQ_PATCHES[@]}" "${OAI_LOCAL_UE_PATCHES[@]}"; do
  check_patch "${entry}"
done
for entry in "${OAI_LOCAL_OPTIONAL_PATCHES[@]}"; do
  check_patch "${entry}" " (optional)"
done

if zmq_verify="$(OCUDU_NATIVE_ROOT="${native_root}" /usr/bin/python3 "${script_dir}/build-oai-zmq-patched.py" --verify 2>&1)"; then
  report PASS "builds/oai-zmq-patched manifest" "lock entry, patch and module hashes match"
else
  report FAIL "builds/oai-zmq-patched manifest" "${zmq_verify##*error: }"
fi
ue_manifest="${native_root}/builds/oai-zmq-local/BUILD-MANIFEST.txt"
ue_ok=1
grep -qx "oai_pin=${OAI_LOCAL_PIN}" "${ue_manifest}" 2>/dev/null || ue_ok=0
for entry in "${OAI_LOCAL_UE_PATCHES[@]}"; do
  grep -qx "patch=${entry%%:*} sha256=${entry##*:}" "${ue_manifest}" 2>/dev/null || ue_ok=0
done
grep -qx "nr_uesoftmodem_sha256=$(sha256sum "${native_root}/builds/oai-zmq-local/nr-uesoftmodem" 2>/dev/null | cut -d' ' -f1)" \
  "${ue_manifest}" 2>/dev/null || ue_ok=0
if [[ "${ue_ok}" == 1 ]]; then
  report PASS "builds/oai-zmq-local manifest" "pin, patches and binary hash match"
else
  report FAIL "builds/oai-zmq-local manifest" "missing or stale; run build-oai-ue-local.sh"
fi

# srsUE local patches (srsue-local-patches.lock.json, S16): same checks, own pin.
srsue_lock="${script_dir}/../../integrations/srsran/srsue-local-patches.lock.json"
srsue_pinned="${native_root}/src/srsRAN_4G"
srsue_pin="$(/usr/bin/python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["srsran_commit"])' "${srsue_lock}")"
if [[ "$(git -C "${srsue_pinned}" rev-parse HEAD 2>/dev/null)" == "${srsue_pin}" ]] && git -C "${srsue_pinned}" diff --quiet; then
  report PASS "srsRAN_4G pinned tree" "${srsue_pin:0:8}, clean"
else
  report FAIL "srsRAN_4G pinned tree" "expected ${srsue_pin:0:8} and no local changes"
fi
srsue_manifest="${native_root}/builds/srsran4g-zmq-local/BUILD-MANIFEST.txt"
srsue_ok=1
grep -qx "srsran_pin=${srsue_pin}" "${srsue_manifest}" 2>/dev/null || srsue_ok=0
while read -r path want; do
  name="${path##*/}"
  got="$(sha256sum "${script_dir}/../../${path}" 2>/dev/null | cut -d' ' -f1)"
  [[ "${got}" == "${want}" ]] && report PASS "${name} sha256" "${got:0:12}" \
    || report FAIL "${name} sha256" "have ${got:-missing}, recorded ${want:0:12}"
  patch -d "${srsue_pinned}" -p1 --dry-run -s <"${script_dir}/../../${path}" >/dev/null 2>&1 \
    && report PASS "${name} applies to pin" "${srsue_pin:0:8}" \
    || report FAIL "${name} applies to pin" "patch --dry-run failed against ${srsue_pinned}"
  grep -qx "patch=${name} sha256=${want}" "${srsue_manifest}" 2>/dev/null || srsue_ok=0
done < <(/usr/bin/python3 -c 'import json,sys
for p in json.load(open(sys.argv[1]))["patches"]: print(p["path"], p["sha256"])' "${srsue_lock}")
grep -qx "srsue_sha256=$(sha256sum "${native_root}/builds/srsran4g-zmq-local/srsue/src/srsue" 2>/dev/null | cut -d' ' -f1)" \
  "${srsue_manifest}" 2>/dev/null || srsue_ok=0
if [[ "${srsue_ok}" == 1 ]]; then
  report PASS "builds/srsran4g-zmq-local manifest" "pin, patches and binary hash match"
else
  report FAIL "builds/srsran4g-zmq-local manifest" "missing or stale; run build-srsue-local.sh"
fi

if [[ "${probe}" == 1 ]]; then
  run_probe()
  {
    # $1 label, then env assignments; prints the report directory.
    local label="$1"
    shift
    local log
    log="$(mktemp /tmp/oai-local-probe.XXXXXX)"
    env HOME=/root OCUDU_NATIVE_ROOT="${native_root}" "$@" \
      OAI2X2_PATH=broker OAI2X2_MAX_RANK=2 OAI2X2_BROKER_SECONDS=55 OAI2X2_IPERF_SECONDS=8 \
      OAI2X2_IPERF_RATE=200M OAI2X2_LABEL="patch-check ${label}" \
      OAI2X2_TOPOLOGY="${repo_root}/use_cases/configs/topologies/ocudu_native/topology.ocudu.oai-2x2-unitary.cuda.yaml" \
      bash "${script_dir}/run-ocudu-oai-2x2.sh" >"${log}" 2>&1
    grep -o 'report=[^ ]*' "${log}" | tail -1 | cut -d= -f2
    rm -f "${log}"
  }
  field()
  {
    /usr/bin/python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))[sys.argv[2]])' "$1/rank-summary.json" "$2" 2>/dev/null
  }
  others="$(nvidia-smi --query-compute-apps=pid,process_name --format=csv,noheader 2>/dev/null | wc -l)"
  [[ "${others}" == 0 ]] || printf 'note: %s other GPU process(es) running during the probe\n' "${others}"

  # ZMQ reply-poll: same UE, the module is the only difference (24 dB, clean link).
  for side in stock patched; do
    dir="$(run_probe "zmq-${side}" OCUDU_NATIVE_OAI_ZMQ_MODULE="${side}" OCUDU_NATIVE_OAI_UE=local OAI2X2_TX_BACKOFF_DB=24)"
    rt="$(field "${dir}" realtime_factor)"
    if [[ "${side}" == stock ]]; then
      awk -v v="${rt:-9}" 'BEGIN{exit !(v < 0.5)}' && report PASS "zmq reply-poll: stock shows defect" "realtime_factor=${rt}" \
        || report FAIL "zmq reply-poll: stock shows defect" "realtime_factor=${rt:-none} (expected < 0.5) ${dir}"
    else
      awk -v v="${rt:-0}" 'BEGIN{exit !(v > 0.7)}' && report PASS "zmq reply-poll: patched fixes it" "realtime_factor=${rt}" \
        || report FAIL "zmq reply-poll: patched fixes it" "realtime_factor=${rt:-none} (expected > 0.7) ${dir}"
    fi
  done
  # MMSE scale: patched module both sides, the UE build is the only difference (20 dB).
  for side in stock local; do
    dir="$(run_probe "mmse-${side}" OCUDU_NATIVE_OAI_ZMQ_MODULE=patched OCUDU_NATIVE_OAI_UE="${side}" \
      OCUDU_NATIVE_OAI_UE_RX_GAIN_DB=0 OAI2X2_TX_BACKOFF_DB=20)"
    nack="$(field "${dir}" nack_ratio)"
    if [[ "${side}" == stock ]]; then
      awk -v v="${nack:-0}" 'BEGIN{exit !(v > 0.20)}' && report PASS "mmse scale: stock UE shows defect" "rank-2 NACK=${nack}" \
        || report FAIL "mmse scale: stock UE shows defect" "rank-2 NACK=${nack:-none} (expected > 0.20) ${dir}"
    else
      awk -v v="${nack:-1}" 'BEGIN{exit !(v < 0.05)}' && report PASS "mmse scale: patched UE fixes it" "rank-2 NACK=${nack}" \
        || report FAIL "mmse scale: patched UE fixes it" "rank-2 NACK=${nack:-none} (expected < 0.05) ${dir}"
    fi
  done
  # ZMQ RX gain: patched module and UE, the gNB at its default 12 dB back-off;
  # the gain the gate applies is the only difference.
  for side in off on; do
    gain=0
    [[ "${side}" == on ]] && gain=-12
    dir="$(run_probe "rx-gain-${side}" OCUDU_NATIVE_OAI_ZMQ_MODULE=patched OCUDU_NATIVE_OAI_UE=local \
      OCUDU_NATIVE_OAI_UE_RX_GAIN_DB="${gain}")"
    nack="$(field "${dir}" nack_ratio)"
    if [[ "${side}" == off ]]; then
      awk -v v="${nack:-0}" 'BEGIN{exit !(v > 0.20)}' && report PASS "zmq rx gain: 0 dB shows defect" "rank-2 NACK=${nack}" \
        || report FAIL "zmq rx gain: 0 dB shows defect" "rank-2 NACK=${nack:-none} (expected > 0.20) ${dir}"
    else
      awk -v v="${nack:-1}" 'BEGIN{exit !(v < 0.05)}' && report PASS "zmq rx gain: -12 dB fixes it" "rank-2 NACK=${nack}" \
        || report FAIL "zmq rx gain: -12 dB fixes it" "rank-2 NACK=${nack:-none} (expected < 0.05) ${dir}"
    fi
  done
  # CSI RI init: patched module both sides, 24 dB so the MMSE overflow stays
  # out of it; the UE build is the only difference. Full-band 50 MHz load is
  # where the stale accumulator flips the rank (SPARK_MILESTONES.md S14).
  rank2_share()
  {
    /usr/bin/python3 -c 'import json,sys; d=json.load(open(sys.argv[1]))["dl_decisions"]; a=d.get("ri1_new",0); b=d.get("ri2_new",0); print("%.3f" % (b/(a+b)) if a+b else "")' \
      "$1/rank-summary.json" 2>/dev/null
  }
  for side in stock local; do
    dir="$(run_probe "csi-ri-${side}" OCUDU_NATIVE_OAI_ZMQ_MODULE=patched OCUDU_NATIVE_OAI_UE="${side}" \
      OAI2X2_TX_BACKOFF_DB=24 OAI2X2_BW_MHZ=50)"
    share="$(rank2_share "${dir}")"
    if [[ "${side}" == stock ]]; then
      awk -v v="${share:-1}" 'BEGIN{exit !(v < 0.50)}' && report PASS "csi ri init: stock UE shows defect" "rank-2 share=${share}" \
        || report FAIL "csi ri init: stock UE shows defect" "rank-2 share=${share:-none} (expected < 0.50) ${dir}"
    else
      awk -v v="${share:-0}" 'BEGIN{exit !(v > 0.90)}' && report PASS "csi ri init: patched UE fixes it" "rank-2 share=${share}" \
        || report FAIL "csi ri init: patched UE fixes it" "rank-2 share=${share:-none} (expected > 0.90) ${dir}"
    fi
  done
fi

printf 'result=%s failures=%d\n' "$([[ "${failures}" == 0 ]] && echo pass || echo fail)" "${failures}"
[[ "${failures}" == 0 ]]
