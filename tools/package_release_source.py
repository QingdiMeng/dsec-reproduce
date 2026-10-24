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
    'docs/architecture/CLUSTER_CONTROL_DESIGN.md', 'docs/guides/CLUSTER_DEPLOYMENT.md',
    'docs/guides/CLUSTER_CONSOLE.md', 'docs/architecture/EXECUTION_ISOLATION.md',
    'docs/architecture/MODULAR_REFACTOR_DESIGN.md',
    'docs/guides/ARTIFACT_PUBLICATION.md', 'docs/architecture/DOCKER_DSEC_BENCHMARK_PROTOCOL.md',
    'docs/reports/DSEC_VERITY_PILOT_REPORT.md',
    'docs/reports/DSEC_V01_GRPO_ACCEPTANCE.md',
    'docs/reports/MBPP_VERL_FIRST_USE.md',
)
# These directories contain portable regression suites. Discovery is independent
# of release construction; a new regression needs no parallel filename list.
REGRESSION_DIRS = ('tests/unit', 'tests/contracts', 'tests/adapters', 'tests/packaging')

TOOLS = (
    'deploy/cluster/prepare.py', 'deploy/cluster/planning.py', 'deploy/cluster/configuration.py', 'deploy/cluster/initialize.py',
    'deploy/cluster/pki.py','deploy/cluster/enroll_node.py',
    'deploy/cluster/upgrade.py',
    'deploy/cluster/rotate.py',
    'deploy/cluster/nodes.example.json',
    'deploy/cluster/compose.yaml', 'deploy/cluster/Dockerfile',
    'deploy/cluster/export_hosts.py',
    '.github/workflows/ci.yml',
    'tools/check_wheel.py', 'tools/package_release_source.py',
    'tests/packaging/test_release_source.py', 'tools/check_docs.py',
    'tests/__init__.py', 'tests/unit/__init__.py', 'tests/adapters/__init__.py',
    'tests/packaging/__init__.py',
    'tests/contracts/__init__.py', 'tests/contracts/v01_compatibility.json',
    'tools/build_smoke_guest.py', 'tools/verify_installed_task.py',
    'tools/build_native_agent.py',
    'tools/verify_native_sdk.py',
    'tools/verify_qemu_backend.py',
    'tools/verify_native_races.py',
    'tools/verify_cluster_control.py',
    'tools/verify_cluster_node.py',
    'tools/verify_rollout_api.py',
    'tools/verify_rollout_restart.py',
    'tools/check_cluster_refinement.py',
    'tools/check_security_refinement.py',
    'tools/check_concurrency_model.py',
    'tools/check_shutdown_refinement.py',
    'tools/check_native_race_refinement.py',
    'verification/AetherRouting.tla', 'verification/AetherRouting.cfg',
    'verification/QemuLifecycle.tla', 'verification/QemuLifecycle.cfg',
    'verification/QemuCID.tla', 'verification/QemuCID.cfg',
    'verification/RolloutDispatch.tla', 'verification/RolloutDispatch.cfg',
    'verification/QueueCancellation.tla', 'verification/QueueCancellation.cfg',
    'verification/NativeCallbackFence.tla', 'verification/NativeCallbackFence.cfg',
    'verification/ContainerNativeGate.tla', 'verification/ContainerNativeGate.cfg',
    'verification/RaceReplay.tla',
    'verification/ClusterAdmission.tla', 'verification/ClusterAdmission.cfg',
    'verification/NodeLifecycle.tla', 'verification/NodeLifecycle.cfg',
    'verification/NodeUpgrade.tla', 'verification/NodeUpgrade.cfg',
    'verification/CredentialRotation.tla','verification/CredentialRotation.cfg',
    'verification/TransportRotation.tla','verification/TransportRotation.cfg',
    'verification/NativeLifecycle.tla', 'verification/NativeLifecycle.cfg',
    'verification/ShutdownSignal.tla', 'verification/ShutdownSignal.cfg',
    'verification/ShutdownReplay.tla', 'verification/native_shutdown_probe.h',
    'tools/verify_installed_fork.py', 'tools/fixtures/tb21-openssl.json', 'tools/fixtures/tb21-regex-log.json',
)
SECRET = re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|'
                    rb'\bsk-[A-Za-z0-9_-]{24,}\b|\bhf_[A-Za-z0-9]{24,}\b|\bAKIA[A-Z0-9]{16}\b')


def source_files(root):
    config = tomllib.loads((root/'pyproject.toml').read_text())
    settings = config['tool']['setuptools']
    files = {'pyproject.toml', 'guest_agent.c', 'guest_native.c', 'native/protocol.h',
             'native/aether.c', 'native/chronus.c', 'native/files.c', 'native/http.c', '.gitignore', *DOCS, *TOOLS,
             *config['project']['license-files']}
    module_root = Path(settings.get('package-dir', {}).get('', ''))
    files.update(str(module_root / (module+'.py')) for module in settings['py-modules'])
    for directory in REGRESSION_DIRS:
        files.update(str(path.relative_to(root)) for path in (root/directory).rglob('*.py')
                     if all(part.isidentifier() for part in
                            path.relative_to(root/directory).with_suffix('').parts))
    for package in settings['packages']:
        parts = package.split('.')
        mappings = settings.get('package-dir', {})
        prefix = next(('.'.join(parts[:n]) for n in range(len(parts), 0, -1)
                       if '.'.join(parts[:n]) in mappings), '')
        relative = (Path(mappings[prefix]).joinpath(*parts[len(prefix.split('.')):])
                    if prefix else Path(mappings.get('', '')).joinpath(*parts))
        directory = root / relative
        files.update(str(p.relative_to(root)) for p in directory.rglob('*.py')
                     if all(part.isidentifier() for part in p.relative_to(directory).with_suffix('').parts))
        for pattern in settings.get('package-data', {}).get(package, []):
            files.update(str(p.relative_to(root)) for p in directory.glob(pattern) if p.is_file())
    for name, package_name in (("tb21", "dsec_tb21_case"), ("mbpp", "dsec_mbpp_case")):
        app = root/'apps'/name
        files.update('apps/'+name+'/'+filename for filename in ('pyproject.toml', 'README.md', 'LICENSE'))
        app_config = tomllib.loads((app/'pyproject.toml').read_text())
        files.update('apps/'+name+'/'+filename for filename in app_config['project']['license-files'])
        files.update(str(p.relative_to(root)) for p in (app/'src'/package_name).glob('*.py')
                     if p.stem.isidentifier())
        files.update(str(p.relative_to(root)) for p in (app/'src'/package_name).glob('*.json')
                     if p.stem.isidentifier())
        files.add('apps/'+name+'/tests/test_case.py')
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
                'boundary':'Runtime, selected regression/acceptance tools, and optional application preparation/adapters; no tasks, images, models, results or GPU training patches.'}
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
