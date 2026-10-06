"""Explicit staging and artifact registration for an optional TB2.1 application."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import platform
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
    from .layers import convert
    from .boot import build
    if platform.system() != 'Linux' or platform.machine() not in ('x86_64', 'amd64'):
        raise ValueError('Guest image preparation requires Linux x86_64')
    suite, manifest = validate_suite(args.suite)
    if args.task not in manifest['tasks']:
        raise ValueError('Task is not staged')
    if not IMAGE_ID.fullmatch(args.image) or not IMAGE_ID.fullmatch(args.tools_image):
        raise ValueError('Task and mkfs tools images must be exact local sha256 image IDs')
    spec = manifest['tasks'][args.task]
    original_id = subprocess.check_output(
        ['docker', 'image', 'inspect', spec['docker_image'], '--format', '{{.Id}}'], text=True).strip()
    if args.image != original_id:
        raise ValueError('Provided image differs from the staged task Docker image')
    tools_id = subprocess.check_output(
        ['docker', 'image', 'inspect', args.tools_image, '--format', '{{.Id}}'], text=True).strip()
    if tools_id != args.tools_image:
        raise ValueError('Tools image pin differs')
    if args.output.exists():
        raise FileExistsError('Use a fresh per-task output directory')
    if not 256 <= args.boot_size_mb <= 102400 or args.reserve_gib < 1:
        raise ValueError('Invalid boot capacity or disk reserve')
    ancestor = args.output.resolve().parent
    while not ancestor.exists():
        ancestor = ancestor.parent
    inspected = json.loads(subprocess.check_output(
        ['docker', 'image', 'inspect', args.image], text=True))[0]
    if spec.get('gpus', 0) != 0 or not 1 <= len(inspected['RootFS']['Layers']) <= 12:
        raise ValueError('Standalone builder supports CPU tasks with 1..12 direct layers; register a separately prepared compacted recipe for larger layouts')
    # Account for save, per-layer conversion and boot allocation before work.
    required = args.reserve_gib * 1024**3 + 3 * inspected['Size'] + args.boot_size_mb * 1024**2
    if shutil.disk_usage(ancestor).free < required:
        raise RuntimeError('Image preparation would violate the disk reserve')
    settings = tomllib.loads((suite/'tasks'/args.task/'task.toml').read_text())
    timeout_ms = max(30000, int(settings['verifier']['timeout_sec']*1000))
    if not 30000 <= timeout_ms <= 12000000:
        raise ValueError('Unsupported verifier command timeout')
    args.output.mkdir(parents=True)
    result = {'status':'running', 'task_id':args.task, 'source_commit':COMMIT,
              'image_id':args.image, 'tools_image_id':args.tools_image,
              'root_block_backend':'file-ext4', 'network':args.network}
    try:
        layers_path = convert(args.image, args.output/'erofs', args.tools_image)
        layers = json.loads(layers_path.read_text())
        boot = args.output/'boot.ext4'
        build(layers_path, args.agent_source.resolve(strict=True), args.busybox.resolve(strict=True),
              boot, timeout_ms, args.network, args.boot_size_mb)
        kernel = args.kernel.resolve(strict=True)
        entry = {'backend':'microvm', 'rootfs':'erofs_layers',
                 'boot_template':str(boot.resolve()), 'boot_sha256':sha(boot),
                 'kernel':str(kernel), 'kernel_sha256':sha(kernel),
                 'cpus':spec['cpus'], 'memory_mb':spec['memory_mb'],
                 'command_timeout_ms':timeout_ms,
                 'layers':[{'name':'layer'+str(i), 'file':layer['erofs'],
                            'sha256':layer['erofs_sha256'], 'bytes':layer['erofs_bytes']}
                           for i, layer in enumerate(layers['layers'])]}
        prepared_catalog = args.output/'catalog.json'
        write_new(prepared_catalog, {'format':1, 'environments':{'tb2-'+args.task:entry}})
        MicroVMEnvironmentCatalog(prepared_catalog).resolve('tb2-'+args.task)
        result.update(status='passed', catalog=str(prepared_catalog.resolve()),
                      layer_count=len(entry['layers']))
        return result
    except BaseException as exc:
        result.update(status='failed', error=repr(exc))
        raise
    finally:
        write_new(args.output/'prepare-result.json', result)


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
