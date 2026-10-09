"""Relocating fixtures must preserve the native gates' content checks."""

from pathlib import Path
import os
import re
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


class NativeFixturePinsTest(unittest.TestCase):
    def test_rank1_checks_accept_relocation_and_reject_content_changes(self):
        for gate in ("run-ocudu-rank1-2x1.sh", "run-ocudu-rank1-4x1.sh"):
            with self.subTest(gate=gate), tempfile.TemporaryDirectory() as directory:
                source = (ROOT / "scripts/native" / gate).read_text()
                end = source.index('  usage_error "pre-MIMO legacy fixture or driver changed"')
                start = source.rfind("printf '%s  %s", 0, end)
                self.assertGreaterEqual(start, 0, "gate must pin content independently of old paths")
                check = source[start:end] + '  exit 1\n'
                paths = re.findall(r'"\$\{repo_root\}/([^"\n]+)"', check)
                self.assertEqual(len(paths), 4)
                for relative in paths:
                    target = Path(directory) / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(ROOT / relative, target)
                env = dict(os.environ, repo_root=directory)
                self.assertEqual(subprocess.run(["bash", "-c", check], env=env).returncode, 0)
                for relative in paths:
                    target = Path(directory) / relative
                    original = target.read_bytes()
                    target.write_bytes(original + b"\n# changed\n")
                    self.assertNotEqual(subprocess.run(["bash", "-c", check], env=env).returncode, 0)
                    target.write_bytes(original)
