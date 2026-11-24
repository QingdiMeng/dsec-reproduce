"""Synchronous runtime facade over one API-owned DSec sandbox and async SDK.

Neither the SDK loop nor task execution falls back to a controller shell or
filesystem. The tool companion lives inside the sandbox; HTTP is tunneled using
the existing native session API, not a host-accessible guest port.
"""
import asyncio
from concurrent.futures import TimeoutError as FutureTimeout, wait
import json
from pathlib import Path
import shlex
import threading
import time
import uuid

from dsec._credentials import secret_file
from dsec.sdk import DSecClient, DSecContainerRunArgs
from . import guest


class Portal:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.lock = threading.Lock()
        self.pending = set()
        self.thread.start()

    def submit(self, coroutine):
        future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        with self.lock:
            self.pending.add(future)
        def settled(done):
            with self.lock:
                self.pending.discard(done)
        future.add_done_callback(settled)
        return future

    @staticmethod
    def result(future, timeout=180):
        try:
            return future.result(timeout)
        except FutureTimeout as exc:
            # Detaching the caller does not revoke an admitted SDK mutation.
            raise TimeoutError('DSec call outcome not settled; do not replay') from exc

    def call(self, coroutine, timeout=180):
        return self.result(self.submit(coroutine), timeout)

    def drain(self, timeout):
        with self.lock:
            pending = tuple(self.pending)
        if pending and wait(pending, timeout=timeout).not_done:
            raise TimeoutError('Admitted DSec work has not settled; retirement remains unknown')

    def has_pending(self):
        with self.lock:
            return any(not future.done() for future in self.pending)

    def close(self):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=5)
        if not self.thread.is_alive():
            self.loop.close()


