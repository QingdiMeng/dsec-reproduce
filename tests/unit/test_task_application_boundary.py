"""Optional task rules must not become a dependency of generic host services."""
import argparse
import hashlib
import importlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from dsec.control.environments import load_configuration, TB21_FLAGS
from dsec.compat.applications import require_tb21
from dsec.compat.task_plugins import execution_command


def args(root, **overrides):
    values = {name: [] for name in TB21_FLAGS}
    values.update(template=str(root/'boot'), microvm_environment_catalog=None, capacity=4)
    values.update(overrides)
    return SimpleNamespace(**values)


def pin(data):
    return hashlib.sha256(data).hexdigest()


def catalog(root, name='tools'):
    for filename in ('boot', 'kernel', 'layer'):
        (root/filename).write_bytes(filename.encode())
    entry = {'backend':'microvm', 'rootfs':'erofs_layers', 'cpus':1, 'memory_mb':512,
             'boot_template':str(root/'boot'), 'boot_sha256':pin(b'boot'),
             'kernel':str(root/'kernel'), 'kernel_sha256':pin(b'kernel'),
             'layers':[{'name':'tools', 'file':str(root/'layer'), 'sha256':pin(b'layer')}]}
    path = root/'catalog.json'
    path.write_text(json.dumps({'format':1, 'environments':{name:entry}}))
    return path


