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
    'docs/architecture/MODULAR_REFACTOR_DESIGN.md',
    'docs/guides/ARTIFACT_PUBLICATION.md', 'docs/architecture/DOCKER_DSEC_BENCHMARK_PROTOCOL.md',
    'docs/reports/DSEC_VERITY_PILOT_REPORT.md',
    'docs/reports/DSEC_V01_GRPO_ACCEPTANCE.md',
    'docs/reports/MBPP_VERL_FIRST_USE.md',
)
TESTS = (
    'tests/contracts/test_module_boundaries.py',
    'tests/unit/test_admission_guard.py',
    'tests/unit/test_agent_environment.py',
    'tests/unit/test_artifact_integrity.py',
    'tests/unit/test_artifact_publisher.py',
    'tests/unit/test_container_lifecycle_journal.py',
    'tests/unit/test_container_edge.py',
    'tests/unit/test_dsec_host.py',
    'tests/unit/test_egress_proxy.py',
    'tests/unit/test_elastic_resource_monitor.py',
    'tests/unit/test_environment_catalog.py',
    'tests/unit/test_image_prepare.py',
    'tests/unit/test_overlaybd_daemon_permissions.py',
    'tests/unit/test_overlaybd_root_store.py',
    'tests/unit/test_overlaybd_ublk_client.py',
    'tests/unit/test_prepared_fork_concurrency.py',
    'tests/unit/test_privileged_netns_helper.py',
    'tests/unit/test_rollout_dialogue.py',
    'tests/unit/test_rollout_scheduler.py',
    'tests/unit/test_rollout_step_options.py',
    'tests/unit/test_sandbox_directory_permissions.py',
    'tests/unit/test_sandbox_parallel_create.py',
    'tests/unit/test_scheduled_dsec.py',
    'tests/unit/test_shared_service_monitor.py',
    'tests/unit/test_shared_snapshot_layers.py',
    'tests/unit/test_snapshot_concurrency.py',
    'tests/unit/test_snapshot_fork.py',
    'tests/unit/test_tb2_netns.py',
    'tests/unit/test_tb2_network_pool.py',
    'tests/unit/test_tb2_offline_verifier.py',
    'tests/unit/test_tb2_task_verifier_selection.py',
    'tests/unit/test_tb2_verifier_artifact.py',
    'tests/unit/test_work_journal.py',
    'tests/unit/test_work_scheduler.py',
    'tests/unit/test_worker_evaluators.py',
    'tests/adapters/test_counter_dsec_environment.py',
    'tests/adapters/test_miles_dsec_agent_function.py',
    'tests/adapters/test_miles_dsec_generate.py',
    'tests/adapters/test_tb2_dsec_environment.py',
    'tests/adapters/test_tb2_evidence_export.py',
    'tests/adapters/test_tb2_verifier_command.py',
)

TOOLS = (
    '.github/workflows/ci.yml',
    'tools/check_wheel.py', 'tools/package_release_source.py',
    'tests/packaging/test_release_source.py', 'tools/check_docs.py',
    'tests/__init__.py', 'tests/unit/__init__.py', 'tests/adapters/__init__.py',
    'tests/packaging/__init__.py',
    'tests/contracts/__init__.py', 'tests/contracts/v01_compatibility.json',
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
        parts = package.split('.')
        mappings = settings.get('package-dir', {})
        prefix = next(('.'.join(parts[:n]) for n in range(len(parts), 0, -1)
                       if '.'.join(parts[:n]) in mappings), '')
        relative = (Path(mappings[prefix]).joinpath(*parts[len(prefix.split('.')):])
                    if prefix else Path(mappings.get('', '')).joinpath(*parts))
        directory = root / relative
        files.update(str(p.relative_to(root)) for p in directory.rglob('*.py'))
        for pattern in settings.get('package-data', {}).get(package, []):
            files.update(str(p.relative_to(root)) for p in directory.glob(pattern) if p.is_file())
    for name, package_name in (("tb21", "dsec_tb21_case"), ("mbpp", "dsec_mbpp_case")):
        app = root/'apps'/name
        files.update('apps/'+name+'/'+filename for filename in ('pyproject.toml', 'README.md', 'LICENSE'))
        files.update(str(p.relative_to(root)) for p in (app/'src'/package_name).glob('*.py'))
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
