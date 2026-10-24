"""Private configuration snapshots; no IAM, runtime or automatic fallback."""
import hashlib
import json
import os
from pathlib import Path
import stat
import threading


def read_bytes(path, *, private=False, limit=65536):
    fd=os.open(path,os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd,'rb') as stream:
        info=os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_size>limit or
                (private and (info.st_uid!=os.getuid() or info.st_mode & 0o077))):
            raise ValueError('Unsafe security material file')
        value=stream.read(limit+1)
    if len(value)>limit:raise ValueError('Oversized security material')
    return value


class VersionedFile:
    """Fence late/edited publications within a running reader.

    The private on-disk document is authoritative across restarts. Operators
    publish it with expected-generation CAS, never restore an older document.
    """
    def __init__(self,path):
        self.path=Path(path);self.lock=threading.RLock();self.generation=0;self.digest=None

    def read(self,validator):
        with self.lock:
            raw=read_bytes(self.path,private=True);value=json.loads(raw)
            if not isinstance(value,dict) or type(value.get('generation')) is not int or value['generation']<1:
                raise ValueError('Positive security generation required')
            generation=value['generation'];digest=hashlib.sha256(raw).hexdigest()
            if generation<self.generation or (generation==self.generation and digest!=self.digest):
                raise ValueError('Stale or edited security generation')
            result=validator(value)
            self.generation,self.digest=generation,digest
            return result
