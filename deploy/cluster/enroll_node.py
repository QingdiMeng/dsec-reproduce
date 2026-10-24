"""Issue one new Node bundle using an existing fleet CA; never change controllers."""
import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import tempfile

if __package__:
    from . import pki
    from .planning import node_spec, ipv4_address, linux_path, validate_bindings, topology_file
else:
    import pki
    from planning import node_spec, ipv4_address, linux_path, validate_bindings, topology_file


def enroll(deployment, entry, output, *, issuer=None):
    deployment = Path(deployment).resolve(strict=True)
    metadata = json.loads((deployment/'deployment.json').read_text())
    controller = json.loads((deployment/'config/controller.json').read_text())
    if not controller.get('node_lifecycle') or not controller.get('dynamic_node_tls'):
        raise ValueError('Upgrade every controller to managed dynamic Nodes before issuing bundles')
    if not isinstance(entry,dict) or set(entry) != {'spec','address','port','directory'}:
        raise ValueError('Supply one entry in the batch Node inventory format')
    spec = deepcopy(node_spec(entry['spec']))
    identity = spec['id']
    if identity in metadata['nodes']:
        raise ValueError('This Node identity belongs to the original deployment')
    address = str(ipv4_address(entry['address'],allow_loopback=False))
    port = entry['port']; remote = PurePosixPath(linux_path(entry['directory']))
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError('Node port must be in [1024, 65535]')
    endpoint = f'https://{address}:{port}'
    if endpoint in {value['endpoint'] for value in metadata['nodes'].values()}:
        raise ValueError('Node listener already belongs to the original deployment')
    previous_nodes=[]
    for old_identity,old in metadata['nodes'].items():
        old_config=json.loads((deployment/old['export_directory']/'node.json').read_text())
        previous_nodes.append(dict(address=old['address'],port=old_config['server']['port'],
            directory=old['directory'],spec=old_config['node']))
    environment=dict(line.split('=',1) for line in (deployment/'.env').read_text().splitlines() if '=' in line)
    topology=topology_file(deployment/'topology.json') if (deployment/'topology.json').exists() else None
    validate_bindings([*previous_nodes,dict(address=address,port=port,directory=str(remote),spec=spec)],
                      topology,environment['HOST_ADDRESS'],int(environment['ETCD_PORT_1']))
    first = next(iter(metadata['nodes'].values()))
    sample = json.loads((deployment/first['export_directory']/'node.json').read_text())
    rotating=bool(controller.get('node_auth_keys_file'))
    if rotating and issuer is None:
        raise ValueError('Rotation-enabled enrollment requires an explicit current CA issuer')
    authority=(deployment/'pki/ca.crt',deployment/'pki/ca.key') if issuer is None else tuple(Path(p).resolve(strict=True) for p in issuer)
    coordination = deepcopy(sample['coordination'])
    coordination.pop('token_file',None)
    if not rotating:
        for field in ('ca_file','cert_file','key_file'):
            coordination[field] = str(remote/'secrets'/PurePosixPath(coordination[field]).name)
        coordination['authentication']['password_file'] = str(remote/'secrets/dsec-password')
    config = dict(coordination=coordination,node_lifecycle=True,route_key_file=str(remote/'secrets/route-key'),
        node=dict(spec,gateway_root=str(remote/'gateway')),
        server=dict(host=address,port=port,max_requests=32,cert_file=str(remote/'secrets/node.crt'),
                    key_file=str(remote/'secrets/node.key'),client_ca_file=str(remote/'secrets/ca.crt'),
                    node_token_mode='derived-v1'))
    required = ('secrets/controllers/route-key',) if rotating else ('pki/ca.crt','pki/ca.key','secrets/controllers/route-key',
                'secrets/controllers/dsec-password','secrets/controllers/etcd-client.crt',
                'secrets/controllers/etcd-client.key')
    for name in required:
        if not (deployment/name).is_file():raise ValueError('Existing private deployment missing '+name)
    output = Path(output).resolve()
    previous = os.umask(0o077)
    try:
        output.mkdir(mode=0o700,parents=True,exist_ok=False)
        private = output/'secrets';private.mkdir(mode=0o700)
        with tempfile.TemporaryDirectory(prefix='.sign-',dir=output) as work:
            leaf = pki.certificate(*authority,Path(work)/'node',
                                   identity,(identity,address),'serverAuth')
            pki.copy_certificate(authority[0],leaf,private,'node')
        for name in (('route-key',) if rotating else ('route-key','dsec-password','etcd-client.crt','etcd-client.key')):
            shutil.copyfile(deployment/'secrets/controllers'/name,private/name)
            (private/name).chmod(0o600)
        if rotating:
            _rotating_credentials(deployment,first,sample,controller,config,private,remote)
        (output/'node.json').write_text(json.dumps(config,indent=2)+'\n')
        registration = dict(id=identity,endpoints=[endpoint])
        (output/'registration.json').write_text(json.dumps(registration,indent=2)+'\n')
        (output/'bundle.json').write_text(json.dumps(dict(schema=1,node=identity,
            destination=str(remote),core_wheel_sha256=metadata['wheel_sha256'],
            files={str(path.relative_to(output)):hashlib.sha256(path.read_bytes()).hexdigest()
                   for path in output.rglob('*') if path.is_file()},
            scope='Private Node credentials only; no CA key, bootstrap token, controller update, registration or service startup'),indent=2)+'\n')
    finally:
        os.umask(previous)
    return dict(directory=str(output),node=identity,endpoint=endpoint,registered=False,services_started=False)


