#!/usr/bin/env python3
"""Build the patched OAI ZMQ radio module the OAI gates load by default.

The pinned OAI tree stays untouched: the radio/zmq sources are copied to a
scratch directory, the patches that oai-local-patches.lock.json lists for the
zmq_module artifact are applied there, and the one shared object is compiled and linked with the exact flags
and link line of the pinned release build (builds/oai-zmq-release). The result
goes to builds/oai-zmq-patched with the release build's other modules
symlinked next to it, plus a manifest.json the gates verify before use. Only
the default patches are built in; the lock's `optional` entries are
measurement knobs that check-oai-local-patches.sh verifies but nothing builds.

Why the module is patched at all: SPARK_MILESTONES.md S9 (the stock driver
paces a lock-step run to ~0.3x real time).

usage: OCUDU_NATIVE_ROOT=<workspace> build-oai-zmq-patched.py [--verify]
  --verify  check an existing build against the current lock and module bytes
            (what the gates run); exit 1 with the reason if it is stale.
"""
import datetime
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent
LOCK = REPO_ROOT / 'integrations/oai/oai-local-patches.lock.json'
MODULE = 'liboai_zmqdevif.so'


def sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as source:
        for chunk in iter(lambda: source.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def die(message):
    sys.exit('error: ' + message)


def read_flags(path):
    flags = {}
    compiler = None
    for line in path.read_text().splitlines():
        match = re.match(r'# compile CXX with (\S+)', line)
        if match:
            compiler = match.group(1)
        match = re.match(r'(CXX_DEFINES|CXX_INCLUDES|CXX_FLAGS) = (.*)', line)
        if match:
            flags[match.group(1)] = shlex.split(match.group(2))
    if compiler is None or set(flags) != {'CXX_DEFINES', 'CXX_INCLUDES', 'CXX_FLAGS'}:
        die('unexpected flags.make layout: ' + str(path))
    return compiler, flags


def load_lock():
    """The zmq_module section with the pin, and the digest a build records.

    The digest covers only this artifact's section and the pin, so editing the
    UE patch list does not invalidate a ZMQ module built from the same inputs.
    """
    full = json.loads(LOCK.read_text())
    lock = dict(full['artifacts']['zmq_module'])
    lock['oai_commit'] = full['oai_commit']
    digest = hashlib.sha256(json.dumps(lock, sort_keys=True).encode()).hexdigest()
    return lock, digest


def toolchain_env(root):
    """The environment build-oai-ue.sh compiled the release build under.

    build-oai-ue.sh sources env.sh, whose CPATH/LIBRARY_PATH point at the
    workspace sysroot (simde, libconfig, ...). flags.make does not carry those
    include directories, so the module must be compiled under the same
    environment or its headers are not found (x86 workstation) or resolve
    differently. Falls back to the caller's environment where env.sh does not
    apply (a workspace without the sysroot components).
    """
    env = dict(os.environ, OCUDU_NATIVE_ROOT=str(root))
    probe = subprocess.run(['bash', '-c', 'source "$1" >/dev/null 2>&1 && env -0', 'env', str(SCRIPT_DIR / 'env.sh')],
                           env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    if probe.returncode != 0:
        return env, 'caller'
    sourced = dict(item.split('=', 1) for item in probe.stdout.decode().split('\0') if '=' in item)
    return sourced, 'env.sh'


def verify(root, lock, lock_digest):
    out = root / lock['output_dir']
    manifest_path = out / 'manifest.json'
    if not manifest_path.is_file():
        die(f'no patched OAI ZMQ module at {out}; run scripts/native/build-oai-zmq-patched.py')
    manifest = json.loads(manifest_path.read_text())
    if manifest.get('lock_section_sha256') != lock_digest:
        die(f'{out} was built from another zmq_module entry in oai-local-patches.lock.json; rebuild it')
    # The patch files themselves, not just the lock that names them: a patch
    # edited without a lock update must stop the gate, not load stale bytes.
    for entry in lock['patches']:
        actual = sha256(REPO_ROOT / entry['path'])
        if actual != entry['sha256']:
            die(f'{entry["path"]} sha256 {actual} does not match the lock {entry["sha256"]}')
    built = [(e['path'], e['sha256']) for e in manifest.get('patches', [])]
    if built != [(e['path'], e['sha256']) for e in lock['patches']]:
        die(f'{out} was built with patches {built}, the lock lists others; rebuild it')
    if manifest.get('module_sha256') != sha256(out / MODULE):
        die(f'{out / MODULE} does not match its manifest')
    print('oai_zmq_module=' + str(out / MODULE))
    print('module_sha256=' + manifest['module_sha256'])
    return 0


def main():
    root = Path(os.environ.get('OCUDU_NATIVE_ROOT', '')).resolve()
    if not root.is_dir() or str(root) == '/':
        die('OCUDU_NATIVE_ROOT must name the native workspace')
    lock, lock_digest = load_lock()
    if sys.argv[1:] == ['--verify']:
        return verify(root, lock, lock_digest)
    if sys.argv[1:]:
        die('usage: build-oai-zmq-patched.py [--verify]')
    src = root / 'src/oai'
    base = root / lock['base_build_dir']
    out = root / lock['output_dir']
    head = subprocess.check_output(['git', '-C', str(src), 'rev-parse', 'HEAD'], text=True).strip()
    if head != lock['oai_commit']:
        die(f'OAI checkout {head} is not the pinned {lock["oai_commit"]}')
    target_dir = base / 'radio/zmq/CMakeFiles' / (lock['target'] + '.dir')
    flags_make, link_txt = target_dir / 'flags.make', target_dir / 'link.txt'
    for path in (flags_make, link_txt, base / MODULE):
        if not path.is_file():
            die('the release OAI build is incomplete (run build-oai-ue.sh first): missing ' + str(path))

    patches = []
    for entry in lock['patches']:
        path = REPO_ROOT / entry['path']
        actual = sha256(path)
        if actual != entry['sha256']:
            die(f'{entry["path"]} sha256 {actual} does not match the lock')
        touched = re.findall(r'^\+\+\+ b/(\S+)', path.read_text(), flags=re.M)
        if sorted(touched) != sorted(entry['files']):
            die(f'{entry["path"]} touches {touched}, the lock says {entry["files"]}')
        patches.append((path, entry))

    compiler, flags = read_flags(flags_make)
    build_env, build_env_source = toolchain_env(root)
    scratch = Path(tempfile.mkdtemp(prefix='oai-zmq-patched-'))
    try:
        shutil.copytree(src / 'radio/zmq', scratch / 'radio/zmq')
        for path, _ in patches:
            subprocess.run(['patch', '-p1', '--forward', '--batch', '-d', str(scratch), '-i', str(path)],
                           check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        source = scratch / 'radio/zmq/zmq_radio.cpp'
        obj = scratch / 'zmq_radio.cpp.o'
        # The patched directory goes first so a patched header would win; the
        # release include list (which names the pristine radio/zmq) follows.
        compile_cmd = ([compiler] + flags['CXX_DEFINES'] + ['-I' + str(scratch / 'radio/zmq')]
                       + flags['CXX_INCLUDES'] + flags['CXX_FLAGS']
                       # Debug info names the pinned tree, not the scratch copy,
                       # so two builds of the same lock give the same bytes.
                       + ['-ffile-prefix-map=%s=%s' % (scratch, src)]
                       + ['-o', str(obj), '-c', str(source)])
        subprocess.run(compile_cmd, check=True, cwd=base / 'radio/zmq', env=build_env)

        stage = out.with_name(out.name + '.tmp')
        shutil.rmtree(stage, ignore_errors=True)
        stage.mkdir(parents=True)
        link = shlex.split(link_txt.read_text().strip())
        release_obj = 'CMakeFiles/%s.dir/zmq_radio.cpp.o' % lock['target']
        release_out = '../../' + MODULE
        if link.count(release_obj) != 1 or link.count(release_out) != 1:
            die('unexpected link.txt layout: ' + str(link_txt))
        link = [str(obj) if arg == release_obj else str(stage / MODULE) if arg == release_out else arg
                for arg in link]
        subprocess.run(link, check=True, cwd=base / 'radio/zmq', env=build_env)
        linked = []
        for lib in sorted(base.glob('lib*.so')):
            if lib.name != MODULE:
                (stage / lib.name).symlink_to(lib)
                linked.append(lib.name)
        compiler_version = subprocess.check_output([compiler, '--version'], text=True).splitlines()[0]
        manifest = {
            'schema': 'ocudu-native-oai-zmq-module/v1',
            'lock_section_sha256': lock_digest,
            'oai_commit': head,
            'patches': [{'path': e['path'], 'sha256': e['sha256']} for _, e in patches],
            'module_sha256': sha256(stage / MODULE),
            'release_module_sha256': sha256(base / MODULE),
            'flags_make_sha256': sha256(flags_make),
            'link_txt_sha256': sha256(link_txt),
            'compiler': compiler_version,
            'toolchain_env': build_env_source,
            'machine': platform.machine(),
            'symlinked_from_release': linked,
            'built_utc': datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ'),
        }
        (stage / 'manifest.json').write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n')
        shutil.rmtree(out, ignore_errors=True)
        stage.rename(out)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    print('oai_zmq_module=' + str(out / MODULE))
    print('module_sha256=' + manifest['module_sha256'])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
