"""Expected-generation publication of private material references; no services."""
import fcntl
import json
import os
from pathlib import Path
import re
import tempfile

from dsec._credentials import NodeCredentials, secret_file
from dsec._persistence import atomic_json
from dsec._security_files import read_bytes
from dsec._tls import TLSFiles
from dsec.host.services import check_private_directory


def validate(path,kind):
    if kind in ('tls-client','tls-server'):
        TLSFiles(server=kind=='tls-server',security_file=path).snapshot()
    elif kind=='node-auth':NodeCredentials(path,'validation-node')
    elif kind=='etcd-identity':
        value=json.loads(read_bytes(path,private=True))
        if (set(value)!={'generation','username','password_file'} or
                not isinstance(value['username'],str) or not re.fullmatch(r'[A-Za-z0-9._-]{1,128}',value['username']) or
                not isinstance(value['password_file'],str) or not Path(value['password_file']).is_absolute()):
            raise ValueError('Invalid service identity document')
        secret_file(value['password_file'])
    else:raise ValueError('Unknown security publication type')


def material_view(value,kind,reference_prefix,material_directory):
    """Validate container references against an explicit private host directory.

    Only direct children are exported by the role renderer. Reject traversal,
    symlinks and references outside that namespace; never rewrite the published
    document or give a service write access to its read-only material mount.
    """
    if reference_prefix is None and material_directory is None:return value
    if reference_prefix is None or material_directory is None:
        raise ValueError('Reference prefix and material directory must be supplied together')
    prefix=Path(reference_prefix);directory=Path(material_directory)
    if not prefix.is_absolute() or '..' in prefix.parts or not directory.is_absolute():
        raise ValueError('Absolute material mapping required')
    check_private_directory(directory)
    fields={'tls-client':('ca_file','cert_file','key_file'),
            'tls-server':('cert_file','key_file','client_ca_file'),
            'etcd-identity':('password_file',)}
    if kind not in fields:raise ValueError('Material mapping requires a TLS or etcd identity document')
    mapped=dict(value)
    for field in fields[kind]:
        if value.get(field) is None:continue
        source=Path(value[field])
        if source.parent!=prefix or source.name in ('.','..') or '..' in source.parts:
            raise ValueError('Material reference is outside the explicit private directory')
        mapped[field]=str(directory/source.name)
    return mapped


def publish(path,value,expected,kind,*,reference_prefix=None,material_directory=None):
    path=Path(path)
    if type(expected) is not int or expected<0 or not isinstance(value,dict) or value.get('generation')!=expected+1 or type(value.get('generation')) is not int:
        raise ValueError('New generation must follow the explicit expected generation')
    check_private_directory(path.parent)
    fd=os.open(path.parent/('.'+path.name+'.lock'),os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        old=json.loads(read_bytes(path,private=True)) if path.exists() else None
        if old==value:return dict(generation=value['generation'],published=False)
        if (old['generation'] if old else 0)!=expected:raise ValueError('Security publication generation changed; inspect before proceeding')
        view=material_view(value,kind,reference_prefix,material_directory)
        descriptor,name=tempfile.mkstemp(prefix='.validate-security-',dir=path.parent)
        try:
            with os.fdopen(descriptor,'w') as stream:json.dump(view,stream)
            validate(name,kind)
        finally:Path(name).unlink()
        atomic_json(path,value)
        return dict(generation=value['generation'],published=True)
