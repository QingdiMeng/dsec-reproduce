"""Edge-owned Container/FnCall slots with Docker entirely inside QEMU/KVM.

The trusted catalog chooses mode, artifacts and resource ceilings. Callers only
choose an environment. There is no host-Docker or TCG fallback. Outbound network,
host LSM/cgroup confinement and block-layer sharing remain separate capabilities.
"""
from dataclasses import asdict
import hashlib
import json
import math
import os
from pathlib import Path
import re
import socket
import stat
import threading

from dsec._persistence import atomic_json
from dsec.contracts.errors import RequestOutcomeUnknown
from dsec.contracts.requests import RequestUncertain
from dsec.contracts.resources import NodeDemand
from dsec.contracts.sandbox import UnsupportedCapability
from dsec.runtime.backends.docker_guest import PORT, receive, send
from dsec.contracts.execution import command_timeout, DEFAULT_COMMAND_TIMEOUT_MS
from dsec.runtime.backends.qemu import QemuArtifact, QemuConfig, QemuVM, QemuReadOnlyLayer, QemuUEFI
from dsec.runtime.isolation.vsock import CIDLease, CIDLeaseConflict, QemuCIDPool
from dsec.runtime.isolation.guest_auth import FEATURE, authenticate_client, credential
from dsec.runtime.requests import RequestJournal
from dsec.runtime.sessions.native import NativeChannel
from dsec.contracts.environments import published_environment


def private_disk_mb(item):
    if 'uefi' in item:
        disk = item['uefi']['disk_mb']
        return disk + math.ceil(disk/128) + 8  # QCOW2 metadata and private firmware variables.
    return math.ceil(Path(item['template']['path']).stat().st_size / 2**20)


def _qemu_config(item, cid):
    return QemuConfig(binary=QemuArtifact(**item['binary']),
        kernel=QemuArtifact(**item['kernel']) if 'kernel' in item else None,
        template=QemuArtifact(**item['template']), guest_cid=cid, memory_mb=item['memory_mb'],
        cpu_count=item['cpu_count'], startup_timeout_s=item.get('startup_timeout_s',15),
        disk_cache=item.get('disk_cache','buffered'), display=item.get('display','none'),
        readonly_layers=tuple(QemuReadOnlyLayer(**layer) for layer in item.get('readonly_layers',[])),
        uefi=QemuUEFI.from_dict(item['uefi']) if 'uefi' in item else None)


class GuestControl:
    def __init__(self, cid, *, auth_key, socket_factory=socket.socket):
        self.cid, self.socket_factory, self.auth_key = cid, socket_factory, auth_key

    def call(self, operation, **args):
        with self.socket_factory(socket.AF_VSOCK, socket.SOCK_STREAM) as sock:
            sock.settimeout(args.get('timeout_ms', 5000)/1000+12)
            sock.connect((self.cid, PORT))
            authenticate_client(sock, self.auth_key, PORT)
            send(sock, dict(operation=operation, **args))
            value = receive(sock)
        if not value.get('ok'):
            raise RuntimeError('Guest supervisor rejected operation: '+value.get('message', 'unknown'))
        return value['result']


