"""Offline rotation material and opt-in role patches; never apply or restart."""
import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import secrets
import ssl

if __package__:
    from . import pki
else:
    import pki


def write(path,value):path.write_text(json.dumps(value,indent=2)+'\n');path.chmod(0o600)


def describe(cert):
    der=ssl.PEM_cert_to_DER_cert(cert.read_text())
    return dict(sha256=hashlib.sha256(der).hexdigest(),certificate=str(cert))


def enable(deployment,out):
    deployment=Path(deployment).resolve(strict=True);out=Path(out).resolve()
    metadata=json.loads((deployment/'deployment.json').read_text())
    controller=json.loads((deployment/'config/controller.json').read_text())
    if not controller.get('node_lifecycle'):raise ValueError('Managed Node deployment required')
    def tls(paths,server=False):
        fields=('cert_file','key_file','client_ca_file') if server else ('ca_file','cert_file','key_file')
        return dict(generation=1,**{k:paths[k] for k in fields if paths.get(k)})
    def coordination(config,root,files):
        c=config['coordination'];files['etcd-tls.json']=tls(c)
        for k in ('ca_file','cert_file','key_file','token_file'):c.pop(k,None)
        c['security_file']=root+'/etcd-tls.json'
        a=c['authentication'];files['etcd-identity.json']=dict(generation=1,username=a.pop('username'),password_file=a.pop('password_file'))
        a['identity_file']=root+'/etcd-identity.json'
    ring=dict(generation=1,active='initial',keys={'initial':secrets.token_hex(32)},allow_legacy=True)
    patches=[]
    previous=os.umask(0o077)
    try:
        out.mkdir(mode=0o700,parents=True,exist_ok=False)
        roles=[('controllers',controller,'/secrets','controller.json')]
        for identity,entry in metadata['nodes'].items():
            config=json.loads((deployment/entry['export_directory']/'node.json').read_text())
            roles.append(('nodes/'+identity,config,str(Path(entry['directory'])/'secrets'),'node.json'))
        for name,config,root,filename in roles:
            target=out/name;(target/'secrets').mkdir(mode=0o700,parents=True)
            files={'node-auth.json':ring};coordination(config,root,files)
            config['node_auth_keys_file']=root+'/node-auth.json'
            files['server-tls.json']=tls(config['server'],True)
            if name!='controllers':
                files['server-tls.json']['peer_sha256']=[describe(deployment/'secrets/controllers/controller.crt')['sha256']]
            for key in ('cert_file','key_file','client_ca_file'):config['server'].pop(key,None)
            config['server']['security_file']=root+'/server-tls.json'
            if name=='controllers':
                files['node-client-tls.json']=tls(config['dynamic_node_tls'])
                config['dynamic_node_tls']={'security_file':root+'/node-client-tls.json'}
                for route in config['nodes'].values():route['tls']={'security_file':root+'/node-client-tls.json'}
            write(target/filename,config)
            for file,value in files.items():write(target/'secrets'/file,value)
            patches.append(dict(role=name,configuration=filename,secret_destination=root))
        client=json.loads((deployment/'client.json').read_text())
        target=out/'client';target.mkdir(mode=0o700)
        for field in ('token_file','ca_file','cert_file','key_file'):
            if client.get(field):client[field]=str((deployment/client[field]).resolve())
        client_tls=tls(client)
        client_tls['peer_sha256']=[describe(deployment/'secrets/controllers/api.crt')['sha256']]
        for field in ('ca_file','cert_file','key_file'):client.pop(field,None)
        client['security_file']=str(target/'tls.json')
        write(target/'tls.json',client_tls);write(target/'client.json',client)
        write(out/'patches.json',dict(roles=patches,services_changed=False,route_key_changed=False,
            order='install Node files and restart only gateways; then install controller files and roll API replicas; verify every peer before disabling legacy authentication'))
    finally:os.umask(previous)
    return dict(directory=str(out),services_changed=False,route_key_changed=False)


def main():
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='action',required=True)
    setup=sub.add_parser('enable');setup.add_argument('--deployment',type=Path,required=True);setup.add_argument('--out',type=Path,required=True)
    ca=sub.add_parser('create-ca');ca.add_argument('--cn',required=True);ca.add_argument('--out',type=Path,required=True)
    issue=sub.add_parser('issue');issue.add_argument('--ca',type=Path,required=True);issue.add_argument('--ca-key',type=Path,required=True)
    issue.add_argument('--cn',required=True);issue.add_argument('--host',action='append',default=[])
    issue.add_argument('--usage',choices=('serverAuth','clientAuth','serverAuth,clientAuth'),required=True);issue.add_argument('--out',type=Path,required=True)
    args=parser.parse_args();os.umask(0o077)
    if args.action=='enable':result=enable(args.deployment,args.out)
    else:
        args.out.mkdir(mode=0o700,parents=True,exist_ok=False)
        if args.action=='create-ca':cert,_=pki.authority(args.out/'ca',args.cn)
        else:cert,key=pki.certificate(args.ca,args.ca_key,args.out/'leaf',args.cn,args.host,args.usage);key.chmod(0o600)
        result=describe(cert);write(args.out/'certificate.json',result)
    print(json.dumps(result))


if __name__=='__main__':main()
