"""Trusted, networkless Docker supervisor inside a QEMU guest.

This file is also copied standalone into prepared guests. It has no DSec package
dependencies beyond the copied guest_auth module. The control/native listeners
require the host CID and a per-VM key; neither exposes Docker argv or a socket.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import signal
import socket
import subprocess
import threading
import time

if __package__:
    from dsec.runtime.isolation import guest_auth

PORT = 5002
MAX_FRAME = 2 * 1024 * 1024
if __package__:
    from dsec.contracts.execution import command_timeout, DEFAULT_COMMAND_TIMEOUT_MS
else:
    from execution_contract import command_timeout, DEFAULT_COMMAND_TIMEOUT_MS


def receive(sock):
    def read(size):
        data = bytearray()
        while len(data) < size:
            part = sock.recv(size - len(data))
            if not part:
                raise EOFError('Incomplete guest frame; outcome unknown')
            data.extend(part)
        return bytes(data)
    size = int.from_bytes(read(4), 'big')
    if not 0 < size <= MAX_FRAME:
        raise ValueError('Guest frame limit exceeded')
    value = json.loads(read(size))
    if not isinstance(value, dict):
        raise ValueError('Object required')
    return value


def send(sock, value):
    data = json.dumps(value, ensure_ascii=True, separators=(',', ':')).encode()
    if len(data) > MAX_FRAME:
        raise ValueError('Guest frame limit exceeded')
    sock.sendall(len(data).to_bytes(4, 'big') + data)


def save(path, value):
    temporary = path.with_suffix('.tmp')
    with temporary.open('w') as stream:
        json.dump(value, stream, sort_keys=True)
        stream.flush(); os.fsync(stream.fileno())
    os.replace(temporary, path)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def command_children():
    """Direct descendants of this helper thread, including adopted orphans."""
    path=Path('/proc/self/task')/str(threading.get_native_id())/'children'
    return {int(pid) for pid in path.read_text().split()}


def subreaper():
    # The standalone helper owns only one command. On Linux, orphaned double
    # forks/setsid children must return here instead of escaping to container PID1.
    import sys
    if sys.platform!='linux':return None,set()
    import ctypes
    libc=ctypes.CDLL(None,use_errno=True);previous=ctypes.c_int()
    if libc.prctl(37,ctypes.byref(previous),0,0,0) or libc.prctl(36,1,0,0,0):
        raise OSError(ctypes.get_errno(),'Command descendant containment unavailable')
    return (libc,previous.value),command_children()


def retire_command(process, adoption, baseline, *, kill_tree=True):
    try:
        if kill_tree:
            try:os.killpg(process.pid,signal.SIGKILL)
            except ProcessLookupError:pass
        process.wait(timeout=2)
        if adoption is not None and kill_tree:
            deadline=time.monotonic()+2
            while True:
                children=command_children()-baseline
                if not children:break
                for pid in children:
                    try:os.kill(pid,signal.SIGKILL)
                    except ProcessLookupError:pass
                    try:os.waitpid(pid,os.WNOHANG)
                    except ChildProcessError:pass
                if time.monotonic()>=deadline:
                    raise RuntimeError('Command descendant retirement not proved')
                time.sleep(.01)
    finally:
        if adoption is not None:
            libc,previous=adoption
            if libc.prctl(36,previous,0,0,0):raise RuntimeError('Subreaper restoration failed')


def bounded_shell(command, timeout_ms, output_limit):
    """Inner-container helper: drain output without buffering an unbounded pipe."""
    adoption,baseline=subreaper()
    try:
        process = subprocess.Popen(['/bin/sh', '-lc', command], cwd='/work',
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
    except BaseException:
        if adoption is not None:adoption[0].prctl(36,adoption[1],0,0,0)
        raise
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    output = bytearray(); truncated = timed_out = False
    deadline = time.monotonic() + timeout_ms / 1000
    try:
        while selector.get_map():
            if time.monotonic() >= deadline:
                timed_out = True
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                # A descendant could retain stdout after the shell exits.
                # End pipe draining at the same bounded command deadline.
                break
            for key, _ in selector.select(min(.05, max(0, deadline-time.monotonic()))):
                part = os.read(key.fd, 65536)
                if not part:
                    selector.unregister(key.fileobj)
                    continue
                available = output_limit - len(output)
                output.extend(part[:available])
                truncated |= len(part) > available
        remaining = max(.01, deadline-time.monotonic())
        try:
            code = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            code = process.wait(timeout=2)
        return dict(exit_code=code, output=output.decode(errors='replace'),
                    timed_out=timed_out, truncated=truncated)
    finally:
        selector.close(); process.stdout.close()
        # Timeout/failure retires all descendants, including new sessions. A
        # successful Container shell may intentionally leave a background server;
        # preserve that existing behavior. FnCall still resets the whole container.
        import sys
        retire_command(process,adoption,baseline,
                       kill_tree=timed_out or process.poll() is None or sys.exc_info()[0] is not None)


class Supervisor:
    def __init__(self, config, root='/var/lib/dsec-control', *, run=None):
        self.config = config
        if (not {'mode','image','memory_mb','cpus'}<=set(config)<=
                {'mode','image','memory_mb','cpus','network','layers','command_timeout_ms'} or
                config['mode'] not in ('container', 'fncall') or
                not re.fullmatch('sha256:[0-9a-f]{64}', config['image']) or
                type(config['memory_mb']) is not int or config['memory_mb'] < 128 or
                type(config['cpus']) not in (int, float) or not 0 < config['cpus'] <= 32):
            raise ValueError('Invalid pinned guest configuration')
        self.command_timeout_ms=command_timeout(config.get('command_timeout_ms',DEFAULT_COMMAND_TIMEOUT_MS))
        self.layers=config.get('layers')
        if self.layers is not None:
            if __package__:
                from dsec.runtime.isolation import guest_layers
            else:import guest_layers
            self.layers=guest_layers.manifest(self.layers)
            self.layer_proof=guest_layers.attest(self.layers)
        self.network=config.get('network')
        if self.network is not None and set(self.network)!={'dns','egress_proxy'}:
            raise ValueError('Pinned guest network policy required')
        self.root = Path(root); self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.run = run or self._run
        self.name, self.failed = 'dsec-task', False

    @staticmethod
    def _run(arguments, *, timeout=30):
        value = subprocess.run(['/usr/local/bin/docker', '--host',
            'unix:///run/dsec-docker/docker.sock', *arguments],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout,
            env={'PATH':'/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'})
        if value.returncode:
            raise RuntimeError('Guest Docker failed: ' + value.stderr[-1000:])
        return value.stdout.strip()

    def start(self):
        # Inner untrusted commands never receive this control directory/socket.
        mode = self.config['mode']
        # "host" here is exclusively the outer QEMU guest, never the Node.
        argv = ['run', '-d', '--name', self.name, '--network', 'host' if self.network else 'none', '--cap-drop', 'ALL',
            '--security-opt', 'no-new-privileges', '--pids-limit', '128',
            '--memory', f"{self.config['memory_mb']}m", '--memory-swap', f"{self.config['memory_mb']}m",
            '--cpus', str(self.config['cpus']), '--user', '1000:1000', '--workdir', '/work',
            '--mount', 'type=bind,src=/usr/local/bin/docker-init,dst=/dsec-init,readonly',
            '--mount', 'type=bind,src=/opt/dsec/docker_guest.py,dst=/dsec-helper.py,readonly',
            '--mount', 'type=bind,src=/opt/dsec/execution_contract.py,dst=/execution_contract.py,readonly',
            '--tmpfs', '/tmp:rw,nosuid,nodev,size=64m,mode=1777']
        if self.layers:
            for target in self.layers['mounts']:
                argv+=['--mount','type=bind,src=/var/lib/dsec-layers/merged'+target+',dst='+target+
                       (',readonly' if mode=='fncall' else '')]
        if self.network and self.network['egress_proxy']:
            proxy=self.network['egress_proxy'];url=f"http://{proxy['ip']}:{proxy['port']}"
            for key in ('HTTP_PROXY','HTTPS_PROXY','http_proxy','https_proxy'):argv+=['--env',key+'='+url]
            argv+=['--env','NO_PROXY=localhost,127.0.0.1','--env','no_proxy=localhost,127.0.0.1']
        if mode == 'fncall':
            argv += ['--read-only', '--tmpfs', '/work:rw,nosuid,nodev,size=64m,uid=1000,gid=1000']
        else:
            argv += ['--mount', 'type=bind,src=/var/lib/dsec-work,dst=/work',
                '--mount', 'type=bind,src=/opt/dsec/native-agent,dst=/dsec-native-agent,readonly',
                '--mount', 'type=bind,src=/run/dsec-native,dst=/dsec-native']
        argv += ['--entrypoint', '/dsec-init', self.config['image'], '--', '/bin/sh', '-c', 'exec sleep infinity']
        self.container_id = self.run(argv)
        if not re.fullmatch('[0-9a-f]{64}', self.container_id):
            raise RuntimeError('Invalid guest container identity')
        if mode == 'container':
            self.run(['exec', '-d', self.name, '/dsec-native-agent', '--unix', '/dsec-native/agent.sock'])
        self.save_state('READY')

    def save_state(self, state):
        save(self.root/'supervisor.json', dict(state=state, container_id=self.container_id))

    def status(self):
        value = json.loads(self.run(['inspect', self.name]))[0]
        if value['Id'] != self.container_id or not value['State']['Running'] or self.failed:
            raise RuntimeError('Inner container identity/state lost; retire outer VM')
        result=dict(mode=self.config['mode'], image=self.config['image'],
                    container_id=self.container_id, state='RUNNING', network='isolated' if self.network else 'none',
                    memory_mb=self.config['memory_mb'], cpus=self.config['cpus'], transport_auth=guest_auth.FEATURE)
        result['command_timeout_ms']=self.command_timeout_ms
        if self.network:result['network_policy']=self.network
        if self.layers:
            # Proof is checked before untrusted execution starts. Inner tasks
            # cannot access the supervisor, pmem devices or backing mount paths.
            result.update(self.layer_proof)
        return result

    def dispatch(self, value):
        if not isinstance(value, dict) or value.get('operation') not in ('status', 'execute', 'query'):
            raise ValueError('Unknown guest operation')
        operation = value['operation']
        # Inspection never queues behind a command or FnCall cleanup. Atomic
        # journal replacement exposes either the old intent or a complete proof.
        if operation == 'status':
            if set(value) != {'operation'}:raise ValueError('Unexpected status arguments')
            return self.status()
        rid = value.get('request_id')
        if not isinstance(rid, str) or not re.fullmatch('[0-9a-f]{32}', rid):
            raise ValueError('Request ID required')
        path = self.root / (rid+'.json')
        if operation == 'query':
            if set(value) != {'operation','request_id'}:raise ValueError('Unexpected query arguments')
            return json.loads(path.read_text()) if path.exists() else dict(state='NOT_FOUND',request_id=rid)
        with self.lock:
            self.status()
            if (set(value) != {'operation','request_id','command','timeout_ms','output_limit'} or
                    not isinstance(value['command'], str) or len(value['command'].encode()) > 65536 or
                    type(value['timeout_ms']) is not int or not 1 <= value['timeout_ms'] <= self.command_timeout_ms or
                    type(value['output_limit']) is not int or not 1 <= value['output_limit'] <= 262144):
                raise ValueError('Invalid bounded invocation')
            digest = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
            if path.exists():
                record = json.loads(path.read_text())
                if record['digest'] != digest:
                    raise ValueError('Request ID conflict')
                return record  # PENDING/UNKNOWN never replay untrusted work.
            record = dict(request_id=rid, digest=digest, state='UNKNOWN')
            save(path, record)  # Durable intent before Docker exec or cleanup.
            try:
                result = json.loads(self.run(['exec', self.name, 'python3', '-B', '/dsec-helper.py',
                    'shell', json.dumps({key:value[key] for key in ('command','timeout_ms','output_limit')})],
                    timeout=value['timeout_ms']/1000+5))
                if self.config['mode'] == 'fncall':
                    self.save_state('CLEANING')
                    self.run(['restart', '--time', '1', self.name], timeout=10)
                    self.status()  # Same container ID; tmpfs and processes replaced.
                    self.save_state('READY')
                record.update(state='DONE', response=dict(ok=True, result=result))
                save(path, record)
                return record
            except BaseException:
                # A possibly dirty function slot is never made available again.
                self.failed = True; self.save_state('FAILED')
                raise


def serve_connection(supervisor, sock, address, auth_key, *, native=False):
    if address[0] != socket.VMADDR_CID_HOST:
        return
    # Authentication must precede JSON parsing, dispatch, and connecting to the
    # inner native agent. A failed handshake never reaches an execution path.
    guest_auth.authenticate_server(sock, auth_key, 5001 if native else PORT)
    idle=max(40,supervisor.command_timeout_ms/1000+12) if native else 40
    sock.settimeout(idle)
    if native:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as target:
            target.connect('/run/dsec-native/agent.sock')
            with selectors.DefaultSelector() as selector:
                selector.register(sock, selectors.EVENT_READ, target)
                selector.register(target, selectors.EVENT_READ, sock)
                while True:
                    events = selector.select(idle)
                    if not events:
                        return
                    for key, _ in events:
                        data = key.fileobj.recv(65536)
                        if not data:
                            return
                        key.data.sendall(data)
    else:
        try:
            send(sock, dict(ok=True, result=supervisor.dispatch(receive(sock))))
        except Exception as exc:
            send(sock, dict(ok=False, error=type(exc).__name__, message=str(exc)[:1000]))


def serve(supervisor, auth_key):
    supervisor.start()
    slots = threading.BoundedSemaphore(32)
    def connection(sock, address, *, native=False):
        try:
            serve_connection(supervisor, sock, address, auth_key, native=native)
        except (OSError, EOFError, ValueError):
            pass  # Unauthenticated/invalid connections have no guest effects.
        finally:
            sock.close(); slots.release()
    def listener(port, native):
        with socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM) as server:
            server.bind((socket.VMADDR_CID_ANY, port)); server.listen(16)
            while True:
                sock, address = server.accept()
                if not slots.acquire(blocking=False):
                    sock.close(); continue
                threading.Thread(target=connection, args=(sock,address), kwargs=dict(native=native), daemon=True).start()
    if supervisor.config['mode'] == 'container':
        threading.Thread(target=listener, args=(5001,True), daemon=True).start()
    listener(PORT, False)


if __name__ == '__main__':
    import sys
    if sys.argv[1:2] == ['shell']:
        print(json.dumps(bounded_shell(**json.loads(sys.argv[2]))))
    elif sys.argv[1:] == ['serve']:
        import guest_auth
        key = guest_auth.bootstrap_key()
        serve(Supervisor(json.loads(Path('/opt/dsec/config.json').read_text())), key)
    else:
        raise SystemExit('Expected shell or serve')
