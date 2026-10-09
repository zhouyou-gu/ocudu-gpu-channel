"""Working-tree uploads omit local artifacts and protect remote exclusions."""

from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
EXCLUDES = ROOT / "scripts/remote/rsync-excludes.txt"


@unittest.skipUnless(shutil.which("rsync"), "rsync is required")
class RemoteSyncTest(unittest.TestCase):
    def test_upload_and_delete_boundaries(self):
        included = [
            "CMakeLists.txt", "src/channel.cpp", "apps/new_untracked.py",
            "scripts/native/build-helper.sh", "use_cases/configs/build_profile.yaml",
            "use_cases/results/expected.csv", "apps/dashboard/vendor/library.js",
            "docs/report.md", "tests/fixture.json", ".config.example",
        ]
        excluded = [
            ".git/config", ".config", ".agents/skill.md", ".claude/settings.json",
            ".playwright-mcp/screenshot.png", "references/paper.pdf", "writing/main.tex",
            "build/app", "build-cuda/app", "out/app", "cmake-build-debug/app",
            "Testing/Temporary/result.xml", ".build/docs/index.html", ".venv-docs/bin/python",
            "ocudu/source.cpp", "srsRAN_4G/source.cpp", "srsran_4g/source.cpp",
            "results/report.json", "artifacts/iq.bin", "datasets/input.bin", "tmp/notes.md",
            "dist/package.whl", "apps/__pycache__/module.pyc", "apps/module.pyc",
            "tests/.pytest_cache/nodeids", "apps/.mypy_cache/state.json",
            "apps/.ruff_cache/state", "apps/package.egg-info/PKG-INFO",
            "nested/output.log", "nested/capture.pcap", "nested/capture.pcapng",
            "nested/.DS_Store",
        ]
        with tempfile.TemporaryDirectory() as directory:
            source, dest = (Path(directory) / name for name in ("source", "dest"))
            for base in (source, dest):
                base.mkdir()
            for name in included + excluded:
                path = source / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("local\n")
            # Existing excluded artifacts must survive --delete without updates.
            for name in excluded:
                path = dest / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("remote\n")
            (dest / "references/remote-only.pdf").write_text("retain\n")
            (dest / "obsolete.cpp").write_text("remove\n")
            subprocess.run([
                "rsync", "-az", "--delete", f"--exclude-from={EXCLUDES}",
                f"{source}/", f"{dest}/",
            ], check=True, capture_output=True)
            for name in included:
                self.assertEqual((dest / name).read_text(), "local\n", name)
            for name in excluded:
                self.assertEqual((dest / name).read_text(), "remote\n", name)
            self.assertTrue((dest / "references/remote-only.pdf").is_file())
            self.assertFalse((dest / "obsolete.cpp").exists())
            # An empty destination must receive none of the excluded material.
            clean = Path(directory) / "clean"
            subprocess.run([
                "rsync", "-az", "--delete", f"--exclude-from={EXCLUDES}",
                f"{source}/", f"{clean}/",
            ], check=True, capture_output=True)
            actual = {p.relative_to(clean).as_posix() for p in clean.rglob("*") if p.is_file()}
            self.assertEqual(actual, set(included))


if __name__ == "__main__":
    unittest.main()