class ExecutionCatalog:
    def __init__(self, path):
        self.path = Path(path).resolve(strict=True)
        data = self.path.read_bytes()
        self.digest = hashlib.sha256(data).hexdigest()
        value = json.loads(data)
        required={'schema','cid_pool','environments'}
        if (not required<=set(value)<=required|{'host_isolation','network','catalog_history'} or value['schema'] != 2 or
                not isinstance(value['environments'], dict) or not value['environments']):
            raise ValueError('QEMU execution catalog schema 2 with authenticated guests required; drain old instances')
        if set(value['cid_pool']) != {'root', 'first', 'last'}:
            raise ValueError('Explicit shared CID pool required')
        self.pool = value['cid_pool']
        self.environments = value['environments']
        history=value.get('catalog_history',[])
        if not isinstance(history,list) or len(history)>32:raise ValueError('Bounded explicit catalog history required')
        self.snapshots=[]
        for pin in history:
            if (not isinstance(pin,dict) or set(pin)!={'path','sha256'} or
                    not isinstance(pin['sha256'],str) or not re.fullmatch('[a-f0-9]{64}',pin['sha256'])):
                raise ValueError('Pinned previous catalog required')
            path=Path(pin['path'])
            if not path.is_absolute() or path.is_symlink() or path.resolve(strict=True)!=path:
                raise ValueError('Canonical previous catalog required')
            blob=path.read_bytes()
            if hashlib.sha256(blob).hexdigest()!=pin['sha256']:raise ValueError('Previous catalog identity changed')
            previous=json.loads(blob)
            if not isinstance(previous,dict) or not isinstance(previous.get('environments'),dict):
                raise ValueError('Invalid previous catalog')
            self.snapshots.append((path,pin['sha256'],previous))
        self.value=value
        self.host_isolation = None
        if 'host_isolation' in value:
            from dsec.runtime.isolation.qemu_host import configuration
            self.host_isolation=configuration(value['host_isolation'])
        self.network=None
        if 'network' in value:
            from dsec.runtime.isolation.qemu_network import configuration
            self.network=configuration(value['network'])
            if self.host_isolation is None:raise ValueError('Networked environments require host_isolation')
        for name, item in self.environments.items():
            fields={'mode','binary','template','memory_mb','cpu_count'}
            fullvm = item.get('mode') == 'fullvm'
            uefi = 'uefi' in item
            fields.add('uefi' if uefi else 'kernel')
            if not fullvm:
                fields |= {'image','inner_memory_mb','inner_cpus'}
            if (not re.fullmatch('[a-z0-9][a-z0-9.-]{0,127}', name) or
                    not fields<=set(item)<=fields|{'network','startup_timeout_s','disk_cache','readonly_layers','shared_mounts','command_timeout_ms','execution_contract','display'} or
                    item.get('disk_cache','buffered') not in ('buffered','direct') or
                    type(item.get('startup_timeout_s',15)) not in (int,float) or
                    not math.isfinite(item.get('startup_timeout_s',15)) or
                    not 1<=item.get('startup_timeout_s',15)<=240 or
                    item.get('network','none') not in ('none','isolated') or
                    item['mode'] not in ('container','fncall','fullvm') or
                    type(item['memory_mb']) is not int or not 512 <= item['memory_mb'] <= 65536 or
                    type(item['cpu_count']) is not int or not 1 <= item['cpu_count'] <= 32):
                raise ValueError('Invalid execution environment')
            display = item.get('display','none')
            if display not in ('none','virtio-2d') or (not fullvm and display != 'none'):
                raise ValueError('2D display requires FullVM')
            if display != 'none' and self.host_isolation is not None and not uefi:
                raise ValueError('Desktop preview has no host confinement profile')
            if uefi and not fullvm:
                raise ValueError('UEFI requires a FullVM environment')
            if fullvm:
                if item.get('network','none') != 'none' or item.get('readonly_layers') or item.get('shared_mounts'):
                    raise ValueError('FullVM preview requires a private local disk and no network')
            elif (not re.fullmatch('sha256:[0-9a-f]{64}', item['image']) or
                  type(item['inner_memory_mb']) is not int or item['inner_memory_mb'] < 128 or
                  item['memory_mb'] < item['inner_memory_mb']+256 or
                  type(item['inner_cpus']) not in (int,float) or
                  not math.isfinite(item['inner_cpus']) or not 0 < item['inner_cpus'] <= item['cpu_count']):
                raise ValueError('Invalid inner container environment')
            command_timeout(item.get('command_timeout_ms',DEFAULT_COMMAND_TIMEOUT_MS))
            layers=item.get('readonly_layers',[])
            if not isinstance(layers,list):raise ValueError('Explicit ordered pmem layers required')
            if layers:
                from dsec.contracts.layers import manifest
                manifest(dict(schema=1,layers=[dict(sha256=layer['sha256'],bytes=layer['bytes']) for layer in layers],
                              mounts=item.get('shared_mounts',[])))
            elif item.get('shared_mounts'):raise ValueError('Shared mounts require pmem layers')
            if item.get('network')=='isolated' and self.network is None:
                raise ValueError('Isolated environment requires a pinned network policy')
            pins = [item[key] for key in ('binary','template')]
            if uefi:
                QemuUEFI.from_dict(item['uefi'])
                pins.extend(value for name,value in item['uefi'].items() if name != 'disk_mb')
            else:
                pins.append(item['kernel'])
            for artifact in pins:
                if not isinstance(artifact, dict) or set(artifact) != {'path','sha256'}:
                    raise ValueError('Pinned execution artifact required')
                if not Path(artifact['path']).is_absolute():
                    raise ValueError('Absolute pinned execution artifact required')
                if not re.fullmatch('[a-f0-9]{64}', artifact['sha256']):
                    raise ValueError('SHA-256 required')
            _qemu_config(item, 1024).validate()
            published_environment(name, 'fullvm' if item['mode']=='fullvm' else 'container', item['mode'], item)

    def execution_contracts(self):
        self.unchanged()
        return tuple(published_environment(name, 'fullvm' if item['mode']=='fullvm' else 'container', item['mode'], item)
                     for name,item in self.environments.items())

    def verify_boot_artifacts(self):
        self.unchanged()
        for item in self.environments.values():
            _qemu_config(item, 1024).boot_artifacts()

    def unchanged(self):
        if hashlib.sha256(self.path.read_bytes()).hexdigest() != self.digest:
            raise UnsupportedCapability('Execution catalog changed; restart Edge explicitly')
        for path,digest,_ in self.snapshots:
            if hashlib.sha256(path.read_bytes()).hexdigest()!=digest:
                raise UnsupportedCapability('Previous catalog changed; no effects permitted')

    def accepts_digest(self,environment_id,digest):
        if digest==self.digest:return True
        # Explicit additive upgrades only. There is no transitive history trust
        # and no permission to reinterpret an old instance under changed limits,
        # image/artifacts, CID ownership or host/network policy.
        return any(old_digest==digest and
            all(previous.get(key)==self.value.get(key) for key in ('schema','cid_pool','host_isolation','network')) and
            previous.get('environments',{}).get(environment_id)==self.environments[environment_id]
            for _,old_digest,previous in self.snapshots)


