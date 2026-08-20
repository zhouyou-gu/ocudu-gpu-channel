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

# --- autotools relocation (M6.1) -------------------------------------------
# Debian's autoconf/automake/libtool bake absolute /usr/share paths into their
# scripts, so extracting them into the user-space sysroot is not enough: the
# tools must be told where they were actually put. Three files carry paths that
# have no environment override and are patched in place in the sysroot
# (`aclocal-1.16` search dirs, `autom4te.cfg` --prepend-include, `libtoolize`
# prefix/datadir/pkgauxdir/pkgltdldir/aclocaldir); everything else is settable
# here. OAI needs this because its build compiles asn1c from source through
# `autoreconf -i` (`CMakeLists.txt` ExternalProject `asn1c_gen`).
if [[ -x "${OCUDU_NATIVE_SYSROOT}/usr/bin/autoreconf" ]]; then
  export PERL5LIB="${OCUDU_NATIVE_SYSROOT}/usr/share/autoconf:${OCUDU_NATIVE_SYSROOT}/usr/share/automake-1.16${PERL5LIB:+:${PERL5LIB}}"
  export AUTOMAKE_LIBDIR="${OCUDU_NATIVE_SYSROOT}/usr/share/automake-1.16"
  export ACLOCAL_PATH="${OCUDU_NATIVE_SYSROOT}/usr/share/aclocal"
  export AUTOM4TE_CFG="${OCUDU_NATIVE_SYSROOT}/usr/share/autoconf/autom4te.cfg"
  export autom4te_perllibdir="${OCUDU_NATIVE_SYSROOT}/usr/share/autoconf"
  export trailer_m4="${OCUDU_NATIVE_SYSROOT}/usr/share/autoconf/autoconf/trailer.m4"
  export AUTOM4TE="${OCUDU_NATIVE_SYSROOT}/usr/bin/autom4te"
  export AUTOCONF="${OCUDU_NATIVE_SYSROOT}/usr/bin/autoconf"
  export AUTOHEADER="${OCUDU_NATIVE_SYSROOT}/usr/bin/autoheader"
  export AUTOMAKE="${OCUDU_NATIVE_SYSROOT}/usr/bin/automake"
  export ACLOCAL="${OCUDU_NATIVE_SYSROOT}/usr/bin/aclocal"
  export LIBTOOLIZE="${OCUDU_NATIVE_SYSROOT}/usr/bin/libtoolize"
  export M4="${OCUDU_NATIVE_SYSROOT}/usr/bin/m4"
fi
