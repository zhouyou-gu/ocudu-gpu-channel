#!/usr/bin/env bash
set -euo pipefail

# M6.1 -- build the OAI nrUE against the pinned workspace toolchain.
#
# Measured on the Threadripper PRO 7965WX (24C/48T) at -j48: the whole thing,
# including the asn1c bootstrap and 9 730 compile units, finishes in about two
# and a half minutes. It was budgeted as a long tmux job and is not one.
#
# Everything it needs was prepared and verified beforehand: the source is
# cloned at the pin, the autotools overlay is extracted and relocated, and
# both the configure and a full build were proven with these exact flags.
#
# It builds only what the UE side needs -- `nr-uesoftmodem` and the ZMQ radio
# device `oai_zmqdevif`. The gNB stays OCUDU; nothing here touches it.
#
# Network: the asn1c ExternalProject clones github.com/mouse07410/asn1c at the
# pin recorded in OAI's own CMakeLists (and in our lock). Everything else is
# local.

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/../.." && pwd)"
native_root="${OCUDU_NATIVE_ROOT:-/home/ubuntu/ocudu-native-workspace}"
jobs="${OCUDU_OAI_BUILD_JOBS:-$(nproc)}"

lock="${script_dir}/native-workspace.lock.json"
src_dir="${native_root}/src/oai"
build_dir="${native_root}/builds/oai-zmq-release"

die() { printf 'error: %s\n' "$1" >&2; exit 1; }

[[ -f "${lock}" ]] || die "missing workspace lock: ${lock}"
command -v python3 >/dev/null || die "python3 is required"
command -v cmake >/dev/null || die "cmake is required"
command -v git >/dev/null || die "git is required"

# The pin is the lock's, not this script's, so the two cannot drift.
pinned_commit="$(python3 -c '
import json,sys
d=json.load(open(sys.argv[1]))
for g in d["git_sources"]:
    if g["name"]=="oai": print(g["commit"]); break
else: sys.exit("oai is not pinned in the workspace lock")
' "${lock}")"
[[ -n "${pinned_commit}" ]] || die "could not read the OAI pin from the lock"

[[ -d "${src_dir}/.git" ]] || die "OAI source is not checked out at ${src_dir}"
actual_commit="$(git -C "${src_dir}" rev-parse HEAD)"
[[ "${actual_commit}" == "${pinned_commit}" ]] || \
  die "OAI checkout ${actual_commit} is not the pinned ${pinned_commit}"
[[ -z "$(git -C "${src_dir}" status --porcelain --untracked-files=no)" ]] || \
  die "tracked OAI source files are modified"

# shellcheck source=/dev/null
source "${script_dir}/env.sh"

# The autotools overlay is what lets the asn1c ExternalProject configure at all.
for tool in autoreconf aclocal automake libtoolize; do
  command -v "${tool}" >/dev/null || die "autotools overlay is incomplete: ${tool} not on PATH"
done

mkdir -p "${build_dir}"
log_dir="${native_root}/results/logs/oai-build/$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "${log_dir}"

printf 'oai_commit=%s\n' "${actual_commit}"
printf 'build_dir=%s\n' "${build_dir}"
printf 'log_dir=%s\n' "${log_dir}"
printf 'jobs=%s\n' "${jobs}"

# CMAKE_PREFIX_PATH is not optional: CMake's find_library/find_path ignore
# CPATH and LIBRARY_PATH, so without it the sysroot's libsctp is invisible.
# The `libcap` pkg-config warning during configure is expected -- CMakeLists.txt
# guards its use with if(cap_FOUND).
#
# AVX512=OFF is forced by the host, not chosen for taste: Zen 4 defines
# __AVX512F__, so radio/zmq/zmq_simd.h takes its AVX-512 branch, and the only
# SIMDe in the Ubuntu noble archive (0.7.2) does not have the intrinsics that
# branch calls. OAI's own switch compiles the branch out.
echo "== configure =="
cmake -S "${src_dir}" -B "${build_dir}" \
  -DCMAKE_BUILD_TYPE=Release \
  -DOAI_ZMQ=ON \
  -DENABLE_WERROR=OFF \
  -DAUTO_DOWNLOAD_ASN1C=ON \
  -DAVX512=OFF \
  -DCMAKE_PREFIX_PATH="${OCUDU_NATIVE_SYSROOT}/usr" \
  >"${log_dir}/cmake-configure.log" 2>&1 || {
    tail -30 "${log_dir}/cmake-configure.log" >&2
    die "cmake configure failed; see ${log_dir}/cmake-configure.log"
  }

echo "== build (nr-uesoftmodem, oai_zmqdevif) =="
cmake --build "${build_dir}" --target nr-uesoftmodem oai_zmqdevif -j"${jobs}" \
  >"${log_dir}/cmake-build.log" 2>&1 || {
    tail -40 "${log_dir}/cmake-build.log" >&2
    die "build failed; see ${log_dir}/cmake-build.log"
  }

ue_bin="${build_dir}/nr-uesoftmodem"
zmq_lib="${build_dir}/liboai_zmqdevif.so"
[[ -x "${ue_bin}" ]] || die "nr-uesoftmodem was not produced at ${ue_bin}"
[[ -f "${zmq_lib}" ]] || die "liboai_zmqdevif.so was not produced at ${zmq_lib}"

# Record what was built, the same way the other native gates do.
summary="${log_dir}/build-summary.json"
python3 - "${summary}" "${actual_commit}" "${ue_bin}" "${zmq_lib}" "${build_dir}" <<'PY'
import hashlib, json, sys
path, commit, ue_bin, zmq_lib, build_dir = sys.argv[1:]
def digest(p):
    h = hashlib.sha256()
    with open(p, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()
data = {
    "schema": "oai-ue-build/v1",
    "oai_commit": commit,
    "build_dir": build_dir,
    "targets": {
        "nr-uesoftmodem": {"path": ue_bin, "sha256": digest(ue_bin)},
        "liboai_zmqdevif.so": {"path": zmq_lib, "sha256": digest(zmq_lib)},
    },
    "claim_boundary": {"ue_side_only": True, "gnb_unchanged": True, "rank2_verified": False},
}
with open(path, "w", encoding="utf-8") as out:
    json.dump(data, out, indent=2, sort_keys=True)
    out.write("\n")
PY

echo "== done =="
printf 'nr_uesoftmodem=%s\n' "${ue_bin}"
printf 'zmq_device=%s\n' "${zmq_lib}"
printf 'summary=%s\n' "${summary}"
"${ue_bin}" --version 2>&1 | head -3 || true
