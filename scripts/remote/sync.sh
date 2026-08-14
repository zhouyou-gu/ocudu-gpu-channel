#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${script_dir}/common.sh"

branch="$(git -C "${repo_root}" branch --show-current)"
if [[ -z "${branch}" ]]; then
  echo "unable to determine current branch" >&2
  exit 1
fi

remote_sh bash -s -- "${REMOTE_PROJECT_ROOT}" "${branch}" <<'REMOTE'
set -euo pipefail

project_root="$1"
branch="$2"

expand_remote_path() {
  case "$1" in
    "~") printf '%s\n' "${HOME}" ;;
    "~/"*) printf '%s/%s\n' "${HOME}" "${1#~/}" ;;
    *) printf '%s\n' "$1" ;;
  esac
}

project_root="$(expand_remote_path "${project_root}")"

if [[ ! -d "${project_root}/.git" ]]; then
  echo "missing remote project clone: ${project_root}" >&2
  exit 1
fi

git -C "${project_root}" fetch origin
git -C "${project_root}" checkout "${branch}"
git -C "${project_root}" pull --ff-only origin "${branch}"
git -C "${project_root}" status --short --ignored
REMOTE
