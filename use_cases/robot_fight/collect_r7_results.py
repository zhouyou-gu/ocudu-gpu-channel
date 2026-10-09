#!/usr/bin/env python3
"""Stream selected completed R7 runs as a small-artifact tar archive on stdout.

Run on the machine holding the logs, e.g. via SSH with this script on stdin and
redirect stdout to a local .tar. This collector never starts or modifies runs.
--prefix selects runner filename stems literally. Failed R7_EXIT runs are kept.
Only an allowlist is collected: no subscriber configs, IQ data, internal UE
logs, Sionna status streams, or symlinks. Each tag gets collection.json recording
missing metadata. Archives preserve empty incomplete fight directories.
"""
from __future__ import annotations

import argparse
import io
import json
from pathlib import Path, PurePosixPath
import re
import sys
import tarfile

STAMP = r'\d{8}T\d{6}Z'
STAMP_PATH = re.compile(r'(?:results/(?:reports|logs)/ocudu-robot-fight|'
                        r'configs/ocudu-robot-fight-native|run/ocudu-robot-fight-native)/('
                        + STAMP + r')(?=/|[\s\\"\']|$)')
EXIT = re.compile(r'^R7_EXIT=(\d+)\s*$', re.MULTILINE)
CONFIGS = ('gnb0.yaml', 'gnb1.yaml', 'topology-*.yaml', 'scenario-*.json',
           'robot-fight-shape.json')
LOGS = ('root-exec.log', 'ue-exec-*.log', 'gnb*-console.log',
        'gnb*-dryrun.log', 'srsue-ue[0-9]*.log', 'broker-*.log',
        'probe-*.jsonl', 'srsue-metrics-*.csv', 'srsue-*.start_unix_ms',
        'ue-ping-*.log', 'hog.log', 'render.log',
        'fights/summary.jsonl', 'fights/f[0-9]*/*.json')


def safe_path(path: Path, root: Path) -> bool:
    """Require containment and reject symlinks, including parent symlinks."""
    try:
        relative = path.relative_to(root)
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    current = root
    if current.is_symlink():
        return False
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return False
    return True


def safe_name(name: str) -> bool:
    path = PurePosixPath(name)
    return not path.is_absolute() and '..' not in path.parts and '\\' not in name


def add_json(archive, name, data):
    payload = (json.dumps(data, indent=2, sort_keys=True) + '\n').encode()
    entry = tarfile.TarInfo(name)
    entry.size = len(payload)
    entry.mode = 0o644
    archive.addfile(entry, io.BytesIO(payload))


def collect(output_root: Path, native_root: Path, prefix: str, stream) -> list[dict]:
    output_root = output_root.absolute()
    native_root = native_root.absolute()
    manifests = []
    with tarfile.open(fileobj=stream, mode='w|', dereference=False) as archive:
        for runner in sorted(output_root.glob('*.out')):
            tag = runner.stem
            if not tag.startswith(prefix) or not safe_name(tag) or not safe_path(runner, output_root):
                continue
            text = runner.read_text(errors='replace')
            exits = EXIT.findall(text)
            if not exits:
                continue
            manifest = {'tag': tag, 'exit_code': int(exits[-1]), 'timestamp': None,
                        'missing_metadata': [], 'skipped': [], 'members': []}
            seen = set()

            def add(path, root, relative):
                name = f'{tag}/{relative}'
                if name in seen:
                    return
                if not safe_name(name) or not safe_path(path, root):
                    manifest['skipped'].append(str(path))
                    return
                if not path.is_file() and not path.is_dir():
                    return
                archive.add(path, arcname=name, recursive=False)
                seen.add(name)
                manifest['members'].append(name)

            add(runner, output_root, 'runner.out')
            stamps = STAMP_PATH.findall(text)
            if not stamps:
                manifest['missing_metadata'].append('timestamp: no recognized native run path in runner output')
            else:
                stamp = stamps[-1]
                manifest['timestamp'] = stamp
                report = native_root / 'results/reports/ocudu-robot-fight' / stamp
                logs = native_root / 'results/logs/ocudu-robot-fight' / stamp
                configs = native_root / 'configs/ocudu-robot-fight-native' / stamp
                for required in (report / 'attach-summary.json', report / 'run-parameters.json'):
                    if not safe_path(required, native_root) or not required.is_file():
                        manifest['missing_metadata'].append(str(required))
                    else:
                        try:
                            json.loads(required.read_text())
                        except (ValueError, OSError) as exc:
                            manifest['missing_metadata'].append(f'{required}: {exc}')
                for root, patterns, destination in ((report, ('*.json',), 'report/'),
                                                      (configs, CONFIGS, 'config/'),
                                                      (logs, LOGS, '')):
                    if not root.is_dir() or not safe_path(root, native_root):
                        manifest['missing_metadata'].append(str(root))
                        continue
                    for pattern in patterns:
                        for path in sorted(root.glob(pattern)):
                            # Broad numeric UE globs must not include internal logs.
                            if 'subscriber' in path.name or '-internal' in path.name:
                                continue
                            if path.is_file():
                                add(path, native_root, destination + path.relative_to(root).as_posix())
                    if root == logs:
                        for fight in sorted((logs / 'fights').glob('f[0-9]*')):
                            if fight.is_dir():
                                add(fight, native_root, 'fights/' + fight.name)
            add_json(archive, f'{tag}/collection.json', manifest)
            manifests.append(manifest)
    return manifests


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-root', type=Path, default=Path('/workspace/gpuch/r7-validation-results'))
    parser.add_argument('--native-root', type=Path, default=Path('/workspace/ocudu-spark'))
    parser.add_argument('--prefix', default='', help='literal runner-tag prefix; empty selects all completed runs')
    args = parser.parse_args()
    if not args.output_root.is_dir():
        parser.error('output-root must be an existing directory')
    manifests = collect(args.output_root, args.native_root, args.prefix, sys.stdout.buffer)
    for item in manifests:
        print(json.dumps({key: item[key] for key in ('tag', 'exit_code', 'timestamp', 'missing_metadata', 'skipped')}), file=sys.stderr)
    print(f'collected {len(manifests)} completed runs', file=sys.stderr)


if __name__ == '__main__':
    main()