class QemuContainerBackend:
    def __init__(self, catalog, environment_id, root, *, control_factory=GuestControl):
        self.catalog = catalog
        self.environment_id = environment_id
        self.item = catalog.environments[environment_id]
        self.network = catalog.network if self.item.get('network')=='isolated' else None
        self.root = Path(root).resolve(strict=True)
        self.root = QemuCIDPool._private_directory(self.root)
        self.pool = QemuCIDPool(**catalog.pool)
        self.control_factory = control_factory
        self.lock, self.handles = threading.RLock(), {}
        # Include the outer VM and the maximum private disk footprint.
        overhead=catalog.host_isolation['memory_overhead_mb'] if catalog.host_isolation else 0
        self.minimum_demand = NodeDemand(self.item['cpu_count'], self.item['memory_mb']+overhead,
                                        private_disk_mb(self.item), 0)

    @property
    def route_backend(self):
        return 'fullvm' if self.item['mode'] == 'fullvm' else 'container'

    @property
    def execution_memory(self):
        return self.item['memory_mb' if self.route_backend == 'fullvm' else 'inner_memory_mb']

    @property
    def execution_cpus(self):
        return self.item['cpu_count' if self.route_backend == 'fullvm' else 'inner_cpus']

    def check_spec(self, spec):
        self.catalog.unchanged()
        if (spec.storage != 'local' or spec.cpu_qos != 'default' or
                spec.execution_mode != self.item['mode'] or
                spec.memory_limit_mb != self.execution_memory or
                spec.cpu_cores_limit != self.execution_cpus):
            raise UnsupportedCapability('QEMU environment has pinned mode, execution limits and local storage')

    def _config(self, cid):
        return _qemu_config(self.item, cid)

    def _directory(self, sid):
        if not isinstance(sid, str) or not re.fullmatch('[0-9a-f]{32}', sid):
            raise ValueError('Invalid sandbox ID')
        return self.root/sid

    def _handle(self, sid):
        with self.lock:
            self.catalog.unchanged()
            if sid in self.handles:
                return self.handles[sid]
            directory = self._directory(sid)
            QemuCIDPool._private_directory(directory)
            record = json.loads((directory/'execution.json').read_text())
            if (set(record) != {'schema','catalog_sha256','environment_id','lease','auth_sha256'} or
                    record['schema'] != 2 or not self.catalog.accepts_digest(self.environment_id,record['catalog_sha256']) or
                    record['environment_id'] != self.environment_id):
                raise ValueError('Execution descriptor identity mismatch')
            lease = CIDLease(**record['lease'])
            if lease.directory != str(directory):
                raise ValueError('CID owner mismatch')
            current = self.pool.snapshot().get(str(lease.cid))
            if current != asdict(lease):
                saved = json.loads((directory/'qemu.json').read_text())
                if saved.get('state') != 'STOPPED' or saved.get('process') is not None:
                    raise CIDLeaseConflict('Live or uncertain execution requires its original CID lease')
            key = credential(directory)
            if hashlib.sha256(key).hexdigest() != record['auth_sha256']:
                raise ValueError('Guest channel credential identity mismatch')
            control = self.control_factory(lease.cid, auth_key=key)
            vm = QemuVM(self._config(lease.cid), directory, ready_probe=lambda: self._ready(control), auth_key=key,
                        host_isolation=self.catalog.host_isolation,network=self.network)
            try:
                if vm.record_path.exists():
                    vm.adopt()
                else:
                    # Descriptor/CID precede boot. Absence of boot intent is
                    # not permission to retry a create after controller loss.
                    pass
                handle = QemuContainer(self, sid, lease, vm, control)
                self.handles[sid] = handle
                return handle
            except BaseException:
                vm.detach(); raise

    def _ready(self, control):
        value = control.call('status')
        expected = dict(mode=self.item['mode'], network='isolated' if self.network else 'none',
                        transport_auth=FEATURE)
        if self.route_backend == 'container':
            expected.update(image=self.item['image'], memory_mb=self.execution_memory, cpus=self.execution_cpus)
        else:
            expected['state'] = 'RUNNING'
        if 'command_timeout_ms' in self.item:expected['command_timeout_ms']=self.item['command_timeout_ms']
        if self.network:expected['network_policy']=self.network['policy']
        if self.item.get('readonly_layers'):
            expected['readonly_layers']=[dict(sha256=layer['sha256'],bytes=layer['bytes'],dax=True)
                                         for layer in self.item['readonly_layers']]
            expected['shared_mounts']=self.item['shared_mounts']
        if any(value.get(key) != expected_value for key,expected_value in expected.items()):
            raise ValueError('Guest execution identity mismatch')
        return value

    def create(self, *, memory_mb, cpus, qos='default', sandbox_id):
        with self.lock:
            self.catalog.unchanged()
            if (memory_mb != self.execution_memory or cpus != self.execution_cpus or qos != 'default'):
                raise UnsupportedCapability('Execution limits differ from prepared guest')
            directory = self._directory(sandbox_id)
            directory.mkdir(mode=0o700)  # No create replay over any existing intent.
            key = credential(directory, create=True)
            lease = self.pool.reserve(directory)
            atomic_json(directory/'execution.json', dict(schema=2,catalog_sha256=self.catalog.digest,
                environment_id=self.environment_id,lease=asdict(lease),auth_sha256=hashlib.sha256(key).hexdigest()))
            control = self.control_factory(lease.cid, auth_key=key)
            vm = QemuVM(self._config(lease.cid),directory,ready_probe=lambda: self._ready(control),auth_key=key,
                        host_isolation=self.catalog.host_isolation,network=self.network)
            handle = QemuContainer(self,sandbox_id,lease,vm,control)
            self.handles[sandbox_id] = handle
        try:
            vm.boot()
            return handle
        except BaseException as exc:
            # Unknown cleanup retains its reservation; no timeout-based freeing.
            original=(vm.record or {}).get('boot_error') or dict(type=type(exc).__name__,message=str(exc)[:8192])
            failure=dict(schema=1,sandbox_id=sandbox_id,error=original['type'],message=original['message'],cleanup_complete=False)
            atomic_json(directory/'failed-create.json',failure)
            try:handle.stop()
            except BaseException as cleanup:
                failure['cleanup_error']=dict(type=type(cleanup).__name__,message=str(cleanup)[:8192])
                atomic_json(directory/'failed-create.json',failure)
                raise
            failure['cleanup_complete']=True
            atomic_json(directory/'failed-create.json',failure)
            raise

    def prove_failed_create(self, sid):
        path = self._directory(sid)/'failed-create.json'
        if path.is_file():
            value=json.loads(path.read_text())
            if value.get('schema')!=1 or value.get('sandbox_id')!=sid:return False
        else:
            # A lost owner can die before publishing failed-create.json. Adopt
            # the exact intent; never destroy a successfully RUNNING creation.
            if not (path.parent/'qemu.json').is_file():return False
            handle=self._handle(sid)
            state=handle.vm.status()
            if state['state'] not in ('FAILED','STOPPED','STOPPING') or state['process_alive']:return False
            error=(handle.vm.record or {}).get('boot_error') or dict(type='InterruptedCreate',message='Unreturned creation retired without replay')
            value=dict(schema=1,sandbox_id=sid,error=error['type'],message=error['message'],cleanup_complete=False)
            atomic_json(path,value)
        if not self.prove_stopped(sid):return False
        if not value.get('cleanup_complete'):
            value['cleanup_complete']=True;atomic_json(path,value)
        return True

    def attach(self, sid):
        handle = self._handle(sid)
        if not self.prove_running(sid):
            raise RuntimeError('QEMU execution is not running')
        return handle

    def prove_running(self, sid):
        handle = self._handle(sid)
        status = handle.vm.status()
        return status['state'] == 'RUNNING' and status['process_alive'] and bool(self._ready(handle.control))

    def prove_stopped(self, sid):
        directory = self._directory(sid)
        if not directory.exists():
            return True  # A Node-owned new root, not a scan of host processes.
        handle = self._handle(sid)
        status = handle.vm.status()
        if status['state'] not in ('STOPPED','FAILED','STOPPING') or status['process_alive']:
            return False
        # Reconcile a crash between pidfd death, CID release and reply commit.
        handle.stop()
        return True

    def native_channel(self, sid):
        if self.item['mode'] == 'fncall':
            raise UnsupportedCapability('FnCall has no persistent native session')
        handle = self.attach(sid)
        return NativeChannel(guest_cid=handle.lease.cid, auth_key=handle.control.auth_key,
                             max_timeout_ms=self.item.get('command_timeout_ms',DEFAULT_COMMAND_TIMEOUT_MS))

    def query_action(self, sid, request_id):
        return self._handle(sid).query_request(request_id)

    def close(self):
        with self.lock:
            for handle in self.handles.values():
                handle.vm.detach()
            self.handles.clear()


