"""QEMU/KVM primitive, composed by the Container/FnCall Edge backend.

Only trusted administrator-pinned artifacts enter this driver. Edge leases and
outer-VM Docker belong to its caller; host confinement/eBPF remain separate gates.
Production requests fail before effects. QMP is private; no arbitrary argv, HMP,
host shares, host Docker, device passthrough or TCG fallback.
"""
from dataclasses import dataclass, asdict
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import socket
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
from dsec.runtime.isolation.guest_auth import BOOT_ACK, BOOT_READY, FEATURE, read_exact, validate_key
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
class QemuReadOnlyLayer:
    """An immutable uncompressed EROFS image, shared at its original inode."""
    path: str
    sha256: str
    bytes: int

    def validate(self):
        if (type(self.bytes) is not int or not 128*2**20 <= self.bytes <= 32*2**30
                or self.bytes % (128*2**20)):
            raise ValueError('pmem image must be 128 MiB aligned and bounded')
        # Paths also enter the administrator's exact-file LSM rules.
        import re
        if (not isinstance(self.path,str) or not re.fullmatch(r'/[A-Za-z0-9_./@-]+',self.path)
                or '..' in Path(self.path).parts or str(Path(self.path))!=self.path
                or not isinstance(self.sha256,str) or not re.fullmatch('[a-f0-9]{64}',self.sha256)):
            raise ValueError('Canonical pinned pmem layer required')
        return self

    def verify(self):
        self.validate()
        path=QemuArtifact(self.path,self.sha256).verify()
        info=path.stat()
        if info.st_size!=self.bytes or info.st_mode & 0o222:
            raise ValueError('pmem layer must have the pinned size and no write bits')
        with path.open('rb') as stream:
            stream.seek(1024)
            if stream.read(4)!=b'\xe2\xe1\xf5\xe0':
                raise ValueError('pmem layer is not EROFS')
        return path


@dataclass(frozen=True)
class QemuUEFI:
    code: QemuArtifact
    variables: QemuArtifact
    image_tool: QemuArtifact
    disk_mb: int
    base: QemuArtifact | None = None

    @classmethod
    def from_dict(cls, item):
        if not isinstance(item, dict) or not {'code','variables','image_tool','disk_mb'} <= item.keys() <= {
                'code','variables','image_tool','disk_mb','base'}:
            raise ValueError('Pinned UEFI boot configuration required')
        try:
            return cls(**{name: value if name == 'disk_mb' else QemuArtifact(**value)
                          for name, value in item.items()}).validate()
        except TypeError as exc:
            raise ValueError('Pinned UEFI boot artifacts required') from exc

    def validate(self):
        if type(self.disk_mb) is not int or not 1 <= self.disk_mb <= 1024**2:
            raise ValueError('UEFI disk virtual size must be declared in MiB')
        return self


def uefi_profile_files(artifacts):
    if 'uefi' not in artifacts:
        return None
    boot = artifacts['uefi']
    files = dict(code=boot['code'], template=artifacts['template'])
    if 'base' in boot:
        files['base'] = boot['base']
    return files


