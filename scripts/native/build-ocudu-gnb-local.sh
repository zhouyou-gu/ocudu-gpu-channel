#!/usr/bin/env bash
set -euo pipefail

# Builds the CPU OCUDU gNB with this repository's local patches, next to the
# pinned build and without touching it (the same policy as the OAI and srsUE
# local patches).
#
#   src/ocudu                pinned checkout (read only here)
#   src/ocudu-local          `git clone --shared` of it at the pin, patches applied
#   builds/ocudu-zmq-local   the patched build (target gnb), with BUILD-MANIFEST.txt
#
# The configure flags are the pinned build's (read from its CMakeCache), so the
# only difference from builds/ocudu-zmq-release is the patches.
#
# Patches (ocudu-gnb-local-patches.lock.json):
#   ocudu-zmq-lower-phy-profile.patch   OCUDU_ZMQ_LOWER_PHY_PROFILE keeps a threaded
#                                       lower PHY with ZMQ; OCUDU_LPHY_RX_TO_TX_MAX_MS
#                                       scales the DL run-ahead bound (S15 follow-up)
#
# Gates select the result with
#   OCUDU_NATIVE_GNB_BINARY=$OCUDU_NATIVE_ROOT/builds/ocudu-zmq-local/apps/gnb/gnb
# and pass the knobs through OCUDU_NATIVE_GNB_WRAPPER="env OCUDU_ZMQ_LOWER_PHY_PROFILE=dual".

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

die() { printf 'error: %s\n' "$1" >&2; exit 1; }

native_root="${OCUDU_NATIVE_ROOT:?set OCUDU_NATIVE_ROOT}"
jobs="${OCUDU_GNB_BUILD_JOBS:-$(nproc)}"
lock="${script_dir}/../../integrations/ocudu/ocudu-gnb-local-patches.lock.json"
pinned="${native_root}/src/ocudu"
pinned_build="${native_root}/builds/ocudu-zmq-release"
src_dir="${native_root}/src/ocudu-local"
build_dir="${native_root}/builds/ocudu-zmq-local"

read -r pin < <(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["ocudu_commit"])' "${lock}")
mapfile -t patches < <(python3 -c '
import json,sys
for p in json.load(open(sys.argv[1]))["patches"]: print(p["path"] + " " + p["sha256"])' "${lock}")

[[ "$(git -C "${pinned}" rev-parse HEAD)" == "${pin}" ]] || die "pinned src/ocudu is not ${pin}"
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

# The pinned build's generator and feature flags, so the two builds differ only by the patches.
generator="$(cache_value CMAKE_GENERATOR)"
flags=()
for key in CMAKE_BUILD_TYPE BUILD_TESTING ENABLE_UHD ENABLE_SIDEKIQ ENABLE_MKL ENABLE_FFTZ ENABLE_ARMPL \
           ENABLE_DPDK ENABLE_LIBNUMA ENABLE_PLUGINS ENABLE_BACKWARD ENABLE_ZEROMQ ENABLE_FFTW ENABLE_EXPORT \
           MCPU MARCH CMAKE_PREFIX_PATH CMAKE_INCLUDE_PATH CMAKE_LIBRARY_PATH; do
  value="$(cache_value "${key}")"
  [[ -n "${value}" ]] && flags+=("-D${key}=${value}")
done

log_dir="${build_dir}/logs-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "${build_dir}" "${log_dir}"
cmake -S "${src_dir}" -B "${build_dir}" ${generator:+-G "${generator}"} "${flags[@]}" \
  -DCMAKE_INSTALL_PREFIX="${native_root}/install/ocudu-local" \
  >"${log_dir}/cmake-configure.log" 2>&1 || { tail -30 "${log_dir}/cmake-configure.log" >&2; die "configure failed"; }
cmake --build "${build_dir}" --target gnb -j"${jobs}" \
  >"${log_dir}/cmake-build.log" 2>&1 || { tail -40 "${log_dir}/cmake-build.log" >&2; die "build failed"; }

binary="${build_dir}/apps/gnb/gnb"
[[ -x "${binary}" ]] || die "not produced: ${binary}"
{
  printf 'ocudu_pin=%s\n' "${pin}"
  printf '%s\n' "${patch_lines[@]}"
  printf 'patched_tree_diff_sha256=%s\n' "$(git -C "${src_dir}" diff | sha256sum | cut -d' ' -f1)"
  printf 'gnb_sha256=%s\n' "$(sha256sum "${binary}" | cut -d' ' -f1)"
  printf 'log_dir=%s\n' "${log_dir}"
} >"${build_dir}/BUILD-MANIFEST.txt"
cat "${build_dir}/BUILD-MANIFEST.txt"
