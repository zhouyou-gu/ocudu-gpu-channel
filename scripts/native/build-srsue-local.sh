#!/usr/bin/env bash
set -euo pipefail

# Builds srsUE with this repository's local patches, next to the pinned build
# and without touching it (the same policy as the OAI local patches).
#
#   src/srsRAN_4G              pinned checkout (read only here)
#   src/srsRAN_4G-local        `git clone --shared` of it at the pin, patches applied
#   builds/srsran4g-zmq-local  the patched build, with BUILD-MANIFEST.txt
#
# The configure flags are the pinned build's (bootstrap-workspace.sh), and the
# dependency search paths are read from its CMakeCache, so the only difference
# from builds/srsran4g-zmq-release is the patches.
#
# Patches (srsue-local-patches.lock.json):
#   srsue-ra-contention.patch   multi-UE RACH: preamble 0 hard-coded, a UE that
#                               lost contention still completed RA (S16)

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

die() { printf 'error: %s\n' "$1" >&2; exit 1; }

native_root="${OCUDU_NATIVE_ROOT:?set OCUDU_NATIVE_ROOT}"
jobs="${OCUDU_SRSUE_BUILD_JOBS:-$(nproc)}"
lock="${script_dir}/../../integrations/srsran/srsue-local-patches.lock.json"
pinned="${native_root}/src/srsRAN_4G"
pinned_build="${native_root}/builds/srsran4g-zmq-release"
src_dir="${native_root}/src/srsRAN_4G-local"
build_dir="${native_root}/builds/srsran4g-zmq-local"

read -r pin < <(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["srsran_commit"])' "${lock}")
mapfile -t patches < <(python3 -c '
import json,sys
for p in json.load(open(sys.argv[1]))["patches"]: print(p["path"] + " " + p["sha256"])' "${lock}")

[[ "$(git -C "${pinned}" rev-parse HEAD)" == "${pin}" ]] || die "pinned srsRAN_4G is not ${pin}"
[[ -f "${pinned_build}/CMakeCache.txt" ]] || die "pinned build missing: ${pinned_build}"

cache_value() { sed -n "s/^$1:[A-Z]*=//p" "${pinned_build}/CMakeCache.txt" | head -1; }

rm -rf "${src_dir}"
git clone -q --shared --no-checkout "${pinned}" "${src_dir}"
git -C "${src_dir}" -c advice.detachedHead=false checkout -q "${pin}"
patch_lines=()
repo_root="$(cd "${script_dir}/../.." && pwd)"
for entry in "${patches[@]}"; do
  path="${entry%% *}"
  want="${entry##* }"
  file="${repo_root}/${path}"
  got="$(sha256sum "${file}" | cut -d' ' -f1)"
  [[ "${got}" == "${want}" ]] || die "${path} sha256 ${got} is not the recorded ${want}"
  patch -d "${src_dir}" -p1 -s --no-backup-if-mismatch <"${file}"
  patch_lines+=("patch=${path##*/} sha256=${got}")
done

log_dir="${build_dir}/logs-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "${build_dir}" "${log_dir}"
cmake -S "${src_dir}" -B "${build_dir}" \
  -DCMAKE_BUILD_TYPE=Release \
  "-DCMAKE_PREFIX_PATH=$(cache_value CMAKE_PREFIX_PATH)" \
  "-DCMAKE_INCLUDE_PATH=$(cache_value CMAKE_INCLUDE_PATH)" \
  "-DCMAKE_LIBRARY_PATH=$(cache_value CMAKE_LIBRARY_PATH)" \
  -DCMAKE_INSTALL_PREFIX="${native_root}/install/srsran4g-local" \
  -DENABLE_SRSUE=ON -DENABLE_SRSENB=OFF -DENABLE_SRSEPC=OFF \
  -DENABLE_UHD=OFF -DENABLE_ZEROMQ=ON -DENABLE_WERROR=OFF -DENABLE_EXPORT=ON \
  >"${log_dir}/cmake-configure.log" 2>&1 || { tail -30 "${log_dir}/cmake-configure.log" >&2; die "configure failed"; }
cmake --build "${build_dir}" --target srsue -j"${jobs}" \
  >"${log_dir}/cmake-build.log" 2>&1 || { tail -40 "${log_dir}/cmake-build.log" >&2; die "build failed"; }

binary="${build_dir}/srsue/src/srsue"
[[ -x "${binary}" ]] || die "not produced: ${binary}"
{
  printf 'srsran_pin=%s\n' "${pin}"
  printf '%s\n' "${patch_lines[@]}"
  printf 'patched_tree_diff_sha256=%s\n' "$(git -C "${src_dir}" diff | sha256sum | cut -d' ' -f1)"
  printf 'srsue_sha256=%s\n' "$(sha256sum "${binary}" | cut -d' ' -f1)"
  printf 'log_dir=%s\n' "${log_dir}"
} >"${build_dir}/BUILD-MANIFEST.txt"
cat "${build_dir}/BUILD-MANIFEST.txt"
