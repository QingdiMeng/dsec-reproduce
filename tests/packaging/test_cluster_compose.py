"""Deployment boundary checks; real Docker/quorum/Node gates are separate."""
import ast
import json
import os
from copy import deepcopy
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
import zipfile
from unittest.mock import patch
from deploy.cluster.export_hosts import export
from deploy.cluster.prepare import topology_file

from deploy.cluster.prepare import prepare, render
from deploy.cluster.planning import read_plan
from deploy.cluster.configuration import role_configurations
from deploy.cluster.initialize import EtcdError, initialize, token_exports


class ClusterComposeTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('openssl'),'Rotation material needs OpenSSL')
    def test_security_patches_preserve_master_and_rotated_enrollment_exports_only_selected_material(self):
        from deploy.cluster.rotate import enable
        from deploy.cluster.enroll_node import enroll
        out=prepare(self.args)
        original={str(p.relative_to(out)):p.read_bytes() for p in out.rglob('*') if p.is_file()}
        patches=self.root/'security';enable(out,patches)
        self.assertEqual(original,{str(p.relative_to(out)):p.read_bytes() for p in out.rglob('*') if p.is_file()})
        self.assertFalse((patches/'pki').exists())
        shutil.copyfile(patches/'controllers/controller.json',out/'config/controller.json')
        for p in (patches/'controllers/secrets').iterdir():shutil.copyfile(p,out/'secrets/controllers'/p.name)
        shutil.copyfile(patches/'nodes/node1/node.json',out/'node/node.json')
        for p in (patches/'nodes/node1/secrets').iterdir():shutil.copyfile(p,out/'node/secrets'/p.name)
        entry=dict(address='192.0.2.30',port=18445,directory='/srv/dsec/new-node',spec=json.loads(self.args.node_spec.read_text()))
        entry['spec']['id']='new-node';entry['spec']['edge_socket']='/srv/dsec/new-edge/service.sock';entry['spec']['worker_socket']='/srv/dsec/new-worker/worker.sock'
        with self.assertRaises(ValueError):enroll(out,entry,self.root/'missing-issuer')
        target=self.root/'new-bundle';enroll(out,entry,target,issuer=(out/'pki/ca.crt',out/'pki/ca.key'))
        config=json.loads((target/'node.json').read_text())
        self.assertIn('security_file',config['coordination']);self.assertIn('identity_file',config['coordination']['authentication'])
        self.assertIn('node_auth_keys_file',config);self.assertIn('security_file',config['server'])
        self.assertFalse(any(p.name in ('ca.key','root-password','bootstrap-token','controller.key') for p in target.rglob('*')))
        self.assertEqual((target/'secrets/route-key').read_bytes(),(out/'secrets/controllers/route-key').read_bytes())

    def test_only_explicit_missing_identity_allows_initializer_creation(self):
        self.assertTrue(EtcdError('auth/user/get',9,'etcdserver: user name not found').missing)
        self.assertTrue(EtcdError('auth/role/get',9,'etcdserver: role name not found').missing)
        self.assertFalse(EtcdError('auth/user/get',9,'etcdserver: authentication failed').missing)
        self.assertFalse(EtcdError('auth/user/get',5,'unrelated missing record').missing)

    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        wheel = self.root/'dsec_reproduce-0.1.0.dev0-py3-none-any.whl'
        with zipfile.ZipFile(wheel,'w') as package:
            for name in ('dsec/host/cluster.py','dsec/sdk/rollout_api.py',
                         'dsec/control/node_lifecycle.py','dsec/host/node_directory.py'):
                package.writestr(name,'# structural fixture, not an installed runtime\n')
        spec = self.root/'node-spec.json'
        spec.write_text(json.dumps(dict(id='node1',edge_socket='/srv/dsec/edge/service.sock',
            worker_socket='/srv/dsec/worker/worker.sock',max_sandboxes=2,
            backends=['microvm'],environments=['default'],minimum_demands={
                'default':dict(concurrency=1,cpu=1,memory_mb=512,disk_mb=1024,network_mbps=1)})))
        self.args = SimpleNamespace(out=self.root/'output',wheel=wheel,
            python_image='python@sha256:'+'a'*64,etcd_image='quay.io/coreos/etcd@sha256:'+'b'*64,
            project='dsec-preview-test',host_address='192.0.2.10',node_address='192.0.2.20',
            node_directory='/srv/dsec/gateway-preview',node_spec=spec,port_base=18430,node_port=18444)

    def test_invalid_identity_or_mutable_image_rejected_before_output(self):
        for key,value in (('python_image','python:latest'),('project','../../other'),
                          ('host_address','0.0.0.0'),('node_directory','relative'),
                          ('port_base',65530)):
            previous = getattr(self.args,key);setattr(self.args,key,value)
            with self.assertRaises(ValueError):prepare(self.args)
            self.assertFalse(self.args.out.exists())
            setattr(self.args,key,previous)

    @unittest.skipUnless(shutil.which('openssl'),'Enrollment signing needs OpenSSL')
    def test_new_node_bundle_preserves_controllers_and_never_exports_ca_or_admin_secrets(self):
        from deploy.cluster.enroll_node import enroll
        out=prepare(self.args)
        original={str(path.relative_to(out)):path.read_bytes() for path in out.rglob('*') if path.is_file()}
        entry=dict(address='192.0.2.30',port=18445,directory='/srv/dsec/third-node',
                   spec=json.loads(self.args.node_spec.read_text()))
        entry['spec']['id']='new-node'
        entry['spec']['edge_socket']='/srv/dsec/third-edge/service.sock'
        entry['spec']['worker_socket']='/srv/dsec/third-worker/worker.sock'
        target=self.root/'enrolled'
        result=enroll(out,entry,target)
        self.assertFalse(result['registered']);self.assertFalse(result['services_started'])
        self.assertEqual(original,{str(path.relative_to(out)):path.read_bytes() for path in out.rglob('*') if path.is_file()})
        config=json.loads((target/'node.json').read_text())
        self.assertTrue(config['node_lifecycle'])
        self.assertEqual(config['server']['node_token_mode'],'derived-v1')
        self.assertNotIn('node_token_file',config['server'])
        self.assertNotIn('token_file',config['coordination'])
        self.assertEqual(set(p.name for p in (target/'secrets').iterdir()),
                         {'node.crt','node.key','ca.crt','etcd-client.crt','etcd-client.key','route-key','dsec-password'})
        self.assertFalse(any(p.name in ('ca.key','bootstrap-token','root-password','controller.key') for p in target.rglob('*')))
        details=subprocess.check_output(['openssl','x509','-in',str(target/'secrets/node.crt'),'-noout','-text'],text=True)
        self.assertIn('IP Address:192.0.2.30',details)
        self.assertEqual(json.loads((target/'registration.json').read_text()),dict(id='new-node',endpoints=['https://192.0.2.30:18445']))
        with self.assertRaises(FileExistsError):enroll(out,entry,target)
        for kind in ('identity','listener','root'):
            bad=deepcopy(entry)
            if kind=='identity':bad['spec']['id']='node1'
            if kind=='listener':bad.update(address='192.0.2.10',port=18440)
            if kind=='root':bad.update(address='192.0.2.20',directory='/srv/dsec/gateway-preview/nested')
            with self.subTest(kind=kind),self.assertRaises(ValueError):enroll(out,bad,self.root/('bad-'+kind))
            self.assertFalse((self.root/('bad-'+kind)).exists())

    def test_plan_is_read_only_and_captures_inputs_without_shared_mutable_state(self):
        environment = dict(os.environ)
        files = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        with patch('pathlib.Path.mkdir', side_effect=AssertionError('no writes')), \
             patch('pathlib.Path.write_text', side_effect=AssertionError('no writes')), \
             patch('pathlib.Path.write_bytes', side_effect=AssertionError('no writes')), \
             patch('os.umask', side_effect=AssertionError('no host effects')), \
             patch('os.chmod', side_effect=AssertionError('no host effects')), \
             patch('subprocess.run', side_effect=AssertionError('no processes')):
            plan = read_plan(self.args)
            summary = plan.describe()
        self.assertFalse(self.args.out.exists())
        self.assertEqual(files, {p.relative_to(self.root): p.read_bytes()
                                for p in self.root.rglob('*') if p.is_file()})
        self.assertEqual(dict(os.environ), environment)
        self.assertEqual(summary['etcd_failure_domains'], 1)
        self.assertEqual(summary['model_gpu_management'], 'external')
        self.assertFalse(summary['credentials_generated'])
        self.assertFalse(summary['services_started'])
        spec = plan.node_spec
        spec['minimum_demands']['default']['memory_mb'] = 0
        self.assertEqual(plan.node_spec['minimum_demands']['default']['memory_mb'], 512)
        self.args.project = 'changed'
        self.args.node_spec.write_text('{}')
        self.assertEqual(plan.project, 'dsec-preview-test')
        self.assertEqual(plan.node_spec['id'], 'node1')

    def test_changed_artifact_requires_replanning_before_render_writes(self):
        plan = read_plan(self.args)
        self.args.wheel.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'Wheel changed after planning'):
            render(plan)
        self.assertFalse(self.args.out.exists())

    def test_plan_cli_needs_neither_installed_core_nor_certificate_generator(self):
        command = [__import__('sys').executable, str(Path(__file__).resolve().parents[2]/'deploy/cluster/prepare.py')]
        for option in ('out', 'wheel', 'python_image', 'etcd_image', 'project', 'host_address',
                       'node_address', 'node_directory', 'node_spec', 'port_base', 'node_port'):
            command.extend(['--'+option.replace('_', '-'), str(getattr(self.args, option))])
        environment = dict(os.environ, PATH='')
        environment.pop('PYTHONPATH', None)
        result = subprocess.run([*command, '--plan'], env=environment,
                                check=True, capture_output=True, text=True)
        self.assertEqual(json.loads(result.stdout)['status'], 'planned')
        self.assertFalse(self.args.out.exists())

    @unittest.skipUnless(shutil.which('openssl'),'Certificate check needs OpenSSL')
    def test_rendered_tls_routes_private_secrets_and_install_boundary(self):
        out = prepare(self.args)
        controller = json.loads((out/'config/controller.json').read_text())
        node = json.loads((out/'node/node.json').read_text())
        client = json.loads((out/'client.json').read_text())
        self.assertEqual(controller['coordination']['prefix'],node['coordination']['prefix'])
        self.assertEqual(node['coordination']['endpoints'],[
            'https://192.0.2.10:18430','https://192.0.2.10:18431','https://192.0.2.10:18432'])
        self.assertEqual(client['endpoints'],['https://192.0.2.10:18440','https://192.0.2.10:18441'])
        self.assertEqual(node['node']['edge_socket'],'/srv/dsec/edge/service.sock')
        self.assertEqual(node['node']['gateway_root'],'/srv/dsec/gateway-preview/gateway')
        for directory in ('secrets/controllers','node/secrets'):
            self.assertFalse((out/directory/'ca.key').exists())
            self.assertFalse((out/directory/'root-password').exists())
            self.assertFalse((out/directory/'etcd-token').exists())  # quorum initializer writes it
        for path in (out/'secrets').rglob('*'):
            if path.is_file():self.assertEqual(stat.S_IMODE(path.stat().st_mode),0o600)
        self.assertEqual((out/'secrets/controllers/route-key').read_bytes(),
                         (out/'node/secrets/route-key').read_bytes())
        result = subprocess.run(['openssl','x509','-in',str(out/'node/secrets/node.crt'),
            '-noout','-text'],check=True,capture_output=True,text=True).stdout
        self.assertIn('IP Address:192.0.2.20',result)
        self.assertIn('Authority Key Identifier',result)
        self.assertIn('TLS Web Server Authentication',result)
        for filename in ('secrets/initialize/client.crt','secrets/controllers/etcd-client.crt'):
            subject = subprocess.run(['openssl','x509','-in',str(out/filename),'-noout','-subject'],
                check=True,capture_output=True,text=True).stdout
            self.assertIn('DSec gateway client',subject)
            self.assertNotIn('CN=',subject.replace(' ',''))
        build = (out/'image/Dockerfile').read_text()
        self.assertIn('--no-index --no-deps',build)
        self.assertFalse((out/'image/pki').exists())
        compose = (out/'compose.yaml').read_text()
        for unsafe in ('privileged:','network_mode: host','docker.sock','NET_ADMIN'):
            self.assertNotIn(unsafe,compose)
        self.assertIn('ETCD_CLIENT_CERT_AUTH: "true"',compose)
        self.assertIn('service_completed_successfully',compose)
        with self.assertRaises(FileExistsError):prepare(self.args)

    def topology(self):
        return dict(etcd=[dict(address=f'192.0.2.{n}',client_port=2379,peer_port=2380,
                    uid=1000,gid=1000) for n in (10,20,30)],
                    controllers=[dict(address=f'192.0.2.{n}',port=8443,uid=1000,gid=1000)
                                 for n in (10,20)])

    def batch_inventory(self):
        first = json.loads(self.args.node_spec.read_text())
        second = deepcopy(first)
        second.update(id='node2', max_sandboxes=8, backends=['microvm', 'container'],
                      environments=['python'], minimum_demands={
                          'python':dict(concurrency=1, cpu=2, memory_mb=1024, disk_mb=2048, network_mbps=0)})
        second.pop('worker_socket')
        return dict(schema=1, root_quota=dict(concurrency=16, cpu=32, memory_mb=32768,
                                             disk_mb=65536, network_mbps=1000), nodes=[
            dict(address='192.0.2.20', port=18444, directory='/srv/dsec/first-gateway', spec=first),
            dict(address='192.0.2.40', port=8444, directory='/srv/dsec/second-gateway', spec=second)])

    def use_batch(self, inventory):
        self.args.nodes = self.root/'nodes.json'
        self.args.nodes.write_text(json.dumps(inventory))
        for name in ('node_address', 'node_port', 'node_directory', 'node_spec'):
            setattr(self.args, name, None)

    def test_batch_plan_and_roles_are_read_only_and_independent_snapshots(self):
        inventory = self.batch_inventory(); self.use_batch(inventory)
        environment = dict(os.environ)
        with patch('pathlib.Path.mkdir', side_effect=AssertionError('no writes')), \
             patch('pathlib.Path.write_text', side_effect=AssertionError('no writes')), \
             patch('pathlib.Path.write_bytes', side_effect=AssertionError('no writes')), \
             patch('os.umask', side_effect=AssertionError('no host effects')), \
             patch('subprocess.run', side_effect=AssertionError('no processes')):
            plan = read_plan(self.args)
            roles = role_configurations(plan, uid=1000, gid=1000)
            summary = plan.describe()
        self.assertFalse(self.args.out.exists())
        self.assertEqual(os.environ, environment)
        self.assertEqual(len(summary['nodes']), 2)
        self.assertEqual(set(roles['controller']['nodes']), {'node1', 'node2'})
        self.assertEqual(roles['controller']['root_quota'], inventory['root_quota'])
        self.assertEqual(roles['initialize']['root_quota'], inventory['root_quota'])
        self.assertEqual(token_exports(roles['initialize']), ('/secrets',))
        self.assertEqual(roles['nodes']['node2']['node']['max_sandboxes'], 8)
        self.assertNotIn('worker_socket', roles['nodes']['node2']['node'])
        self.assertNotIn('token_file', roles['nodes']['node2']['coordination'])
        roles['nodes']['node1']['coordination']['endpoints'].clear()
        roles['initialize']['coordination']['authentication']['username'] = 'modified'
        self.assertTrue(roles['nodes']['node2']['coordination']['endpoints'])
        self.assertTrue(roles['controller']['coordination']['endpoints'])
        self.assertEqual(roles['controller']['coordination']['authentication']['username'], 'dsec')
        plan.nodes[0]['spec']['minimum_demands'].clear()
        self.args.nodes.write_text('{}')
        self.assertEqual(plan.nodes, inventory['nodes'])
        with self.assertRaisesRegex(ValueError, 'plan.nodes'):
            _ = plan.node_spec

    def test_bad_batch_inventory_fails_before_render_or_secret_generation(self):
        original = self.batch_inventory(); self.use_batch(original)
        def mutate(case):
            value = deepcopy(original)
            first, second = value['nodes']
            if case == 'duplicate-id': second['spec']['id'] = first['spec']['id']
            elif case == 'duplicate-port': second.update(address=first['address'], port=first['port'])
            elif case == 'controller-port': first.update(address=self.args.host_address, port=18440)
            elif case == 'nested-directory': second.update(address=first['address'], directory=first['directory']+'/child')
            elif case == 'edge-lock-parent': second.update(address=first['address'], directory='/srv/second')
            elif case == 'worker-owner':
                second.update(address=first['address'], directory='/srv/second')
                second['spec']['edge_socket'] = '/srv/second-edge/service.sock'
                second['spec']['worker_socket'] = first['spec']['worker_socket']
            elif case == 'bad-port': first['port'] = 0
            elif case == 'wildcard': first['address'] = '0.0.0.0'
            elif case == 'integer-address': first['address'] = 1234
            elif case == 'boolean-address': first['address'] = True
            elif case == 'bad-path': first['directory'] = '/srv/../other'
            elif case == 'traversal-id': first['spec']['id'] = '../other'
            elif case == 'missing-quota': first['spec']['minimum_demands']['default'].pop('memory_mb')
            elif case == 'negative-quota': first['spec']['minimum_demands']['default']['cpu'] = -1
            elif case == 'bool-quota': value['root_quota']['cpu'] = True
            elif case == 'missing-socket': first['spec']['edge_socket'] = ''
            elif case == 'empty-backends': first['spec']['backends'] = []
            elif case == 'empty-nodes': value['nodes'] = []
            elif case == 'schema': value['schema'] = True
            elif case == 'unknown-field': value['gpu_scheduler'] = True
            return value
        for case in ('duplicate-id', 'duplicate-port', 'controller-port', 'nested-directory',
                     'edge-lock-parent', 'worker-owner', 'bad-port', 'wildcard', 'integer-address',
                     'boolean-address', 'bad-path',
                     'traversal-id', 'missing-quota', 'negative-quota', 'bool-quota',
                     'missing-socket', 'empty-backends', 'empty-nodes', 'schema', 'unknown-field'):
            with self.subTest(case=case):
                self.args.nodes.write_text(json.dumps(mutate(case)))
                with patch('subprocess.run', side_effect=AssertionError('no processes')):
                    with self.assertRaises(ValueError): prepare(self.args)
                self.assertFalse(self.args.out.exists())
        self.args.nodes.write_text(json.dumps(original))
        self.args.node_port = 18444
        with self.assertRaisesRegex(ValueError, 'combined'): read_plan(self.args)

    def test_generated_roles_are_consumed_by_the_actual_cluster_composition(self):
        from dsec.host import cluster
        from dsec.control.cluster_api import ClusterAPI
        from dsec.control.node_gateway import NodeGateway
        self.use_batch(self.batch_inventory())
        roles = role_configurations(read_plan(self.args), uid=1000, gid=1000)
        with patch.object(cluster, 'EtcdStore'), patch.object(cluster, 'JSONRPC'), \
             patch.object(cluster, 'SandboxClient'), patch.object(cluster, 'RolloutClient'), \
             patch.object(cluster, 'secret_file', side_effect=lambda p: 'ab'*32 if p.endswith('route-key') else 'c'*64), \
             patch('pathlib.Path.read_text', return_value='boot1'):
            controller = cluster.components(roles['controller'], 'controller')
            self.assertIsInstance(controller, ClusterAPI)
            self.assertEqual(set(controller.nodes), {'node1', 'node2'})
            for identity, config in roles['nodes'].items():
                gateway = cluster.components(config, 'node')
                self.assertIsInstance(gateway, NodeGateway)
                self.assertEqual(gateway.node_id, identity)
                self.assertEqual(gateway.environments, tuple(config['node']['environments']))
                self.assertEqual(gateway.max_sandboxes, config['node']['max_sandboxes'])

    def test_colocated_nodes_and_control_roles_require_disjoint_roots_and_listeners(self):
        inventory = self.batch_inventory()
        first, second = inventory['nodes']
        second['address'] = first['address']
        second['spec']['edge_socket'] = '/srv/dsec/second-edge/service.sock'
        self.use_batch(inventory)
        self.assertEqual(len(read_plan(self.args).nodes), 2)
        self.args.topology = self.root/'topology.json'
        self.args.topology.write_text(json.dumps(self.topology()))
        self.assertEqual(len(read_plan(self.args).nodes), 2)
        inventory['nodes'][0].update(address='192.0.2.10', port=2380)
        self.args.nodes.write_text(json.dumps(inventory))
        with self.assertRaisesRegex(ValueError, 'listener collision'):
            read_plan(self.args)

    def test_batch_plan_cli_and_legacy_token_exports(self):
        self.use_batch(self.batch_inventory())
        command = [sys.executable, str(Path(__file__).resolve().parents[2]/'deploy/cluster/prepare.py')]
        for name in ('out', 'wheel', 'python_image', 'etcd_image', 'project', 'host_address', 'nodes'):
            command.extend(['--'+name.replace('_', '-'), str(getattr(self.args, name))])
        environment = dict(os.environ, PATH=''); environment.pop('PYTHONPATH', None)
        result = subprocess.run([*command, '--plan'], check=True, capture_output=True,
                                text=True, env=environment)
        self.assertEqual(len(json.loads(result.stdout)['nodes']), 2)
        self.assertFalse(self.args.out.exists())
        self.assertEqual(token_exports({}), ('/secrets', '/node-secrets'))
        with patch('ssl.create_default_context', side_effect=AssertionError('no connection')):
            with self.assertRaises(ValueError): initialize({'export_node_token': 'false'})

    def test_deployment_planning_and_role_configuration_have_no_runtime_dependencies(self):
        root = Path(__file__).resolve().parents[2]
        for name in ('planning', 'configuration'):
            source = root/'deploy/cluster'/(name+'.py')
            for node in ast.walk(ast.parse(source.read_text())):
                names = ([n.name for n in node.names] if isinstance(node, ast.Import)
                         else [node.module or ''] if isinstance(node, ast.ImportFrom) else [])
                for imported in names:
                    self.assertIn(imported.split('.')[0], sys.stdlib_module_names)
                    self.assertNotIn(imported.split('.')[0], ('os', 'subprocess', 'socket'))

    @unittest.skipUnless(shutil.which('openssl'),'Certificate check needs OpenSSL')
    def test_batch_render_has_per_node_credentials_and_matching_routes(self):
        inventory = self.batch_inventory(); self.use_batch(inventory)
        out = prepare(self.args)
        controller = json.loads((out/'config/controller.json').read_text())
        initializer = json.loads((out/'config/initialize.json').read_text())
        self.assertFalse(initializer['export_node_token'])
        self.assertNotIn('/node-secrets', (out/'compose.yaml').read_text())
        self.assertFalse((out/'node').exists())
        tokens, certificates = [], []
        for node in inventory['nodes']:
            identity = node['spec']['id']; directory = out/'nodes'/identity
            config = json.loads((directory/'node.json').read_text())
            route = controller['nodes'][identity]
            self.assertEqual(route['endpoints'], [f"https://{node['address']}:{node['port']}"])
            self.assertEqual(config['server']['host'], node['address'])
            self.assertEqual(config['server']['port'], node['port'])
            self.assertEqual(config['node']['gateway_root'], node['directory']+'/gateway')
            token = (directory/'secrets/node-token').read_bytes(); tokens.append(token)
            self.assertEqual(token, (out/'secrets/controllers'/Path(route['token_file']).name).read_bytes())
            self.assertEqual((directory/'secrets/route-key').read_bytes(), (out/'secrets/controllers/route-key').read_bytes())
            self.assertEqual(set(p.name for p in (directory/'secrets').iterdir()), {
                'ca.crt', 'node.crt', 'node.key', 'etcd-client.crt', 'etcd-client.key',
                'node-token', 'dsec-password', 'route-key'})
            for path in directory.rglob('*'):
                if path.is_file(): self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                else: self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
            cert = directory/'secrets/node.crt'; certificates.append(cert.read_bytes())
            detail = subprocess.run(['openssl', 'x509', '-in', str(cert), '-noout', '-text'],
                                    check=True, capture_output=True, text=True).stdout
            self.assertIn('IP Address:'+node['address'], detail)
        self.assertNotEqual(tokens[0], tokens[1])
        self.assertNotEqual(certificates[0], certificates[1])

    @unittest.skipUnless(shutil.which('openssl'),'Certificate check needs OpenSSL')
    def test_batch_host_export_includes_only_local_nodes_and_bootstrap_on_designated_host(self):
        inventory = self.batch_inventory(); self.use_batch(inventory)
        self.args.topology = self.root/'topology.json'
        self.args.topology.write_text(json.dumps(self.topology()))
        out = prepare(self.args)
        template = dict(name=self.args.project, services={})
        for name in ('etcd1', 'etcd2', 'etcd3'):
            template['services'][name] = dict(environment={}, volumes=[], ports=[])
        for name in ('api1', 'api2', 'initialize'):
            template['services'][name] = dict(build=dict(context='/original'), depends_on={}, volumes=[], ports=[])
        with patch('deploy.cluster.export_hosts.subprocess.run', return_value=
                   SimpleNamespace(returncode=0, stdout=json.dumps(template))):
            hosts = export(out, self.root/'hosts')
        self.assertEqual(hosts['192.0.2.20']['nodes'], ['node1'])
        self.assertEqual(hosts['192.0.2.40']['nodes'], ['node2'])
        for address, roles in hosts.items():
            host = self.root/'hosts'/address
            self.assertFalse((host/'pki').exists())
            self.assertEqual((host/'secrets/initialize').exists(), address == '192.0.2.10')
            node_paths = set(p.name for p in (host/'nodes').iterdir()) if (host/'nodes').exists() else set()
            self.assertEqual(node_paths, set(roles['nodes']))
            if address == '192.0.2.40':
                self.assertFalse((host/'compose.yaml').exists())
                self.assertFalse((host/'secrets').exists())
                self.assertFalse((host/'image').exists())
                info = json.loads((host/'host.json').read_text())
                self.assertEqual(info['node_destinations'], {'node2': '/srv/dsec/second-gateway'})
            if address == '192.0.2.10':
                config = json.loads((host/'compose.yaml').read_text())
                self.assertNotIn('/node-secrets', [v['target'] for v in config['services']['initialize']['volumes']])

    def test_cross_host_topology_rejects_ambiguous_ports_and_ownership(self):
        value=self.topology();file=self.root/'topology.json'
        for mutation in ('port','owner','root','address-type'):
            bad=json.loads(json.dumps(value))
            if mutation=='port':bad['controllers'][0]['port']=2379
            if mutation=='owner':bad['controllers'][0]['uid']=1001
            if mutation=='root':bad['etcd'][0]['uid']=0
            if mutation=='address-type':bad['etcd'][0]['address']=1234
            file.write_text(json.dumps(bad))
            with self.assertRaises(ValueError):topology_file(file)

    @unittest.skipUnless(shutil.which('openssl'),'Certificate check needs OpenSSL')
    def test_cross_host_export_limits_credentials_and_nat_peer_identity(self):
        file=self.root/'topology.json';file.write_text(json.dumps(self.topology()))
        self.args.topology=file;out=prepare(self.args)
        # CLI rendering is separate from role export; no daemon in this test.
        template=dict(name=self.args.project,services={})
        for name in ('etcd1','etcd2','etcd3'):
            template['services'][name]=dict(environment={},volumes=[],ports=[])
        for name in ('api1','api2','initialize'):
            template['services'][name]=dict(build=dict(context='/original'),
                depends_on=dict(initialize={}),volumes=[],ports=[])
        with patch('deploy.cluster.export_hosts.subprocess.run',return_value=
                   SimpleNamespace(returncode=0,stdout=json.dumps(template))):
            export(out,self.root/'hosts')
        for address,roles in (('192.0.2.10',{'etcd1','api1','initialize'}),
                              ('192.0.2.20',{'etcd2','api2'}),('192.0.2.30',{'etcd3'})):
            host=self.root/'hosts'/address;cfg=json.loads((host/'compose.yaml').read_text())
            self.assertEqual(set(cfg['services']),roles)
            self.assertFalse((host/'pki').exists())
            self.assertEqual((host/'secrets/initialize/root-password').exists(),address=='192.0.2.10')
            for name,service in cfg['services'].items():
                self.assertNotIn('depends_on',service)
                self.assertEqual(service['user'],'1000:1000')
                if name.startswith('etcd'):
                    env=service['environment']
                    self.assertEqual(env['ETCD_PEER_CERT_ALLOWED_CN'],'dsec-peer-1,dsec-peer-2,dsec-peer-3')
                    self.assertNotIn('ETCD_PEER_SKIP_CLIENT_SAN_VERIFICATION',env)
                    self.assertEqual(service['volumes'][0]['source'],'./data/'+name)
                    cert=host/'secrets'/name/'peer-client.crt'
                    text=subprocess.run(['openssl','x509','-in',str(cert),'-text','-noout'],
                                         check=True,capture_output=True,text=True).stdout
                    self.assertNotIn('Subject Alternative Name',text)
                    self.assertIn('TLS Web Client Authentication',text)
                    member=host/'secrets'/name/'member.crt'
                    subject=subprocess.run(['openssl','x509','-in',str(member),'-subject','-noout'],
                                           check=True,capture_output=True,text=True).stdout
                    # etcd applies its peer CN allowlist to both directions.
                    self.assertIn('CN=dsec-peer-'+name[-1],subject.replace(' ',''))
        node=json.loads((out/'node/node.json').read_text())
        self.assertNotIn('token_file',node['coordination'])
        self.assertEqual(node['coordination']['authentication']['username'],'dsec')
        self.assertEqual(node['coordination']['endpoints'],[
            f'https://192.0.2.{n}:2379' for n in (10,20,30)])


if __name__ == '__main__':unittest.main()
