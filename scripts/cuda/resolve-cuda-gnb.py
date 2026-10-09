#!/usr/bin/env python3
"""Resolve and audit the CUDA gNB build, without touching the shared lock.

Fails loudly rather than letting a run silently proceed on a stale build or an
altered source tree: the whole point of naming an alternative gNB binary to the
gate is that the binary is what the lock says it is.
"""
from __future__ import annotations
import argparse, ctypes, hashlib, json, os, re, subprocess, sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
# Another platform's lock (e.g. cuda-workspace.spark.lock.json) can be selected
# with OCUDU_CUDA_WORKSPACE_LOCK; unset, this is the RTX 5090 lock as before.
LOCK = Path(os.environ.get('OCUDU_CUDA_WORKSPACE_LOCK', str(REPO / 'integrations/ocudu/cuda-workspace.lock.json')))


def git(root: Path, *args: str) -> bytes:
    return subprocess.check_output(['git', '-C', str(root), *args])


def resolve(root: Path, hardware: bool = False) -> dict:
    lock = json.loads(LOCK.read_text())
    source, build = lock['source'], lock['build']
    src = root / source['path']

    if git(src, 'rev-parse', 'HEAD').decode().strip() != source['commit']:
        raise ValueError('CUDA source revision does not match the lock')
    if git(src, 'ls-files', '--others', '--exclude-standard'):
        raise ValueError('CUDA source contains untracked files')
    patch_path = REPO / source['patch']['path']
    expected = patch_path.read_bytes()
    if hashlib.sha256(expected).hexdigest() != source['patch']['sha256']:
        raise ValueError('locked patch checksum mismatch')
    # The locked patch carries 7-hex index lines. git's automatic abbreviation grows
    # with the object count (a clone holding every WG1 branch prints 10), which
    # would fail a byte comparison of identical content, so pin it.
    if git(src, '-c', 'core.abbrev=7', 'diff', '--binary', 'HEAD') != expected:
        raise ValueError('CUDA source differs from the locked patch')

    build_path = root / build['build_dir']
    binary = build_path / 'apps/gnb/gnb'
    cache = {}
    for line in (build_path / 'CMakeCache.txt').read_text().splitlines():
        if '=' in line and ':' in line and not line.startswith(('//', '#')):
            key, value = line.split('=', 1)
            cache[key.split(':', 1)[0]] = value
    if cache.get('ENABLE_CUDA') != 'ON':
        raise ValueError('build is not a CUDA build')
    if cache.get('CMAKE_CUDA_ARCHITECTURES') != build['cuda_architectures']:
        raise ValueError('build architecture does not match the lock')
    if cache.get('ENABLE_ZEROMQ') != 'ON':
        raise ValueError('build lacks ZMQ')
    if Path(cache.get('CMAKE_HOME_DIRECTORY', '')).resolve() != src.resolve():
        raise ValueError('build/source path mismatch')

    version = subprocess.check_output([str(binary), '--version'], text=True)
    # The build hash is `git rev-parse --short`, so its length follows the same
    # automatic abbreviation as above (7 on the workstation, 10 on the Spark clone).
    if not re.search(r'OCUDU 5G gNB version .*\(' + source['commit'][:7] + r'[0-9a-f]*\)', version):
        raise ValueError('gNB binary revision mismatch')
    if 'GPU acceleration:' not in subprocess.check_output([str(binary), '--help'], text=True):
        raise ValueError('gNB binary lacks CUDA support')

    data = dict(binary=str(binary), source=str(src), build=str(build_path),
                commit=source['commit'], patch=source['patch'],
                binary_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),
                cuda_architectures=cache['CMAKE_CUDA_ARCHITECTURES'])
    if hardware:
        data['gpu_driver_inventory'] = subprocess.check_output(
            ['nvidia-smi', '--query-gpu=index,uuid,name,driver_version', '--format=csv,noheader'], text=True).strip()
        runtime = ctypes.CDLL('libcudart.so.12')
        value = ctypes.c_int()
        if runtime.cudaRuntimeGetVersion(ctypes.byref(value)) != 0:
            raise ValueError('CUDA runtime version query failed')
        data['cuda_runtime_version'] = value.value
    return data


def resolve_cpu_baseline(root: Path) -> dict:
    """The non-CUDA gNB named by the lock's optional cpu_baseline entry, audited the same way."""
    base = json.loads(LOCK.read_text()).get('cpu_baseline')
    if not base:
        raise ValueError('lock has no cpu_baseline entry')
    src, build_path = root / base['path'], root / base['build_dir']
    if git(src, 'rev-parse', 'HEAD').decode().strip() != base['commit']:
        raise ValueError('CPU baseline source revision does not match the lock')
    if git(src, 'status', '--porcelain', '--untracked-files=no'):
        raise ValueError('CPU baseline source tree is modified')
    cache = {}
    for line in (build_path / 'CMakeCache.txt').read_text().splitlines():
        if '=' in line and ':' in line and not line.startswith(('//', '#')):
            key, value = line.split('=', 1)
            cache[key.split(':', 1)[0]] = value
    if cache.get('ENABLE_CUDA') == 'ON':
        raise ValueError('CPU baseline build is a CUDA build')
    if cache.get('ENABLE_ZEROMQ') != 'ON':
        raise ValueError('build lacks ZMQ')
    if Path(cache.get('CMAKE_HOME_DIRECTORY', '')).resolve() != src.resolve():
        raise ValueError('build/source path mismatch')
    binary = build_path / 'apps/gnb/gnb'
    version = subprocess.check_output([str(binary), '--version'], text=True)
    if not re.search(r'OCUDU 5G gNB version .*\(' + base['commit'][:7] + r'[0-9a-f]*\)', version):
        raise ValueError('gNB binary revision mismatch')
    return dict(binary=str(binary), source=str(src), build=str(build_path), commit=base['commit'],
                binary_sha256=hashlib.sha256(binary.read_bytes()).hexdigest())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--fields', action='store_true')
    parser.add_argument('--hardware', action='store_true')
    parser.add_argument('--cpu-baseline', action='store_true', help='resolve the lock\'s non-CUDA gNB instead')
    args = parser.parse_args()
    data = resolve_cpu_baseline(args.root) if args.cpu_baseline else resolve(args.root, args.hardware)
    print('\t'.join(data[k] for k in ('binary', 'source', 'build', 'commit'))
          if args.fields else json.dumps(data, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        raise SystemExit(f'CUDA gNB resolution failed: {error}')
