"""Explicit overlap of prefix-limited etcd service accounts; root stays external."""
import argparse
import base64
import json
import os
from pathlib import Path
import re
import secrets

from dsec._persistence import atomic_json
from dsec.control.coordination import EtcdStore


def store(path):return EtcdStore(**json.loads(Path(path).read_text()))


def restricted_role(admin):
    raw=admin.prefix.encode()
    expected=dict(permType='READWRITE',key=base64.b64encode(raw).decode(),
                  range_end=base64.b64encode(raw[:-1]+bytes([raw[-1]+1])).decode())
    role=admin._rpc('auth/role/get',{'role':'dsec-cluster'})
    if role.get('perm')!=[expected]:raise ValueError('Existing etcd role is not restricted to this deployment prefix')


def prepare(admin,username,out):
    if not isinstance(username,str) or not re.fullmatch(r'dsec[-_][a-z0-9_-]{1,64}',username):
        raise ValueError('Use a fresh dsec-prefixed service username')
    restricted_role(admin)
    if username in admin._rpc('auth/user/list',{}).get('users',[]):raise ValueError('Service username already exists')
    out=Path(out).resolve();out.mkdir(mode=0o700,parents=True,exist_ok=False)
    password=secrets.token_urlsafe(48);path=out/'password'
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as stream:stream.write(password+'\n');stream.flush();os.fsync(stream.fileno())
    atomic_json(out/'identity.json',dict(generation=1,username=username,password_file=str(path)))
    intent=dict(username=username,state='UNCONFIRMED',prefix=admin.prefix)
    atomic_json(out/'intent.json',intent)
    admin._rpc('auth/user/add',dict(name=username,password=password),retry_unknown=False)
    admin._rpc('auth/user/grant',dict(user=username,role='dsec-cluster'),retry_unknown=False)
    intent['state']='OVERLAP';atomic_json(out/'intent.json',intent)
    return intent


def retire(admin,old,replacement):
    if old=='root' or not re.fullmatch(r'dsec(?:[-_][a-z0-9_-]{1,64})?',old):raise ValueError('Only a DSec service account may be retired')
    if replacement.prefix!=admin.prefix or replacement.credential is None:raise ValueError('Replacement must use the same isolated prefix and explicit authentication')
    username,_=replacement.credential._selected_identity()
    if username in ('root',old):raise ValueError('A distinct limited replacement account is required')
    restricted_role(admin)
    roles=admin._rpc('auth/user/get',{'name':username}).get('roles',[])
    if set(roles)!={'dsec-cluster'}:raise ValueError('Replacement must have exactly the deployment role')
    if replacement.read('projects/root').value is None:raise ValueError('Replacement has not proved authenticated access to the existing ledger')
    if old not in admin._rpc('auth/user/list',{}).get('users',[]):return dict(state='RETIRED',username=old)
    admin._rpc('auth/user/delete',{'name':old},retry_unknown=False)
    return dict(state='RETIRED',username=old)


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--admin-config',type=Path,required=True)
    sub=parser.add_subparsers(dest='action',required=True)
    stage=sub.add_parser('prepare');stage.add_argument('--username',required=True);stage.add_argument('--out',type=Path,required=True)
    remove=sub.add_parser('retire');remove.add_argument('--username',required=True);remove.add_argument('--replacement-config',type=Path,required=True);remove.add_argument('--out',type=Path,required=True)
    args=parser.parse_args();os.umask(0o077);admin=store(args.admin_config)
    if args.action=='prepare':result=prepare(admin,args.username,args.out)
    else:
        with args.out.open('x') as stream:os.fchmod(stream.fileno(),0o600);json.dump(dict(state='UNCONFIRMED',username=args.username),stream);stream.flush();os.fsync(stream.fileno())
        result=retire(admin,args.username,store(args.replacement_config));atomic_json(args.out,result)
    print(json.dumps(result))


if __name__=='__main__':main()
