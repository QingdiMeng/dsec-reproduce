"""Publish baseline and ancestor Git objects without changing source files.

Deleting refs is insufficient: unreachable objects, reflogs, alternates and pack
delta bases can still disclose a future repair. Build a fresh database from an
explicit commit/tree/blob allowlist, preserving the original baseline commit ID.
No repository hooks, task commands, clone/fetch or network access are used.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile


def git(directory, *args, **kwargs):
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL='/dev/null',
               GIT_TERMINAL_PROMPT='0', GIT_ALLOW_PROTOCOL='file', GIT_NO_REPLACE_OBJECTS='1')
    return subprocess.run(['git', '--no-pager', '--git-dir=' + str(directory),
        '-c', 'core.hooksPath=/dev/null', '-c', 'pack.threads=1', *args],
        env=env, check=True, stderr=subprocess.PIPE, **kwargs)


def output(directory, *args):
    return git(directory, *args, stdout=subprocess.PIPE).stdout.decode().strip()


def verify_baseline_git(directory, base):
    directory = Path(directory).resolve(strict=True)
    if not re.fullmatch('[a-f0-9]{40}', base):
        raise ValueError('Exact baseline commit required')
    if any((directory / name).exists() for name in
           ['objects/info/alternates', 'objects/info/http-alternates', 'commondir', 'logs', 'shallow', 'info/grafts']):
        raise ValueError('Alternate/shared object stores, shallow boundaries, grafts and reflogs are forbidden')
    rows = output(directory, 'cat-file', '--batch-all-objects',
                  '--batch-check=%(objectname) %(objecttype)').splitlines()
    objects = dict(row.split() for row in rows)
    commits = {name for name, kind in objects.items() if kind == 'commit'}
    ancestors=set(output(directory, 'rev-list', base).splitlines())
    if commits != ancestors or set(objects.values()) - {'commit', 'tree', 'blob'}:
        raise ValueError('Git database contains commits outside baseline ancestry or unsupported objects')
    tree = output(directory, 'rev-parse', base + '^{tree}')
    allowed = set(output(directory, 'rev-list', '--objects', '--no-object-names', base).splitlines())
    if set(objects) != allowed:
        raise ValueError('Git database contains objects outside baseline ancestry')
    if output(directory, 'show-ref') != base + ' refs/heads/master':
        raise ValueError('Only the baseline branch may be published')
    if output(directory, 'remote') or output(directory, 'rev-parse', 'HEAD') != base:
        raise ValueError('Offline baseline HEAD required')
    git(directory, 'fsck', '--full', '--no-reflogs', stdout=subprocess.PIPE)
    digest = hashlib.sha256(('\n'.join(sorted(objects)) + '\n').encode()).hexdigest()
    return dict(schema='dsec.swegym.git-baseline.v2', base_commit=base, tree=tree,
                objects=len(objects), commits=len(commits), object_set_sha256=digest,
                commit_set_sha256=hashlib.sha256(('\n'.join(sorted(commits))+'\n').encode()).hexdigest(),
                history='baseline_and_all_ancestors')


def publish_baseline_git(repository, base, destination):
    repository = Path(repository).resolve(strict=True)
    source = repository / '.git'
    destination = Path(destination).absolute()
    if not re.fullmatch('[a-f0-9]{40}', base):
        raise ValueError('Exact baseline commit required')
    if source.is_symlink() or not source.is_dir() or destination.exists() or destination.is_symlink():
        raise ValueError('Conventional source .git and fresh destination required')
    if any((source / name).exists() for name in
           ['objects/info/alternates', 'objects/info/http-alternates', 'commondir', 'shallow', 'info/grafts']):
        raise ValueError('Source requires complete ancestry without another object store, shallow boundary or grafts')
    if output(source, 'rev-parse', 'HEAD') != base:
        raise ValueError('Prepared repository HEAD differs from baseline')
    allowed = sorted(set(output(source, 'rev-list', '--objects', '--no-object-names', base).splitlines()))
    destination.mkdir(mode=0o700, parents=True)
    git(destination, 'init', '--bare', '--quiet', '--template=', stdout=subprocess.PIPE)
    with tempfile.TemporaryFile() as pack:
        # Recompute deltas only among this explicit ancestry allowlist. No thin
        # pack or reused future delta base can enter the fresh object store.
        git(source, 'pack-objects', '--stdout', '--no-reuse-delta', '--no-reuse-object',
            '--window=10', '--depth=50', '--window-memory=128m', input=('\n'.join(allowed) + '\n').encode(), stdout=pack)
        pack.seek(0)
        git(destination, 'index-pack', '--stdin', stdin=pack, stdout=subprocess.PIPE)
    git(destination, 'config', 'core.logAllRefUpdates', 'false')
    git(destination, 'update-ref', 'refs/heads/master', base)
    git(destination, 'symbolic-ref', 'HEAD', 'refs/heads/master')
    git(destination, 'config', 'core.bare', 'false')
    git(destination, '--work-tree=' + str(repository), 'read-tree', base)
    report = verify_baseline_git(destination, base)
    if report['object_set_sha256'] != hashlib.sha256(('\n'.join(allowed) + '\n').encode()).hexdigest():
        raise ValueError('Published Git allowlist differs from source baseline')
    return report


VERIFY_SOURCE = '''import json, subprocess, hashlib, sys
from pathlib import Path
gitdir = Path('/testbed/.git')
import os
os.environ['GIT_NO_REPLACE_OBJECTS']='1'
assert not any((gitdir / n).exists() for n in ['objects/info/alternates', 'objects/info/http-alternates', 'commondir', 'logs', 'shallow', 'info/grafts'])
def git(*args):
    return subprocess.check_output(['git', '-C', '/testbed', *args], text=True).strip()
base, expected_tree, expected_objects, expected_commits = sys.argv[1:]
rows = git('cat-file', '--batch-all-objects', '--batch-check=%(objectname) %(objecttype)').splitlines()
objects = dict(r.split() for r in rows)
commits={k for k, v in objects.items() if v == 'commit'}
assert commits == set(git('rev-list', base).splitlines())
assert set(objects) == set(git('rev-list', '--objects', '--no-object-names', base).splitlines())
commit_digest=hashlib.sha256(('\\n'.join(sorted(commits))+'\\n').encode()).hexdigest()
assert commit_digest == expected_commits
digest = hashlib.sha256(('\\n'.join(sorted(objects)) + '\\n').encode()).hexdigest()
assert digest == expected_objects
assert git('rev-parse', 'HEAD') == base
assert git('rev-parse', 'HEAD^{tree}') == expected_tree
assert git('show-ref') == base + ' refs/heads/master'
assert not git('remote')
assert not git('status', '--porcelain')
print(json.dumps(dict(base_commit=base, tree=expected_tree, object_set_sha256=digest, objects=len(objects), commits=len(commits), commit_set_sha256=commit_digest, history='baseline_and_all_ancestors', clean_worktree=True)))
'''


def verification_command(policy):
    for key, length in [('base_commit', 40), ('tree', 40), ('object_set_sha256', 64), ('commit_set_sha256', 64)]:
        if not isinstance(policy.get(key), str) or not re.fullmatch('[a-f0-9]{'+str(length)+'}', policy[key]):
            raise ValueError('Pinned baseline Git policy required')
    return 'python -c ' + shlex.quote(VERIFY_SOURCE) + ' ' + ' '.join(
        shlex.quote(policy[key]) for key in ('base_commit', 'tree', 'object_set_sha256', 'commit_set_sha256'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repository', type=Path, required=True)
    parser.add_argument('--base-commit', required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(publish_baseline_git(args.repository, args.base_commit, args.out)))
