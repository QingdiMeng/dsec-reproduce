"""Networkless QEMU/KVM execution primitive, not yet a public FullVM backend.

Only trusted administrator-pinned artifacts enter this driver. Edge integration,
outer-VM Docker, host confinement and eBPF policy are separate unfinished gates.
Production requests fail before effects. QMP is private; no arbitrary argv, HMP,
host shares, host Docker, device passthrough or TCG fallback.
"""
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import threading
import time
import uuid

from dsec._persistence import atomic_json
from dsec.contracts.execution import ShellRequest
from dsec.contracts.sandbox import UnsupportedCapability
from dsec.runtime.backends.qmp import QMP
from dsec.runtime.sessions.channel import VsockCommandChannel
from dsec.runtime.sessions.native import NativeChannel
from dsec.storage.snapshots import _copy_sparse


@dataclass(frozen=True)
class QemuArtifact:
    path: str
    sha256: str

    def verify(self):
        path = Path(self.path)
        if not path.is_absolute() or path.is_symlink() or path.resolve(strict=True) != path:
            raise ValueError('Pinned QEMU artifact requires a canonical absolute path')
        if len(self.sha256) != 64 or any(c not in '0123456789abcdef' for c in self.sha256):
            raise ValueError('Pinned SHA-256 required')
        if not stat.S_ISREG(path.stat().st_mode):
            raise ValueError('QEMU artifact must be a regular file')
        with path.open('rb') as stream:
            actual = hashlib.file_digest(stream, 'sha256').hexdigest()
        if actual != self.sha256:
            raise ValueError('QEMU artifact identity mismatch')
        return path


@dataclass(frozen=True)
class QemuConfig:
    binary: QemuArtifact
    kernel: QemuArtifact
    template: QemuArtifact
    guest_cid: int
    memory_mb: int = 512
    cpu_count: int = 1
    production: bool = False

    def validate(self):
        if (type(self.guest_cid) is not int or not 1024 <= self.guest_cid < 2**32-1
                or type(self.memory_mb) is not int or not 128 <= self.memory_mb <= 65536
                or type(self.cpu_count) is not int or not 1 <= self.cpu_count <= 32
                or type(self.production) is not bool):
            raise ValueError('Invalid QEMU resource/CID configuration')
        if self.production:
            raise UnsupportedCapability('QEMU production requires Edge leases, host confinement and network policy acceptance')
        return self