@dataclass(frozen=True)
class QemuConfig:
    binary: QemuArtifact
    kernel: QemuArtifact | None
    template: QemuArtifact
    guest_cid: int
    memory_mb: int = 512
    cpu_count: int = 1
    production: bool = False
    startup_timeout_s: float = 15
    disk_cache: str = 'buffered'
    readonly_layers: tuple[QemuReadOnlyLayer, ...] = ()
    display: str = 'none'
    uefi: QemuUEFI | None = None

    def validate(self):
        if (type(self.guest_cid) is not int or not 1024 <= self.guest_cid < 2**32-1
                or type(self.memory_mb) is not int or not 128 <= self.memory_mb <= 65536
                or type(self.cpu_count) is not int or not 1 <= self.cpu_count <= 32
                or type(self.production) is not bool
                or self.disk_cache not in ('buffered','direct')
                or self.display not in ('none','virtio-2d')
                or type(self.startup_timeout_s) not in (int,float)
                or not math.isfinite(self.startup_timeout_s) or not 1<=self.startup_timeout_s<=240):
            raise ValueError('Invalid QEMU resource/CID configuration')
        if (self.kernel is None) == (self.uefi is None):
            raise ValueError('Choose exactly one of direct kernel boot or UEFI')
        if self.uefi is not None:
            self.uefi.validate()
            if self.readonly_layers or self.disk_cache != 'buffered':
                raise ValueError('UEFI requires buffered QCOW2 without pmem layers')
        if not isinstance(self.readonly_layers,tuple) or len(self.readonly_layers)>8:
            raise ValueError('At most eight explicit ordered pmem layers supported')
        for layer in self.readonly_layers:
            if not isinstance(layer,QemuReadOnlyLayer):raise ValueError('Pinned pmem layer required')
            layer.validate()
        if len({layer.path for layer in self.readonly_layers})!=len(self.readonly_layers):
            raise ValueError('Duplicate pmem layer path')
        # Guest struct-page metadata consumes real guest RAM, not free capacity.
        if sum(layer.bytes for layer in self.readonly_layers)//64 > (self.memory_mb-128)*2**20:
            raise ValueError('pmem metadata exceeds guest memory budget')
        if self.production:
            raise UnsupportedCapability('QEMU production requires Edge leases, host confinement and network policy acceptance')
        return self

    def boot_artifacts(self, *, verify=True):
        def pin(artifact):
            path = str(artifact.verify()) if verify else artifact.path
            return dict(path=path, sha256=artifact.sha256)

        result = dict(binary=pin(self.binary), template=pin(self.template))
        if self.uefi is None:
            result['kernel'] = pin(self.kernel)
        else:
            boot = self.uefi
            result['uefi'] = {name: pin(getattr(boot, name)) for name in ('code','variables','image_tool')}
            result['uefi']['disk_mb'] = boot.disk_mb
            if boot.base is not None:
                result['uefi']['base'] = pin(boot.base)
        return result


