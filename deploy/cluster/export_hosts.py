"""Export role-limited Compose directories from a prepared private topology.

Uses `docker compose config` only to resolve the canonical template. This is a
read-only CLI render: it neither connects to the daemon nor starts services.
Never copy the master private directory to every host.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

if __package__:
    from .prepare import topology_file,private_json
else:
    from prepare import topology_file,private_json


def export(root,out,docker='docker'):
    root=root.resolve(strict=True)
    topology=topology_file(root/'topology.json')
    result=subprocess.run([docker,'compose','-f',str(root/'compose.yaml'),'--env-file',str(root/'.env'),
                           'config','--format','json'],cwd=root,capture_output=True,text=True)
    if result.returncode:raise RuntimeError('Compose template render failed; no services were started')
    template=json.loads(result.stdout)
    out.mkdir(mode=0o700,parents=True,exist_ok=False)
    previous=os.umask(0o077)
    try:
        hosts={}
        for kind in ('etcd','controllers'):
            for member in topology[kind]:hosts.setdefault(member['address'],dict(etcd=[],controllers=[]))
        for kind in ('etcd','controllers'):
            for index,member in enumerate(topology[kind],1):hosts[member['address']][kind].append(index)
        peers=','.join(f"etcd{n}=https://{m['address']}:{m['peer_port']}" for n,m in enumerate(topology['etcd'],1))
        bootstrap_host=topology['controllers'][0]['address']
        for address,roles in hosts.items():
            target=out/address;target.mkdir(mode=0o700)
            def copytree(source,destination):
                shutil.copytree(root/source,target/destination)
            services={};volumes={}
            for index in roles['etcd']:
                name='etcd'+str(index);service=template['services'][name];member=topology['etcd'][index-1]
                service['environment'].update(ETCD_INITIAL_CLUSTER=peers,
                    ETCD_PEER_CLIENT_CERT_FILE='/tls/peer-client.crt',
                    ETCD_PEER_CLIENT_KEY_FILE='/tls/peer-client.key',
                    ETCD_PEER_CERT_ALLOWED_CN='dsec-peer-1,dsec-peer-2,dsec-peer-3',
                    ETCD_ADVERTISE_CLIENT_URLS=f"https://{address}:{member['client_port']}",
                    ETCD_INITIAL_ADVERTISE_PEER_URLS=f"https://{address}:{member['peer_port']}")
                service['ports']=[dict(target=2379,published=str(member['client_port']),host_ip=address,protocol='tcp'),
                                  dict(target=2380,published=str(member['peer_port']),host_ip=address,protocol='tcp')]
                service['user']=f"{member['uid']}:{member['gid']}"
                (target/'data'/name).mkdir(mode=0o700,parents=True)
                service['volumes']=[dict(type='bind',source='./data/'+name,target='/data'),
                    dict(type='bind',source='./secrets/'+name,target='/tls',read_only=True)]
                services[name]=service
                copytree('secrets/'+name,'secrets/'+name)
            if roles['controllers']:
                copytree('image','image');copytree('config','config')
                copytree('secrets/controllers','secrets/controllers')
                config=json.loads((target/'config/controller.json').read_text())
                config['coordination'].pop('token_file',None)  # login/renew from limited identity
                private_json(target/'config/controller.json',config)
                for index in roles['controllers']:
                    name='api'+str(index);service=template['services'][name];member=topology['controllers'][index-1]
                    service.pop('depends_on',None)
                    service['user']=f"{member['uid']}:{member['gid']}"
                    service['build']['context']='./image'
                    service['ports']=[dict(target=8443,published=str(member['port']),host_ip=address,protocol='tcp')]
                    service['volumes']=[dict(type='bind',source='./config',target='/config',read_only=True),
                        dict(type='bind',source='./secrets/controllers',target='/secrets',read_only=True)]
                    services[name]=service
                # Initial root provisioning is isolated from API replicas and
                # only exported to the designated administrator host.
                if address==bootstrap_host:
                    copytree('secrets/initialize','secrets/initialize')
                    (target/'node/secrets').mkdir(mode=0o700,parents=True)
                    service=template['services']['initialize'];service.pop('depends_on',None)
                    member=topology['controllers'][0];service['user']=f"{member['uid']}:{member['gid']}"
                    service['build']['context']='./image'
                    service['volumes']=[dict(type='bind',source='./config',target='/config',read_only=True),
                        dict(type='bind',source='./secrets/initialize',target='/initialize',read_only=True),
                        dict(type='bind',source='./secrets/controllers',target='/secrets'),
                        dict(type='bind',source='./node/secrets',target='/node-secrets')]
                    services['initialize']=service
            private_json(target/'compose.yaml',dict(name=template['name'],services=services,volumes=volumes))
            private_json(target/'host.json',dict(address=address,roles=roles,
                administrator_bootstrap=address==bootstrap_host,
                exporter_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                source_deployment_sha256=hashlib.sha256((root/'deployment.json').read_bytes()).hexdigest()))
        return hosts
    finally:os.umask(previous)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--prepared',type=Path,required=True);p.add_argument('--out',type=Path,required=True)
    p.add_argument('--docker',default='docker')
    a=p.parse_args();hosts=export(a.prepared,a.out,a.docker)
    print(json.dumps(dict(status='exported',hosts=hosts,services_started=False)))


if __name__=='__main__':main()