class QemuVM:
    """Single owner, serialized lifecycle. Stop completion is terminal.

    This raw-disk primitive boots a trusted guest. Records permit adoption of an
    attested RUNNING process, not replay of interrupted boot or guest operations.
    A guest CID is an administrator-reserved host-wide resource; its allocator
    will belong to Edge. Kernel CID collisions reject boot, never replace a VM.
    """
    def __init__(self, config, directory):
        self.config = config.validate()
        path = Path(directory)
        if not path.is_absolute() or path.resolve(strict=True) != path or path.is_symlink():
            raise ValueError('Private canonical instance directory required')
        info = path.stat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError('Instance directory must be owned by runtime user and mode 0700')
        if len(os.fsencode(str(path/'qmp.sock'))) >= 104:
            raise ValueError('Private QMP socket path is too long')
        self.directory, self.record_path = path, path/'qemu.json'
        self.owner = (path/'qemu.lock').open('a')
        try:
            fcntl.flock(self.owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            self.owner.close()
            raise
        self.lock = threading.RLock()
        self.process = self.child = self.log = None
        self.state, self.reason = 'NEW', None
        self.qmp = QMP(path/'qmp.sock')
        self.channel = VsockCommandChannel(guest_cid=config.guest_cid)
        self.native = NativeChannel(guest_cid=config.guest_cid)
        self.record = None

    def _require_owner(self):
        if self.owner.closed:
            raise RuntimeError('QEMU instance owner retired')

    def _save(self):
        self.record.update(state=self.state, reason=self.reason)
        atomic_json(self.record_path, self.record)

    def _artifacts(self):
        return {name: {'path':str(getattr(self.config, name).verify()),
                       'sha256':getattr(self.config, name).sha256}
                for name in ('binary', 'kernel', 'template')}

    def _argv(self, machine_uuid, artifacts):
        # blockdev JSON prevents commas/options in a path from adding drives.
        root = {'driver':'raw', 'node-name':'root', 'file':{
            'driver':'file', 'filename':str(self.directory/'root.ext4')}, 'read-only':False}
        # Chardev uses QemuOpts, not blockdev JSON. Double commas escape paths;
        # each whole value remains one argv item and never passes through shell.
        qmp = 'socket,id=qmp,path=' + str(self.directory/'qmp.sock').replace(',', ',,') + ',server=on,wait=off'
        serial = 'file,id=serial0,path=' + str(self.directory/'console.log').replace(',', ',,')
        return [artifacts['binary']['path'], '-no-user-config', '-nodefaults',
                '-machine', 'q35,accel=kvm', '-cpu', 'host', '-m', str(self.config.memory_mb),
                '-smp', str(self.config.cpu_count), '-uuid', machine_uuid,
                '-display', 'none', '-monitor', 'none', '-nic', 'none', '-no-reboot', '-S',
                '-sandbox', 'on,obsolete=deny,elevateprivileges=deny,spawn=deny,resourcecontrol=deny',
                '-chardev', qmp, '-mon', 'chardev=qmp,mode=control',
                '-chardev', serial, '-serial', 'chardev:serial0',
                '-kernel', artifacts['kernel']['path'], '-append',
                'console=ttyS0 reboot=k panic=1 root=/dev/vda rw init=/dsec-init',
                '-blockdev', json.dumps(root, separators=(',', ':')),
                '-device', 'virtio-blk-pci,drive=root', '-device',
                f'vhost-vsock-pci,guest-cid={self.config.guest_cid}']

    def _preflight(self):
        if os.geteuid() == 0:
            raise PermissionError('QEMU must run unprivileged')
        for device in ('/dev/kvm', '/dev/vhost-vsock'):
            if not stat.S_ISCHR(os.stat(device).st_mode) or not os.access(device, os.R_OK | os.W_OK):
                raise PermissionError(f'QEMU requires read/write access to {device}')

    def _initial_record(self, artifacts):
        record = dict(version=1, backend='qemu-kvm-development', artifacts=artifacts,
            guest_cid=self.config.guest_cid, memory_mb=self.config.memory_mb,
            cpu_count=self.config.cpu_count, machine_uuid=str(uuid.uuid4()), process=None)
        record['argv'] = self._argv(record['machine_uuid'], artifacts)
        return record

    def _attach(self, saved):
        # Same pidfd/start-time/UID/executable verifier as the Firecracker path.
        from dsec.runtime.registry import AttachedProcess
        return AttachedProcess(saved, self.config.binary.path, self.qmp.endpoint,
                               expected_argv=self.record['argv'])

    def _prove_kvm(self):
        value = self.qmp.execute('query-kvm')
        if value != {'enabled':True, 'present':True}:
            raise RuntimeError('QEMU KVM enforcement not attested')

    def _spawn(self):
        self.child = subprocess.Popen(self.record['argv'], stdin=subprocess.DEVNULL,
            stdout=self.log, stderr=subprocess.STDOUT, close_fds=True,
            env={'PATH':'/usr/bin:/bin', 'LANG':'C'})
        self._checkpoint('spawned_before_pid_record')
        from dsec.runtime.registry import identity
        saved = identity(self.child.pid)
        self.process = self._attach(saved)
        self.record['process'] = saved; self._save()

    def _checkpoint(self, stage):
        """No-op named boundary for deterministic controller-crash acceptance."""

    def _retire_unrecorded_processes(self):
        # Resolve the fork-to-pid-record crash gap using the prior durable exact
        # argv (includes unique UUID and private QMP/disk paths). No prefix/PID
        # guesses. Opening a pidfd and re-attesting fences PID replacement.
        from dsec.runtime.registry import identity
        for entry in Path('/proc').iterdir():
            if not entry.name.isdigit():
                continue
            try:
                saved = identity(int(entry.name))
            except (OSError, ValueError):
                continue
            if saved['uid'] != os.getuid() or saved['argv'] != self.record['argv']:
                continue
            process = self._attach(saved)
            try:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill(); process.wait(timeout=5)
            finally:
                process.close()

    def boot(self):
        with self.lock:
            self._require_owner()
            if self.state != 'NEW' or self.record_path.exists():
                raise RuntimeError('QEMU boot is single-use; adopt or retire existing intent')
            self._preflight()
            artifacts = self._artifacts()
            self.record = self._initial_record(artifacts)
            self.state = 'STARTING'; self._save()
            try:
                disk = self.directory/'root.ext4'
                if disk.exists() or disk.is_symlink():
                    raise FileExistsError('Private writable disk already exists')
                _copy_sparse(artifacts['template']['path'], disk)
                disk.chmod(0o600)
                if disk.stat().st_nlink != 1:
                    raise ValueError('Private disk must not be hard-linked')
                self.log = (self.directory/'vmm.log').open('xb')
                self._spawn()
                deadline = time.monotonic() + 15
                while not Path(self.qmp.endpoint).exists():
                    if self.process.poll() is not None:
                        raise RuntimeError('QEMU exited before QMP readiness; inspect vmm.log')
                    if time.monotonic() >= deadline:
                        raise TimeoutError('QMP socket not ready')
                    time.sleep(.02)
                self._prove_kvm()
                self.qmp.execute('cont')
                while True:
                    if self.process.poll() is not None:
                        raise RuntimeError('QEMU exited before guest readiness')
                    try:
                        self.native.capabilities(); break
                    except (OSError, EOFError, UnsupportedCapability):
                        if time.monotonic() >= deadline:
                            raise TimeoutError('QEMU guest native protocol not ready')
                        time.sleep(.02)
                self.state = 'RUNNING'; self._save()
                return self.status()
            except BaseException:
                self._terminate()
                self.state, self.reason = 'FAILED', 'boot_failed_no_replay'
                self._save()
                raise

    def adopt(self):
        with self.lock:
            self._require_owner()
            if self.state != 'NEW':
                raise RuntimeError('Adoption is single-use')
            record = json.loads(self.record_path.read_text())
            artifacts = self._artifacts()
            if (record.get('version') != 1 or record.get('artifacts') != artifacts
                    or record.get('guest_cid') != self.config.guest_cid
                    or record.get('memory_mb') != self.config.memory_mb
                    or record.get('cpu_count') != self.config.cpu_count
                    or record.get('argv') != self._argv(str(uuid.UUID(record['machine_uuid'])), artifacts)):
                raise ValueError('QEMU record configuration identity mismatch')
            self.record = record
            if record.get('process'):
                self.process = self._attach(record['process'])
            if record['state'] in ('STOPPED', 'FAILED') and not record.get('process'):
                self.state, self.reason = record['state'], record.get('reason')
            elif record['state'] in ('RUNNING', 'PAUSED') and self.process is not None:
                self._prove_kvm()
                actual = self.qmp.execute('query-status')
                if actual.get('running') != (record['state'] == 'RUNNING'):
                    raise RuntimeError('QEMU control outcome unknown; explicit stop required')
                self.state = record['state']
            else:
                # A pending boot/control operation is never retried after loss.
                self._terminate()
                if record['state'] == 'STARTING' and not record.get('process'):
                    self._retire_unrecorded_processes()
                self.state, self.reason = 'FAILED', 'interrupted_intent_no_replay'
                self._save()
            return self.status()

    def status(self):
        with self.lock:
            self._require_owner()
            live = self.process is not None and self.process.poll() is None
            if self.state in ('RUNNING', 'PAUSED') and not live:
                self.state, self.reason = 'FAILED', 'vmm_missing_no_replay'
                self._save()
            return dict(state=self.state, process_alive=live, guest_cid=self.config.guest_cid,
                        production=False, reason=self.reason)

    def execute(self, command, *, timeout_ms=5000, output_limit=65536):
        with self.lock:
            self._require_owner()
            if self.state != 'RUNNING' or self.process.poll() is not None:
                raise RuntimeError('QEMU guest is not running')
            # No retry/journal claim at this low-level primitive. Edge must own it.
            return self.channel.execute(ShellRequest(command, timeout_ms, output_limit))

    def checkpoint(self):
        raise UnsupportedCapability('QEMU checkpoint/restore is not Firecracker snapshot format')

    def _control(self, source, pending, target, command):
        with self.lock:
            self._require_owner()
            if self.state != source or self.process is None or self.process.poll() is not None:
                raise RuntimeError('Unexpected QEMU lifecycle state')
            self.state = pending; self._save()
            # If the response is lost, pending intent survives. Adoption retires
            # the process; it cannot guess completion and retry a mutation.
            self.qmp.execute(command)
            self.state = target; self._save()
            return self.status()

    def pause(self):
        return self._control('RUNNING', 'PAUSING', 'PAUSED', 'stop')

    def resume(self):
        return self._control('PAUSED', 'RESUMING', 'RUNNING', 'cont')

    def _terminate(self):
        if self.process is not None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill(); self.process.wait(timeout=5)
            self.process = None
        elif self.child is not None and self.child.poll() is None:
            # Still our unreaped direct child if pidfd acquisition failed.
            self.child.terminate()
            try:
                self.child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.child.kill(); self.child.wait(timeout=5)
        if self.child is not None:
            self.child.wait(timeout=5); self.child = None
        if self.log is not None:
            self.log.close(); self.log = None
        if self.record is not None:
            # Death is proved before clearing the identity. A later owner must
            # not open a pidfd for a terminal record's old/reused PID.
            self.record['process'] = None

    def stop(self):
        with self.lock:
            self._require_owner()
            if self.record is None and self.record_path.exists():
                raise RuntimeError('Adopt attested intent before stop')
            if self.record is None:
                # Persist the fence even if stop precedes boot. Otherwise a
                # later controller could reuse this directory after CID return.
                # No artifact read or VMM effect is needed to stop an empty VM.
                artifacts = {name: {'path': getattr(self.config, name).path,
                                    'sha256': getattr(self.config, name).sha256}
                             for name in ('binary', 'kernel', 'template')}
                self.record = self._initial_record(artifacts)
                self.state = 'STOPPED'; self._save(); return self.status()
            self.state = 'STOPPING'; self._save()
            self._terminate()
            self.state = 'STOPPED'; self._save()
            return self.status()

    def detach(self):
        with self.lock:
            if self.owner.closed:
                return
            if self.process is not None:
                self.process.close(); self.process = None
            if self.log is not None:
                self.log.close(); self.log = None
            self.owner.close()
