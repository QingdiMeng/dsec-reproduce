"""Application identity and artifact registration fail before publishing drift."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dsec_tb21_case.cli import COMMIT, register, stage, validate_suite


def digest(data):
    return hashlib.sha256(data).hexdigest()


class CaseTests(unittest.TestCase):
    def fixture(self, root):
        repo = root/'repo'
        for i in range(89):
            name = 'example' if i == 0 else 'task-'+str(i)
            directory = repo/'tasks'/name
            (directory/'tests').mkdir(parents=True)
            (directory/'task.toml').write_text(
                '[environment]\ndocker_image="example:1"\ncpus=1\nmemory_mb=512\n'
                '[verifier]\ntimeout_sec=30\n')
            (directory/'instruction.md').write_text('Do the task')
            (directory/'tests/test.sh').write_text('exit 0')
        (repo/'tasks/example/solution').mkdir()
        (repo/'tasks/example/solution/solve.sh').write_text('oracle')
        return repo

    def git(self, arguments, **kwargs):
        if arguments[-2:] == ['rev-parse', 'HEAD']:
            return COMMIT+'\n'
        if arguments[-3:] == ['remote', 'get-url', 'origin']:
            return 'https://github.com/harbor-framework/terminal-bench-2-1.git\n'
        return ''

    def staged(self, root):
        repo = self.fixture(root)
        with patch('dsec_tb21_case.cli.subprocess.check_output', side_effect=self.git):
            stage(repo, root/'suite', ['example'])
        return root/'suite'

    def test_stage_excludes_oracle_and_checks_all_task_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            suite = self.staged(root)
            self.assertFalse((suite/'tasks/example/solution').exists())
            self.assertEqual(validate_suite(suite)[1]['task_count'], 1)
            (suite/'tasks/example/instruction.md').write_text('changed instruction')
            with self.assertRaisesRegex(ValueError, 'Pinned task file changed'):
                validate_suite(suite)

    def test_wrong_revision_and_dirty_sources_do_not_publish(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self.fixture(root)
            with patch('dsec_tb21_case.cli.subprocess.check_output', return_value='bad'):
                with self.assertRaisesRegex(ValueError, 'revision'):
                    stage(repo, root/'suite', ['example'])
            with patch('dsec_tb21_case.cli.subprocess.check_output',
                       side_effect=[COMMIT+'\n', ' M tasks/example/instruction.md\n']):
                with self.assertRaisesRegex(ValueError, 'changes'):
                    stage(repo, root/'suite', ['example'])
            self.assertFalse((root/'suite').exists())

    def test_added_file_and_oracle_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            suite = self.staged(Path(tmp))
            extra = suite/'tasks/example/extra.txt'
            extra.write_text('added')
            with self.assertRaisesRegex(ValueError, 'added or removed'):
                validate_suite(suite)
            extra.unlink()
            (suite/'tasks/example/solution').mkdir()
            with self.assertRaisesRegex(ValueError, 'oracle'):
                validate_suite(suite)

    def test_resource_drift_and_changed_layers_do_not_register(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            suite = self.staged(root)
            for name in ('boot', 'kernel', 'layer'):
                (root/name).write_bytes(name.encode())
            entry = {'backend':'microvm', 'rootfs':'erofs_layers', 'cpus':2, 'memory_mb':512,
                     'boot_template':str(root/'boot'), 'boot_sha256':digest(b'boot'),
                     'kernel':str(root/'kernel'), 'kernel_sha256':digest(b'kernel'),
                     'layers':[{'name':'base','file':str(root/'layer'), 'sha256':digest(b'layer')}]}
            catalog = root/'source-catalog.json'
            catalog.write_text(json.dumps({'format':1,'environments':{'tb2-example':entry}}))
            with self.assertRaisesRegex(ValueError, 'resources differ'):
                register(suite, catalog, root/'catalog.json')
            entry['cpus'] = 1
            catalog.write_text(json.dumps({'format':1,'environments':{'tb2-example':entry}}))
            (root/'layer').write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError, 'changed'):
                register(suite, catalog, root/'catalog.json')
            self.assertFalse((root/'catalog.json').exists())

    def test_registration_reuses_paths_and_does_not_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            suite = self.staged(root)
            for name in ('boot', 'kernel', 'layer'):
                (root/name).write_bytes(name.encode())
            entry = {'backend':'microvm', 'rootfs':'erofs_layers', 'cpus':1, 'memory_mb':512,
                     'boot_template':str(root/'boot'), 'boot_sha256':digest(b'boot'),
                     'kernel':str(root/'kernel'), 'kernel_sha256':digest(b'kernel'),
                     'layers':[{'name':'base','file':str(root/'layer'), 'sha256':digest(b'layer')}]}
            catalog = root/'source-catalog.json'
            catalog.write_text(json.dumps({'format':1,'environments':{'tb2-example':entry}}))
            report = register(suite, catalog, root/'catalog.json')
            self.assertEqual(report['copied_artifact_bytes'], 0)
            self.assertEqual(json.loads((root/'catalog.json').read_text())['environments']['tb2-example'], entry)
            with self.assertRaises(FileExistsError):
                register(suite, catalog, root/'catalog.json')


if __name__ == '__main__':
    unittest.main()
