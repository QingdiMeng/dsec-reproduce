"""Controlled capability fixtures; no VM, namespace or host installation here."""
import ast
from contextlib import ExitStack, redirect_stdout
from copy import deepcopy
import errno
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from dsec.host import node_probe, node_setup, cluster, cli
from dsec.host.configuration import normalize_config
from dsec.host.install_template import INSTALLER


class ProbeTests(unittest.TestCase):
    def observe(self, *, bounds='ffffffffffffffff', effective='0', missing=(), markers=(),
                uid_map='0 0 4294967295', namespace_mismatch=False, administrator=False, denied=(),
                no_new_privs=0, forwarding='1', missing_tools=(), namespace_denied=False):
        status = f'CapBnd:\t{bounds}\nCapEff:\t{effective}\nSeccomp:\t2\nNoNewPrivs:\t{no_new_privs}\n'
        texts = {'/proc/self/status':status, '/proc/self/uid_map':uid_map,
                 '/proc/sys/net/ipv4/ip_forward':forwarding}
        def path(value):
            name = str(value)
            return SimpleNamespace(read_text=lambda: texts[name], exists=lambda: name in markers)
        def device_info(name):
            if name in missing:
                raise FileNotFoundError(errno.ENOENT, 'device unavailable', name)
            return SimpleNamespace(st_mode=stat.S_IFCHR | 0o660,
                                   st_rdev=os.makedev(10, 232 if name == '/dev/kvm' else 200))
        def open_device(name, flags):
            if name in denied:
                raise PermissionError(errno.EPERM, 'device cgroup denied')
            self.assertFalse(flags & os.O_CREAT)
            return 30 if name == '/dev/kvm' else 31
        def readlink(name):
            if namespace_denied and '/1/' in name:
                raise PermissionError(errno.EACCES, 'ptrace permission required')
            return name.split('/')[-1]+('other' if namespace_mismatch and '/self/' in name else '')
        with patch.object(node_probe.sys, 'platform', 'linux'), \
             patch.object(node_probe, 'Path', side_effect=path), \
             patch('os.stat', side_effect=device_info), patch('os.open', side_effect=open_device), \
             patch('os.close') as close, patch('os.geteuid', return_value=0 if administrator else 1001), \
             patch('shutil.which', side_effect=lambda name: None if name in missing_tools else '/usr/bin/tool'), \
             patch('os.readlink', side_effect=readlink), \
             patch('fcntl.ioctl', side_effect=lambda fd, op, *rest: 12 if op == 0xAE00 else 0) as ioctl, \
             patch('subprocess.run', side_effect=AssertionError('no processes')):
            report = node_probe.observe(administrator=administrator)
        return report, ioctl, close

    def test_read_only_device_ioctls_and_unprivileged_native_context(self):
        report, ioctl, close = self.observe()
        self.assertTrue(report['ok'])
        self.assertFalse(report['sandbox_boot_verified'])
        self.assertEqual([call.args[1] for call in ioctl.call_args_list], [0xAE00, 0x800454CF])
        self.assertEqual(close.call_count, 2)
        self.assertEqual(report['facts']['CapEff'], '0')

    def test_runpod_like_device_and_capability_limits_fail_without_provider_name_rules(self):
        report, _, _ = self.observe(bounds='a80425fb', missing=('/dev/kvm', '/dev/net/tun'),
                                    markers=('/.dockerenv',))
        self.assertFalse(report['ok'])
        self.assertEqual({x['check'] for x in report['checks'] if not x['ok']},
                         {'kvm', 'tun', 'network_capabilities'})
        with self.assertRaisesRegex(RuntimeError, 'host prerequisites failed'):
            node_probe.require_eligible(report)
        # Container packaging alone is not a rejection rule.
        supported, _, _ = self.observe(markers=('/.dockerenv',))
        self.assertTrue(supported['ok'])

    def test_permissions_user_namespace_and_context_are_real_gates(self):
        for options, failed in ((dict(denied=('/dev/kvm',)), 'kvm'),
                                (dict(uid_map='0 1000 1'), 'user_namespace'),
                                (dict(namespace_mismatch=True), 'namespace_context'),
                                (dict(no_new_privs=1), 'network_capabilities'),
                                (dict(administrator=True), 'network_capabilities')):
            with self.subTest(options=options):
                report, _, _ = self.observe(**options)
                self.assertFalse(next(x for x in report['checks'] if x['check'] == failed)['ok'])
        report, _, _ = self.observe(administrator=True, effective='ffffffffffffffff')
        self.assertTrue(report['ok'])

    def test_non_linux_returns_before_device_or_process_access(self):
        with patch.object(node_probe.sys, 'platform', 'darwin'), \
             patch('os.open', side_effect=AssertionError('device access')):
            report = node_probe.observe()
        self.assertFalse(report['ok'])
        self.assertEqual([x['check'] for x in report['checks']], ['linux'])

    def test_missing_forwarding_and_tools_are_reported_without_changing_host(self):
        report, _, _ = self.observe(forwarding='0', missing_tools=('ip', 'visudo'))
        self.assertFalse(report['ok'])
        self.assertEqual({x['check'] for x in report['checks'] if not x['ok']},
                         {'ipv4_forwarding', 'tool:ip', 'tool:visudo'})

    def test_unprivileged_pid1_visibility_is_deferred_but_administrator_must_verify(self):
        report, _, _ = self.observe(namespace_denied=True)
        self.assertTrue(report['ok'])
        self.assertEqual(report['administrator_checks_required'], ['namespace_context'])
        self.assertIsNone(next(x for x in report['checks'] if x['check'] == 'namespace_context')['ok'])
        denied, _, _ = self.observe(namespace_denied=True, administrator=True, effective='ffffffffffffffff')
        self.assertFalse(denied['ok'])
        mismatch, _, _ = self.observe(namespace_mismatch=True)
        self.assertFalse(mismatch['ok'])


class NodeSetupTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        catalog = self.root/'catalog.json'
        catalog.write_text(json.dumps(dict(format=1, environments={'default':{}})))
        self.cfg = normalize_config(dict(schema=1, instance='pilot', state_root=str(self.root/'state'),
            sandbox=dict(binary='vmm', kernel='kernel', template='rootfs', microvm_environment_catalog=str(catalog)),
            worker={}, scheduler=dict(cpu=2, memory_mb=1024, disk_mb=2048, network_mbps=100,
                api_episode_slots=2, api_inflight=2, api_rpm=60, api_tpm=100000, network_interface='eth0')),
            self.root/'host.json')
        self.node = dict(node=dict(id='n1', backends=['microvm'], environments=['default'], max_sandboxes=2,
            minimum_demands={'default':dict(concurrency=1,cpu=1,memory_mb=512,disk_mb=1024,network_mbps=0)},
            edge_socket=str(self.root/'state/sandboxes/service.sock'),
            worker_socket=str(self.root/'state/worker/worker.sock')))
        self.platform = dict(ok=True, checks=[dict(check='linux', ok=True)], facts={}, sandbox_boot_verified=False)

    def guards(self, doctor=None):
        stack = ExitStack()
        stack.enter_context(patch.object(node_probe, 'observe', return_value=self.platform))
        stack.enter_context(patch.object(node_setup, 'doctor', return_value=doctor or dict(checks=[])))
        stack.enter_context(patch.object(node_setup, 'memory_available_mb', return_value=8192))
        stack.enter_context(patch('shutil.which', return_value='/usr/bin/tool'))
        stack.enter_context(patch('os.geteuid', return_value=1001))
        stack.enter_context(patch('grp.getgrnam', return_value=SimpleNamespace(gr_gid=os.getgid())))
        return stack

    def test_preflight_is_read_only_and_setup_missing_is_distinct_from_host_block(self):
        original = deepcopy(self.cfg)
        environment = dict(os.environ)
        with self.guards(), ExitStack() as effects:
            for target in ('os.mkdir', 'os.chmod', 'os.chown', 'os.umask', 'os.execv', 'subprocess.run', 'socket.socket'):
                effects.enter_context(patch(target, side_effect=AssertionError(target)))
            report = node_setup.preflight(self.cfg, self.node)
        self.assertEqual(report['status'], 'NEEDS_INITIALIZATION')
        self.assertTrue(report['eligible_for_initialization'])
        self.assertFalse(report['ok'])
        self.assertEqual(self.cfg, original)
        self.assertEqual(dict(os.environ), environment)
        self.assertFalse((self.root/'state').exists())
        self.platform = dict(ok=False, checks=[dict(check='kvm', ok=False)], facts={})
        with self.guards(), patch.object(node_setup, 'doctor', side_effect=AssertionError('fail early')):
            report = node_setup.preflight(self.cfg, self.node)
        self.assertEqual(report['status'], 'BLOCKED')
        self.assertFalse(report['eligible_for_initialization'])

    def test_cross_instance_unprepared_environment_and_minimum_over_budget_are_rejected(self):
        for kind in ('edge', 'worker', 'environment', 'demand'):
            node = deepcopy(self.node)
            if kind == 'edge': node['node']['edge_socket'] = '/different/service.sock'
            elif kind == 'worker': node['node']['worker_socket'] = '/different/worker.sock'
            elif kind == 'environment': node['node']['environments'] = ['absent']
            else: node['node']['minimum_demands']['default']['memory_mb'] = 8192
            with self.subTest(kind=kind), self.guards():
                report = node_setup.preflight(self.cfg, node)
                self.assertEqual(report['status'], 'BLOCKED')
                self.assertFalse(next(x for x in report['checks'] if x['check'] == 'node_binding')['ok'])

    def test_unverified_namespace_is_not_reported_ready_or_blocked_for_initialization(self):
        self.platform['checks'].append(dict(check='namespace_context', ok=None))
        self.cfg['network'] = dict(helper='/usr/local/libexec/dsec-pilot-netns-helper')
        with self.guards():
            report = node_setup.preflight(self.cfg, self.node)
        self.assertEqual(report['status'], 'ADMIN_CHECK_REQUIRED')
        self.assertFalse(report['ok'])
        self.assertTrue(report['eligible_for_initialization'])
        self.assertEqual(next(x for x in report['checks'] if x['check'] == 'namespace_context')['status'],
                         'ADMIN_CHECK_REQUIRED')

    def test_resource_pressure_and_missing_storage_socket_do_not_authorize_initialization(self):
        with self.guards(), patch.object(node_setup, 'memory_available_mb', return_value=0):
            report = node_setup.preflight(self.cfg, self.node)
        self.assertFalse(report['eligible_for_initialization'])
        self.assertEqual(next(x for x in report['checks'] if x['check'] == 'memory_floor')['status'], 'RESOURCE_WAIT')
        with self.guards(dict(checks=[dict(check='ublk_socket', ok=False, error='unavailable')])):
            report = node_setup.preflight(self.cfg, self.node)
        self.assertEqual(report['status'], 'BLOCKED')

    def test_memory_headroom_is_capped_by_container_cgroup_not_host_memavailable(self):
        contents = {'/proc/meminfo':'MemAvailable: 8388608 kB\n',
                    '/sys/fs/cgroup/memory.max':str(1024*2**20),
                    '/sys/fs/cgroup/memory.current':str(768*2**20)}
        def path(name):
            return SimpleNamespace(read_text=lambda: contents[name], is_file=lambda: name in contents)
        with patch.object(node_setup, 'Path', side_effect=path):
            self.assertEqual(node_setup.memory_available_mb(), 256)
            contents['/sys/fs/cgroup/memory.max'] = 'max'
            self.assertEqual(node_setup.memory_available_mb(), 8192)

    def test_prepare_node_rejects_before_output_and_reuses_existing_renderer_when_eligible(self):
        out = self.root/'bundle'
        with patch('pwd.getpwnam', return_value=SimpleNamespace(pw_uid=1001)), \
             patch('os.getuid', return_value=1001), patch('os.geteuid', return_value=1001), \
             patch.object(node_setup, 'preflight', return_value=dict(eligible_for_initialization=False,
                 checks=[dict(check='kvm', status='BLOCKED')])):
            with patch('dsec.host.deployment.render_privileges') as renderer:
                with self.assertRaisesRegex(ValueError, 'kvm'):
                    node_setup.prepare_node(self.cfg, self.node, out, 'runner', 4096)
                renderer.assert_not_called()
        self.assertFalse(out.exists())
        with self.guards(), patch('pwd.getpwnam', return_value=SimpleNamespace(pw_uid=os.getuid())), \
             patch('dsec.host.deployment.render_privileges', return_value={'bundle':'reviewable'}) as renderer:
            result = node_setup.prepare_node(self.cfg, self.node, out, 'runner', 4096)
        renderer.assert_called_once_with(self.cfg, out, 'runner', 4096)
        self.assertFalse(result['services_started'])

    def test_node_gateway_rejects_before_directory_lock_or_control_client_creation(self):
        config = self.root/'node.json'
        for explicit in (True, False):
            node = deepcopy(self.node)
            if not explicit:
                node['node'].pop('backends')  # The gateway defaults to microvm.
            config.write_text(json.dumps(node))
            with self.subTest(explicit=explicit), \
                 patch.object(sys, 'argv', ['cluster', 'node', '--config', str(config)]), \
                 patch.object(node_probe, 'observe', return_value=dict(ok=False, checks=[dict(check='kvm',ok=False)])), \
                 patch('os.umask', side_effect=AssertionError('mutation')), \
                 patch('pathlib.Path.mkdir', side_effect=AssertionError('mkdir')), \
                 patch.object(cluster, 'components', side_effect=AssertionError('clients')):
                with self.assertRaisesRegex(RuntimeError, 'kvm'):
                    cluster.main()
        self.assertFalse((self.root/'state').exists())

    def test_controller_and_bootstrap_roles_do_not_require_microvm_host_permissions(self):
        config = self.root/'controller.json'; config.write_text('{}')
        for role in ('controller', 'bootstrap'):
            with self.subTest(role=role), \
                 patch.object(sys, 'argv', ['cluster', role, '--config', str(config)]), \
                 patch.object(node_probe, 'observe', side_effect=AssertionError('not an Edge')), \
                 patch('os.umask'), patch.object(cluster, 'components', side_effect=RuntimeError('composition reached')):
                with self.assertRaisesRegex(RuntimeError, 'composition reached'): cluster.main()

    def test_cli_preflight_has_no_service_effects_and_returns_nonzero_for_blocked_node(self):
        configuration = self.root/'host.json'
        configuration.write_text(json.dumps({k:v for k,v in self.cfg.items() if k != '_file'}))
        with patch.object(sys, 'argv', ['dsec-host','--config',str(configuration),'preflight']), \
             patch.object(cli, 'preflight', return_value=dict(ok=False,status='BLOCKED')), \
             patch.object(cli, 'run_service', side_effect=AssertionError('start')), \
             redirect_stdout(io.StringIO()) as output:
            with self.assertRaises(SystemExit) as ended: cli.main()
        self.assertEqual(ended.exception.code, 1)
        self.assertEqual(json.loads(output.getvalue())['status'], 'BLOCKED')