class CoreApplicationBoundaryTests(unittest.TestCase):
    def test_generic_host_worker_and_counter_do_not_load_optional_application(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            path = catalog(root)
            code = '''
import importlib.abc, sys
from types import SimpleNamespace
from pathlib import Path
class BlockTasks(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith(('dsec_tb21_case', 'dsec_adapters.tb2', 'tb2_verifier_artifact')):
            raise ModuleNotFoundError('optional case unavailable', name=fullname)
sys.meta_path.insert(0, BlockTasks())
from dsec.control.environments import load_configuration, TB21_FLAGS
from dsec.control import local_api
from dsec.rollout.worker import RolloutWorker
from dsec_adapters.dsec_task_registry import task_adapter
from dsec.compat.task_plugins import execution_command
RolloutWorker(None)
assert task_adapter('counter').__class__.__name__ == 'CounterDSecEnvironment'
arguments = {name: [] for name in TB21_FLAGS}
arguments.update(template=sys.argv[2], microvm_environment_catalog=sys.argv[1], capacity=4)
config = load_configuration(SimpleNamespace(**arguments), None)
assert set(config.templates) == {'tools'}
assert config.resources['tools']['memory_mb'] == 512
assert execution_command(SimpleNamespace(backend='microvm', environment_id='tools'), 'true') == 'true'
from dsec.contracts.profiles import FrameworkProfile
for storage in ('local', 'threefs_lazy'):
    profile = FrameworkProfile(environment='erofs_layers', environment_id='tools', verifier_storage=storage)
    assert profile.validate_runtime() is profile
# Exercise the actual Miles session wrapper without its optional OpenAI SDK.
import asyncio, types
sdk = types.ModuleType('openai')
closed = []
class Policy:
    def __init__(self, **kwargs):
        assert kwargs['base_url'] == 'http://session/v1'
    async def close(self):
        closed.append(True)
sdk.AsyncOpenAI = Policy
sys.modules['openai'] = sdk
from dsec_adapters import openenv_agent_function, miles_session
assert openenv_agent_function is miles_session
async def episode(policy, model, messages, sampling, metadata):
    assert messages == [{'role':'user', 'content':'counter instruction'}]
    assert sampling == {'temperature':0.7}
    return 1.0, {'dsec_environment':'counter'}
result = asyncio.run(openenv_agent_function.run_for_training(
    'http://session', 'counter instruction', {'temperature':0.7}, {}, episode,
    manages_episode_budget=True))
assert result['reward'] == 1.0 and result['exit_status'] == 'completed'
assert closed == [True]
assert not any(name.startswith('dsec_tb21_case') for name in sys.modules)
arguments['tb2_template'] = ['example='+sys.argv[2]]
try:
    load_configuration(SimpleNamespace(**arguments), None)
except RuntimeError as exc:
    assert 'install ./apps/tb21' in str(exc)
else:
    raise AssertionError('task configuration must require the application')
'''
            subprocess.run([sys.executable, '-B', '-c', code, str(path), str(root/'boot')],
                           check=True, capture_output=True)

    def test_loader_reports_only_missing_case_not_missing_internal_dependency(self):
        missing = ModuleNotFoundError('case unavailable', name='dsec_tb21_case')
        with patch('dsec.compat.applications.import_module', side_effect=missing):
            with self.assertRaisesRegex(RuntimeError, 'optional application'):
                require_tb21('host_configuration')
        broken = ModuleNotFoundError('internal dependency unavailable', name='unrelated_dependency')
        with patch('dsec.compat.applications.import_module', side_effect=broken):
            with self.assertRaises(ModuleNotFoundError) as caught:
                require_tb21('host_configuration')
            self.assertIs(caught.exception, broken)

    def test_empty_configuration_has_no_task_or_artifact_defaults(self):
        config = load_configuration(args(Path('/unused')), argparse.ArgumentParser())
        self.assertEqual(config.templates, {})
        self.assertIsNone(config.verifier_artifacts)
        self.assertEqual(config.resources, {})
        self.assertEqual(config.warm_pool_specs, {})


class TB21ApplicationBoundaryTests(unittest.TestCase):
    def test_legacy_aliases_share_application_identity_and_packaged_manifests(self):
        for name in ('tb2_dsec_environment', 'tb2_task_runtime', 'tb2_microvm_env',
                     'tb2_verifier_command', 'tb2_offline_verifier'):
            self.assertIs(importlib.import_module('dsec_adapters.'+name),
                          importlib.import_module('dsec_tb21_case.'+name))
        artifact = importlib.import_module('tb2_verifier_artifact')
        self.assertIs(artifact, importlib.import_module('dsec_tb21_case.verifier_artifact'))
        offline = importlib.import_module('dsec_adapters.tb2_offline_verifier')
        microvm = importlib.import_module('dsec_adapters.tb2_microvm_env')
        for path in (offline.SHARED_MANIFEST, offline.LAYERED_UV_MANIFEST,
                     microvm.LEGACY_CANONICAL_LINKS):
            self.assertEqual(path.parent, Path(offline.__file__).parent)
            self.assertIsInstance(json.loads(path.read_text()), dict)
        with patch('tb2_verifier_artifact.sha256', return_value='legacy-hook'):
            self.assertEqual(artifact.sha256(None), 'legacy-hook')

    def test_legacy_openenv_api_is_forwarded_to_application_with_shared_session_wrapper(self):
        code = """
import sys, types
sdk = types.ModuleType('openai')
sdk.AsyncOpenAI = object
sys.modules['openai'] = sdk
from dsec_adapters import openenv_agent_function as legacy, miles_session
from dsec_tb21_case import openenv_agent_function as application
assert legacy is miles_session
assert legacy.multi_turn is application.multi_turn
assert legacy.run_episode is application.run_episode
assert legacy.run_for_training is application.run_for_training
assert legacy.TB2_AGENT_SYSTEM_PROMPT == application.TB2_AGENT_SYSTEM_PROMPT
"""
        subprocess.run([sys.executable, '-B', '-c', code], check=True, capture_output=True)

    def test_task_path_transformation_preserves_historical_execution_bytes(self):
        profile = SimpleNamespace(backend='microvm', environment_id='tb2-example')
        expected = ('export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:'
                    '/usr/bin:/sbin:/bin; cat /data')
        self.assertEqual(execution_command(profile, 'cat /data'), expected)
        self.assertEqual(execution_command(SimpleNamespace(profile=profile), 'cat /data'), expected)

    def test_catalog_task_manifest_preserves_vm_limits_and_separate_verifier_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            path = catalog(root, 'tb2-example')
            task = root/'tasks/example/task.toml'
            task.parent.mkdir(parents=True)
            task.write_text('[verifier]\ntimeout_sec=3600\n')
            manifest = root/'manifest.json'
            manifest.write_text(json.dumps({'tasks':{'example':{'cpus':2, 'memory_mb':1024,
                                            'task_toml_sha256':pin(task.read_bytes())}}}))
            arguments = args(root, microvm_environment_catalog=str(path), tb2_manifest=str(manifest))
            config = load_configuration(arguments, argparse.ArgumentParser())
            self.assertEqual(config.resources['tb2-example'],
                             {'cpus':1, 'memory_mb':512, 'command_timeout_ms':30000,
                              'verifier_timeout_ms':3600000})
            task.write_text('[verifier]\ntimeout_sec=1\n')
            with self.assertRaises(SystemExit), patch('sys.stderr'):
                load_configuration(arguments, argparse.ArgumentParser())

    def test_catalog_task_requires_suite_pin_and_rejects_duplicate_template(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            path = catalog(root, 'tb2-example')
            parser = argparse.ArgumentParser()
            with self.assertRaises(SystemExit), patch('sys.stderr'):
                load_configuration(args(root, microvm_environment_catalog=str(path)), parser)
            with self.assertRaises(SystemExit), patch('sys.stderr'):
                load_configuration(args(root, tb2_template=['example='+str(root/'boot')]*2), parser)

    def test_legacy_layer_and_warm_configuration_pins_paths_without_copying(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            catalog(root)
            layer = root/'layer.json'
            layer.write_text(json.dumps({'format':1, 'layers':[
                {'erofs':str(root/'layer'), 'erofs_bytes':5, 'erofs_sha256':pin(b'layer')}]}))
            image = root/'image.json'
            image.write_text('{}')
            config = load_configuration(args(root,
                tb2_template=['example='+str(root/'boot')],
                tb2_layer_manifest=['example='+str(layer)],
                tb2_overlaybd_root=['example='+str(image)], warm_pool=['example:none:2']),
                argparse.ArgumentParser())
            self.assertEqual(config.templates, {'tb2-example':root/'boot'})
            self.assertEqual(config.layers, {'tb2-example':[root/'layer']})
            self.assertEqual(config.layer_counts, {'tb2-example':1})
            self.assertEqual(config.roots, {'tb2-example':image})
            self.assertEqual(config.warm_pool_specs, {('tb2-example',None):2})
            self.assertEqual(set(p.name for p in root.iterdir()),
                             {'boot','kernel','layer','catalog.json','layer.json','image.json'})
            (root/'layer').write_bytes(b'size changed')
            with self.assertRaises(SystemExit), patch('sys.stderr'):
                load_configuration(args(root, tb2_template=['example='+str(root/'boot')],
                    tb2_layer_manifest=['example='+str(layer)]), argparse.ArgumentParser())

    def test_dax_layer_integrity_failure_precedes_runtime_effects(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            catalog(root)
            (root/'layer').write_bytes(bytes(2*1024*1024))
            manifest = root/'layer.json'
            manifest.write_text(json.dumps({'format':1, 'layers':[
                {'erofs':str(root/'layer'), 'erofs_bytes':2*1024*1024,
                 'erofs_sha256':pin(bytes(2*1024*1024)), 'dax':True}]}))
            arguments = args(root, tb2_template=['example='+str(root/'boot')],
                             tb2_layer_manifest=['example='+str(manifest)])
            config = load_configuration(arguments, argparse.ArgumentParser())
            self.assertEqual(config.layer_dax_indices, {'tb2-example':(0,)})
            with (root/'layer').open('r+b') as disk:
                disk.write(b'changed')
            with self.assertRaises(SystemExit), patch('sys.stderr'):
                load_configuration(arguments, argparse.ArgumentParser())
