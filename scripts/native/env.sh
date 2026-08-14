#!/usr/bin/env bash
# Source this file to run the rootless native OCUDU/Open5GS/srsUE toolchain.
# It intentionally contains no package installation or privileged operation.

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  echo "source scripts/native/env.sh instead of executing it" >&2
  exit 2
fi

OCUDU_NATIVE_ROOT="${OCUDU_NATIVE_ROOT:-/home/ubuntu/ocudu-native-workspace}"
OCUDU_NATIVE_SYSROOT="${OCUDU_NATIVE_ROOT}/install/sysroot"
OCUDU_NATIVE_GNUTLS="${OCUDU_NATIVE_ROOT}/install/gnutls-3.7.3"
OCUDU_NATIVE_BISON="${OCUDU_NATIVE_ROOT}/install/bison-3.8.2"

for native_path in "${OCUDU_NATIVE_ROOT}" "${OCUDU_NATIVE_SYSROOT}" \
                   "${OCUDU_NATIVE_GNUTLS}" "${OCUDU_NATIVE_BISON}"; do
  [[ -d "${native_path}" ]] || {
    echo "missing native workspace component: ${native_path}" >&2
    return 2
  }
done

export OCUDU_NATIVE_ROOT OCUDU_NATIVE_SYSROOT OCUDU_NATIVE_GNUTLS \
       OCUDU_NATIVE_BISON
export PATH="${OCUDU_NATIVE_BISON}/bin:${OCUDU_NATIVE_SYSROOT}/usr/bin:${PATH}"
export PYTHONPATH="${OCUDU_NATIVE_SYSROOT}/usr/lib/python3/dist-packages${PYTHONPATH:+:${PYTHONPATH}}"
export CPATH="${OCUDU_NATIVE_GNUTLS}/include:${OCUDU_NATIVE_SYSROOT}/usr/include/libmongoc-1.0:${OCUDU_NATIVE_SYSROOT}/usr/include/libbson-1.0:${OCUDU_NATIVE_SYSROOT}/usr/include:${OCUDU_NATIVE_SYSROOT}/usr/include/x86_64-linux-gnu${CPATH:+:${CPATH}}"
export LIBRARY_PATH="${OCUDU_NATIVE_GNUTLS}/lib:${OCUDU_NATIVE_SYSROOT}/usr/lib/x86_64-linux-gnu${LIBRARY_PATH:+:${LIBRARY_PATH}}"
export LD_LIBRARY_PATH="${OCUDU_NATIVE_GNUTLS}/lib:${OCUDU_NATIVE_SYSROOT}/usr/lib/x86_64-linux-gnu${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}"
export PKG_CONFIG_PATH="${OCUDU_NATIVE_GNUTLS}/lib/pkgconfig:${OCUDU_NATIVE_SYSROOT}/usr/lib/x86_64-linux-gnu/pkgconfig:${OCUDU_NATIVE_SYSROOT}/usr/share/pkgconfig${PKG_CONFIG_PATH:+:${PKG_CONFIG_PATH}}"

# The custom GnuTLS pkg-config file has an absolute user prefix. A sysroot
# rewrite would incorrectly prepend the sysroot to that prefix.
unset PKG_CONFIG_SYSROOT_DIR
