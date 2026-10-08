"""Release contracts: portable config, isolated entry points, and safe daemon targeting."""
import asyncio
import json
from pathlib import Path
import shlex
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import dsec_host
from service_admin import matches_daemon


class HostConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.config = self.root/'host.json'
        self.data = {
            'schema':1, 'instance':'pilot', 'state_root':'state',
            'sandbox':{'binary':'firecracker', 'kernel':'kernel', 'template':'guest.ext4'},
            'worker':{'tb2_tasks_dir':'tasks'},
            'scheduler':{'cpu':2, 'memory_mb':1024, 'disk_mb':2048,
                         'network_mbps':100, 'api_episode_slots':2, 'api_inflight':2,
                         'api_rpm':60, 'api_tpm':100000, 'network_interface':'eth0'}}

    def load(self):
        self.config.write_text(json.dumps(self.data))
        return dsec_host.load_config(self.config)

    def test_paths_are_config_relative_and_instance_scoped(self):
        cfg = self.load()
        self.assertEqual(cfg['sandbox']['template'], str(self.root/'guest.ext4'))
        self.assertEqual(cfg['worker']['tb2_tasks_dir'], str(self.root/'tasks'))
        args = dsec_host.sandbox_arguments(cfg)
        self.assertEqual(args[args.index('--root')+1], str(self.root/'state/sandboxes'))
        self.assertEqual(args[args.index('--node-budget')+1],
                         str(self.root/'state/worker/budget.json'))

    def test_proxy_bypass_host_reaches_daemon_arguments(self):
        self.data['sandbox']['egress_proxy_bypass_host'] = ['archive.ubuntu.com', 'security.ubuntu.com']
        args = dsec_host.sandbox_arguments(self.load())
        indices = [i for i, arg in enumerate(args) if arg == '--egress-proxy-bypass-host']
        self.assertEqual([args[i+1] for i in indices], ['archive.ubuntu.com', 'security.ubuntu.com'])
        self.data['sandbox']['egress_proxy_bypass_host'] = ['archive.ubuntu.com; id']
        with self.assertRaises(ValueError):
            self.load()

    def test_worker_verifier_pin_follows_configured_storage_artifact(self):
        import os
        self.data['sandbox']['tb2_verifier_artifact_manifest'] = 'default-manifest.json'
        self.data['sandbox']['tb2_task_verifier_artifact'] = ['precise-task=task-manifest.json=cache.ext4']
        cfg = self.load()
        with patch.dict(os.environ, {'DSEC_TB2_CANONICAL_VERIFIER_MANIFEST':'/old/deployment.json'}), \
                patch('sandbox_client.SandboxClient.call', return_value={'pid':42}), \
                patch('os.execv') as execute:
            dsec_host.run_service(cfg, 'worker')
            self.assertEqual(os.environ['DSEC_TB2_CANONICAL_VERIFIER_MANIFEST'],
                             str(self.root/'default-manifest.json'))
            index = Path(os.environ['DSEC_TB2_TASK_VERIFIER_MANIFESTS_FILE'])
            self.assertEqual(json.loads(index.read_text()),
                             {'precise-task':str(self.root/'task-manifest.json')})
            self.assertIn('-I', execute.call_args.args[1])

    def test_rejects_invalid_budget_and_ambiguous_flags(self):
        changes = [({'cpu':float('nan')}, {}), ({'memory_mb':512.5}, {}),
                   ({'min_memory_free_mb':-1}, {}), ({}, {'capacity':'2'}),
                   ({}, {'tb2_free_page_reporting':'false'}),
                   ({}, {'warm_min_disk_gib':0.5}), ({}, {'warm_idle_quiet_seconds':-1}),
                   ({}, {'typo':True})]
        for scheduler, sandbox in changes:
            with self.subTest(scheduler=scheduler, sandbox=sandbox):
                saved = json.loads(json.dumps(self.data))
                self.data['scheduler'].update(scheduler)
                self.data['sandbox'].update(sandbox)
                with self.assertRaises(ValueError):
                    self.load()
                self.data = saved

    def test_container_configuration_is_applied_to_edge_and_broker_to_both_services(self):
        import os
        self.data['worker'].update(container_root='containers', container_catalog='catalog.json',
                                   container_agent='agent.py', docker_broker_socket='broker.sock')
        cfg = self.load()
        with patch.dict(os.environ, {}, clear=True), patch('os.umask'), patch('os.execv') as execute:
            dsec_host.run_service(cfg, 'sandbox')
            for variable, name in [('DSEC_CONTAINER_ROOT', 'containers'),
                                   ('DSEC_ENVIRONMENT_CATALOG', 'catalog.json'),
                                   ('DSEC_CONTAINER_AGENT', 'agent.py'),
                                   ('DSEC_DOCKER_BROKER_SOCKET', 'broker.sock')]:
                self.assertEqual(os.environ[variable], str(self.root / name))
            self.assertIn('sandboxd', execute.call_args.args[1])
        with patch.dict(os.environ, {}, clear=True), \
                patch('os.umask'), \
                patch('sandbox_client.SandboxClient.call', return_value={'pid':42}), \
                patch('os.execv'):
            dsec_host.run_service(cfg, 'worker')
            self.assertEqual(os.environ['DSEC_DOCKER_BROKER_SOCKET'], str(self.root / 'broker.sock'))
            self.assertNotIn('DSEC_CONTAINER_ROOT', os.environ)
            self.assertNotIn('DSEC_ENVIRONMENT_CATALOG', os.environ)

    def test_unit_quotes_shell_arguments_and_disables_environment_expansion(self):
        self.config = self.root/"host $cash %name's.json"
        self.data['service_group'] = 'kvm'
        cfg = self.load()
        with patch.object(dsec_host.sys, 'executable', '/opt/dsec/bin/python'):
            output = dsec_host.render_units(cfg, self.root/'units')
            dsec_host.render_units(cfg, self.root/'units')  # repeat rendering is stable
        text = Path(output['units'][0]).read_text()
        line = next(x for x in text.splitlines() if x.startswith('ExecStart='))
        self.assertTrue(line.startswith('ExecStart=:"'))
        # Decode the systemd double-quoted words and undo specifier escaping.
        decoder = json.JSONDecoder()
        remaining, words = line.removeprefix('ExecStart=:'), []
        while remaining:
            word, end = decoder.raw_decode(remaining)
            words.append(word.replace('%%','%'))
            remaining = remaining[end:].lstrip()
        self.assertEqual(words[:3], ['/usr/bin/sg', 'kvm', '-c'])
        command = shlex.split(words[3])
        self.assertEqual(command[:5], ['exec', '/opt/dsec/bin/python', '-I', '-B', '-m'])
        self.assertEqual(command[command.index('--config')+1], str(self.config))
        self.assertNotIn('PYTHONPATH', text)
        self.assertIn('ExecStop=', text)
        self.assertIn('Restart=always', text)  # sg can report child failure as exit zero
        worker_text = Path(output['units'][1]).read_text()
        self.assertIn('Wants=dsec-pilot-sandbox.service', worker_text)
        self.assertNotIn('Requires=', worker_text)  # a daemon crash must not stop the worker
        Path(output['units'][0]).write_text('unrelated service')
        with self.assertRaises(FileExistsError):
            dsec_host.render_units(cfg, self.root/'units')

    def test_rejects_network_without_catalog(self):
        self.data['network'] = {'helper':'helper'}
        with self.assertRaisesRegex(ValueError, 'catalog'):
            self.load()

    def test_private_state_refuses_shared_permissions(self):
        directory = self.root/'state'
        directory.mkdir(mode=0o755)
        with self.assertRaises(PermissionError):
            dsec_host.private_directory(directory)

    def test_doctor_rejects_shared_state_without_repairing_it(self):
        directory=self.root/'state'
        directory.mkdir(mode=0o755)
        cfg=self.load()
        before=directory.stat()
        report=dsec_host.doctor(cfg)
        check=next(c for c in report['checks'] if c['check']=='state_permissions:state')
        self.assertFalse(check['ok'])
        after=directory.stat()
        self.assertEqual((before.st_mode,before.st_uid,before.st_gid),
                         (after.st_mode,after.st_uid,after.st_gid))

    def test_network_helper_defaults_to_the_scoped_installer_target(self):
        self.data['sandbox']['microvm_environment_catalog']='catalog.json'
        self.data['network']={'max_slots':2,'dns':'1.1.1.1'}
        cfg=self.load()
        self.assertEqual(cfg['network']['helper'],'/usr/local/libexec/dsec-pilot-netns-helper')

    def test_storage_root_accepts_only_service_group_traversal(self):
        import os
        from types import SimpleNamespace
        directory = self.root/'storage'
        directory.mkdir(mode=0o700)
        with patch('grp.getgrnam', return_value=SimpleNamespace(gr_gid=os.getgid())), \
                patch('os.chown') as chown:
            dsec_host.private_directory(directory, storage_access=True)
            self.assertEqual(directory.stat().st_mode & 0o777, 0o710)
            dsec_host.private_directory(directory, storage_access=True)  # service restart
            chown.assert_called_with(directory, -1, os.getgid())
            directory.chmod(0o2770)
            with self.assertRaises(PermissionError):
                dsec_host.private_directory(directory, storage_access=True)

    def test_smoke_saves_evidence_when_worker_connection_fails(self):
        cfg = self.load()
        output = self.root/'connection-failure.json'
        with patch('scheduled_dsec.ScheduledDSecClient.open',
                   new=AsyncMock(side_effect=RuntimeError('worker unavailable'))), \
                patch('dsec_host.importlib.metadata.version', return_value='test'):
            with self.assertRaisesRegex(RuntimeError, 'worker unavailable'):
                asyncio.run(dsec_host.smoke(cfg, output))
        evidence = json.loads(output.read_text())
        self.assertEqual(evidence['status'], 'failed')
        self.assertIn('worker unavailable', evidence['error'])
        self.assertIn('traceback', evidence)

    def test_privilege_bundle_is_scoped_and_embeds_reviewed_content_hashes(self):
        import ast
        from types import SimpleNamespace
        (self.root/'firecracker').write_bytes(b'test-vmm')
        self.data['sandbox']['microvm_environment_catalog'] = 'catalog.json'
        cfg = self.load()
        with patch('pwd.getpwnam', return_value=SimpleNamespace(pw_uid=1001, pw_gid=1001)), \
                patch('grp.getgrnam', return_value=SimpleNamespace(gr_gid=993, gr_mem=['runner'])):
            result = dsec_host.render_privileges(cfg, self.root/'bundle', 'runner', 4096)
        bundle = Path(result['bundle'])
        administrator = json.loads((bundle/'helper-config.json').read_text())
        self.assertEqual(administrator['runtime_root'], str(self.root/'state/sandboxes'))
        self.assertEqual(administrator['uid'], 1001)
        self.assertEqual(administrator['gid'], 993)
        connected = dsec_host.load_config(result['configuration'])
        self.assertEqual(connected['network']['helper'], '/usr/local/libexec/dsec-pilot-netns-helper')
        self.assertEqual(result['slot_range'], [4096, 4099])
        source = (bundle/'netns-helper.py').read_text()
        self.assertIn('SLOT_OFFSET = 4096', source)
        self.assertIn('MAX_SLOTS = 4', source)
        ast.parse(source)
        ast.parse((bundle/'install-privileges.py').read_text())
        self.assertNotIn('dsec_host', (bundle/'install-privileges.py').read_text())
        self.assertIn('Refusing to replace', (bundle/'install-privileges.py').read_text())


class InstalledCompatibilityTests(unittest.TestCase):
    @unittest.skipUnless((Path(__file__).resolve().parents[2] /
                          "experiments/openenv_api/tb2_dsec_environment.py").is_file(),
                         "Legacy experiment aliases are absent from a release checkout")
    def test_legacy_adapter_is_the_same_module(self):
        from dsec_adapters import tb2_dsec_environment as maintained
        from experiments.openenv_api import tb2_dsec_environment as legacy
        self.assertIs(maintained, legacy)

    def test_service_admin_targets_only_matching_entry_and_root(self):
        module = ['python', '-I', '-B', '-m', 'sandboxd', '--root', str(Path('/tmp/dsec-host').resolve())]
        self.assertTrue(matches_daemon(module, '/tmp/dsec-host'))
        self.assertFalse(matches_daemon(module, '/tmp/another-host'))
        self.assertFalse(matches_daemon(['python', '-m', 'unrelated', '--root', '/tmp/dsec-host'],
                                        '/tmp/dsec-host'))
        self.assertFalse(matches_daemon(['python', '-c', 'sandboxd', '--root', '/tmp/dsec-host'],
                                        '/tmp/dsec-host'))


if __name__ == '__main__':
    unittest.main()