class QemuVM:
    """Single owner, serialized lifecycle. Stop completion is terminal.

    This primitive boots a trusted guest. Records permit adoption of an
    attested RUNNING process, not replay of interrupted boot or guest operations.
    A guest CID is an administrator-reserved host-wide resource allocated by
    Edge. Kernel CID collisions reject boot, never replace a VM.
    """
    def __init__(self, config, directory, *, ready_probe=None, auth_key=None, host_isolation=None, network=None):
        self.config = config.validate()
        self.auth_key = validate_key(auth_key) if auth_key is not None else None
        path = Path(directory)
        if not path.is_absolute() or path.resolve(strict=True) != path or path.is_symlink():
            raise ValueError('Private canonical instance directory required')
        info = path.stat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError('Instance directory must be owned by runtime user and mode 0700')
        if max(len(os.fsencode(str(path/name))) for name in ('qmp.sock', 'bootstrap.sock')) >= 104:
            raise ValueError('Private QMP socket path is too long')
        self.directory, self.record_path = path, path/'qemu.json'
        self.host = None
        if config.uefi is not None and network is not None:
            raise UnsupportedCapability('UEFI FullVM requires an offline environment')
        if host_isolation is not None:
            from dsec.runtime.isolation.qemu_host import HostIsolation
            self.host = HostIsolation(host_isolation,path,config.memory_mb,config.cpu_count)
            self.host.readonly_layers=[asdict(layer) for layer in config.readonly_layers]
            self.host.uefi = uefi_profile_files(config.boot_artifacts(verify=False))
        self.network = None
        if network is not None:
            if self.host is None:raise ValueError('Isolated QEMU networking requires host confinement')
            from dsec.runtime.isolation.qemu_network import QemuNetwork
            self.network=QemuNetwork(network,path)
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
        self.native = NativeChannel(guest_cid=config.guest_cid, auth_key=self.auth_key)
        self.ready_probe = ready_probe
        self.record = None

    def _require_owner(self):
        if self.owner.closed:
            raise RuntimeError('QEMU instance owner retired')

    def _save(self):
        self.record.update(state=self.state, reason=self.reason)
        atomic_json(self.record_path, self.record)

    def _artifacts(self, *, verify=True):
        result = self.config.boot_artifacts(verify=verify)
        if self.config.readonly_layers:
            if verify:
                for layer in self.config.readonly_layers:
                    layer.verify()
            result['readonly_layers']=[asdict(layer) for layer in self.config.readonly_layers]
        return result

    def _argv(self, machine_uuid, artifacts, network=None):
        # blockdev JSON prevents commas/options in a path from adding drives.
        root = {'driver':'raw', 'node-name':'root', 'file':{
            'driver':'file', 'filename':str(self.directory/'root.ext4')}, 'read-only':False}
        boot = artifacts.get('uefi')
        if boot is not None:
            def image(path, backing):
                return {'driver':'qcow2', 'read-only':True, 'backing':backing,
                        'file':{'driver':'file', 'filename':path, 'read-only':True}}
            base = image(boot['base']['path'], None) if 'base' in boot else None
            root = {'driver':'qcow2', 'node-name':'root', 'read-only':False,
                    'file':{'driver':'file', 'filename':str(self.directory/'root.qcow2')},
                    'backing':image(artifacts['template']['path'], base)}
        elif self.config.disk_cache=='direct':
            # Bypass host file caching; guest writeback must not fill the VMM
            # cgroup with a second dirty copy. Keep flushes enabled for durability.
            root['cache']=dict(direct=True,**{'no-flush':False})
            root['file']['cache']=dict(root['cache'])
        # Chardev uses QemuOpts, not blockdev JSON. Double commas escape paths;
        # each whole value remains one argv item and never passes through shell.
        qmp = 'socket,id=qmp,path=' + str(self.directory/'qmp.sock').replace(',', ',,') + ',server=on,wait=off'
        serial = 'file,id=serial0,path=' + str(self.directory/'console.log').replace(',', ',,')
        argv = [artifacts['binary']['path'], '-no-user-config', '-nodefaults',
                '-machine', 'q35,accel=kvm', '-cpu', 'host', '-m', str(self.config.memory_mb),
                '-smp', str(self.config.cpu_count), '-uuid', machine_uuid,
                '-display', 'none', '-monitor', 'none', '-nic', 'none', '-no-reboot', '-S',
                '-sandbox', 'on,obsolete=deny,elevateprivileges=deny,spawn=deny,resourcecontrol=deny',
                '-chardev', qmp, '-mon', 'chardev=qmp,mode=control',
                '-chardev', serial, '-serial', 'chardev:serial0']
        if boot is None:
            argv += ['-kernel', artifacts['kernel']['path'], '-append',
                     'console=ttyS0 reboot=k panic=1 root=/dev/vda rw init=/dsec-init']
        else:
            code = boot['code']['path'].replace(',', ',,')
            variables = str(self.directory/'uefi-vars.fd').replace(',', ',,')
            argv += ['-drive', f'if=pflash,format=raw,readonly=on,file={code}',
                     '-drive', f'if=pflash,format=raw,file={variables}']
        drive = 'virtio-blk-pci,drive=root' + (',bootindex=0' if boot is not None else '')
        argv += ['-blockdev', json.dumps(root, separators=(',', ':')), '-device', drive,
                 '-device', f'vhost-vsock-pci,guest-cid={self.config.guest_cid}']
        if self.config.display == 'virtio-2d':
            argv += ['-device','virtio-gpu-pci,id=dsecdisplay,xres=1024,yres=768,max_outputs=1',
                     '-device','virtio-keyboard-pci','-device','virtio-tablet-pci']
        if self.config.readonly_layers:
            # Reserve a hotplug address window. No preallocation or per-VM copy;
            # read-only shared ROM prevents a guest from dirtying a common inode.
            total=sum(layer.bytes for layer in self.config.readonly_layers)
            argv[argv.index('-m')+1]=f'size={self.config.memory_mb}M,maxmem={self.config.memory_mb*2**20+total},slots={len(self.config.readonly_layers)}'
            for index,layer in enumerate(self.config.readonly_layers):
                memory=dict(qom_type='memory-backend-file',id=f'dsecpmem{index}',
                    **{'mem-path':layer.path,'size':layer.bytes,'share':True,'readonly':True,
                       'rom':'on','prealloc':False,'discard-data':False})
                memory['qom-type']=memory.pop('qom_type')
                argv += ['-object',json.dumps(memory,separators=(',',':')), '-device',
                         f'virtio-pmem-pci,id=dseclayer{index},memdev=dsecpmem{index}']
        if self.auth_key is not None:
            bootstrap = ('socket,id=bootstrap,path=' +
                         str(self.directory/'bootstrap.sock').replace(',', ',,') + ',server=on,wait=off')
            argv += ['-chardev', bootstrap, '-device', 'virtio-serial-pci,id=dsecserial',
                     '-device', f'virtserialport,bus=dsecserial.0,chardev=bootstrap,name={FEATURE}']
        if network is not None:
            if type(network['fd']) is not int or network['fd']<3:raise ValueError('Private TAP descriptor required')
            # With an inherited TAP descriptor, its creator chose the header
            # mode already. QEMU rejects vnet_hdr= alongside fd=.
            argv += ['-netdev',f"tap,id=dsecnet,fd={network['fd']},vhost=off",
                     '-device','virtio-net-pci,netdev=dsecnet,mac=06:00:dc:00:00:02']
        return argv

    def _preflight(self):
        if os.geteuid() == 0:
            raise PermissionError('QEMU must run unprivileged')
        for device in ('/dev/kvm', '/dev/vhost-vsock'):
            if not stat.S_ISCHR(os.stat(device).st_mode) or not os.access(device, os.R_OK | os.W_OK):
                raise PermissionError(f'QEMU requires read/write access to {device}')

    def _initial_record(self, artifacts):
        record = dict(version=1, backend='qemu-kvm-development', artifacts=artifacts,
            guest_cid=self.config.guest_cid, memory_mb=self.config.memory_mb,
            cpu_count=self.config.cpu_count,startup_timeout_s=self.config.startup_timeout_s,
            disk_cache=self.config.disk_cache,
            machine_uuid=str(uuid.uuid4()), process=None)
        record['argv'] = self._argv(record['machine_uuid'], artifacts)
        if self.auth_key is not None:
            record['auth_sha256'] = hashlib.sha256(self.auth_key).hexdigest()
        if self.host is not None:
            record['host_policy'] = self.host.policy
        if self.network is not None:record['network_policy']=self.network.policy
        return record

    def _provision_channel(self,*,deadline=None):
        if self.auth_key is not None:
            # The only boot transfer. Never replay on adoption of a live VM.
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                deadline=deadline if deadline is not None else time.monotonic()+15
                sock.settimeout(max(.001,deadline-time.monotonic()))
                sock.connect(str(self.directory/'bootstrap.sock'))
                if self.config.uefi is not None:
                    if read_exact(sock, len(BOOT_READY), deadline=deadline) != BOOT_READY:
                        raise ValueError('Guest channel bootstrap readiness mismatch')
                sock.sendall(self.auth_key)
                if read_exact(sock, len(BOOT_ACK), deadline=deadline) != BOOT_ACK:
                    raise ValueError('Guest channel bootstrap acknowledgement mismatch')

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
        if self.host is None:
            self.child = subprocess.Popen(self.record['argv'], stdin=subprocess.DEVNULL,
                stdout=self.log, stderr=subprocess.STDOUT, close_fds=True,
                env={'PATH':'/usr/bin:/bin', 'LANG':'C'})
        else:
            def committed(saved,argv):
                self.record.update(launch_process=saved,launch_argv=argv)
                self._save()
                self._checkpoint('confined_child_identity_committed')
            options = (dict(pass_fds=(self.network.fd,),claim=self.network.claim)
                       if self.network is not None else {})
            try:
                self.child = self.host.launch(self.record['argv'],self.log,committed,
                                             on_spawn=lambda child:setattr(self,'child',child),**options)
            finally:
                if self.network is not None:self.network.close()
        self._checkpoint('spawned_before_pid_record')
        from dsec.runtime.registry import identity
        saved = identity(self.child.pid)
        self.process = self._attach(saved)
        self.record['process'] = saved; self._save()

    def _attach_launcher(self, saved):
        from dsec.runtime.registry import identity, AttachedProcess
        try:current=identity(saved['pid'])
        except FileNotFoundError:return None
        if (any(current[key]!=saved[key] for key in ('uid','start_ticks','boot_id'))):
            raise RuntimeError('Launch process identity changed; recovery fenced')
        if current['argv']==self.record['argv']:
            binary=self.config.binary.path
        elif current['argv']==self.record['launch_argv']:
            binary=self.record['launch_argv'][0]
        else:raise RuntimeError('Unexpected confined launch process; recovery fenced')
        return AttachedProcess(current,binary,self.qmp.endpoint,expected_argv=current['argv'])

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

    def _prepare_disk(self, artifacts):
        boot = artifacts.get('uefi')
        if boot is None:
            disk = self.directory/'root.ext4'
            if disk.exists() or disk.is_symlink():
                raise FileExistsError('Private writable disk already exists')
            _copy_sparse(artifacts['template']['path'], disk)
        else:
            disk, variables = (self.directory/name for name in ('root.qcow2','uefi-vars.fd'))
            if any(path.exists() or path.is_symlink() for path in (disk, variables)):
                raise FileExistsError('Private UEFI disk or firmware variables already exist')
            tool = boot['image_tool']['path']
            base = boot.get('base',{}).get('path')
            template = artifacts['template']['path']
            chain = [(template,base)] + ([(base,None)] if base else [])
            for path, backing in chain:
                info = json.loads(subprocess.check_output(
                    [tool,'info','-f','qcow2','--output=json',path], timeout=20))
                details = info['format-specific']['data']
                if (info.get('backing-filename') != backing or info.get('dirty-flag')
                        or details.get('corrupt') or details.get('data-file')
                        or details.get('data-file-raw')
                        or info['virtual-size'] != boot['disk_mb'] * 2**20):
                    raise ValueError('QCOW2 backing chain or virtual size differs from its pin')
            subprocess.run([tool,'create','-q','-f','qcow2','-F','qcow2','-b',template,str(disk)],
                           check=True, timeout=30)
            _copy_sparse(boot['variables']['path'], variables)
            variables.chmod(0o600)
        disk.chmod(0o600)
        if disk.stat().st_nlink != 1:
            raise ValueError('Private disk must not be hard-linked')

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
                if self.host is not None:
                    self.record['host_isolation']=self.host.prepare();self._save()
                self._prepare_disk(artifacts)
                if self.network is not None:
                    self.record['network_isolation']=self.network.prepare()
                    self.record['argv']=self._argv(self.record['machine_uuid'],artifacts,self.network.record)
                    self._save()
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
                # QEMU installs its own seccomp during initialization, after
                # exec. Require the full kernel proof once QMP is serving and
                # before the paused guest can execute, not at the exec gap.
                if self.host is not None:self.host.attest(self.process.pid)
                if self.network is not None:self.network.attest()
                self.qmp.execute('cont')
                ready_deadline=time.monotonic()+self.config.startup_timeout_s
                self._provision_channel(deadline=ready_deadline)
                while True:
                    if self.process.poll() is not None:
                        raise RuntimeError('QEMU exited before guest readiness')
                    try:
                        (self.ready_probe or self.native.capabilities)(); break
                    except (OSError, EOFError, UnsupportedCapability):
                        if time.monotonic() >= ready_deadline:
                            raise TimeoutError('QEMU guest native protocol not ready')
                        time.sleep(.02)
                self.state = 'RUNNING'; self._save()
                return self.status()
            except BaseException as exc:
                # Preserve the first error even if cleanup itself fails.
                self.record['boot_error']=dict(type=type(exc).__name__,message=str(exc)[:8192])
                self._save()
                try:self._terminate()
                except BaseException as cleanup:
                    self.state,self.reason='STOPPING','boot_failed_cleanup_pending'
                    self.record['cleanup_error']=dict(type=type(cleanup).__name__,message=str(cleanup)[:8192])
                    self._save()
                    raise
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
            expected_argv=self._argv(str(uuid.UUID(record['machine_uuid'])),artifacts,record.get('network_isolation'))
            valid_argv=record.get('argv')==expected_argv
            # The rejected FD/header combination can exist in old failed
            # records. Accept that exact historical spelling only for fully
            # retired instances; never attach a live process with changed argv.
            if (not valid_argv and '-netdev' in expected_argv and record.get('state') in ('STOPPED','FAILED')
                    and not record.get('process') and not record.get('launch_process')
                    and record.get('host_retired') is True and record.get('network_retired') is True):
                legacy=list(expected_argv);legacy[legacy.index('-netdev')+1]+=',vnet_hdr=off'
                valid_argv=record.get('argv')==legacy
            if (record.get('version') != 1 or record.get('artifacts') != artifacts
                    or record.get('guest_cid') != self.config.guest_cid
                    or record.get('memory_mb') != self.config.memory_mb
                    or record.get('cpu_count') != self.config.cpu_count
                    or record.get('startup_timeout_s',15)!=self.config.startup_timeout_s
                    or record.get('disk_cache','buffered')!=self.config.disk_cache
                    or record.get('auth_sha256') != (hashlib.sha256(self.auth_key).hexdigest()
                                                    if self.auth_key is not None else None)
                    or record.get('host_policy') != (self.host.policy if self.host is not None else None)
                    or record.get('network_policy') != (self.network.policy if self.network is not None else None)
                    or not valid_argv):
                raise ValueError('QEMU record configuration identity mismatch')
            self.record = record
            if self.host is not None and record.get('host_isolation'):
                self.host.restore(record['host_isolation'])
            if self.network is not None and record.get('network_isolation'):
                self.network.restore(record['network_isolation'])
            if record.get('process'):
                try:
                    self.process = self._attach(record['process'])
                except ProcessLookupError:
                    # pidfd_open returned ESRCH: this exact recorded PID no
                    # longer exists. Retire its durable intent below; never
                    # re-launch it or signal a later process using that PID.
                    # Identity mismatch/permission errors still fence recovery.
                    self.process = None
            elif record.get('launch_process'):
                self.process = self._attach_launcher(record['launch_process'])
            if record['state'] in ('STOPPED', 'FAILED') and not record.get('process'):
                self.state, self.reason = record['state'], record.get('reason')
            elif record['state'] in ('RUNNING', 'PAUSED') and self.process is not None and self.process.poll() is None:
                if self.host is not None:self.host.attest(self.process.pid)
                if self.network is not None:self.network.attest()
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
            confinement = self.host.attest(self.process.pid) if live and self.host is not None else None
            result = dict(state=self.state, process_alive=live, guest_cid=self.config.guest_cid,
                        host_isolation=confinement,
                        production=False, reason=self.reason)
            if self.network is not None:
                result['network_isolation']=self.network.attest() if live else None
            return result

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
            if self.host is not None:
                needs_retirement=bool(self.record.get('host_isolation') or
                                      self.record.get('launch_process') or self.host.created)
                self.record['launch_process']=None
                self._save()  # Proved death survives a later broker/cleanup failure.
                if needs_retirement:self.host.retire()
                self.record['host_retired']=True
            if self.network is not None:
                self._save()
                self.network.retire()
                self.record['network_retired']=True

    def stop(self):
        with self.lock:
            self._require_owner()
            if self.record is None and self.record_path.exists():
                raise RuntimeError('Adopt attested intent before stop')
            if self.record is None:
                # Persist the fence even if stop precedes boot. Otherwise a
                # later controller could reuse this directory after CID return.
                # No artifact read or VMM effect is needed to stop an empty VM.
                self.record = self._initial_record(self._artifacts(verify=False))
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
            if self.network is not None:self.network.close()
            self.owner.close()
