#!/usr/bin/env bash
# Validate, and eventually provision, the locked rootless native workspace.
#
# Reproducibility boundary: this lock describes the audited Ubuntu 24.04 host
# contract plus a user-space Debian overlay.  It is not a hermetic or arbitrary
# clean-host build: 48 Debian dependency clauses and the base compiler/runtime
# are supplied by the host.  Archive hashes authenticate bytes against this
# repository's lock; only MongoDB also has a cached upstream checksum sidecar.

set -euo pipefail
export LC_ALL=C

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/../.." && pwd)"
lock_file="${script_dir}/native-workspace.lock.json"
verifier="${script_dir}/verify-workspace-lock.py"
checker="${script_dir}/check-workspace.sh"

native_root="/home/ubuntu/ocudu-native-workspace"
verify_only=false
offline=false
default_jobs="$(getconf _NPROCESSORS_ONLN 2>/dev/null || printf '1')"
jobs="${default_jobs}"

usage()
{
  cat <<'EOF'
Usage: scripts/native/bootstrap-workspace.sh [OPTIONS]

Options:
  --verify-only       Verify the locked, already-provisioned workspace only.
  --offline           Forbid network provisioning (accepted fail-closed).
  --root PATH         Dedicated workspace root (default:
                      /home/ubuntu/ocudu-native-workspace).
  --jobs N            Positive build parallelism (default: online CPU count).
  -h, --help          Show this help.

This script never invokes sudo or Docker and never resets, deletes, or cleans
an existing tree.  At present, only --verify-only is enabled.  Build mode exits
before mutation until every download/extract/build phase is implemented and
audited against the committed lock.
EOF
}

die()
{
  printf 'bootstrap-workspace: %s\n' "$*" >&2
  exit 1
}

usage_error()
{
  printf 'bootstrap-workspace: %s\n' "$*" >&2
  usage >&2
  exit 2
}

