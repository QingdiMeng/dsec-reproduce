"""Durable host-wide vsock CID reservations for the QEMU development path.

All cooperating Edges must use one administrator-provisioned pool and disjoint
ranges from other host VMMs. No timeout, health report or PID integer releases a
CID. Retirement requires the owning QemuVM to stop under its lifecycle mutex.
This is not guest-to-host vsock authorization or production confinement.
"""
from contextlib import contextmanager
from dataclasses import asdict, dataclass
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import uuid

from dsec._persistence import atomic_json


class CIDLeaseConflict(RuntimeError):
    pass


@dataclass(frozen=True)
class CIDLease:
    cid: int
    token: str
    directory: str


class QemuCIDPool:
    """Short cross-process allocation lock; no VMM I/O under that lock.

    Tokens fence delayed retirement after CID reuse. The durable reservation
    precedes boot intent, so a controller crash retains its CID until explicit
    adoption/retirement. A damaged ledger fails closed; it is never recreated.
    """
    def __init__(self, root, *, first=20000, last=20999):
        if (type(first) is not int or type(last) is not int or
                not 1024 <= first <= last < 2**32 - 1 or last - first >= 65536):
            raise ValueError('Invalid administrator CID range')
        self.root = self._private_directory(root)
        self.path = self.root / 'cids.json'
        self.first, self.last = first, last
        with self._locked():
            if not self.path.exists() and not self.path.is_symlink():
                # Only a completely unused pool may initialize. The lock file
                # itself is the durable marker: losing an established ledger
                # must not silently permit duplicate CID allocation.
                marker = self.root / 'initialized'
                if marker.exists() or marker.is_symlink():
                    raise CIDLeaseConflict('CID ledger missing; recovery required')
                atomic_json(self.path, dict(version=1, first=first, last=last, leases={}))
            self._load()
            atomic_json(self.root / 'initialized', {'version': 1})

    @staticmethod
    def _private_directory(directory):
        path = Path(directory)
        if not path.is_absolute() or path.resolve(strict=True) != path or path.is_symlink():
            raise ValueError('Canonical private directory required')
        info = path.stat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError('Directory must be runtime-owned and mode 0700')
        return path

    @contextmanager
    def _locked(self):
        fd = os.open(self.root / 'cids.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                    info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
                raise ValueError('Invalid CID lock file')
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def _load(self):
        fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'r') as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                    info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600 or
                    info.st_size > 16 * 1024**2):
                raise CIDLeaseConflict('Invalid CID ledger file')
            record = json.load(stream)
        if (set(record) != {'version', 'first', 'last', 'leases'} or record['version'] != 1 or
                record['first'] != self.first or record['last'] != self.last or
                not isinstance(record['leases'], dict)):
            raise CIDLeaseConflict('CID pool configuration mismatch')
        directories = set()
        for key, lease in record['leases'].items():
            if (not key.isdecimal() or str(int(key)) != key or
                    not self.first <= int(key) <= self.last or not isinstance(lease, dict) or
                    set(lease) != {'cid', 'token', 'directory'} or
                    type(lease['cid']) is not int or lease['cid'] != int(key) or
                    not isinstance(lease['token'], str) or
                    not re.fullmatch('[0-9a-f]{32}', lease['token']) or
                    not isinstance(lease['directory'], str) or
                    not Path(lease['directory']).is_absolute() or
                    lease['directory'] in directories):
                raise CIDLeaseConflict('Invalid CID lease identity')
            directories.add(lease['directory'])
        return record

    def snapshot(self):
        with self._locked():
            return self._load()['leases']

    def reserve(self, directory):
        directory = self._private_directory(directory)
        with self._locked():
            record = self._load()
            for lease in record['leases'].values():
                if lease['directory'] == str(directory):
                    return CIDLease(**lease)
            if (directory / 'qemu.json').exists() or (directory / 'qemu.json').is_symlink():
                raise CIDLeaseConflict('Existing VM intent has no CID lease; recovery required')
            cid = next((cid for cid in range(self.first, self.last + 1)
                        if str(cid) not in record['leases']), None)
            if cid is None:
                raise CIDLeaseConflict('CID pool exhausted; uncertain leases remain reserved')
            lease = CIDLease(cid, uuid.uuid4().hex, str(directory))
            record['leases'][str(cid)] = asdict(lease)
            atomic_json(self.path, record)
            return lease

    @staticmethod
    def _matches(record, lease):
        if record['leases'].get(str(lease.cid)) != asdict(lease):
            raise CIDLeaseConflict('CID lease token/owner changed; retirement fenced')

    def retire(self, lease, vm):
        from dsec.runtime.backends.qemu import QemuVM
        if (not isinstance(lease, CIDLease) or not isinstance(vm, QemuVM) or
                vm.config.guest_cid != lease.cid or str(vm.directory) != lease.directory):
            raise CIDLeaseConflict('CID retirement requires its owning QEMU instance')
        # Keep lifecycle ownership through the release commit. The pool lock
        # only covers file transactions: unrelated VMs can stop concurrently.
        with vm.lock:
            vm._require_owner()
            with self._locked():
                self._matches(self._load(), lease)
            status = vm.stop()  # pidfd-attested death; errors retain reservation
            if status['state'] != 'STOPPED' or status['process_alive']:
                raise CIDLeaseConflict('QEMU death not proved; CID retained')
            with self._locked():
                record = self._load()
                self._matches(record, lease)
                del record['leases'][str(lease.cid)]
                atomic_json(self.path, record)
            return status
