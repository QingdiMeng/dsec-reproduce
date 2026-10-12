"""Render a private Docker control-plane preview plus one external Node config.

No Docker calls, host changes, downloads or service starts. The operator supplies
an installed-core wheel, immutable image references and a prepared Node spec.
"""
import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import zipfile


HERE = Path(__file__).resolve().parent
IMAGE = re.compile(r'[a-zA-Z0-9./:_-]+@sha256:[0-9a-f]{64}')
NAME = re.compile(r'[a-z0-9][a-z0-9_-]{0,47}')


def private_json(path, value):
    path.write_text(json.dumps(value, indent=2)+'\n')
    path.chmod(0o600)


def topology_file(path):
    if path is None:
        return None
    value=json.loads(path.read_text())
    if not isinstance(value,dict) or set(value)!={'etcd','controllers'} or len(value['etcd'])!=3 or len(value['controllers'])!=2:
        raise ValueError('Topology requires three etcd members and two controllers')
    listeners=set()
    identities={}
    for kind,fields in (('etcd',{'address','client_port','peer_port','uid','gid'}),
                        ('controllers',{'address','port','uid','gid'})):
        for member in value[kind]:
            if not isinstance(member,dict) or set(member)!=fields:
                raise ValueError('Invalid topology member fields')
            address=ipaddress.IPv4Address(member['address'])
            if address.is_unspecified or address.is_multicast or address.is_link_local or address.is_loopback:
                raise ValueError('Topology needs explicit reachable host addresses')
            for name in fields-{'address','uid','gid'}:
                port=member[name]
                if type(port) is not int or not 1024<=port<=65535 or (str(address),port) in listeners:
                    raise ValueError('Topology listener collision or invalid port')
                listeners.add((str(address),port))
            if (type(member['uid']) is not int or member['uid']<=0
                    or type(member['gid']) is not int or member['gid']<0):
                raise ValueError('Service requires an explicit non-root UID/GID')
            identity=(member['uid'],member['gid'])
            if identities.setdefault(str(address),identity)!=identity:
                raise ValueError('One exported host directory requires one service UID/GID')
    return value


