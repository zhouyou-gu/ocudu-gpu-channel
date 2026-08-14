#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
config_file="${repo_root}/.config"

if [[ ! -f "${config_file}" ]]; then
  echo "missing ${config_file}; create it from .config.example" >&2
  exit 1
fi

while IFS='=' read -r key value; do
  [[ -z "${key}" || "${key}" == \#* ]] && continue
  case "${key}" in
    REMOTE_USER | REMOTE_HOST | REMOTE_SSH_KEY | REMOTE_WORKSPACE | REMOTE_OCUDU_ROOT | REMOTE_PROJECT_ROOT | REMOTE_BUILDS_ROOT | REMOTE_RESULTS_ROOT | REMOTE_REPO_URL | REMOTE_OCUDU_URL)
      printf -v "${key}" '%s' "${value}"
      ;;
  esac
done <"${config_file}"

: "${REMOTE_USER:?REMOTE_USER is required}"
: "${REMOTE_HOST:?REMOTE_HOST is required}"
: "${REMOTE_SSH_KEY:?REMOTE_SSH_KEY is required}"
: "${REMOTE_WORKSPACE:?REMOTE_WORKSPACE is required}"
: "${REMOTE_OCUDU_ROOT:?REMOTE_OCUDU_ROOT is required}"

REMOTE_PROJECT_ROOT="${REMOTE_PROJECT_ROOT:-${REMOTE_WORKSPACE%/}/ocudu-gpu-channel}"
REMOTE_BUILDS_ROOT="${REMOTE_BUILDS_ROOT:-${REMOTE_WORKSPACE%/}/builds}"
REMOTE_RESULTS_ROOT="${REMOTE_RESULTS_ROOT:-${REMOTE_WORKSPACE%/}/results}"
REMOTE_REPO_URL="${REMOTE_REPO_URL:-$(git -C "${repo_root}" remote get-url origin)}"
REMOTE_OCUDU_URL="${REMOTE_OCUDU_URL:-https://gitlab.com/ocudu/ocudu.git}"

if [[ "${REMOTE_SSH_KEY}" == "~/"* ]]; then
  REMOTE_SSH_KEY="${HOME}/${REMOTE_SSH_KEY:2}"
fi

ssh_base=(ssh -i "${REMOTE_SSH_KEY}" -o BatchMode=yes -o ConnectTimeout=8 "${REMOTE_USER}@${REMOTE_HOST}")

remote_sh() {
  "${ssh_base[@]}" "$@"
}
