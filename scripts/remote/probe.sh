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

printf '\nmatrix verification python\n'
# The wire-capture matrix checker (scripts/native/verify-mimo-matrix-capture.py)
# imports numpy and PyYAML. Neither ships with a stock Ubuntu system Python, and
# their absence used to surface as an ImportError in the middle of a live gate.
# The gates provision a private venv for this, so report both the system state
# and the provisioned venv.
for module in numpy yaml; do
  if /usr/bin/python3 -c "import ${module}" >/dev/null 2>&1; then
    printf 'system_python_%s=present\n' "${module}"
  else
    printf 'system_python_%s=missing (gates use the provisioned venv)\n' "${module}"
  fi
done
matrix_venv="${workspace}/tools/matrix-verify-venv"
if [[ -x "${matrix_venv}/bin/python" ]]; then
  if "${matrix_venv}/bin/python" -c 'import numpy, yaml' >/dev/null 2>&1; then
    printf 'matrix_venv=ok (%s)\n' "${matrix_venv}"
  else
    printf 'matrix_venv=incomplete (%s)\n' "${matrix_venv}"
  fi
else
  printf 'matrix_venv=absent (created on first matrix-scoring gate run)\n'
fi

printf '\ndocker network pools\n'
# The containerised gates need a free /24 for their RAN network. A workstation
# hosting another 5G stack usually holds 10.53.1.0/24 already, which is why the
# gates now select a free pool instead of assuming that one.
if command -v docker >/dev/null 2>&1; then
  for net in $(docker network ls --format '{{.Name}}' 2>/dev/null); do
    subnet="$(docker network inspect "${net}" --format '{{range .IPAM.Config}}{{.Subnet}} {{end}}' 2>/dev/null)"
    [[ -n "${subnet// /}" ]] && printf '%s=%s\n' "${net}" "${subnet}"
  done
else
  printf 'docker=missing\n'
fi

printf '\nnetwork\n'
ip -br addr
REMOTE
