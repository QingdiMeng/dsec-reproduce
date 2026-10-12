"""Deployment boundary checks; real Docker/quorum/Node gates are separate."""
import json
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
import zipfile
from unittest.mock import patch
from deploy.cluster.export_hosts import export
from deploy.cluster.prepare import topology_file

from deploy.cluster.prepare import prepare
from deploy.cluster.initialize import EtcdError


class ClusterComposeTests(unittest.TestCase):
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
            for name in ('dsec/host/cluster.py','dsec/sdk/rollout_api.py'):
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

    def test_cross_host_topology_rejects_ambiguous_ports_and_ownership(self):
        value=self.topology();file=self.root/'topology.json'
        for mutation in ('port','owner','root'):
            bad=json.loads(json.dumps(value))
            if mutation=='port':bad['controllers'][0]['port']=2379
            if mutation=='owner':bad['controllers'][0]['uid']=1001
            if mutation=='root':bad['etcd'][0]['uid']=0
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
