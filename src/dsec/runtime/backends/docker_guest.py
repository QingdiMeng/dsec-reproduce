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


def bounded_shell(command, timeout_ms, output_limit):
    """Inner-container helper: drain output without buffering an unbounded pipe."""
    process = subprocess.Popen(['/bin/sh', '-lc', command], cwd='/work',
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
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
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL); process.wait(timeout=2)


class Supervisor:
    def __init__(self, config, root='/var/lib/dsec-control', *, run=None):
        self.config = config
        if (set(config) not in ({'mode', 'image', 'memory_mb', 'cpus'},
                               {'mode', 'image', 'memory_mb', 'cpus', 'network'}) or
                config['mode'] not in ('container', 'fncall') or
                not re.fullmatch('sha256:[0-9a-f]{64}', config['image']) or
                type(config['memory_mb']) is not int or config['memory_mb'] < 128 or
                type(config['cpus']) not in (int, float) or not 0 < config['cpus'] <= 32):
            raise ValueError('Invalid pinned guest configuration')
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
            '--mount', 'type=bind,src=/opt/dsec/docker_guest.py,dst=/dsec-helper.py,readonly',
            '--tmpfs', '/tmp:rw,nosuid,nodev,size=64m,mode=1777']
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
        argv += ['--entrypoint', '/bin/sh', self.config['image'], '-c', 'exec sleep infinity']
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
        if self.network:result['network_policy']=self.network
        return result

    def dispatch(self, value):
        if not isinstance(value, dict) or value.get('operation') not in ('status', 'execute', 'query'):
            raise ValueError('Unknown guest operation')
        operation = value['operation']
        with self.lock:
            self.status()
            if operation == 'status':
                if set(value) != {'operation'}:
                    raise ValueError('Unexpected status arguments')
                return self.status()
            rid = value.get('request_id')
            if not isinstance(rid, str) or not re.fullmatch('[0-9a-f]{32}', rid):
                raise ValueError('Request ID required')
            path = self.root / (rid+'.json')
            if operation == 'query':
                if set(value) != {'operation', 'request_id'}:
                    raise ValueError('Unexpected query arguments')
                return json.loads(path.read_text()) if path.exists() else dict(state='NOT_FOUND', request_id=rid)
            if (set(value) != {'operation','request_id','command','timeout_ms','output_limit'} or
                    not isinstance(value['command'], str) or len(value['command'].encode()) > 65536 or
                    type(value['timeout_ms']) is not int or not 1 <= value['timeout_ms'] <= 30000 or
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
    sock.settimeout(40)
    if native:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as target:
            target.connect('/run/dsec-native/agent.sock')
            with selectors.DefaultSelector() as selector:
                selector.register(sock, selectors.EVENT_READ, target)
                selector.register(target, selectors.EVENT_READ, sock)
                while True:
                    events = selector.select(40)
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
