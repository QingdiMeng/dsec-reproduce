"""Private service credential files; no networking or lifecycle dependencies."""
import os
import stat
import hmac
import re

from dsec._security_files import VersionedFile


def secret_file(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        details = os.fstat(fd)
        if (not stat.S_ISREG(details.st_mode) or details.st_uid != os.getuid()
                or details.st_mode & 0o077 or details.st_size > 4096):
            raise ValueError('Secret must be a private regular file owned by this service user')
        with os.fdopen(fd, closefd=False) as stream:
            value = stream.read().strip()
        if len(value) < 32 or '\n' in value or '\r' in value:
            raise ValueError('Invalid private credential')
        return value
    finally:
        os.close(fd)


class FileCredential:
    """Read the selected private token per request; never try a previous token."""
    def __init__(self,path):self.path=path;secret_file(path)
    def __call__(self):return secret_file(self.path)
    def __repr__(self):return '<FileCredential>'


class NodeCredentials:
    """Fleet authentication keys are separate from long-lived route signing."""
    def __init__(self,path,node,*,legacy_token=None):
        from dsec.contracts.cluster import identifier
        self.reader,self.node,self.legacy=VersionedFile(path),identifier(node),legacy_token
        self.values()

    def values(self):
        from dsec.contracts.cluster import node_credential
        def validate(value):
            if set(value)!={'generation','active','keys','allow_legacy'} or type(value['allow_legacy']) is not bool:
                raise ValueError('Explicit Node authentication generation required')
            keys=value['keys']
            if (not isinstance(keys,dict) or not 1<=len(keys)<=4 or value['active'] not in keys or
                    any(not isinstance(k,str) or not re.fullmatch(r'[a-z0-9_-]{1,32}',k) or
                        not isinstance(v,str) or not re.fullmatch(r'[0-9a-f]{64}',v) for k,v in keys.items())):
                raise ValueError('Invalid Node authentication key set')
            tokens={k:node_credential(bytes.fromhex(v),self.node) for k,v in keys.items()}
            accepted=list(tokens.values())
            if value['allow_legacy'] and self.legacy is not None:accepted.append(self.legacy)
            return tokens[value['active']],tuple(accepted)
        return self.reader.read(validate)

    def __call__(self):return self.values()[0]
    def accepts(self,token):return any(hmac.compare_digest(token,value) for value in self.values()[1])
    def __repr__(self):return '<NodeCredentials>'
