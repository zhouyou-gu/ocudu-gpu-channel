#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${script_dir}/common.sh"

remote_sh bash -s -- \
  "${REMOTE_WORKSPACE}" \
  "${REMOTE_PROJECT_ROOT}" \
  "${REMOTE_OCUDU_ROOT}" \
  "${REMOTE_BUILDS_ROOT}" \
  "${REMOTE_RESULTS_ROOT}" \
  "${REMOTE_REPO_URL}" \
  "${REMOTE_OCUDU_URL}" <<'REMOTE'
set -euo pipefail

workspace="$1"
project_root="$2"
ocudu_root="$3"
builds_root="$4"
results_root="$5"
repo_url="$6"
ocudu_url="$7"

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
builds_root="$(expand_remote_path "${builds_root}")"
results_root="$(expand_remote_path "${results_root}")"

mkdir -p \
  "${workspace}" \
  "${builds_root}/ocudu-gpu-channel/cpu-release" \
  "${builds_root}/ocudu-gpu-channel/cuda-release" \
  "${builds_root}/ocudu" \
  "${workspace}/configs/local" \
  "${workspace}/configs/ocudu" \
  "${workspace}/configs/distributed" \
  "${results_root}/benchmarks" \
  "${results_root}/logs" \
  "${results_root}/pcaps" \
  "${results_root}/reports" \
  "${workspace}/datasets" \
  "${workspace}/tools" \
  "${workspace}/tmp"

if [[ ! -d "${project_root}/.git" ]]; then
  git clone "${repo_url}" "${project_root}"
else
  git -C "${project_root}" remote -v
fi

if [[ ! -d "${ocudu_root}/.git" ]]; then
  git clone --depth 1 "${ocudu_url}" "${ocudu_root}"
else
  git -C "${ocudu_root}" remote -v
fi

echo "workspace=${workspace}"
echo "project_root=${project_root}"
echo "ocudu_root=${ocudu_root}"
REMOTE
