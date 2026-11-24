"""Pinned OpenHands Runtime implementation, independent of task and policy."""
import asyncio
import posixpath
import tempfile
import uuid
from pathlib import Path

from openhands.runtime.base import Runtime
from openhands.runtime.utils.files import insert_lines, read_lines
from openhands.events.observation import (CmdOutputObservation, ErrorObservation,
    FileReadObservation, FileWriteObservation, IPythonRunCellObservation)
from .transport import APISandbox


class DSecRuntime(Runtime):
    def __init__(self, *args, dsec_config, sandbox_factory=APISandbox, **kwargs):
        super().__init__(*args, **kwargs)
        self.backend = sandbox_factory(dsec_config)
        self._runtime_initialized = False

    async def connect(self):
        await asyncio.to_thread(self.backend.connect)
        await asyncio.to_thread(self.setup_initial_env)
        await asyncio.to_thread(self._initialize_upstream_shell)
        self._runtime_initialized = True

    def _initialize_upstream_shell(self):
        from openhands.events.action import CmdRunAction
        from openhands.runtime.action_execution_server import INIT_COMMANDS
        # Retain the pinned runtime's Git identity and --no-pager alias. A PTY
        # without this initialization can turn source inspection into a pager.
        for command in INIT_COMMANDS:
            action = CmdRunAction(command=command)
            action.timeout = self.backend.timeout_ms / 1000
            observation = self.run(action)
            if observation.exit_code != 0:
                raise RuntimeError('Upstream shell initialization failed: ' + observation.content)

    @property
    def runtime_initialized(self):
        return self._runtime_initialized

    def close(self):
        if hasattr(self, 'backend'):
            self.backend.close()
        self._runtime_initialized = False

    def run(self, action):
        value = self.backend.action('bash', command=action.command,
            blocking=action.blocking, timeout_seconds=action.timeout, request_id=self._identity(action))
        text = value['content']
        if value.get('timed_out'):
            text += '\n[Command timed out; SIGINT sent to the foreground process.]'
        if value.get('truncated'):
            text += '\n[Command output capture truncated.]'
        return CmdOutputObservation(command_id=action.id, command=action.command,
            exit_code=value['exit_code'], content=text, hidden=action.hidden)

    def run_ipython(self, action):
        value = self.backend.action('python', code=action.code, timeout_seconds=action.timeout,
                                    request_id=self._identity(action))
        text = value['content']
        if value.get('timed_out'):
            text += '\n[IPython execution timed out; kernel interrupted.]'
        if value.get('truncated'):
            text += '\n[IPython output capture truncated.]'
        return IPythonRunCellObservation(code=action.code, content=text)

    def _path(self, path):
        return posixpath.normpath(posixpath.join(self.backend.cwd, path))

    def _identity(self, action, phase='execute'):
        if action.id < 0:
            return uuid.uuid4().hex
        return uuid.uuid5(uuid.NAMESPACE_URL,
            f'dsec-openhands:{self.sid}:{action.id}:{phase}').hex

    def read(self, action):
        path = self._path(action.path)
        info = self.backend.action('stat', path=path, request_id=self._identity(action, 'stat'))
        if not info['exists']:
            return ErrorObservation('File not found: ' + action.path)
        if info['directory']:
            return ErrorObservation('Path is a directory: ' + action.path)
        try:
            data = self.backend.read_file(path).decode('utf-8')
        except UnicodeDecodeError:
            return ErrorObservation('File could not be decoded as utf-8: ' + action.path)
        return FileReadObservation(path=action.path,
            content=''.join(read_lines(data.splitlines(keepends=True), action.start, action.end)))

    def write(self, action):
        path = self._path(action.path)
        info = self.backend.action('stat', path=path, request_id=self._identity(action, 'stat'))
        if info.get('directory'):
            return ErrorObservation('Path is a directory: ' + action.path)
        if not info['exists'] or (action.start == 0 and action.end == -1):
            data = '\n'.join(action.content.split('\n')) + '\n'
        else:
            original = self.backend.read_file(path).decode('utf-8').splitlines(keepends=True)
            data = ''.join(insert_lines(action.content.split('\n'), original, action.start, action.end))
        self.backend.action('mkdir', path=posixpath.dirname(path), request_id=self._identity(action, 'parent'))
        self.backend.write_file(path, data.encode(), mode=info.get('mode', 0o644),
                                request_id=self._identity(action, 'commit'))
        return FileWriteObservation(path=action.path, content='')

    def browse(self, action):
        return ErrorObservation('Browser capability is not published by this DSec coding profile.')

    def browse_interactive(self, action):
        return self.browse(action)

    def copy_to(self, host_src, sandbox_dest, recursive=False):
        if recursive or not Path(host_src).is_file():
            raise ValueError('This adapter supports bounded file upload; recursive archive import is not advertised')
        self.backend.write_file(self._path(sandbox_dest), Path(host_src).read_bytes())

    def list_files(self, path=None):
        return self.backend.action('list', path=self._path(path) if path else self.backend.cwd)['files']

    def copy_from(self, path):
        data = self.backend.read_file(self._path(path))
        with tempfile.NamedTemporaryFile(prefix='dsec-openhands-', delete=False) as stream:
            stream.write(data)
            return Path(stream.name)

    @property
    def vscode_url(self):
        return None
