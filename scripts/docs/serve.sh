#!/usr/bin/env bash
# Build the documentation site and serve it on this machine.
#
# The site is generated from the Markdown in docs/ by Sphinx, so it needs the
# pinned Python 3.12 environment in docs/requirements.txt (the same Python as
# CI). This script creates that environment in .venv-docs (using uv to fetch
# Python 3.12 when python3.12 is not installed), rebuilds .build/docs/html and
# serves it on 127.0.0.1. Run it from any directory; stop with Ctrl-C.
#
# Usage: scripts/docs/serve.sh [--port N] [--build-only]
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/../.." && pwd)"
venv="${repo_root}/.venv-docs"
html="${repo_root}/.build/docs/html"
requirements="${repo_root}/docs/requirements.txt"
port=19492
serve=1

usage() { sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --port)
      if [[ $# -lt 2 || ! "$2" =~ ^[0-9]+$ ]]; then
        echo "--port needs a numeric value" >&2; exit 2
      fi
      port="$2"; shift 2 ;;
    --build-only) serve=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

is_py312() { "$1" -c 'import sys; sys.exit(sys.version_info[:2] != (3, 12))' 2>/dev/null; }

find_uv() {
  if command -v uv >/dev/null 2>&1; then
    command -v uv
    return 0
  fi
  # Self-contained fallback inside the ignored build directory.
  local tools="${repo_root}/.build/docs/uv"
  if [[ ! -x "${tools}/bin/uv" ]]; then
    echo "Installing uv into ${tools} to provision Python 3.12..." >&2
    if ! python3 -m pip install --quiet --disable-pip-version-check --target "$tools" uv >&2; then
      echo "Could not install uv. Install python3.12 or uv (https://docs.astral.sh/uv/), then rerun." >&2
      exit 1
    fi
  fi
  echo "${tools}/bin/uv"
}

if [[ ! -x "${venv}/bin/python" ]] || ! is_py312 "${venv}/bin/python"; then
  rm -rf "$venv"
  if command -v python3.12 >/dev/null 2>&1 && is_py312 python3.12; then
    echo "Creating ${venv} with $(command -v python3.12)"
    python3.12 -m venv "$venv"
  else
    uv="$(find_uv)"
    echo "python3.12 not found; creating ${venv} with uv-managed Python 3.12"
    "$uv" venv --quiet --python 3.12 --seed "$venv"
  fi
fi

# Reinstall only when the pinned requirements change.
stamp="${venv}/.requirements.sha256"
want="$("${venv}/bin/python" -c 'import hashlib, sys; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())' "$requirements")"
if [[ "$(cat "$stamp" 2>/dev/null)" != "$want" ]]; then
  echo "Installing documentation requirements"
  "${venv}/bin/python" -m pip install --quiet --disable-pip-version-check -r "$requirements"
  echo "$want" > "$stamp"
fi

echo "Building ${html}"
"${venv}/bin/python" -m sphinx -b html -n -W -q "${repo_root}/docs" "$html"

if [[ "$serve" -eq 1 ]]; then
  echo "Serving http://127.0.0.1:${port}/README.html (Ctrl-C to stop)"
  exec "${venv}/bin/python" -m http.server "$port" --bind 127.0.0.1 --directory "$html"
fi
