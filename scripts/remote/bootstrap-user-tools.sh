#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${script_dir}/common.sh"

remote_sh bash -s -- "${REMOTE_WORKSPACE}" <<'REMOTE'
set -euo pipefail

workspace="$1"

expand_remote_path() {
  case "$1" in
    "~") printf '%s\n' "${HOME}" ;;
    "~/"*) printf '%s/%s\n' "${HOME}" "${1#~/}" ;;
    *) printf '%s\n' "$1" ;;
  esac
}

download() {
  local url="$1"
  local output="$2"
  if command -v curl >/dev/null 2>&1; then
    curl -fL --retry 3 --retry-delay 5 -o "${output}" "${url}"
  elif command -v wget >/dev/null 2>&1; then
    wget -O "${output}" "${url}"
  else
    python3 - "$url" "$output" <<'PY'
import sys
import urllib.request
urllib.request.urlretrieve(sys.argv[1], sys.argv[2])
PY
  fi
}

workspace="$(expand_remote_path "${workspace}")"
tools_dir="${workspace}/tools"
downloads_dir="${workspace}/tmp/downloads"
cmake_version="3.31.12"
cmake_dir="${tools_dir}/cmake-${cmake_version}"
cuda_version="12.8.1"
cuda_dir="${tools_dir}/cuda-${cuda_version}"
zeromq_version="4.3.5"
zeromq_dir="${tools_dir}/zeromq-${zeromq_version}"

mkdir -p "${tools_dir}" "${downloads_dir}"

if [[ ! -x "${cmake_dir}/bin/cmake" ]]; then
  cmake_archive="${downloads_dir}/cmake-${cmake_version}-linux-x86_64.tar.gz"
  download "https://github.com/Kitware/CMake/releases/download/v${cmake_version}/cmake-${cmake_version}-linux-x86_64.tar.gz" "${cmake_archive}"
  rm -rf "${cmake_dir}"
  mkdir -p "${cmake_dir}"
  tar -xzf "${cmake_archive}" --strip-components=1 -C "${cmake_dir}"
fi

export PATH="${cmake_dir}/bin:${PATH}"

if [[ ! -x "${cuda_dir}/bin/nvcc" ]]; then
  cuda_runfile="${downloads_dir}/cuda_${cuda_version}_570.124.06_linux.run"
  download "https://developer.download.nvidia.com/compute/cuda/${cuda_version}/local_installers/cuda_${cuda_version}_570.124.06_linux.run" "${cuda_runfile}"
  chmod +x "${cuda_runfile}"
  rm -rf "${cuda_dir}"
  "${cuda_runfile}" \
    --silent \
    --toolkit \
    --toolkitpath="${cuda_dir}" \
    --no-man-page \
    --override
fi

export PATH="${cuda_dir}/bin:${PATH}"
export LD_LIBRARY_PATH="${cuda_dir}/lib64${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}"
export CMAKE_PREFIX_PATH="${cuda_dir}${CMAKE_PREFIX_PATH:+:${CMAKE_PREFIX_PATH}}"

if ! pkg-config --modversion libzmq >/dev/null 2>&1; then
  zeromq_archive="${downloads_dir}/zeromq-${zeromq_version}.tar.gz"
  zeromq_src="${downloads_dir}/libzmq-${zeromq_version}"
  download "https://github.com/zeromq/libzmq/archive/refs/tags/v${zeromq_version}.tar.gz" "${zeromq_archive}"
  rm -rf "${zeromq_src}" "${zeromq_dir}" "${downloads_dir}/libzmq-build"
  mkdir -p "${zeromq_src}" "${downloads_dir}/libzmq-build"
  tar -xzf "${zeromq_archive}" --strip-components=1 -C "${zeromq_src}"
  cmake -S "${zeromq_src}" -B "${downloads_dir}/libzmq-build" \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_INSTALL_PREFIX="${zeromq_dir}" \
    -DBUILD_TESTS=OFF \
    -DWITH_PERF_TOOL=OFF \
    -DWITH_LIBSODIUM=OFF \
    -DWITH_LIBBSD=OFF \
    -DWITH_TLS=OFF
  cmake --build "${downloads_dir}/libzmq-build" -j"$(nproc)"
  cmake --install "${downloads_dir}/libzmq-build"
fi

if [[ -d "${zeromq_dir}" ]]; then
  export CMAKE_PREFIX_PATH="${zeromq_dir}:${CMAKE_PREFIX_PATH}"
  export PKG_CONFIG_PATH="${zeromq_dir}/lib/pkgconfig:${zeromq_dir}/lib64/pkgconfig${PKG_CONFIG_PATH:+:${PKG_CONFIG_PATH}}"
  export LD_LIBRARY_PATH="${zeromq_dir}/lib:${zeromq_dir}/lib64:${LD_LIBRARY_PATH}"
fi

{
  echo "export PATH=\"${cmake_dir}/bin:${cuda_dir}/bin:\${PATH}\""
  if [[ -d "${zeromq_dir}" ]]; then
    echo "export LD_LIBRARY_PATH=\"${cuda_dir}/lib64:${zeromq_dir}/lib:${zeromq_dir}/lib64\${LD_LIBRARY_PATH:+:\${LD_LIBRARY_PATH}}\""
    echo "export CMAKE_PREFIX_PATH=\"${cuda_dir}:${zeromq_dir}\${CMAKE_PREFIX_PATH:+:\${CMAKE_PREFIX_PATH}}\""
    echo "export PKG_CONFIG_PATH=\"${zeromq_dir}/lib/pkgconfig:${zeromq_dir}/lib64/pkgconfig\${PKG_CONFIG_PATH:+:\${PKG_CONFIG_PATH}}\""
  else
    echo "export LD_LIBRARY_PATH=\"${cuda_dir}/lib64\${LD_LIBRARY_PATH:+:\${LD_LIBRARY_PATH}}\""
    echo "export CMAKE_PREFIX_PATH=\"${cuda_dir}\${CMAKE_PREFIX_PATH:+:\${CMAKE_PREFIX_PATH}}\""
  fi
} > "${tools_dir}/env.sh"

echo "tools_env=${tools_dir}/env.sh"
cmake --version | head -n1
nvcc --version | tail -n4
pkg-config --modversion libzmq
nvidia-smi | sed -n '1,4p'
REMOTE
