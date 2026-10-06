"""Create a reproducible, allowlisted control-plane/application source archive."""
import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import re
import tarfile
import tomllib


DOCS = (
    'README.md', 'docs/README.md', 'ROADMAP.md', 'docs/guides/USE_CASES.md', 'CONTRIBUTING.md', 'docs/guides/QUICKSTART.md', 'docs/guides/DSEC_HOST_CONFIGURATION.md',
    'docs/reports/DSEC_V01_INSTALL_ACCEPTANCE.md',
    'docs/guides/SERVICE_DEPLOYMENT.md', 'docs/architecture/SANDBOX_DAEMON.md',
    'docs/architecture/AGENT_ENVIRONMENT_CONTRACT.md', 'docs/guides/RL_FRAMEWORK_ADAPTERS.md',
    'docs/guides/ARTIFACT_PUBLICATION.md', 'docs/architecture/DOCKER_DSEC_BENCHMARK_PROTOCOL.md',
    'docs/reports/DSEC_VERITY_PILOT_REPORT.md',
    'docs/reports/DSEC_V01_GRPO_ACCEPTANCE.md',
)
TESTS = (
    'test_agent_environment.py',
    'test_work_scheduler.py', 'test_rollout_scheduler.py', 'test_work_journal.py',
    'test_admission_guard.py', 'test_scheduled_dsec.py',
    'test_shared_snapshot_layers.py', 'test_prepared_fork_concurrency.py',
    'test_sandbox_directory_permissions.py',
    'test_overlaybd_daemon_permissions.py',
    'test_privileged_netns_helper.py',
    'test_artifact_integrity.py', 'test_tb2_verifier_artifact.py',
)
TOOLS = (
    '.github/workflows/ci.yml',
    'tools/check_wheel.py', 'tools/package_release_source.py',
    'tools/test_release_source.py', 'tools/check_docs.py',
    'tools/build_smoke_guest.py', 'tools/verify_installed_task.py',
    'tools/verify_installed_fork.py', 'tools/fixtures/tb21-openssl.json',
)
SECRET = re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|'
                    rb'\bsk-[A-Za-z0-9_-]{24,}\b|\bhf_[A-Za-z0-9]{24,}\b|\bAKIA[A-Z0-9]{16}\b')


def source_files(root):
    config = tomllib.loads((root/'pyproject.toml').read_text())
    settings = config['tool']['setuptools']
    files = {'pyproject.toml', 'guest_agent.c', '.gitignore', *DOCS, *TESTS, *TOOLS,
             *config['project']['license-files']}
    files.update(module+'.py' for module in settings['py-modules'])
    for package in settings['packages']:
        directory = root/package.replace('.', '/')
        files.update(str(p.relative_to(root)) for p in directory.rglob('*.py'))
        for pattern in settings.get('package-data', {}).get(package, []):
            files.update(str(p.relative_to(root)) for p in directory.glob(pattern) if p.is_file())
    app = root/'apps/tb21'
    files.update('apps/tb21/'+name for name in ('pyproject.toml', 'README.md', 'LICENSE'))
    files.update(str(p.relative_to(root)) for p in (app/'src/dsec_tb21_case').glob('*.py'))
    files.add('apps/tb21/tests/test_case.py')
    return config, sorted(files)


def package(root, output):
    root = Path(root).resolve(strict=True)
    output = Path(output).resolve()
    if output.exists():
        raise FileExistsError('Refusing to replace release evidence')
    config, names = source_files(root)
    contents = {}
    for name in names:
        path = root/name
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):
            raise ValueError('Missing or unsafe release source: '+name)
        data = path.read_bytes()
        if len(data) > 1024**2:
            raise ValueError('Unexpectedly large release source: '+name)
        if SECRET.search(data):
            raise ValueError('Possible embedded credential in release source: '+name)
        if path.suffix == '.py':
            compile(data, name, 'exec')
        contents[name] = data
    manifest = {'schema':1, 'version':config['project']['version'],
                'license_expression':config['project']['license'],
                'files':{name:hashlib.sha256(data).hexdigest() for name, data in contents.items()},
                'boundary':'Runtime, selected regression/acceptance tools, and optional TB2.1 preparation code; no tasks, images, models, results or GPU training patches.'}
    contents['SOURCE_MANIFEST.json'] = (json.dumps(manifest, indent=2, sort_keys=True)+'\n').encode()
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with output.open('xb') as stream, gzip.GzipFile(fileobj=stream, mode='wb', filename='', mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode='w', format=tarfile.PAX_FORMAT) as archive:
                for name, data in sorted(contents.items()):
                    info = tarfile.TarInfo(name)
                    info.size = len(data)
                    info.mode = 0o644
                    info.mtime = 0
                    archive.addfile(info, io.BytesIO(data))
    except BaseException:
        output.unlink(missing_ok=True)
        raise
    return {'status':'passed', 'files':len(contents), 'bytes':output.stat().st_size,
            'sha256':hashlib.sha256(output.read_bytes()).hexdigest(), 'out':str(output)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(package(args.project, args.out)))


if __name__ == '__main__':
    main()
