"""Explicit operator security commands; secrets stay in private files."""
import argparse
import json
import os
from pathlib import Path
import secrets
import uuid

from dsec._credentials import secret_file
from dsec._persistence import atomic_json
from dsec._security_files import read_bytes
from dsec.host.security_publication import publish
from dsec.sdk.cluster import ClusterTransport
from dsec.sdk.rollout_api import api_configuration


def prepare_api(client,out):
    config=api_configuration(client);transport=ClusterTransport(**config)
    out=Path(out).resolve();out.mkdir(mode=0o700,parents=True,exist_ok=False)
    token=secrets.token_urlsafe(48);token_path=out/'token'
    descriptor=os.open(token_path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(descriptor,'w') as stream:stream.write(token+'\n');stream.flush();os.fsync(stream.fileno())
    source=json.loads(Path(client).read_text())
    for field in ('ca_file','cert_file','key_file','security_file'):
        if source.get(field):source[field]=str((Path(client).resolve().parent/source[field]).resolve())
    source['token_file']=str(token_path)
    atomic_json(out/'client.json',source)
    identity=uuid.uuid4().hex
    intent=dict(rotation_id=identity,state='UNCONFIRMED',replacement_client=str(out/'client.json'))
    atomic_json(out/'intent.json',intent)
    try:
        result=transport.call('credential.rotation.prepare',rotation_id=identity,new_token=token,request_id=identity)
        intent.update(state=result['state']);atomic_json(out/'intent.json',intent)
        return intent
    except Exception:
        # Keep the sole new credential/config and original identity to query
        # uncertainty. Never mint another replacement or resend automatically.
        raise


def main():
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='action',required=True)
    pub=sub.add_parser('publish');pub.add_argument('--path',type=Path,required=True);pub.add_argument('--document',type=Path,required=True)
    pub.add_argument('--expected-generation',type=int,required=True);pub.add_argument('--kind',choices=('tls-client','tls-server','node-auth','etcd-identity'),required=True)
    pub.add_argument('--reference-prefix',type=Path);pub.add_argument('--material-directory',type=Path)
    prepare=sub.add_parser('prepare-api');prepare.add_argument('--client',type=Path,required=True);prepare.add_argument('--out',type=Path,required=True)
    check=sub.add_parser('check-api');check.add_argument('--client',type=Path,required=True)
    for name in ('complete-api','cancel-api','get-api'):
        command=sub.add_parser(name);command.add_argument('--client',type=Path,required=True);command.add_argument('--rotation-id',required=True)
        if name!='get-api':command.add_argument('--out',type=Path,required=True)
    args=parser.parse_args();os.umask(0o077)
    if args.action=='publish':
        result=publish(args.path,json.loads(read_bytes(args.document,private=True)),args.expected_generation,args.kind,
                       reference_prefix=args.reference_prefix,material_directory=args.material_directory)
    elif args.action=='prepare-api':result=prepare_api(args.client,args.out)
    elif args.action=='check-api':result=ClusterTransport(**api_configuration(args.client)).call('health')
    else:
        action=args.action.removesuffix('-api');transport=ClusterTransport(**api_configuration(args.client))
        identity=uuid.uuid4().hex;intent=dict(request_id=identity,rotation_id=args.rotation_id,operation=action,state='UNCONFIRMED')
        if action!='get':
            with args.out.open('x') as stream:os.fchmod(stream.fileno(),0o600);json.dump(intent,stream);stream.flush();os.fsync(stream.fileno())
        result=transport.call('credential.rotation.'+action,rotation_id=args.rotation_id,request_id=identity)
        if action!='get':atomic_json(args.out,dict(intent,state=result['state']))
    print(json.dumps(result))


if __name__=='__main__':main()
