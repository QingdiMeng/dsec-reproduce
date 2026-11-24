import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from dsec_swegym_case.patch import CAPTURE_SOURCE


class PatchCaptureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.repository('agent')
        self.patch = self.root / 'solution.patch'

    def git(self, root, *args):
        return subprocess.run(['git', *args], cwd=root, check=True,
                              capture_output=True).stdout

    def repository(self, name):
        root = self.root / name
        root.mkdir()
        self.git(root, 'init', '-q')
        (root / 'source.py').write_text('original\n')
        (root / '.gitignore').write_text('cache/\n')
        self.git(root, 'add', '.')
        self.git(root, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                 '-c', 'commit.gpgsign=false', 'commit', '-qm', 'baseline')
        return root

    def capture(self):
        result = subprocess.run([sys.executable, '-c', CAPTURE_SOURCE, str(self.patch)],
                                cwd=self.repo, check=True, capture_output=True, text=True)
        return json.loads(result.stdout)

    def test_unfinished_and_complete_nested_repositories_do_not_block_tracked_patch(self):
        for name in ('unfinished', 'complete'):
            nested = self.repo / name
            nested.mkdir()
            self.git(nested, 'init', '-q')
            (nested / 'unrelated.py').write_text('not part of parent repository\n')
            if name == 'complete':
                self.git(nested, 'add', '.')
                self.git(nested, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                         '-c', 'commit.gpgsign=false', 'commit', '-qm', 'nested')
        (self.repo / 'source.py').write_text('fixed\n')
        report = self.capture()
        self.assertEqual({item['path'] for item in report['skipped']}, {'unfinished/', 'complete/'})
        clean = self.repository('verifier')
        self.git(clean, 'apply', str(self.patch))
        self.assertEqual((clean / 'source.py').read_text(), 'fixed\n')
        self.assertNotIn('unrelated.py', self.patch.read_text())

    def test_new_files_with_literal_pathspec_names_and_symlinks_are_preserved(self):
        name = ':(glob)* odd\nfile.py'
        (self.repo / name).write_text('new source\n')
        (self.repo / 'link').symlink_to(name)
        report = self.capture()
        self.assertEqual(report, dict(added_intent_files=2, skipped=[]))
        clean = self.repository('verifier')
        self.git(clean, 'apply', str(self.patch))
        self.assertEqual((clean / name).read_text(), 'new source\n')
        self.assertEqual(os.readlink(clean / 'link'), name)

    def test_ignored_files_are_not_added_and_empty_candidate_is_valid(self):
        (self.repo / 'cache').mkdir()
        (self.repo / 'cache/download.bin').write_bytes(b'ignored')
        self.assertEqual(self.capture(), dict(added_intent_files=0, skipped=[]))
        self.assertEqual(self.patch.read_bytes(), b'')

    def test_symlink_outside_repository_does_not_copy_target_contents(self):
        outside = self.root / 'outside'
        outside.write_text('DO_NOT_COPY_CONTENTS\n')
        (self.repo / 'link').symlink_to(outside)
        self.capture()
        self.assertNotIn('DO_NOT_COPY_CONTENTS', self.patch.read_text())
        self.assertIn('120000', self.patch.read_text())

    def test_git_failure_is_not_converted_to_an_empty_successful_patch(self):
        result = subprocess.run([sys.executable, '-c', CAPTURE_SOURCE, str(self.patch)],
                                cwd=self.root, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.patch.exists())
