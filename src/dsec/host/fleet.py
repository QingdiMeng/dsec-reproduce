"""Operator API client. No SSH, root operations or direct Edge access."""
import argparse
import json
import os
from pathlib import Path
import uuid

from dsec._credentials import secret_file
from dsec._persistence import atomic_json
from dsec.sdk.cluster import ClusterTransport


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--client',type=Path,required=True)
    parser.add_argument('--out',type=Path,help='New private intent/result file, required for changes')
    parser.add_argument('--request-id',help='Reuse only to reconcile the exact same transition')
    sub=parser.add_subparsers(dest='action',required=True)
    join=sub.add_parser('join');join.add_argument('--spec',type=Path,required=True)
    for action in ('get','security','activate','drain','maintain','resume','retire','withdraw','upgrade','commit','rollback'):
        command=sub.add_parser(action);command.add_argument('--node',required=True)
        if action not in ('get','security'):command.add_argument('--version',type=int,required=True)
        if action=='upgrade':command.add_argument('--release',type=Path,required=True,help='Validated release reference JSON')
    args=parser.parse_args()
    if args.action not in ('get','security') and args.out is None:
        parser.error('Mutations require --out to retain intent before any network call')
    config=json.loads(args.client.read_text())
    token=Path(config['token_file'])
    if not token.is_absolute():token=args.client.resolve().parent/token
    transport=ClusterTransport(config['endpoints'],token=secret_file(token),project='root',
        **{key:str((args.client.resolve().parent/Path(value)).resolve()) for key,value in config.items()
           if key in ('ca_file','cert_file','key_file','security_file')})
    values=dict(spec=json.loads(args.spec.read_text())) if args.action=='join' else dict(node=args.node)
    if args.action not in ('join','get','security'):values['version']=args.version
    if args.action=='upgrade':values['release']=json.loads(args.release.read_text())
    operation='node.'+args.action
    identity=args.request_id or uuid.uuid4().hex
    record=dict(request_id=identity,operation=operation,args=values,state='PENDING')
    if args.out:
        with args.out.open('x') as stream:
            os.fchmod(stream.fileno(),0o600)
            stream.write(json.dumps(record,indent=2)+'\n')
            stream.flush()
            os.fsync(stream.fileno())
    try:
        record['result']=transport.call(operation,request_id=identity,**values)
        record['state']='DONE'
    except Exception as exc:
        record.update(state='UNCONFIRMED',error_type=type(exc).__name__)
        raise
    finally:
        if args.out:atomic_json(args.out, record)
        print(json.dumps(record))


if __name__=='__main__':main()
