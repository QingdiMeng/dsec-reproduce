"""Validate the actual archive, including its reproducibility and boundary."""
import hashlib
import json
from pathlib import Path
import tarfile
import tempfile
import unittest

from tools.package_release_source import package, SECRET
from tools.check_docs import check


class SourceReleaseTests(unittest.TestCase):
    def test_reproducible_archive_has_only_pinned_sources_and_license(self):
        root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as tmp:
            a, b = Path(tmp)/'a.tar.gz', Path(tmp)/'b.tar.gz'
            package(root, a)
            package(root, b)
            self.assertEqual(a.read_bytes(), b.read_bytes())
            with tarfile.open(a) as archive:
                manifest = json.load(archive.extractfile('SOURCE_MANIFEST.json'))
                self.assertEqual(set(archive.getnames()), set(manifest['files'])|{'SOURCE_MANIFEST.json'})
                for name, digest in manifest['files'].items():
                    self.assertEqual(hashlib.sha256(archive.extractfile(name).read()).hexdigest(), digest)
                    self.assertFalse(name.startswith(('experiments/', 'results/', '.runtime/')))
                for name in ('LICENSE', 'licenses/miles-APACHE-2.0.txt', 'guest_agent.c',
                             'docs/guides/QUICKSTART.md', 'apps/tb21/pyproject.toml',
                             'apps/mbpp/pyproject.toml', 'apps/mbpp/src/dsec_mbpp_case/reward.py'):
                    self.assertIn(name, manifest['files'])
                for name in ('src/dsec/contracts/resources.py', 'src/dsec/storage/overlaybd.py',
                             'src/dsec/sdk/sandbox_transport.py',
                             'src/dsec/contracts/evaluation.py',
                             'src/dsec/contracts/sandbox.py', 'src/dsec/sdk/client.py',
                             'src/dsec/runtime/container_edge.py', 'src/dsec/control/server.py',
                             'apps/tb21/src/dsec_tb21_case/worker_evaluator.py',
                             'tests/unit/test_worker_evaluators.py',
                             'tests/unit/test_container_edge.py',
                             'tests/contracts/v01_compatibility.json'):
                    self.assertIn(name, manifest['files'])
                self.assertFalse(any('miles_lora_nvme' in n or n.endswith('.ext4')
                                     for n in manifest['files']))
                # Validate the exported tree, which has fewer files than the
                # development workspace. A link must work for a new checkout.
                exported = Path(tmp)/'exported'
                for name in manifest['files']:
                    target = exported/name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(archive.extractfile(name).read())
                self.assertEqual(check(exported), [])
            with self.assertRaises(FileExistsError):
                package(root, a)

    def test_secret_guard_recognizes_tokens_and_private_keys(self):
        for value in (b'-----BEGIN OPENSSH '+b'PRIVATE KEY-----',
                      b'sk-'+b'a'*30, b'hf_'+b'b'*30):
            self.assertIsNotNone(SECRET.search(value))
        self.assertIsNone(SECRET.search(b'export API_KEY="$API_KEY"'))


if __name__ == '__main__':
    unittest.main()