while (($#)); do
  case "$1" in
    --verify-only)
      verify_only=true
      shift
      ;;
    --offline)
      offline=true
      shift
      ;;
    --root)
      (($# >= 2)) || usage_error "--root requires a path"
      native_root="$2"
      shift 2
      ;;
    --jobs)
      (($# >= 2)) || usage_error "--jobs requires a positive integer"
      jobs="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    --)
      shift
      (($# == 0)) || usage_error "unexpected positional arguments: $*"
      ;;
    *)
      usage_error "unknown argument: $1"
      ;;
  esac
done

[[ "${jobs}" =~ ^[1-9][0-9]*$ ]] || usage_error "--jobs must be a positive integer"
[[ "${native_root}" == /* ]] || usage_error "--root must be an absolute path"
[[ ! -L "${native_root}" ]] || die "workspace root must not be a symlink: ${native_root}"

command -v readlink >/dev/null 2>&1 || die "required host tool is missing: readlink"
native_root="$(readlink -m -- "${native_root}")"

case "${native_root}" in
  /|/home|/home/ubuntu|/tmp|/var|/var/tmp)
    die "refusing broad or shared workspace root: ${native_root}"
    ;;
esac

root_base="$(basename -- "${native_root}")"
[[ "${root_base}" =~ ^ocudu-native-workspace([._-].+)?$ ]] || \
  die "workspace root basename must be ocudu-native-workspace or a suffixed variant"

case "${native_root}/" in
  "${repo_root}/"*) die "workspace root must not be inside the audit repository" ;;
esac
case "${repo_root}/" in
  "${native_root}/"*) die "workspace root must not contain the audit repository" ;;
esac

for required_file in "${lock_file}" "${verifier}" "${checker}"; do
  [[ -f "${required_file}" ]] || die "required repository file is missing: ${required_file}"
done
[[ -x /usr/bin/python3 ]] || die "required host tool is missing: /usr/bin/python3"

json_value()
{
  /usr/bin/python3 - "${lock_file}" "$1" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    value = json.load(stream)
for component in sys.argv[2].split("."):
    value = value[component]
if not isinstance(value, (str, int)):
    raise SystemExit(f"lock value is not scalar: {sys.argv[2]}")
print(value)
PY
}

require_executable()
{
  local path="$1"
  [[ -x "${path}" ]] || die "locked host executable is missing: ${path}"
}

require_version()
{
  local name="$1"
  local actual="$2"
  local expected="$3"
  [[ "${actual}" == "${expected}" ]] || \
    die "${name} version mismatch: ${actual} != ${expected}"
  printf 'host_%s=%s\n' "${name}" "${actual}"
}

check_host_contract()
{
  local path expected actual output architecture

  path="$(json_value host_contract.python.path)"
  expected="$(json_value host_contract.python.version)"
  require_executable "${path}"
  actual="$(${path} --version 2>&1)"
  require_version python "${actual#Python }" "${expected}"

  path="$(json_value host_contract.gcc.path)"
  expected="$(json_value host_contract.gcc.version)"
  require_executable "${path}"
  require_version gcc "$(${path} -dumpfullversion -dumpversion)" "${expected}"

  path="$(json_value host_contract.gxx.path)"
  expected="$(json_value host_contract.gxx.version)"
  require_executable "${path}"
  require_version gxx "$(${path} -dumpfullversion -dumpversion)" "${expected}"

  path="$(json_value host_contract.cmake.path)"
  expected="$(json_value host_contract.cmake.version)"
  require_executable "${path}"
  output="$(${path} --version)"
  actual="${output%%$'\n'*}"
  require_version cmake "${actual##* }" "${expected}"

  path="$(json_value host_contract.binutils.linker)"
  expected="$(json_value host_contract.binutils.version)"
  require_executable "${path}"
  output="$(${path} --version)"
  actual="${output%%$'\n'*}"
  require_version binutils "${actual##* }" "${expected}"

  path="$(json_value host_contract.make.path)"
  expected="$(json_value host_contract.make.version)"
  require_executable "${path}"
  output="$(${path} --version)"
  actual="${output%%$'\n'*}"
  require_version make "${actual##* }" "${expected}"

  path="$(json_value host_contract.pkg_config.path)"
  expected="$(json_value host_contract.pkg_config.version)"
  require_executable "${path}"
  require_version pkg_config "$(${path} --version)" "${expected}"

  command -v dpkg >/dev/null 2>&1 || die "required host tool is missing: dpkg"
  command -v dpkg-query >/dev/null 2>&1 || die "required host tool is missing: dpkg-query"
  architecture="$(dpkg --print-architecture)"
  expected="$(json_value host_contract.glibc_package_version)"
  actual="$(dpkg-query -W -f='${Version}' "libc6:${architecture}")"
  require_version glibc_package "${actual}" "${expected}"

  # CUDA is part of the lock but is only needed by the channel CUDA build.
  # If present, reject drift; absence does not invalidate this stack-only check.
  path="$(json_value host_contract.cuda.nvcc)"
  expected="$(json_value host_contract.cuda.version)"
  if [[ -x "${path}" ]]; then
    output="$(${path} --version)"
    actual="$(sed -n 's/.*V\([0-9][0-9.]*\).*/\1/p' <<<"${output}")"
    require_version cuda_nvcc "${actual}" "${expected}"
  else
    printf 'host_cuda_nvcc=absent_optional_for_stack_only\n'
  fi
}

printf '%s\n' \
  'claim_boundary=locked Ubuntu-24.04 host contract plus user-space overlay; not hermetic' \
  "workspace_root=${native_root}" \
  "offline=${offline}" \
  "jobs=${jobs}"

check_host_contract

if [[ "${verify_only}" != true ]]; then
  die "build provisioning is intentionally disabled: use --verify-only; no workspace changes were made"
fi

[[ -d "${native_root}" ]] || die "workspace root is missing: ${native_root}"

/usr/bin/python3 "${verifier}" \
  --root "${native_root}" \
  --repo-root "${repo_root}" \
  --lock "${lock_file}"

OCUDU_NATIVE_ROOT="${native_root}" "${checker}"
printf 'bootstrap_verify_only=ok\n'