class QemuContainer:
    def __init__(self, backend, sid, lease, vm, control):
        self.backend, self.id, self.lease, self.vm, self.control = backend,sid,lease,vm,control
        self.name = 'guest-only:'+sid
        self.journal = RequestJournal(vm.directory,retain_args=True)

    def status(self):
        value = self.vm.status()
        result = dict(value,id=self.id,backend=('fullvm' if self.backend.item['mode']=='fullvm' else 'container'),execution_path=self.backend.item['mode'],
            containment='qemu-kvm',resource_cleanup_complete=False)
        if self.vm.record and self.vm.record.get('process'):
            result['pid'] = self.vm.record['process']['pid']
        if self.backend.item.get('display') == 'virtio-2d':
            result['display'] = 'virtio-2d'
        if value['state'] == 'RUNNING':
            result['guest'] = self.backend._ready(self.control)
        return result

    def _display_running(self):
        if self.backend.item.get('display') != 'virtio-2d':
            raise UnsupportedCapability('FullVM environment does not enable 2D display')
        status = self.vm.status()
        if status['state'] != 'RUNNING' or not status['process_alive']:
            raise RuntimeError('Display VM is not running')

    def screenshot(self):
        import base64
        import struct
        with self.vm.lock:
            self._display_running()
            # No caller path. The private directory belongs to this exact VM.
            path = self.vm.directory/'display.png'
            path.unlink(missing_ok=True)
            try:
                self.vm.qmp._screendump(path)
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(fd,'rb') as image:
                    info = os.fstat(image.fileno())
                    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
                        raise RuntimeError('Unexpected QEMU screenshot file identity')
                    data = image.read(3*1024**2+1)
                if (len(data)>3*1024**2 or len(data)<33 or
                        data[:16] != b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR'):
                    raise RuntimeError('Invalid or oversized QEMU screenshot')
                width,height = struct.unpack('>II',data[16:24])
                if not 1<=width<=4096 or not 1<=height<=4096 or width*height>4096*2160:
                    raise RuntimeError('QEMU screenshot exceeds pixel budget')
                return dict(format='png',width=width,height=height,data=base64.b64encode(data).decode('ascii'))
            finally:
                path.unlink(missing_ok=True)

    def desktop_input(self, events, *, request_id):
        from dsec.contracts.desktop import input_events
        normalized = input_events(events)
        with self.vm.lock:
            self._display_running()
            request = dict(operation='desktop_input',sandbox_id=self.id,request_id=request_id,
                           args=dict(events=normalized))
            try:
                saved = self.journal.begin(request)
            except RequestUncertain as exc:
                raise RequestOutcomeUnknown('Desktop input outcome unknown; query instead of replay',request_id) from exc
            if saved is not None:
                return saved['result']
            try:
                self.vm.qmp._send_input(normalized)
                result = dict(accepted=len(normalized))
                self.journal.finish(request_id,dict(ok=True,request_id=request_id,result=result))
                return result
            except BaseException as exc:
                if self.journal.lookup(request_id)['state'] == 'PENDING':
                    self.journal.unknown(request_id,reason=type(exc).__name__)
                if not isinstance(exc, Exception):
                    raise
                raise RequestOutcomeUnknown('Desktop input outcome unknown; query instead of replay',request_id) from exc

    def run_shell(self, command, *, timeout_ms=5000, output_limit=65536, request_id=None):
        if request_id is None:
            raise ValueError('Request identity required for QEMU execution')
        if (not isinstance(command,str) or len(command.encode()) > 65536 or
                type(timeout_ms) is not int or not 1 <= timeout_ms <= self.backend.item.get('command_timeout_ms',DEFAULT_COMMAND_TIMEOUT_MS) or
                type(output_limit) is not int or not 1 <= output_limit <= 262144):
            raise ValueError('Command outside supported execution limits')
        with self.vm.lock:
            request = dict(operation='execute',sandbox_id=self.id,request_id=request_id,
                args=dict(command=command,timeout_ms=timeout_ms,output_limit=output_limit))
            saved = self.journal.begin(request)
            if saved is not None:
                return saved['result']
            if self.vm.status()['state'] != 'RUNNING':
                self.journal.unknown(request_id)
                raise RuntimeError('Execution slot is not running')
        try:
            proof = self.control.call('execute',request_id=request_id, command=command,
                                      timeout_ms=timeout_ms,output_limit=output_limit)
            if proof.get('state') == 'DONE' and proof.get('response',{}).get('ok'):
                result = proof['response']['result']
                self.journal.finish(request_id,dict(ok=True,request_id=request_id,result=result))
                return result
            raise RequestOutcomeUnknown('Guest command result unknown; query instead of replay',request_id)
        except BaseException as exc:
            if self.journal.lookup(request_id)['state'] == 'PENDING':
                self.journal.unknown(request_id,reason=f'{type(exc).__name__}: {exc}')
            # A function cannot return to service with uncertain cleanup.
            if self.backend.item['mode'] == 'fncall':
                self.stop()
            if not isinstance(exc, Exception):
                raise
            raise RequestOutcomeUnknown('Guest execution/cleanup uncertain',request_id) from exc

    def query_request(self, request_id):
        saved=self.journal.lookup(request_id)
        if saved['state']!='UNKNOWN' or saved.get('operation')!='execute':return saved
        if saved.get('sandbox_id')!=self.id or not isinstance(saved.get('args'),dict):return saved
        # Guest journals are durable within this exact VM identity. Never run an
        # execute to recover a lost reply, and never infer success from liveness.
        if self.vm.status()['state']!='RUNNING':return saved
        try:proof=self.control.call('query',request_id=request_id)
        except (OSError,EOFError,RuntimeError,ValueError) as exc:
            return dict(saved,reconciliation_error=dict(type=type(exc).__name__,message=str(exc)[:1000]))
        expected=dict(operation='execute',request_id=request_id,**saved['args'])
        digest=hashlib.sha256(json.dumps(expected,sort_keys=True).encode()).hexdigest()
        if (proof.get('state')=='DONE' and proof.get('request_id')==request_id and
                proof.get('digest')==digest and proof.get('response',{}).get('ok') is True):
            return self.journal.recover_done(request_id,saved['digest'],proof['response']['result'])
        return saved

    def stop(self):
        with self.vm.lock:
            record = self.backend.pool.snapshot()
            if record.get(str(self.lease.cid)) == asdict(self.lease):
                self.backend.pool.retire(self.lease, self.vm)
            else:
                # A stopped predecessor may share the numeric CID with a new
                # owner. Its terminal proof is local; never retire the new token.
                status = self.vm.status()
                if status['state'] != 'STOPPED' or status['process_alive']:
                    raise RuntimeError('Missing CID lease without proven terminal VM')
            # Disk quota is not free while a stopped slot still retains its
            # private writable disk. Keep journals/logs, retire only task data.
            names = ('root.qcow2','uefi-vars.fd') if 'uefi' in self.backend.item else ('root.ext4',)
            retired = []
            for name in names:
                disk = self.vm.directory/name
                try:
                    info = disk.lstat()
                except FileNotFoundError:
                    continue
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1):
                    raise RuntimeError('Private disk ownership/type changed; cleanup not proved')
                retired.append(disk)
            for disk in retired:
                disk.unlink()
            atomic_json(self.vm.directory/'retired.json',dict(schema=1,lease=asdict(self.lease),
                vm_stopped=True,cid_retired=True,private_disk_retired=True))
            return dict(id=self.id,backend=('fullvm' if self.backend.item['mode']=='fullvm' else 'container'),state='STOPPED',resource_cleanup_complete=True)
