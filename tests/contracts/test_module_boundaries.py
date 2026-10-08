"""Refactor gates: stable v0.1 schemas, import identity and dependency direction."""
import ast
import dataclasses
import importlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

from dsec.contracts.requests import MUTATING, RequestUncertain, request_digest
from dsec.contracts.resources import ResourceBudget, ResourceDemand
from request_journal import RequestJournal


ROOT = Path(__file__).resolve().parents[2]
BASELINE = json.loads(Path(__file__).with_name('v01_compatibility.json').read_text())
ALIASES = {
    'environment_catalog': 'dsec.storage.catalog',
    'artifact_integrity': 'dsec.storage.integrity',
    'artifact_publisher': 'dsec.storage.publication',
    'overlaybd_ublk_client': 'dsec.storage.ublk',
    'overlaybd_root_store': 'dsec.storage.overlaybd',
    'shared_snapshot_layers': 'dsec.storage.layers',
    'sandbox_client': 'dsec.sdk.sandbox_transport',
    'rollout_client': 'dsec.sdk.rollout_transport',
    'work_scheduler': 'dsec.runtime.scheduler',
    'request_journal': 'dsec.runtime.requests',
    'rollout_store': 'dsec.rollout.store',
    'work_journal': 'dsec.rollout.work_journal',
    'network_namespace': 'dsec.runtime.isolation.network',
    'egress_proxy': 'dsec.runtime.isolation.proxy',
    'sandbox_resource_meter': 'dsec.observability.meters',
    'framework_profile': 'dsec.contracts.profiles',
    'scheduled_dsec': 'dsec.sdk.scheduled',
    'libdsec_compat': 'dsec.sdk.client',
    'dsec.compat.libdsec': 'dsec.sdk.client',
    'agent_environment': 'dsec.rollout.environment',
    'sandbox_sdk': 'dsec.runtime.lifecycle',
    'microvm': 'dsec.runtime.backends.firecracker',
    'container_backend': 'dsec.runtime.backends.container',
    'container_lifecycle_journal': 'dsec.runtime.container_journal',
    'container_runtime_agent': 'dsec.runtime.backends.container_agent',
    'container_supervisord': 'dsec.runtime.backends.container_supervisor',
    'docker_broker': 'dsec.runtime.backends.docker_broker',
    'admission_guard': 'dsec.runtime.admission_guard',
    'snapshot_fork': 'dsec.runtime.fork',
    'sandbox_resource_rpc': 'dsec.runtime.resource_rpc',
    'rollout_workerd': 'dsec.rollout.worker',
    'elastic_resource_monitor': 'dsec.observability.elastic',
    'shared_service_monitor': 'dsec.observability.shared',
    'dsec_host': 'dsec.host.cli',
    'privileged_install_template': 'dsec.host.install_template',
    'privileged_netns_helper': 'dsec.host.privileged_helper',
}


class ModuleBoundaryTests(unittest.TestCase):
    def test_v01_resource_fields_defaults_and_cli_entry_points(self):
        for cls in (ResourceDemand, ResourceBudget):
            actual = [{'name': f.name,
                       'default': None if f.default is dataclasses.MISSING else f.default,
                       'required': f.default is dataclasses.MISSING}
                      for f in dataclasses.fields(cls)]
            self.assertEqual(actual, BASELINE['resources'][cls.__name__])
        scripts = tomllib.loads((ROOT / 'pyproject.toml').read_text())['project']['scripts']
        self.assertEqual(scripts, BASELINE['entry_points'])

    def test_existing_request_identity_and_pending_recovery(self):
        self.assertEqual(sorted(MUTATING), BASELINE['mutating_operations'])
        for sample in BASELINE['requests']:
            request = sample['request']
            self.assertEqual(request_digest(**request), sample['digest'])
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp) / 'requests'
            directory.mkdir()
            request = dict(BASELINE['requests'][1]['request'], request_id='a' * 32)
            # Pre-migration v1 record: load it, preserve its identity and refuse replay.
            record = dict(version=1, request_id=request['request_id'],
                          operation=request['operation'], sandbox_id=request['sandbox_id'],
                          digest=BASELINE['requests'][1]['digest'], state='PENDING')
            (directory / (request['request_id'] + '.json')).write_text(json.dumps(record))
            journal = RequestJournal(temp)
            self.assertEqual(journal.lookup(request['request_id'])['state'], 'UNKNOWN')
            with self.assertRaises(RequestUncertain):
                journal.begin(request)

    def test_compatibility_imports_share_implementation_and_patches(self):
        for legacy, canonical in ALIASES.items():
            with self.subTest(module=legacy):
                self.assertIs(importlib.import_module(legacy), importlib.import_module(canonical))
        from work_scheduler import ResourceDemand as legacy_demand
        from request_journal import RequestUncertain as legacy_uncertain
        self.assertIs(legacy_demand, ResourceDemand)
        self.assertIs(legacy_uncertain, RequestUncertain)
        with patch('environment_catalog.file_sha256', return_value='patched'):
            from dsec.storage.catalog import file_sha256
            self.assertEqual(file_sha256(None), 'patched')

    def test_legacy_first_and_canonical_first_imports_in_fresh_processes(self):
        for legacy, canonical in ALIASES.items():
            for first, second in ((legacy, canonical), (canonical, legacy)):
                with self.subTest(first=first, second=second):
                    subprocess.run([sys.executable, '-c',
                                    'import importlib; '
                                    f'a=importlib.import_module({first!r}); '
                                    f'b=importlib.import_module({second!r}); assert a is b'],
                                   cwd=ROOT, check=True, capture_output=True)

    @unittest.skipUnless(sys.platform == 'linux', 'Runtime registry requires Linux /proc')
    def test_linux_service_and_registry_aliases(self):
        for old, new in (('durable_manager', 'dsec.runtime.registry'),
                         ('sandboxd', 'dsec.control.local_api')):
            self.assertIs(importlib.import_module(old), importlib.import_module(new))

    def test_privileged_and_guest_agents_remain_standalone_sources(self):
        # These sources are embedded into a root-owned helper or guest image.
        # They must not require an installed DSec Python package in that context.
        for relative in ('host/privileged_helper.py', 'runtime/backends/container_agent.py'):
            source = ROOT / 'src' / 'dsec' / relative
            for node in ast.walk(ast.parse(source.read_text())):
                names = ([n.name for n in node.names] if isinstance(node, ast.Import)
                         else [node.module or ''] if isinstance(node, ast.ImportFrom) else [])
                for name in names:
                    self.assertIn(name.split('.')[0], sys.stdlib_module_names)

    def test_migrated_layers_cannot_import_servers_tasks_or_legacy_shims(self):
        for layer in ('contracts', 'sdk', 'storage'):
            paths = (ROOT / 'src' / 'dsec' / layer).rglob('*.py')
            for path in paths:
                for node in ast.walk(ast.parse(path.read_text())):
                    names = ([n.name for n in node.names] if isinstance(node, ast.Import)
                             else [node.module or ''] if isinstance(node, ast.ImportFrom) else [])
                    for name in names:
                        top = name.split('.')[0]
                        if name == '__future__' or top in sys.stdlib_module_names:
                            continue
                        allowed = (name.startswith('dsec.contracts.') or
                                   (layer != 'contracts' and (
                                    (layer == 'sdk' and name.startswith('dsec.sdk.')) or
                                    (layer == 'storage' and
                                     (name.startswith('dsec.storage.') or name == 'dsec._persistence')))))
                        self.assertTrue(allowed, f'{path.relative_to(ROOT)} imports {name}')


if __name__ == '__main__':
    unittest.main()
