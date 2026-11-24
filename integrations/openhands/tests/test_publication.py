import importlib.util
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch


class PublicationTests(unittest.TestCase):
    def test_tool_layer_contains_the_declared_shared_mount_in_guest_root(self):
        path = Path(__file__).resolve().parents[1] / 'prepare_tools.py'
        if not path.exists():
            self.skipTest('operator publication helper is only in the source release')
        spec = importlib.util.spec_from_file_location('prepare_openhands_tools', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            interpreter = root / 'python'
            (interpreter / 'bin').mkdir(parents=True)
            (interpreter / 'bin/python').write_text('prepared interpreter')
            dependencies = root / 'dependencies'
            dependencies.mkdir()
            (dependencies / 'package.py').write_text('prepared dependency')
            def publish(archive, output, **kwargs):
                with tarfile.open(archive) as stream:
                    names = set(stream.getnames())
                    self.assertIn('opt/dsec-openhands', names)
                    self.assertIn('opt/dsec-openhands/python/bin/python', names)
                    self.assertIn('opt/dsec-openhands/site-packages/package.py', names)
                    self.assertEqual(stream.getmember('opt/dsec-openhands').mode, 0o755)
                    self.assertNotIn('python', names)
                return dict(path=str(output))
            with patch.object(sys, 'argv', ['prepare_tools', '--python-root', str(interpreter),
                    '--dependencies', str(dependencies), '--out', str(root / 'tools.erofs')]), \
                    patch.object(module, 'build_tar_layer', side_effect=publish) as build:
                module.main()
                self.assertEqual(build.call_count, 1)
