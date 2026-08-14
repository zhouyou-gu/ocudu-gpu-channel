#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${script_dir}/common.sh"

remote_sh bash -s -- "${REMOTE_WORKSPACE}" "${REMOTE_PROJECT_ROOT}" "${REMOTE_OCUDU_ROOT}" <<'REMOTE'
set -euo pipefail

workspace="$1"
project_root="$2"
ocudu_root="$3"

expand_remote_path() {
  case "$1" in
    "~") printf '%s\n' "${HOME}" ;;
    "~/"*) printf '%s/%s\n' "${HOME}" "${1#~/}" ;;
    *) printf '%s\n' "$1" ;;
  esac
}

workspace="$(expand_remote_path "${workspace}")"
project_root="$(expand_remote_path "${project_root}")"
ocudu_root="$(expand_remote_path "${ocudu_root}")"

if [[ -f "${workspace}/tools/env.sh" ]]; then
  # shellcheck source=/dev/null
  source "${workspace}/tools/env.sh"
fi

printf 'host=%s\n' "$(hostname)"
printf 'workspace=%s\n' "${workspace}"
printf 'project_root=%s\n' "${project_root}"
printf 'ocudu_root=%s\n' "${ocudu_root}"
printf 'tools_env=%s\n' "${workspace}/tools/env.sh"

printf '\npaths\n'
for path in "${workspace}" "${project_root}" "${ocudu_root}" "${workspace}/builds" "${workspace}/configs" "${workspace}/results" "${workspace}/datasets" "${workspace}/tools" "${workspace}/tmp"; do
  if [[ -e "${path}" ]]; then
    ls -ld "${path}"
  else
    echo "missing ${path}"
  fi
done

printf '\ntools\n'
for tool in git cmake c++ gcc g++ pkg-config nvcc nvidia-smi docker iperf3 python3; do
  if path="$(command -v "${tool}")"; then
    printf '%s=%s\n' "${tool}" "${path}"
  else
    printf '%s=missing\n' "${tool}"
  fi
done

printf '\nversions\n'
cmake --version 2>/dev/null | head -n1 || true
pkg-config --modversion libzmq 2>/dev/null || true
nvcc --version 2>/dev/null | tail -n4 || true
nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader 2>/dev/null || true

printf '\nnetwork\n'
ip -br addr
REMOTE
