"""External integration assets remain usable after directory relocation."""

import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


def patch_entries(value):
    if isinstance(value, dict):
        if str(value.get("path", "")).endswith(".patch"):
            yield value
        for child in value.values():
            yield from patch_entries(child)
    elif isinstance(value, list):
        for child in value:
            yield from patch_entries(child)


class IntegrationAssetsTest(unittest.TestCase):
    def test_all_locked_patches_resolve_and_match(self):
        count = 0
        for lock in (ROOT / "integrations").rglob("*.lock.json"):
            for entry in patch_entries(json.loads(lock.read_text())):
                with self.subTest(lock=lock.name, patch=entry["path"]):
                    content = (ROOT / entry["path"]).read_bytes()
                    self.assertEqual(hashlib.sha256(content).hexdigest(), entry["sha256"])
                    count += 1
        self.assertGreater(count, 0)

    def test_oai_loaders_resolve_locks_outside_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                ["bash", "-c", 'source "$1"; printf "%s\\n" "$OAI_LOCAL_PIN" "${#OAI_LOCAL_ZMQ_PATCHES[@]}"',
                 "check", str(ROOT / "scripts/native/oai-local-patches.sh")],
                cwd=directory, capture_output=True, text=True, check=True,
            )
        expected = json.loads((ROOT / "integrations/oai/oai-local-patches.lock.json").read_text())
        self.assertEqual(result.stdout.splitlines(),
                         [expected["oai_commit"], str(len(expected["artifacts"]["zmq_module"]["patches"]))])
        path = ROOT / "scripts/native/build-oai-zmq-patched.py"
        spec = importlib.util.spec_from_file_location("oai_build_assets", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        lock, digest = module.load_lock()
        self.assertEqual(lock["oai_commit"], expected["oai_commit"])
        self.assertEqual(len(digest), 64)
