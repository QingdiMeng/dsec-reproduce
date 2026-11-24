import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from dsec_swegym_case.repository import publish_baseline_git, verify_baseline_git


class BaselineGitTests(unittest.TestCase):
    def git(self, repo, *args):
        return subprocess.check_output(['git', '-C', str(repo), '-c', 'user.name=Test',
            '-c', 'user.email=test@example.invalid', '-c', 'commit.gpgsign=false', *args]).decode().strip()

    def test_future_packed_loose_tagged_and_unreachable_objects_are_absent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); repo = root/'repo';repo.mkdir()
            self.git(repo, 'init', '-q')
            (repo/'history.txt').write_text('past\n')
            self.git(repo,'add','.');self.git(repo,'commit','-qm','ancestor')
            ancestor=self.git(repo,'rev-parse','HEAD')
            (repo/'source.py').write_text('original\n')
            self.git(repo, 'add', '.');self.git(repo, 'commit', '-qm', 'baseline')
            base = self.git(repo, 'rev-parse', 'HEAD')
            (repo/'source.py').write_text('GOLD_FUTURE_FIX\n')
            self.git(repo, 'commit', '-qam', 'future repair')
            future = self.git(repo, 'rev-parse', 'HEAD')
            future_blob = self.git(repo, 'rev-parse', 'HEAD:source.py')
            self.git(repo, 'tag', '-am', 'future answer tag', 'answer')
            self.git(repo, 'gc', '--quiet')
            self.git(repo, 'reset', '--hard', base)
            loose = subprocess.check_output(['git','-C',str(repo),'hash-object','-w','--stdin'],input=b'UNREACHABLE_ANSWER').decode().strip()
            self.git(repo, 'remote', 'add', 'origin', 'https://example.invalid/repo.git')
            published = root/'published.git'
            report = publish_baseline_git(repo, base, published)
            self.assertEqual(report['commits'], 2)
            self.assertEqual(report, verify_baseline_git(published, base))
            for oid in [future, future_blob, loose]:
                result=subprocess.run(['git','--git-dir='+str(published),'cat-file','-e',oid],capture_output=True)
                self.assertNotEqual(result.returncode, 0)
            clean = root/'clean';clean.mkdir();(clean/'source.py').write_text('original\n');(clean/'history.txt').write_text('past\n')
            shutil.copytree(published,clean/'.git')
            self.assertEqual(self.git(clean,'status','--porcelain'),'')
            self.assertEqual(self.git(clean,'log','--all','--format=%H'),base+'\n'+ancestor)
            for args in [('log','--format=raw',base),('show','--full-index',base),
                         ('blame','--line-porcelain',base,'--','history.txt')]:
                self.assertEqual(self.git(repo,*args),self.git(clean,*args))
            self.assertEqual(self.git(clean,'rev-parse','HEAD^'),ancestor)
            self.assertFalse((clean/'.git/shallow').exists())
            (clean/'source.py').write_text('legitimate fix\n')
            self.assertIn('+legitimate fix',self.git(clean,'diff','HEAD'))
            self.assertIn(future,self.git(repo,'rev-list','--all'))
            # Even an unreachable loose blob added after publication invalidates it.
            subprocess.run(['git','--git-dir='+str(published),'hash-object','-w','--stdin'],input=b'EXTRA',check=True,capture_output=True)
            with self.assertRaisesRegex(ValueError,'outside'):
                verify_baseline_git(published,base)

    def test_alternates_or_wrong_baseline_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);repo=root/'repo';repo.mkdir()
            self.git(repo,'init','-q');(repo/'a').write_text('a')
            self.git(repo,'add','.');self.git(repo,'commit','-qm','base')
            base=self.git(repo,'rev-parse','HEAD')
            with self.assertRaisesRegex(ValueError,'differs'):
                publish_baseline_git(repo,'0'*40,root/'wrong')
            (repo/'.git/objects/info/alternates').write_text('/some/object/store\n')
            with self.assertRaisesRegex(ValueError,'another'):
                publish_baseline_git(repo,base,root/'alternate')
            (repo/'.git/objects/info/alternates').unlink()
            (repo/'.git/shallow').write_text(base+'\n')
            with self.assertRaisesRegex(ValueError,'complete ancestry'):
                publish_baseline_git(repo,base,root/'shallow')

    def test_merge_ancestors_and_history_queries_are_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);repo=root/'repo';repo.mkdir()
            self.git(repo,'init','-q');(repo/'a').write_text('root\n')
            self.git(repo,'add','.');self.git(repo,'commit','-qm','root')
            self.git(repo,'checkout','-qb','side');(repo/'side').write_text('historical side branch\n')
            self.git(repo,'add','.');self.git(repo,'commit','-qm','side ancestor')
            side=self.git(repo,'rev-parse','HEAD')
            self.git(repo,'checkout','-qb','baseline-branch','HEAD^');(repo/'a').write_text('main\n')
            self.git(repo,'commit','-qam','main ancestor')
            self.git(repo,'merge','--no-ff','side','-m','baseline merge')
            base=self.git(repo,'rev-parse','HEAD')
            published=root/'published.git';report=publish_baseline_git(repo,base,published)
            self.assertEqual(report['commits'],4)
            self.assertEqual(self.git(repo,'rev-list',base),
                subprocess.check_output(['git','--git-dir='+str(published),'rev-list',base]).decode().strip())
            self.assertEqual(subprocess.check_output(['git','--git-dir='+str(published),'rev-parse','HEAD^2']).decode().strip(),side)