class APISandbox:
    companion = guest
    preflight = 'import ipykernel, pexpect, openhands_aci.editor'
    initialize_python = True
    response_limit = 65536
    separate_interrupt_session = False
    def __init__(self, settings):
        self.settings = dict(settings)
        self.cwd = settings.get('cwd', '/workspace')
        self.timeout_ms = settings.get('command_timeout_ms', 30000)
        self.transport_timeout_ms = settings['transport_timeout_ms']
        from dsec.contracts.execution import command_timeout
        command_timeout(self.timeout_ms)
        command_timeout(self.transport_timeout_ms)
        if self.transport_timeout_ms < max(35000, self.timeout_ms + 10000):
            raise ValueError('Published HTTP transport budget must allow tool startup/interrupt to settle')
        self.portal = Portal()
        self.client = self.box = self.session = None
        self.interrupt_session = None
        self.cleanup = 'not_created'
        self.operations = []
        self.startup_evidence = {}
        self.epoch = None
        self._closed = False
        self._closing = False
        self.state_lock = threading.Lock()
        self.close_lock = threading.Lock()

    def _call(self, coroutine, timeout=180):
        with self.state_lock:
            if self._closing:
                coroutine.close()
                raise RuntimeError('OpenHands sandbox is closing; no new work admitted')
            future = self.portal.submit(coroutine)
        return self.portal.result(future, timeout)

    @property
    def id(self):
        return self.box.id if self.box else None

    def connect(self):
        try:
            return self._call(self._connect(), timeout=420 + 3 * self.timeout_ms / 1000)
        except Exception as exc:
            from dsec.contracts.rpc import ServiceError
            if isinstance(exc, ServiceError) and exc.kind == 'ExecutionCreateFailed' and exc.details.get('cleanup_complete'):
                self.cleanup = 'not_created_proved'
            raise

    async def _connect(self):
        config = json.loads(Path(self.settings['api_config']).read_text())
        self.client = DSecClient(**config['client'], token=secret_file(config['token_file']))
        await self.client.open()
        self.cleanup = 'creation_pending'
        identity = uuid.uuid4().hex
        self.operations.append(dict(kind='create', request_id=identity))
        self.box = await self.client.run_container(DSecContainerRunArgs(**self.settings['spec']),
            resource_demand=self.settings['resource_demand'], request_id=identity)
        self.cleanup = 'pending'
        self.session = await self.box.open_session()
        if self.separate_interrupt_session:
            self.interrupt_session = await self.box.open_session()
        directory = '/tmp/dsec-openhands-' + uuid.uuid4().hex
        python = self.settings.get('tool_python', '/opt/dsec-openhands/python/bin/python')
        library = self.settings.get('tool_pythonpath', '/opt/dsec-openhands/site-packages')
        environment = dict(PYTHONPATH=library, PYTHONDONTWRITEBYTECODE='1',
            LITELLM_LOCAL_MODEL_COST_MAP='True', OPENHANDS_SUPPRESS_BANNER='1', DO_NOT_TRACK='1',
            PYTHONHOME=self.settings.get('tool_pythonhome', '/opt/dsec-openhands/python'),
            LD_LIBRARY_PATH=self.settings.get('tool_library_path', '/opt/dsec-openhands/python/lib'))
        prefix = ' '.join(key + '=' + shlex.quote(value) for key, value in environment.items()) + ' ' + shlex.quote(python)
        result = await self.box.run_shell('mkdir -m 700 ' + shlex.quote(directory) + ' && ' + prefix +
            ' -c ' + shlex.quote(self.preflight),
            timeout_ms=self.timeout_ms)
        self.startup_evidence['preflight'] = result
        if result['exit_code'] or result['timed_out'] or result['truncated']:
            raise RuntimeError('Published OpenHands guest tools are missing: ' + result['output'])
        await self.box.write_file(directory + '/dsec_guest_protocol.py', Path(guest.__file__).read_bytes())
        await self.box.write_file(directory + '/guest.py', Path(self.companion.__file__).read_bytes())
        port = 58731
        config = dict(cwd=self.cwd, shell_init=self.settings.get('shell_init', ''),
                      timeout_seconds=self.timeout_ms / 1000, port=port)
        await self.box.write_file(directory + '/config.json', json.dumps(config).encode())
        command = prefix + ' -u ' + shlex.quote(directory + '/guest.py') + ' --config ' + shlex.quote(directory + '/config.json')
        result = await self.box.run_shell(command + ' >' + shlex.quote(directory + '/server.log') + ' 2>&1 </dev/null &',
                                          timeout_ms=self.timeout_ms)
        self.startup_evidence['launch'] = result
        if result['exit_code'] or result['timed_out']:
            raise RuntimeError('OpenHands guest companion did not start')
        self.url = f'http://127.0.0.1:{port}'
        # PTY framing and an environment activation can each take time during
        # cold page-in. Health polls are read-only; initialization is not replayed.
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                response = await self.session.request('GET', self.url + '/health', timeout_ms=3000)
                self.startup_evidence['last_health'] = {key: response[key] for key in
                    ('status_code', 'timed_out', 'truncated', 'headers') if key in response}
                if response['status_code'] == 200 and not response['timed_out'] and not response['truncated']:
                    value = json.loads(response['body'])
                    self.epoch = value['epoch']
                    self.capabilities = value['capabilities']
                    self.cwd = value['cwd']
                    break
            except Exception as exc:
                self.startup_evidence['last_health_error'] = dict(type=type(exc).__name__, message=str(exc))
                from dsec.contracts.errors import RequestOutcomeUnknown
                from dsec.contracts.sandbox import UnsupportedCapability
                if isinstance(exc, (RequestOutcomeUnknown, UnsupportedCapability)):
                    raise
                from dsec.contracts.rpc import ServiceError
                if not (isinstance(exc, OSError) or isinstance(exc, ServiceError) and
                        exc.kind in ('OSError', 'ConnectionError', 'TimeoutError', 'ServiceBusy')):
                    raise
            await asyncio.sleep(min(.25, max(0, deadline - time.monotonic())))
        else:
            log = (await self.box.read_file(directory + '/server.log', max_bytes=65536)).decode(errors='replace')
            self.startup_evidence['server_log'] = log[-8000:]
            self.startup_evidence['processes'] = await self.box.run_shell('ps -eo pid,ppid,args',
                timeout_ms=self.timeout_ms, output_limit=12000)
            raise RuntimeError('Guest tools did not become ready: ' + json.dumps(self.startup_evidence))
        # This is a single mutation, outside the read-only health polling loop.
        # Missing tools or an uncertain kernel setup must never be retried.
        if self.initialize_python:
            await self._action(dict(id=uuid.uuid4().hex, epoch=self.epoch,
                kind='python', code='pass', timeout_seconds=self.timeout_ms / 1000))
        self.guest_description = value
        return value

    def action(self, kind, *, request_id=None, timeout_seconds=None, **arguments):
        identity = request_id or uuid.uuid4().hex
        seconds = self.timeout_ms / 1000 if timeout_seconds is None else timeout_seconds
        if type(seconds) not in (int, float) or not 0 < seconds <= self.timeout_ms / 1000:
            raise ValueError('OpenHands action timeout exceeds the configured tool budget')
        request = dict(id=identity, epoch=self.epoch, kind=kind,
                       timeout_seconds=seconds, **arguments)
        record = dict(kind=kind, request_id=identity, started=time.time())
        self.operations.append(record)
        value = self._call(self._action(request), timeout=self.transport_timeout_ms / 1000 + 30)
        self.cwd = value['cwd']
        record.update(completed=time.time())
        return value['result']

    async def _action(self, request):
        body = json.dumps(request).encode()
        if len(body) > 60000:
            raise ValueError('OpenHands action exceeds guest request budget')
        route = '/interrupt' if request['kind'] == 'interrupt' else '/action'
        session = self.interrupt_session if request['kind'] == 'interrupt' and self.interrupt_session else self.session
        response = await session.request('POST', self.url + route,
            headers={'Content-Type': 'application/json'}, body=body,
            timeout_ms=self.transport_timeout_ms, response_limit=self.response_limit, request_id=request['id'])
        if response['timed_out'] or response['truncated']:
            from dsec.contracts.errors import RequestOutcomeUnknown
            raise RequestOutcomeUnknown('OpenHands tool HTTP result incomplete; do not replay', request['id'])
        value = json.loads(response['body'])
        if response['status_code'] != 200 or not value.get('ok'):
            raise RuntimeError('Guest tool action did not settle: ' + str(value.get('error')))
        return value

    def read_file(self, path, max_bytes=8 * 1024**2):
        return self._call(self.box.read_file(path, max_bytes=max_bytes))

    def run_shell(self, command):
        """Harness-owned, non-PTY command in the same sandbox, never on host."""
        identity = uuid.uuid4().hex
        self.operations.append(dict(kind='harness_command', request_id=identity))
        return self._call(self.box.run_shell(command, timeout_ms=self.timeout_ms, request_id=identity))

    def write_file(self, path, data, *, mode=0o600, request_id=None):
        return self._call(self.box.write_file(path, data, mode=mode, request_id=request_id))

    def close(self):
        with self.close_lock:
            if self._closed:
                return
            with self.state_lock:
                self._closing = True
            try:
                # Cancellation of the upstream awaiter does not cancel its
                # admitted native request. Join it before asking for retirement.
                self.portal.drain(self.transport_timeout_ms / 1000 + 30)
                self.portal.call(self._close(), timeout=120)
            except BaseException:
                self.cleanup = 'unknown'
                self._closed = True  # UNKNOWN is sticky; an atexit close must not replay it.
                if not self.portal.has_pending():
                    self.portal.close()
                raise
            else:
                self._closed = True
                self.portal.close()

    async def _close(self):
        try:
            if self.box is not None:
                identity = uuid.uuid4().hex
                self.operations.append(dict(kind='stop', request_id=identity))
                self.cleanup = 'unknown'
                deadline = time.monotonic() + 60
                while True:
                    try:
                        value = await self.box.stop(request_id=identity)
                        break
                    except Exception as exc:
                        from dsec.contracts.rpc import ServiceError
                        if not isinstance(exc, ServiceError) or exc.kind != 'ServiceBusy' or time.monotonic() >= deadline:
                            raise
                        # Only explicit non-admission permits resubmission of
                        # the same stop identity; unknown outcomes never do.
                        await asyncio.sleep(.1)
                if value.get('state') != 'STOPPED' or not value.get('resource_cleanup_complete'):
                    self.cleanup = 'unknown'
                    raise RuntimeError('Sandbox retirement not proved')
                self.cleanup = 'stopped'
        finally:
            if self.client is not None:
                await self.client.close()
