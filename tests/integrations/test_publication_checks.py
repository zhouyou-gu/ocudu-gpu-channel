"""Public copies must redact only declared metadata and reject text disclosures."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('publication_checks', ROOT / 'scripts/docs/publication_checks.py')
checks = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checks)


class PublicationChecksTest(unittest.TestCase):
    def test_private_path_and_process_text_fail_without_echoing_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'record.md'
            identity = 'personal-review-fixture'
            path.write_text('/home/' + identity + '/results\n' + 'user' + '-approved decision\n')
            errors = checks.text_issues(path)
            self.assertEqual(errors, ['1: personal home path', '2: conversation-specific narration'])
            self.assertNotIn(identity, str(errors))
            self.assertEqual(checks.text_issues(path, governance=True), ['1: personal home path'])
            path.write_text('${HOME}/results\n/home/dev/workspace\nthread handoff\nuser namespace\n')
            self.assertEqual(checks.text_issues(path), [])

    def test_public_evidence_manifest_is_valid(self):
        inventory = json.loads((ROOT / 'docs/_compat/inventory.json').read_text())
        expected, errors = checks.evidence_hashes(ROOT, inventory)
        self.assertEqual(errors, [])
        self.assertEqual(len(expected), 71)

    def test_measurements_and_undeclared_fields_cannot_change(self):
        fields = [{'pointer': '/runs/0/path'}]
        original = {'runs': [{'path': '/private/location', 'latency_ms': 12.5}], 'status': 'failed'}
        public = {'runs': [{'path': 'results/run', 'latency_ms': 12.5}], 'status': 'failed'}
        digest = checks.evidence_payload_digest(original, fields)
        self.assertEqual(checks.evidence_payload_digest(public, fields), digest)
        public['runs'][0]['latency_ms'] = 12.4
        self.assertNotEqual(checks.evidence_payload_digest(public, fields), digest)
        public['runs'][0]['latency_ms'] = 12.5
        public['status'] = 'passed'
        self.assertNotEqual(checks.evidence_payload_digest(public, fields), digest)

    def test_manifest_rejects_changed_paths_and_measurements(self):
        source = json.loads((ROOT / 'docs/_compat/redactions.json').read_text())['files'][0]
        inventory = {'files': [{'kind': 'evidence', 'destination': source['path'],
                                'sha256': source['original_sha256']}]}
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            manifest = repo / 'docs/_compat/redactions.json'
            manifest.parent.mkdir(parents=True)
            manifest.write_text(json.dumps({'files': [source]}))
            artifact = repo / source['path']
            artifact.parent.mkdir(parents=True, exist_ok=True)
            content = (ROOT / source['path']).read_text()
            artifact.write_text(content)
            self.assertEqual(checks.evidence_hashes(repo, inventory)[1], [])
            data = json.loads(content)
            data['_unexpected_measurement'] = 1
            artifact.write_text(json.dumps(data))
            self.assertTrue(checks.evidence_hashes(repo, inventory)[1])
            artifact.write_text(content.replace('results/robot-fight/', 'results/changed/', 1))
            self.assertTrue(checks.evidence_hashes(repo, inventory)[1])


if __name__ == '__main__':
    unittest.main()
