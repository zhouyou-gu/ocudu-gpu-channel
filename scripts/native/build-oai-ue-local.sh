#!/usr/bin/env bash
set -euo pipefail

# Builds the OAI nrUE with this repository's local UE PHY patches, next to the
# pinned build and without touching it.
#
#   src/oai            pinned checkout (read only here)
#   src/oai-local      `git clone --shared` of it at the pin, patches applied
#   builds/oai-zmq-local   the patched build (same flags and targets as
#                      build-oai-ue.sh), with BUILD-MANIFEST.txt
#
# The ZMQ radio module is patched separately (build-oai-zmq-patched.py); the
# gates load it with --loader.oai_zmqdevif.shlibpath, so it is not rebuilt here.
#
# UE patches applied, in order (the `ue` artifact in oai-local-patches.lock.json):
#   oai-nr-dlsch-mmse-scale.patch   2-layer equaliser int16 wrap (M6 8.1 / 8.7)

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=env.sh
source "${script_dir}/env.sh"
# shellcheck source=oai-local-patches.sh
source "${script_dir}/oai-local-patches.sh"

die() { printf 'error: %s\n' "$1" >&2; exit 1; }

native_root="${OCUDU_NATIVE_ROOT:?set OCUDU_NATIVE_ROOT}"
jobs="${OCUDU_OAI_BUILD_JOBS:-$(nproc)}"
pinned="${native_root}/src/oai"
src_dir="${native_root}/src/oai-local"
build_dir="${native_root}/builds/oai-zmq-local"

[[ "$(git -C "${pinned}" rev-parse HEAD)" == "${OAI_LOCAL_PIN}" ]] || die "pinned OAI is not ${OAI_LOCAL_PIN}"
for tool in autoreconf aclocal automake libtoolize; do
  command -v "${tool}" >/dev/null || die "autotools overlay is incomplete: ${tool} not on PATH"
done

# A fresh shared clone every time, so the patched tree is exactly pin + patches.
rm -rf "${src_dir}"
git clone -q --shared --no-checkout "${pinned}" "${src_dir}"
git -C "${src_dir}" -c advice.detachedHead=false checkout -q "${OAI_LOCAL_PIN}"
patch_lines=()
for entry in "${OAI_LOCAL_UE_PATCHES[@]}"; do
  name="${entry%%:*}"
  want="${entry##*:}"
  file="${script_dir}/../../integrations/oai/patches/${name}"
  got="$(sha256sum "${file}" | cut -d' ' -f1)"
  [[ "${got}" == "${want}" ]] || die "${name} sha256 ${got} is not the recorded ${want}"
  patch -d "${src_dir}" -p1 -s --no-backup-if-mismatch <"${file}"
  patch_lines+=("patch=${name} sha256=${got}")
done

log_dir="${build_dir}/logs-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "${build_dir}" "${log_dir}"
cmake -S "${src_dir}" -B "${build_dir}" \
  -DCMAKE_BUILD_TYPE=Release \
  -DOAI_ZMQ=ON \
  -DENABLE_WERROR=OFF \
  -DAUTO_DOWNLOAD_ASN1C=ON \
  -DAVX512=OFF \
  -DCMAKE_PREFIX_PATH="${OCUDU_NATIVE_SYSROOT}/usr" \
  >"${log_dir}/cmake-configure.log" 2>&1 || { tail -30 "${log_dir}/cmake-configure.log" >&2; die "configure failed"; }
cmake --build "${build_dir}" \
  --target nr-uesoftmodem oai_zmqdevif params_libconfig coding ldpc dfts \
  -j"${jobs}" >"${log_dir}/cmake-build.log" 2>&1 || { tail -40 "${log_dir}/cmake-build.log" >&2; die "build failed"; }

for file in nr-uesoftmodem liboai_zmqdevif.so libparams_libconfig.so libcoding.so libldpc.so libdfts.so; do
  [[ -f "${build_dir}/${file}" ]] || die "not produced: ${file}"
done
{
  printf 'oai_pin=%s\n' "${OAI_LOCAL_PIN}"
  printf '%s\n' "${patch_lines[@]}"
  printf 'patched_tree_diff_sha256=%s\n' "$(git -C "${src_dir}" diff | sha256sum | cut -d' ' -f1)"
  printf 'nr_uesoftmodem_sha256=%s\n' "$(sha256sum "${build_dir}/nr-uesoftmodem" | cut -d' ' -f1)"
  printf 'log_dir=%s\n' "${log_dir}"
} >"${build_dir}/BUILD-MANIFEST.txt"
cat "${build_dir}/BUILD-MANIFEST.txt"