def prepare(args):
    topology=topology_file(getattr(args,'topology',None))
    if not NAME.fullmatch(args.project):
        raise ValueError('Use a short lowercase dedicated Compose project name')
    for value in (args.python_image, args.etcd_image):
        if not IMAGE.fullmatch(value):
            raise ValueError('Container images must be repository@sha256 identities')
    for value in (args.host_address, args.node_address):
        address = ipaddress.IPv4Address(value)
        if address.is_unspecified or address.is_multicast or address.is_link_local:
            raise ValueError('Explicit reachable IPv4 addresses required')
    if (type(args.port_base) is not int or not 1024 <= args.port_base <= 65524
            or type(args.node_port) is not int or not 1024 <= args.node_port <= 65535):
        raise ValueError('Use valid non-privileged ports')
    if not Path(args.node_directory).is_absolute():
        raise ValueError('Node destination must be an absolute Linux path')
    spec = json.loads(args.node_spec.read_text())
    required = {'id','edge_socket','max_sandboxes','backends','environments','minimum_demands'}
    if not isinstance(spec, dict) or not required <= set(spec) or set(spec)-required-{'worker_socket'}:
        raise ValueError('Node spec requires identity, sockets, backends, environments and minimum demands')
    if not NAME.fullmatch(spec['id']):
        raise ValueError('Invalid Node identity')
    for key in ('edge_socket','worker_socket'):
        if spec.get(key) and not Path(spec[key]).is_absolute():
            raise ValueError('Node sockets must be absolute paths')
    if (not spec['environments'] or set(spec['environments']) != set(spec['minimum_demands'])
            or type(spec['max_sandboxes']) is not int or spec['max_sandboxes'] < 1):
        raise ValueError('Register every advertised environment with a minimum demand')
    wheel = args.wheel.resolve(strict=True)
    if not wheel.name.startswith('dsec_reproduce-') or not wheel.name.endswith('-py3-none-any.whl'):
        raise ValueError('An explicitly built universal dsec-reproduce wheel is required')
    with zipfile.ZipFile(wheel) as package:
        for module in ('dsec/host/cluster.py','dsec/sdk/rollout_api.py'):
            if module not in package.namelist():
                raise ValueError('Wheel lacks the current cluster/rollout API entry')
    out = args.out.resolve()
    out.mkdir(mode=0o700, parents=True, exist_ok=False)
    previous = os.umask(0o077)
    try:
        for name in ('pki','config','image/wheels','node/secrets','secrets/initialize',
                     'secrets/controllers','secrets/etcd1','secrets/etcd2','secrets/etcd3'):
            (out/name).mkdir(mode=0o700, parents=True, exist_ok=True)

        def openssl(*values):
            result = subprocess.run(['openssl', *map(str, values)], capture_output=True)
            if result.returncode:
                raise RuntimeError('Certificate generation failed; inspect the private output directory')

        ca, key = out/'pki/ca.crt', out/'pki/ca.key'
        ca_config = out/'pki/ca.cnf'
        ca_config.write_text('[req]\nprompt=no\ndistinguished_name=dn\nx509_extensions=ca\n'
            '[dn]\nCN=DSec Compose preview\n[ca]\nbasicConstraints=critical,CA:TRUE\n'
            'keyUsage=critical,keyCertSign,cRLSign\nsubjectKeyIdentifier=hash\n')
        openssl('req','-new','-x509','-nodes','-newkey','rsa:2048','-days','30',
                '-keyout',key,'-out',ca,'-config',ca_config)

        def certificate(name, cn, hosts, usages):
            base = out/'pki'/name
            config = base.with_suffix('.cnf')
            san = ','.join(('IP:' if host[0].isdigit() else 'DNS:')+host for host in hosts)
            # The etcd JSON gateway authenticates explicit tokens; it rejects a
            # client certificate CN during password authentication. Keep mTLS
            # validation, but give gateway certificates an organization only.
            subject = 'CN='+cn if cn else 'O=DSec gateway client'
            config.write_text('[req]\nprompt=no\ndistinguished_name=dn\n[dn]\n'+subject+
                '\n[leaf]\nbasicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\n'
                'extendedKeyUsage='+usages+'\nsubjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid,issuer\n'
                +('subjectAltName='+san+'\n' if san else ''))
            openssl('req','-new','-nodes','-newkey','rsa:2048','-keyout',base.with_suffix('.key'),
                    '-out',base.with_suffix('.csr'),'-config',config)
            openssl('x509','-req','-in',base.with_suffix('.csr'),'-CA',ca,'-CAkey',key,
                '-set_serial','0x'+secrets.token_hex(12),'-days','30',
                '-out',base.with_suffix('.crt'),'-extfile',config,'-extensions','leaf')
            return base.with_suffix('.crt'), base.with_suffix('.key')

        def copy_certificate(cert, directory, name):
            for suffix, source in zip(('.crt','.key'), cert):
                shutil.copyfile(source, directory/(name+suffix))
                (directory/(name+suffix)).chmod(0o600)
            shutil.copyfile(ca, directory/'ca.crt')

        health = certificate('health','root',('localhost','127.0.0.1'),'clientAuth')
        etcd_client = certificate('etcd-client','',('dsec-client',),'clientAuth')
        initializer = certificate('initializer','',('dsec-initializer',),'clientAuth')
        jwt_key, jwt_public = out/'pki/jwt.key', out/'pki/jwt.pub'
        openssl('genrsa','-out',jwt_key,'2048')
        openssl('rsa','-in',jwt_key,'-pubout','-out',jwt_public)
        for index in range(1,4):
            directory = out/f'secrets/etcd{index}'
            leaf = certificate(f'etcd{index}',f'dsec-peer-{index}' if topology else f'etcd{index}',
                (f'etcd{index}','localhost','127.0.0.1',
                 topology['etcd'][index-1]['address'] if topology else args.host_address),'serverAuth,clientAuth')
            copy_certificate(leaf, directory, 'member')
            if topology:
                # Authenticate peer clients by a dedicated CA-signed identity,
                # not their source IP, which Docker NAT may rewrite. Server
                # certificates still verify the advertised address normally.
                peer = certificate(f'peer{index}',f'dsec-peer-{index}',(),'clientAuth')
                copy_certificate(peer,directory,'peer-client')
            copy_certificate(health, directory, 'health')
            shutil.copyfile(jwt_key, directory/'jwt.key')
            shutil.copyfile(jwt_public, directory/'jwt.pub')
        copy_certificate(initializer, out/'secrets/initialize', 'client')
        copy_certificate(etcd_client, out/'secrets/controllers', 'etcd-client')
        copy_certificate(etcd_client, out/'node/secrets', 'etcd-client')
        api_hosts=tuple(member['address'] for member in topology['controllers']) if topology else (args.host_address,)
        api = certificate('api','dsec-api',('api1','api2','localhost','127.0.0.1',*api_hosts),'serverAuth')
        copy_certificate(api, out/'secrets/controllers', 'api')
        node = certificate('node',spec['id'],(spec['id'],args.node_address),'serverAuth')
        copy_certificate(node, out/'node/secrets', 'node')
        controller = certificate('controller','dsec-controller',('dsec-controller',),'clientAuth')
        copy_certificate(controller, out/'secrets/controllers', 'controller')
        for filename in ('root-password','dsec-password'):
            (out/'secrets/initialize'/filename).write_text(secrets.token_hex(32)+'\n')
        for directory in ('secrets/controllers','node/secrets'):
            shutil.copyfile(out/'secrets/initialize/dsec-password',out/directory/'dsec-password')
        route_key = secrets.token_hex(32)+'\n'
        for directory in ('secrets/controllers','node/secrets'):
            (out/directory/'route-key').write_text(route_key)
        node_token = secrets.token_hex(32)+'\n'
        (out/'node/secrets/node-token').write_text(node_token)
        (out/'secrets/controllers/node-token').write_text(node_token)
        (out/'secrets/controllers/bootstrap-token').write_text(secrets.token_hex(32)+'\n')

        endpoints = ([f"https://{member['address']}:{member['client_port']}" for member in topology['etcd']]
                     if topology else [f'https://etcd{n}:2379' for n in range(1,4)])
        prefix = '/dsec/compose/'+args.project+'/'
        coordination = dict(endpoints=endpoints,prefix=prefix,ca_file='/secrets/ca.crt',
            cert_file='/secrets/etcd-client.crt',key_file='/secrets/etcd-client.key',
            token_file='/secrets/etcd-token',timeout=1,
            authentication=dict(username='dsec',password_file='/secrets/dsec-password',renew_before_seconds=300))
        quota = dict(concurrency=4,cpu=8,memory_mb=8192,disk_mb=16384,network_mbps=1000)
        node_endpoint = f'https://{args.node_address}:{args.node_port}'
        controller_config = dict(coordination=coordination,route_key_file='/secrets/route-key',
            bootstrap_token_file='/secrets/bootstrap-token',root_quota=quota,
            nodes={spec['id']:dict(endpoints=[node_endpoint],token_file='/secrets/node-token',
                tls=dict(ca_file='/secrets/ca.crt',cert_file='/secrets/controller.crt',key_file='/secrets/controller.key'))},
            watcher_poll_seconds=1,watcher_max_age=5,placement_choices=2,
            server=dict(host='0.0.0.0',port=8443,max_requests=32,console=True,
                        cert_file='/secrets/api.crt',key_file='/secrets/api.key'))
        private_json(out/'config/controller.json',controller_config)
        init_coordination = dict(coordination)
        init_coordination.pop('token_file')
        private_json(out/'config/initialize.json',dict(endpoints=endpoints,prefix=prefix,
            coordination=init_coordination,root_quota=quota))
        remote = Path(args.node_directory)
        node_coordination = dict(coordination,endpoints=(endpoints if topology else
            [f'https://{args.host_address}:{args.port_base+n}' for n in range(3)]))
        for field in ('ca_file','cert_file','key_file','token_file'):
            node_coordination[field] = str(remote/'secrets'/Path(coordination[field]).name)
        node_coordination['authentication'] = dict(coordination['authentication'],
            password_file=str(remote/'secrets/dsec-password'))
        if topology:
            node_coordination.pop('token_file')
        spec = dict(spec,gateway_root=str(remote/'gateway'))
        private_json(out/'node/node.json',dict(coordination=node_coordination,
            route_key_file=str(remote/'secrets/route-key'),node=spec,
            server=dict(host=args.node_address,port=args.node_port,max_requests=32,
                cert_file=str(remote/'secrets/node.crt'),key_file=str(remote/'secrets/node.key'),
                client_ca_file=str(remote/'secrets/ca.crt'),node_token_file=str(remote/'secrets/node-token'))))
        api_endpoints=([f"https://{member['address']}:{member['port']}" for member in topology['controllers']]
            if topology else [f'https://{args.host_address}:{args.port_base+n}' for n in (10,11)])
        private_json(out/'client.json',dict(endpoints=api_endpoints,
            project='root',token_file='secrets/controllers/bootstrap-token',ca_file='secrets/controllers/ca.crt'))
        env = dict(COMPOSE_PROJECT_NAME=args.project,ETCD_IMAGE=args.etcd_image,PYTHON_IMAGE=args.python_image,
            HOST_ADDRESS=args.host_address,DSEC_UID=os.getuid(),DSEC_GID=os.getgid(),
            **{f'ETCD_PORT_{n+1}':args.port_base+n for n in range(3)},
            API_PORT_1=args.port_base+10,API_PORT_2=args.port_base+11)
        (out/'.env').write_text(''.join(f'{key}={value}\n' for key,value in env.items()))
        shutil.copyfile(HERE/'compose.yaml', out/'compose.yaml')
        shutil.copyfile(HERE/'Dockerfile', out/'image/Dockerfile')
        shutil.copyfile(HERE/'initialize.py', out/'image/initialize.py')
        shutil.copyfile(wheel, out/'image/wheels'/wheel.name)
        private_json(out/'deployment.json',dict(schema=1,project=args.project,
            scope=('explicit cross-host deployment; availability evidence is separate' if topology else
                   'single Docker-host process/member failure preview with one external Linux Node'),
            wheel_sha256=hashlib.sha256(wheel.read_bytes()).hexdigest(),
            python_image=args.python_image,etcd_image=args.etcd_image,
            node_id=spec['id'],node_endpoint=node_endpoint,etcd_token_ttl_seconds=86400,
            source_sha256={name:hashlib.sha256((HERE/name).read_bytes()).hexdigest()
                           for name in ('prepare.py','initialize.py','compose.yaml','Dockerfile')}))
        if topology is not None:
            private_json(out/'topology.json',topology)
            private_json(out/'deployment.json',dict(json.loads((out/'deployment.json').read_text()),
                etcd_failure_domains=len({member['address'] for member in topology['etcd']})))
        return out
    finally:
        os.umask(previous)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--wheel',type=Path,required=True)
    parser.add_argument('--python-image',required=True)
    parser.add_argument('--etcd-image',required=True)
    parser.add_argument('--project',required=True)
    parser.add_argument('--host-address',required=True)
    parser.add_argument('--node-address',required=True)
    parser.add_argument('--node-directory',required=True)
    parser.add_argument('--node-spec',type=Path,required=True)
    parser.add_argument('--port-base',type=int,default=18430)
    parser.add_argument('--node-port',type=int,default=18444)
    parser.add_argument('--topology',type=Path,help='Explicit cross-host etcd/controller listeners and service UIDs')
    args = parser.parse_args()
    result = prepare(args)
    print(json.dumps({'status':'prepared','directory':str(result),
        'next':'review configuration; build image; start quorum; initialize; copy node/ and start the installed Node gateway; start API replicas'}))


if __name__ == '__main__':
    main()