class InstallerGuardTests(unittest.TestCase):
    def installer(self, probe):
        source = INSTALLER.replace('__SPECS__', '[]').replace('__STATE__', repr('/must-not-create'))
        source = source.replace('__GUARD_SOURCE__', repr(probe)).replace('__RUNTIME_IDENTITY__', repr(('runner',1001,993)))
        namespace = {'__name__':'test_installer', '__file__':'/reviewed/install.py'}
        exec(compile(source,'generated-installer.py','exec'),namespace)
        return namespace

    def test_root_apply_rechecks_live_capabilities_before_any_target_or_state_mutation(self):
        probe = "def observe(**kwargs): return {'ok':False}\ndef require_eligible(report): raise RuntimeError('no KVM')\n"
        namespace = self.installer(probe)
        with patch('os.geteuid', return_value=0), patch.object(sys,'argv',['installer','--apply']), \
             patch('pathlib.Path.read_bytes', side_effect=AssertionError('target read')), \
             patch('os.mkdir', side_effect=AssertionError('write')), \
             patch('subprocess.run', side_effect=AssertionError('process')):
            with self.assertRaisesRegex(RuntimeError,'no KVM'): namespace['main']()

    def test_root_success_does_not_replace_runtime_identity_device_check(self):
        probe = "def observe(**kwargs): return {'ok':True}\ndef require_eligible(report): pass\n"
        namespace = self.installer(probe)
        with patch('pwd.getpwnam', return_value=SimpleNamespace(pw_uid=1001,pw_gid=1001)), \
             patch('grp.getgrnam', return_value=SimpleNamespace(gr_gid=993)), \
             patch('os.getgrouplist', return_value=[1001,993]), \
             patch('subprocess.run', return_value=SimpleNamespace(returncode=1)) as runtime:
            with self.assertRaisesRegex(RuntimeError,'Runtime account'): namespace['check_node_platform']()
        command = runtime.call_args.args[0]
        self.assertEqual(command[1:3], ['-I','-c'])
        ast.parse(command[3])
        self.assertLess(command[3].index('os.setuid'), command[3].index('report = observe()'))


if __name__ == '__main__':
    unittest.main()
