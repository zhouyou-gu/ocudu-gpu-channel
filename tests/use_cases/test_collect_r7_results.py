#!/usr/bin/env python3
"""Fixture-only archive coverage; no radios, SSH, or GPU required."""
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[2] / 'use_cases/robot_fight/collect_r7_results.py'
spec = importlib.util.spec_from_file_location('collector', SCRIPT)
collector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)


class CollectorTests(unittest.TestCase):
    def test_failed_runs_artifacts_exclusions_and_empty_fights(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            output = base / 'output'
            output.mkdir()
            native = base / 'native'
            stamp = '20260930T072455Z'
            logs = native / 'results/logs/ocudu-robot-fight' / stamp
            config = native / 'configs/ocudu-robot-fight-native' / stamp
            report = native / 'results/reports/ocudu-robot-fight' / stamp
            for root in (logs, config, report):
                root.mkdir(parents=True)
            (report / 'run-parameters.json').write_text('{}')
            (logs / 'fights/f001').mkdir(parents=True)
            (logs / 'fights/f002').mkdir()
            (logs / 'fights/f001/arena.json').write_text('{}')
            for filename in ('gnb0-console.log', 'broker-a.log', 'probe-ue0.jsonl',
                             'srsue-metrics-ue0.csv', 'srsue-ue0.start_unix_ms',
                             'srsue-ue0.log', 'srsue-ue0-internal.log', 'sionna-status-a.jsonl'):
                (logs / filename).write_text('fixture')
            for filename in ('gnb0.yaml', 'scenario-a.json', 'topology-a.yaml',
                             'robot-fight-shape.json', 'subscriber.csv'):
                (config / filename).write_text('fixture')
            outside = base / 'secret'
            outside.write_text('must not collect')
            (config / 'gnb1.yaml').symlink_to(outside)
            (logs / 'fights/f003').symlink_to(base, target_is_directory=True)
            (output / 'keep-failed.out').write_text(f'config={config}/gnb0.yaml\nR7_EXIT=2\n')
            (output / 'keep-no-metadata.out').write_text('early error\nR7_EXIT=1\n')
            (output / 'keep-running.out').write_text('still running\n')
            (output / 'other.out').write_text('R7_EXIT=0\n')
            stream = io.BytesIO()
            manifests = collector.collect(output, native, 'keep-', stream)
            self.assertEqual([m['exit_code'] for m in manifests], [2, 1])
            self.assertTrue(manifests[0]['missing_metadata'])
            self.assertTrue(manifests[1]['missing_metadata'])
            stream.seek(0)
            with tarfile.open(fileobj=stream) as archive:
                names = archive.getnames()
                for suffix in ('runner.out', 'collection.json', 'report/run-parameters.json',
                               'config/gnb0.yaml', 'config/topology-a.yaml',
                               'config/scenario-a.json', 'config/robot-fight-shape.json',
                               'probe-ue0.jsonl', 'srsue-metrics-ue0.csv',
                               'srsue-ue0.start_unix_ms', 'fights/f001/arena.json', 'fights/f002'):
                    self.assertIn('keep-failed/' + suffix, names)
                self.assertTrue(archive.getmember('keep-failed/fights/f002').isdir())
                self.assertIn('keep-no-metadata/runner.out', names)
                for name in names:
                    self.assertTrue(collector.safe_name(name))
                    self.assertNotIn('subscriber', name)
                    self.assertNotIn('internal', name)
                    self.assertNotIn('sionna-status', name)
                    self.assertNotIn('gnb1.yaml', name)
                    self.assertNotIn('f003', name)
                self.assertFalse(any(m.issym() or m.islnk() for m in archive.getmembers()))
                info = json.load(archive.extractfile('keep-failed/collection.json'))
                self.assertEqual(info['timestamp'], stamp)

    def test_completed_success_and_literal_prefix(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'a[1].out').write_text('R7_EXIT=0\n')
            (root / 'a1.out').write_text('R7_EXIT=4\n')
            results = collector.collect(root, root / 'native', 'a[', io.BytesIO())
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]['exit_code'], 0)


if __name__ == '__main__':
    unittest.main()