def _rotating_credentials(deployment,first,sample,controller,config,private,remote):
    """Export selected limited materials, never a stale password or signing CA key."""
    control=deployment/'secrets/controllers'
    old_node=deployment/first['export_directory']/'secrets'
    def read(directory,path):return json.loads((directory/PurePosixPath(path).name).read_text())
    def write(name,value):
        path=private/name;path.write_text(json.dumps(value,indent=2)+'\n');path.chmod(0o600)
    def copy(directory,path,name):
        source=directory/PurePosixPath(path).name
        if source.is_symlink() or not source.is_file():raise ValueError('Missing current rotation material')
        shutil.copyfile(source,private/name);(private/name).chmod(0o600)
        return str(remote/'secrets'/name)
    tls=read(control,controller['coordination']['security_file'])
    tls['generation']=1
    for field,name in (('ca_file','etcd-ca.pem'),('cert_file','etcd-client.crt'),('key_file','etcd-client.key')):
        tls[field]=copy(control,tls[field],name)
    write('etcd-tls.json',tls)
    identity=read(control,controller['coordination']['authentication']['identity_file'])
    identity['generation']=1;identity['password_file']=copy(control,identity['password_file'],'etcd-password')
    write('etcd-identity.json',identity)
    coordination=config['coordination']
    for field in ('ca_file','cert_file','key_file','token_file'):coordination.pop(field,None)
    coordination['security_file']=str(remote/'secrets/etcd-tls.json')
    coordination['authentication']={k:v for k,v in coordination['authentication'].items() if k not in ('username','password_file','identity_file')}
    coordination['authentication']['identity_file']=str(remote/'secrets/etcd-identity.json')
    source=read(old_node,sample['server']['security_file'])
    trust=copy(old_node,source['client_ca_file'],'controller-ca.pem')
    server=dict(generation=1,cert_file=str(remote/'secrets/node.crt'),key_file=str(remote/'secrets/node.key'),client_ca_file=trust)
    if source.get('peer_sha256'):server['peer_sha256']=source['peer_sha256']
    write('server-tls.json',server)
    for field in ('cert_file','key_file','client_ca_file'):config['server'].pop(field,None)
    config['server']['security_file']=str(remote/'secrets/server-tls.json')
    copy(control,controller['node_auth_keys_file'],'node-auth.json')
    config['node_auth_keys_file']=str(remote/'secrets/node-auth.json')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--deployment',type=Path,required=True)
    parser.add_argument('--node',type=Path,required=True,help='One entry in batch inventory format')
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--ca',type=Path);parser.add_argument('--ca-key',type=Path)
    args=parser.parse_args()
    if bool(args.ca)!=bool(args.ca_key):parser.error('Supply both current CA certificate and key')
    print(json.dumps(enroll(args.deployment,json.loads(args.node.read_text()),args.out,
        issuer=(args.ca,args.ca_key) if args.ca else None)))

if __name__=='__main__':main()
