#!/usr/bin/env bash
# Install the optional Sionna controller environment outside the project tree.
# This script does not install or modify OCUDU.
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${script_dir}/common.sh"

remote_sh bash -s -- "${REMOTE_WORKSPACE}" "${REMOTE_PROJECT_ROOT}" <<'REMOTE'
set -euo pipefail

expand_remote_path() {
  case "$1" in
    "~") printf '%s\n' "${HOME}" ;;
    "~/"*) printf '%s/%s\n' "${HOME}" "${1#~/}" ;;
    *) printf '%s\n' "$1" ;;
  esac
}

workspace="$(expand_remote_path "$1")"
project_root="$(expand_remote_path "$2")"
venv="${workspace}/venvs/sionna"
requirements="${project_root}/apps/sionna_bridge/requirements.txt"

if [[ ! -f "${requirements}" ]]; then
  echo "missing ${requirements}; run scripts/remote/sync.sh first" >&2
  exit 1
fi

mkdir -p "${workspace}/venvs"
python3 -m venv "${venv}"
"${venv}/bin/python" -m pip install --upgrade pip
"${venv}/bin/python" -m pip install -r "${requirements}"
"${venv}/bin/python" -c 'import sionna.rt, zmq; print("Sionna RT controller environment ready")'
printf 'python=%s\n' "${venv}/bin/python"
REMOTE
