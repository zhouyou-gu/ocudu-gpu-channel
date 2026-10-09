"""Configuration rejection must not silently cancel the remaining sweep."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[2] / 'use_cases/robot_fight/launch/run_r7_probe_sweep.sh'


class SweepTests(unittest.TestCase):
    def run_sweep(self, marker):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        launch = root / 'use_cases/robot_fight/launch'
        launch.mkdir(parents=True)
        shutil.copyfile(SOURCE, launch / SOURCE.name)
        (launch / 'run_r7_spark.sh').write_text('''#!/usr/bin/env bash
echo "$1" >> "$R7_OUTPUT_ROOT/calls"
if [[ "$1" == *-k2 ]]; then
''' + ('echo R7_EXIT=2 > "$R7_OUTPUT_ROOT/$1.out"\nexit 2\n' if marker else 'exit 3\n') + '''fi
echo R7_EXIT=0 > "$R7_OUTPUT_ROOT/$1.out"
''')
        env = dict(os.environ, R7_OUTPUT_ROOT=str(root / 'out'), R7_PROBE_CASES='k2,sr40')
        result = subprocess.run(['bash', str(launch / SOURCE.name), 'trial'], env=env, text=True, capture_output=True)
        return result, (root / 'out/calls').read_text().splitlines()

    def test_native_rejection_retained_and_next_condition_runs(self):
        result, calls = self.run_sweep(True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, ['trial-k2', 'trial-sr40'])
        self.assertIn('exit=2', result.stdout)
        self.assertIn('SWEEP_DONE', result.stdout)

    def test_gpu_refusal_stops_without_attempting_next_condition(self):
        result, calls = self.run_sweep(False)
        self.assertEqual(result.returncode, 3)
        self.assertEqual(calls, ['trial-k2'])
        self.assertNotIn('SWEEP_DONE', result.stdout)


if __name__ == '__main__':
    unittest.main()
