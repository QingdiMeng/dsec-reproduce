"""Render a private Docker control-plane preview and external Node configurations.

No Docker calls, host changes, downloads or service starts. The operator supplies
an installed-core wheel, immutable image references and a prepared Node spec.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess


HERE = Path(__file__).resolve().parent
if __package__:
    from . import pki
    from .pki import openssl
    from .planning import IMAGE, NAME, read_plan, topology_file
    from .configuration import node_directory, node_token_file, role_configurations
else:
    import pki
    from pki import openssl
    from planning import IMAGE, NAME, read_plan, topology_file
    from configuration import node_directory, node_token_file, role_configurations


def private_json(path, value):
    path.write_text(json.dumps(value, indent=2)+'\n')
    path.chmod(0o600)


def prepare(args):
    return render(read_plan(args))


def render(plan):
    # Use the reviewed input snapshot. Refuse a changed artifact before creating
    # output; the same verified bytes are then written into the deployment.
    wheel_bytes = plan.wheel.read_bytes()
    if hashlib.sha256(wheel_bytes).hexdigest() != plan.wheel_sha256:
        raise ValueError('Wheel changed after planning; create a new deployment plan')
    args = plan
    topology, wheel = plan.topology, plan.wheel
    roles = role_configurations(plan, uid=os.getuid(), gid=os.getgid())
    out = args.out.resolve()
    out.mkdir(mode=0o700, parents=True, exist_ok=False)
    previous = os.umask(0o077)
    try:
        for name in ('pki','config','image/wheels','secrets/initialize',
                     'secrets/controllers','secrets/etcd1','secrets/etcd2','secrets/etcd3'):
            (out/name).mkdir(mode=0o700, parents=True, exist_ok=True)

        for node in plan.nodes:
            (out/node_directory(plan, node)/'secrets').mkdir(mode=0o700, parents=True)

        ca, key = out/'pki/ca.crt', out/'pki/ca.key'
        pki.authority(out/'pki/ca','DSec Compose preview')

        def certificate(name, cn, hosts, usages):
            return pki.certificate(ca, key, out/'pki'/name, cn, hosts, usages)

        def copy_certificate(cert, directory, name):
            return pki.copy_certificate(ca, cert, directory, name)

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
        for node in plan.nodes:
            copy_certificate(etcd_client, out/node_directory(plan, node)/'secrets', 'etcd-client')
        api_hosts=tuple(member['address'] for member in topology['controllers']) if topology else (args.host_address,)
        api = certificate('api','dsec-api',('api1','api2','localhost','127.0.0.1',*api_hosts),'serverAuth')
        copy_certificate(api, out/'secrets/controllers', 'api')
        for node in plan.nodes:
            identity = node['spec']['id']
            leaf = certificate('node' if plan.legacy_node else 'node-'+identity,
                identity, (identity, node['address']), 'serverAuth')
            copy_certificate(leaf, out/node_directory(plan, node)/'secrets', 'node')
        controller = certificate('controller','dsec-controller',('dsec-controller',),'clientAuth')
        copy_certificate(controller, out/'secrets/controllers', 'controller')
        for filename in ('root-password','dsec-password'):
            (out/'secrets/initialize'/filename).write_text(secrets.token_hex(32)+'\n')
        directories = ['secrets/controllers']+[node_directory(plan, node)+'/secrets' for node in plan.nodes]
        for directory in directories:
            shutil.copyfile(out/'secrets/initialize/dsec-password', out/directory/'dsec-password')
        route_key = secrets.token_hex(32)+'\n'
        for directory in directories:
            (out/directory/'route-key').write_text(route_key)
        for node in plan.nodes:
            token = secrets.token_hex(32)+'\n'
            (out/node_directory(plan, node)/'secrets/node-token').write_text(token)
            (out/'secrets/controllers'/node_token_file(plan, node)).write_text(token)
        (out/'secrets/controllers/bootstrap-token').write_text(secrets.token_hex(32)+'\n')
        private_json(out/'config/controller.json', roles['controller'])
        private_json(out/'config/initialize.json', roles['initialize'])
        for node in plan.nodes:
            private_json(out/node_directory(plan, node)/'node.json', roles['nodes'][node['spec']['id']])
            private_json(out/node_directory(plan, node)/'registration.json',dict(id=node['spec']['id'],
                endpoints=roles['controller']['nodes'][node['spec']['id']]['endpoints']))
        private_json(out/'client.json', roles['client'])
        env = roles['env']
        (out/'.env').write_text(''.join(f'{key}={value}\n' for key,value in env.items()))
        compose = (HERE/'compose.yaml').read_text()
        if not plan.legacy_node:
            # Batch Nodes authenticate/renew independently; no static Node-token export.
            mount = '      - ./node/secrets:/node-secrets\n'
            if compose.count(mount) != 1:
                raise ValueError('Initializer mount template changed; review batch role mapping')
            compose = compose.replace(mount, '')
        (out/'compose.yaml').write_text(compose)
        shutil.copyfile(HERE/'Dockerfile', out/'image/Dockerfile')
        shutil.copyfile(HERE/'initialize.py', out/'image/initialize.py')
        (out/'image/wheels'/wheel.name).write_bytes(wheel_bytes)
        private_json(out/'deployment.json',dict(schema=1,project=args.project,
            scope=('explicit cross-host deployment; availability evidence is separate' if topology else
                   'single Docker-host process/member failure preview with external Linux Nodes'),
            wheel_sha256=plan.wheel_sha256,
            python_image=args.python_image,etcd_image=args.etcd_image,
            nodes={node['spec']['id']:dict(address=node['address'], endpoint=roles['controller']['nodes'][node['spec']['id']]['endpoints'][0],
                directory=node['directory'], export_directory=node_directory(plan, node)) for node in plan.nodes},
            node_layout='legacy' if plan.legacy_node else 'batch-v1', etcd_token_ttl_seconds=86400,
            source_sha256={name:hashlib.sha256((HERE/name).read_bytes()).hexdigest()
                           for name in ('prepare.py','planning.py','configuration.py','pki.py','initialize.py','compose.yaml','Dockerfile')}))
        if plan.legacy_node:
            deployment = json.loads((out/'deployment.json').read_text())
            private_json(out/'deployment.json', dict(deployment, node_id=plan.node_spec['id'],
                node_endpoint=deployment['nodes'][plan.node_spec['id']]['endpoint']))
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
    parser.add_argument('--nodes',type=Path,help='Schema-1 batch Node inventory; exclusive with single-Node options')
    parser.add_argument('--node-address')
    parser.add_argument('--node-directory')
    parser.add_argument('--node-spec',type=Path)
    parser.add_argument('--port-base',type=int,default=18430)
    parser.add_argument('--node-port',type=int)
    parser.add_argument('--topology',type=Path,help='Explicit cross-host etcd/controller listeners and service UIDs')
    parser.add_argument('--plan',action='store_true',help='Validate and describe deployment without generating credentials or files')
    args = parser.parse_args()
    if args.plan:
        print(json.dumps(read_plan(args).describe()))
        return
    result = prepare(args)
    print(json.dumps({'status':'prepared','directory':str(result),
        'next':'review configuration; build image; start quorum; initialize; copy role-specific Node exports and start their installed gateways; start API replicas'}))


if __name__ == '__main__':
    main()
