#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=env.sh
source "${script_dir}/env.sh"

repo_root="$(cd "${script_dir}/../.." && pwd)"
"/usr/bin/python3" "${script_dir}/verify-workspace-lock.py" \
  --root "${OCUDU_NATIVE_ROOT}" \
  --repo-root "${repo_root}" \
  --lock "${script_dir}/native-workspace.lock.json"

audited_ocudu="a1916edcdbcd70ba6e0af47ee87be061dad5a4e4"
audited_srsran="eea87b1d893ae58e0b08bc381730c502024ae71f"
audited_open5gs="d9d3abdd480be96fac3bc8a997e83446648763ca"

check_revision()
{
  local name="$1"
  local root="$2"
  local expected="$3"
  local actual
  [[ -d "${root}/.git" ]] || {
    echo "missing ${name} Git checkout: ${root}" >&2
    return 1
  }
  actual="$(git -C "${root}" rev-parse HEAD)"
  [[ "${actual}" == "${expected}" ]] || {
    echo "${name} revision mismatch: ${actual} != ${expected}" >&2
    return 1
  }
  printf '%s_revision=%s\n' "${name}" "${actual}"
}

check_binary()
{
  local name="$1"
  local binary="$2"
  local report="/tmp/ocudu-native-${name}-ldd.txt"
  [[ -x "${binary}" ]] || {
    echo "missing executable ${name}: ${binary}" >&2
    return 1
  }
  if ! ldd "${binary}" >"${report}" 2>&1; then
    cat "${report}" >&2
    echo "${name} ldd inspection failed" >&2
    return 1
  fi
  if grep -q 'not found' "${report}"; then
    cat "${report}" >&2
    echo "${name} has unresolved shared libraries" >&2
    return 1
  fi
  printf '%s_ldd=ok\n' "${name}"
  sha256sum "${binary}"
}

check_revision ocudu "${OCUDU_NATIVE_ROOT}/src/ocudu" "${audited_ocudu}"
check_revision srsran4g "${OCUDU_NATIVE_ROOT}/src/srsRAN_4G" "${audited_srsran}"
check_revision open5gs "${OCUDU_NATIVE_ROOT}/src/open5gs" "${audited_open5gs}"

check_binary gnb "${OCUDU_NATIVE_ROOT}/builds/ocudu-zmq-release/apps/gnb/gnb"
check_binary srsue "${OCUDU_NATIVE_ROOT}/builds/srsran4g-zmq-release/srsue/src/srsue"
check_binary open5gs5gc "${OCUDU_NATIVE_ROOT}/builds/open5gs-v2.7.6/tests/app/5gc"
check_binary mongod "${OCUDU_NATIVE_ROOT}/install/mongodb-6.0.29/bin/mongod"

"${OCUDU_NATIVE_ROOT}/builds/ocudu-zmq-release/apps/gnb/gnb" --version
"${OCUDU_NATIVE_ROOT}/builds/srsran4g-zmq-release/srsue/src/srsue" --version
"${OCUDU_NATIVE_ROOT}/install/mongodb-6.0.29/bin/mongod" --version | head -n 3

if [[ -c /dev/net/tun ]]; then
  echo "tun_device=present"
else
  echo "tun_device=missing"
fi
cap_bnd="$(awk '/^CapBnd:/ {print $2}' /proc/self/status)"
cap_eff="$(awk '/^CapEff:/ {print $2}' /proc/self/status)"
no_new_privs="$(awk '/^NoNewPrivs:/ {print $2}' /proc/self/status)"
printf 'capability_effective_set=%s\ncapability_bounding_set=%s\nno_new_privileges=%s\n' \
  "${cap_eff}" "${cap_bnd}" "${no_new_privs}"

set +e
sctp_report="$(mktemp /tmp/ocudu-native-sctp.XXXXXX)"
/usr/bin/python3 - >"${sctp_report}" 2>&1 <<'PY'
import socket

sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_SCTP)
sock.bind(("127.0.0.1", 0))
print("sctp_loopback_bind=ok")
sock.close()
PY
sctp_status="$?"
set -e
if [[ "${sctp_status}" -eq 0 ]]; then
  cat "${sctp_report}"
else
  # Managed command sandboxes can deny SCTP even when the containing LXC
  # permits it. The no-core two-port gate does not use SCTP; a full 1x1 core
  # run must repeat this preflight outside that inner sandbox.
  echo "sctp_loopback_bind=blocked_or_unavailable"
fi
rm -f "${sctp_report}"

cap_eff_value=$((16#${cap_eff}))
cap_net_admin=$((1 << 12))
cap_sys_admin=$((1 << 21))
if [[ -c /dev/net/tun ]] && \
   (( (cap_eff_value & cap_net_admin) != 0 && \
      (cap_eff_value & cap_sys_admin) != 0 )); then
  echo "legacy_1x1_direct_namespace=requires_runtime_preflight"
else
  echo "legacy_1x1_direct_namespace=blocked"
fi
if command -v unshare >/dev/null 2>&1 && \
   unshare --user --map-root-user --net --mount --fork /bin/true \
     >/dev/null 2>&1; then
  echo "legacy_1x1_rootless_namespace=candidate_requires_full_primitive_probe"
else
  echo "legacy_1x1_rootless_namespace=unavailable"
fi
"${OCUDU_NATIVE_ROOT}/builds/ocudu-zmq-release/apps/gnb/gnb" \
  -c "${script_dir}/../../examples/native/ocudu/gnb_zmq_b210_fdd_2port_no_core.yaml" \
  --dryrun >/dev/null
echo "native_2port_no_core_dependencies=ready"
