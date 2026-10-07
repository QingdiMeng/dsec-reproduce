"""Explicit staging and artifact registration for an optional TB2.1 application."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import tomllib

from environment_catalog import MicroVMEnvironmentCatalog

COMMIT = '7131e4375048a0e408a8fb404b5f499d726b695b'
TASK_ID = re.compile(r'[a-z0-9][a-z0-9.-]{0,127}\Z')
IMAGE_ID = re.compile(r'sha256:[a-f0-9]{64}\Z')


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def write_new(path, payload):
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.tb21-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)  # never replace a concurrently published file
    finally:
        Path(temporary).unlink(missing_ok=True)


def stage(repo, output, tasks=None):
    repo = Path(repo).resolve(strict=True)
    output = Path(output).resolve()
    if output.exists():
        raise FileExistsError('Refusing to replace a staged suite')
    commit = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip()
    if commit != COMMIT:
        raise ValueError('Source checkout does not match the pinned TB2.1 revision')
    source = repo/'tasks'
    available = {p.name:p for p in source.iterdir() if (p/'task.toml').is_file()}
    if len(available) != 89:
        raise ValueError('The pinned TB2.1 checkout must contain 89 tasks')
    selected = sorted(available if tasks is None else tasks)
    if not selected or len(set(selected)) != len(selected) or any(
            not TASK_ID.fullmatch(t) or t not in available for t in selected):
        raise ValueError('Invalid, repeated or unknown task selection')
    dirty = subprocess.check_output(
        ['git', '-C', str(repo), 'status', '--porcelain', '--untracked-files=all', '--',
         *(str(Path('tasks')/t) for t in selected)], text=True)
    if dirty:
        raise ValueError('Selected source tasks contain tracked or untracked changes')
    origin = subprocess.check_output(['git', '-C', str(repo), 'remote', 'get-url', 'origin'],
                                     text=True).strip()
    manifest = {'benchmark_id':'terminal-bench-2.1', 'source_commit':commit,
                'source_repository':origin, 'source_task_count':89,
                'task_count':len(selected), 'tasks':{}}
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.tb21-stage-', dir=output.parent) as temp:
        staging = Path(temp)/'suite'
        (staging/'tasks').mkdir(parents=True)
        for task in selected:
            task_source = available[task]
            if task_source.is_symlink() or any(p.is_symlink() for p in task_source.rglob('*')):
                raise ValueError('Task symlinks are not permitted in the staged suite')
            destination = staging/'tasks'/task
            shutil.copytree(task_source, destination, ignore=shutil.ignore_patterns(
                'solution', '.git', '__pycache__', '*.pyc'))
            for relative in ('instruction.md', 'task.toml', 'tests/test.sh'):
                if not (destination/relative).is_file():
                    raise ValueError('Incomplete task: '+task)
            settings = tomllib.loads((destination/'task.toml').read_text())
            env = settings['environment']
            files = {str(p.relative_to(destination)):sha(p)
                     for p in destination.rglob('*') if p.is_file()}
            manifest['tasks'][task] = {
                'docker_image':env['docker_image'], 'cpus':env.get('cpus', 1),
                'memory_mb':env.get('memory_mb', 2048), 'gpus':env.get('gpus', 0),
                'task_toml_sha256':files['task.toml'], 'files':files}
        write_new(staging/'manifest.json', manifest)
        # Atomic directory publication; an existing destination remains an error.
        if output.exists():
            raise FileExistsError(output)
        os.rename(staging, output)
    return {'status':'passed', 'suite':str(output), 'tasks':selected, 'source_commit':commit}


def validate_suite(suite):
    suite = Path(suite).resolve(strict=True)
    manifest = json.loads((suite/'manifest.json').read_text())
    if (manifest.get('benchmark_id') != 'terminal-bench-2.1' or
            manifest.get('source_commit') != COMMIT or
            manifest.get('task_count') != len(manifest.get('tasks', {})) or not manifest['tasks']):
        raise ValueError('Invalid pinned TB2.1 suite identity')
    for task, spec in manifest['tasks'].items():
        if not TASK_ID.fullmatch(task):
            raise ValueError('Invalid task identity')
        root = suite/'tasks'/task
        if root.is_symlink() or any(p.is_symlink() for p in root.rglob('*')) or (root/'solution').exists():
            raise ValueError('Task contains a symlink or oracle solution')
        files = spec.get('files')
        if not isinstance(files, dict) or not {'instruction.md', 'task.toml', 'tests/test.sh'} <= files.keys():
            raise ValueError('Application staging requires full task-file digest pins')
        actual = {str(p.relative_to(root)):p for p in root.rglob('*') if p.is_file()}
        if actual.keys() != files.keys():
            raise ValueError('Task files added or removed: '+task)
        for name, path in actual.items():
            if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()) or sha(path) != files[name]:
                raise ValueError('Pinned task file changed: '+task+'/'+name)
        settings = tomllib.loads((root/'task.toml').read_text())['environment']
        if any(spec.get(k) != settings.get(k, default) for k, default in
               (('docker_image', None), ('cpus', 1), ('memory_mb', 2048), ('gpus', 0))):
            raise ValueError('Task resource or image mapping differs')
        if spec.get('task_toml_sha256') != files['task.toml']:
            raise ValueError('Task configuration pin differs')
    return suite, manifest


def register(suite, catalog_path, output):
    suite, manifest = validate_suite(suite)
    catalog = MicroVMEnvironmentCatalog(catalog_path)
    result = {'format':1, 'environments':{}}
    for task, spec in manifest['tasks'].items():
        environment_id = 'tb2-'+task
        entry = catalog.entries.get(environment_id)
        if (spec.get('gpus', 0) != 0 or entry is None or entry.get('cpus') != spec['cpus']
                or entry.get('memory_mb') != spec['memory_mb']):
            raise ValueError('Environment or resources differ from task: '+task)
        catalog.resolve(environment_id, 'local')
        result['environments'][environment_id] = copy.deepcopy(entry)
    write_new(output, result)
    return {'status':'passed', 'task_count':len(result['environments']),
            'source_commit':COMMIT, 'catalog':str(Path(output).resolve()),
            'catalog_sha256':sha(output), 'copied_artifact_bytes':0}


def prepare_image(args):
    from dsec_image.prepare import prepare
    suite, manifest = validate_suite(args.suite)
    if args.task not in manifest['tasks']:
        raise ValueError('Task is not staged')
    spec = manifest['tasks'][args.task]
    original_id = subprocess.check_output(
        ['docker', 'image', 'inspect', spec['docker_image'], '--format', '{{.Id}}'], text=True).strip()
    if args.image != original_id:
        raise ValueError('Provided image differs from the staged task Docker image')
    if spec.get('gpus', 0) != 0:
        raise ValueError('Standalone builder supports CPU tasks only')
    settings = tomllib.loads((suite/'tasks'/args.task/'task.toml').read_text())
    timeout_ms = max(30000, int(settings['verifier']['timeout_sec']*1000))
    return prepare(image=args.image, tools_image=args.tools_image, output=args.output,
                   environment_id='tb2-'+args.task, kernel=args.kernel,
                   agent_source=args.agent_source, busybox=args.busybox,
                   cpus=spec['cpus'], memory_mb=spec['memory_mb'], timeout_ms=timeout_ms,
                   network=args.network, boot_size_mb=args.boot_size_mb,
                   reserve_gib=args.reserve_gib, metadata={'task_id':args.task, 'source_commit':COMMIT})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    staging = commands.add_parser('stage')
    staging.add_argument('--repo', type=Path, required=True)
    staging.add_argument('--out', type=Path, required=True)
    selection = staging.add_mutually_exclusive_group(required=True)
    selection.add_argument('--tasks', nargs='+')
    selection.add_argument('--all', action='store_true')
    checking = commands.add_parser('check')
    checking.add_argument('--suite', type=Path, required=True)
    registration = commands.add_parser('register')
    registration.add_argument('--suite', type=Path, required=True)
    registration.add_argument('--catalog', type=Path, required=True)
    registration.add_argument('--out', type=Path, required=True)
    image = commands.add_parser('prepare-image')
    for name in ('suite', 'output', 'kernel', 'agent-source', 'busybox'):
        image.add_argument('--'+name, type=Path, required=True)
    for name in ('task', 'image', 'tools-image'):
        image.add_argument('--'+name, required=True)
    image.add_argument('--network', action='store_true')
    image.add_argument('--boot-size-mb', type=int, default=10240)
    image.add_argument('--reserve-gib', type=int, default=20)
    args = parser.parse_args()
    if args.command == 'stage':
        result = stage(args.repo, args.out, None if args.all else args.tasks)
    elif args.command == 'check':
        suite, manifest = validate_suite(args.suite)
        result = {'status':'passed', 'suite':str(suite), 'task_count':manifest['task_count']}
    elif args.command == 'register':
        result = register(args.suite, args.catalog, args.out)
    else:
        result = prepare_image(args)
    print(json.dumps(result))


if __name__ == '__main__':
    main()
