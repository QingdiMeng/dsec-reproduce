"""Guest companion for unmodified OpenHands SDK 1.58.2 tools.

Only this process constructs real upstream terminal/editor executors. The
controller receives their JSON schemas and observations over native DSec HTTP.
"""
import argparse
import hashlib
from importlib.metadata import version
import json
import os
import re
import threading
from types import SimpleNamespace
import uuid

try:
    from .guest import Tools, serve, terminal_signals
except ImportError:  # Standalone copy published by APISandbox.
    from dsec_guest_protocol import Tools, serve, terminal_signals


class SDKTools(Tools):
    kinds = ('terminal', 'file_editor', 'settle')
    capabilities = ['openhands-sdk-1.58.2', 'persistent-bash', 'foreground-stdin',
                    'poll', 'interrupt', 'upstream-file-editor']

    def __init__(self, config):
        terminal_signals()  # Background launch must not make PTY children ignore SIGINT.
        from openhands.tools.terminal import TerminalTool
        from openhands.tools.terminal.definition import TerminalAction
        from openhands.tools.file_editor import FileEditorTool
        for package in ('openhands-sdk', 'openhands-tools'):
            if version(package) != '1.58.2':
                raise RuntimeError('Published SDK tools version differs: ' + package)
        # Imports have resolved against the portable tool Python. Its launch
        # variables must not change the task's Python or dynamic libraries.
        for key in ('PYTHONHOME', 'PYTHONPATH', 'LD_LIBRARY_PATH'):
            os.environ.pop(key, None)
        self.cwd = config['cwd']
        self.shell = self  # Common journal uses shell.cwd; no second shell.
        self.python = None
        self.epoch = uuid.uuid4().hex
        self.ceiling = config['timeout_seconds']
        self.lock = threading.Lock()
        self.results = {}
        self.interrupt_lock = threading.Lock()
        self.interrupt_results = {}
        state = SimpleNamespace(workspace=SimpleNamespace(working_dir=self.cwd),
            agent=SimpleNamespace(llm=SimpleNamespace(vision_is_active=lambda: False)),
            env_observation_persistence_dir='/tmp/dsec-openhands-output-' + self.epoch)
        self.tools = {}
        try:
            self.tools['terminal'] = TerminalTool.create(state, terminal_type='subprocess',
                no_change_timeout_seconds=max(1, int(self.ceiling)))[0]
            self.tools['file_editor'] = FileEditorTool.create(state)[0]
            if config.get('shell_init'):
                result = self.tools['terminal'].executor(TerminalAction(
                    command=config['shell_init'], timeout=self.ceiling))
                if result.exit_code != 0:
                    raise RuntimeError('Task shell initialization failed: ' + str(result))
        except BaseException:
            self.close()
            raise

    def health(self):
        value = super().health()
        value['tools'] = {name: dict(description=tool.description,
            annotations=tool.annotations.model_dump(mode='json')) for name, tool in self.tools.items()}
        value['tools']['terminal']['description'] += (
            f'\nDSec environment limit: timeout must be <= {self.ceiling:g} seconds per call. '
            'Use an empty command to poll longer-running work after the soft timeout.')
        return value

    def dispatch(self, kind, request, seconds):
        if kind == 'settle':
            from openhands.tools.terminal.definition import TerminalAction
            executor = self.tools['terminal'].executor
            if executor.session.is_running():
                self.interrupt_terminal()
                result = executor(TerminalAction(command='', timeout=min(5, seconds)))
                if result.exit_code == -1:
                    raise RuntimeError('Foreground task did not settle; candidate capture is unsafe')
            return dict(settled=True)
        tool = self.tools[kind]
        action = tool.action_type.model_validate(request['action'])
        if kind == 'terminal':
            if action.reset and action.is_input:
                from openhands.tools.terminal.definition import TerminalObservation
                return TerminalObservation.from_text(
                    text='Cannot use reset=True with is_input=True. No command was executed and the session was not reset.',
                    command=action.command, is_error=True).model_dump(mode='json')
            # Explicit soft timeout; longer operations can be polled. A model's
            # unsupported limit is a known rejection before execution, not an
            # uncertain infrastructure failure or an automatic command retry.
            if action.timeout is not None and action.timeout > seconds:
                from openhands.tools.terminal.definition import TerminalObservation
                return TerminalObservation.from_text(
                    text=f'DSec permits timeout <= {seconds:g} seconds per tool call. '
                         'No command was executed. Use that limit and poll the continuing command with an empty command.',
                    command=action.command, is_error=True).model_dump(mode='json')
            action = action.model_copy(update={'timeout': action.timeout if action.timeout is not None else seconds})
        observation = tool.executor(action)
        if kind == 'terminal' and observation.metadata.working_dir:
            self.cwd = observation.metadata.working_dir
        return observation.model_dump(mode='json')

    def interrupt(self, request):
        # This path deliberately does not take the action lock: an interrupt
        # must reach an executor while its blocking call owns that lock.
        identity = request['id']
        if not re.fullmatch('[0-9a-f]{32}', identity) or request['epoch'] != self.epoch:
            raise ValueError('Interrupt identity/epoch differs')
        digest = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
        with self.interrupt_lock:
            if identity in self.interrupt_results:
                previous, result = self.interrupt_results[identity]
                if digest != previous:
                    raise ValueError('Interrupt ID reused with different arguments')
                return result
            if len(self.interrupt_results) >= 1000:
                raise ValueError('Interrupt limit reached')
            self.interrupt_results[identity] = (digest, dict(ok=False, error='Interrupt outcome unknown'))
            self.interrupt_terminal()
            result = dict(ok=True, result=dict(interrupted=True), cwd=self.cwd)
            self.interrupt_results[identity] = digest, result
            return result

    def interrupt_terminal(self):
        # SDK 1.58.2's subprocess interrupt() signals the shell's process
        # group, which can differ from the current foreground job's group.
        # Send its documented Ctrl-C key through the PTY instead: the kernel
        # delivers SIGINT to the foreground group. Do not enter __call__ here;
        # its session execute loop is already running on the action thread.
        self.tools['terminal'].executor.session.terminal.send_keys('C-c', enter=False)

    def close(self):
        for tool in self.tools.values():
            tool.executor.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    with open(args.config) as stream:
        serve(json.load(stream), SDKTools)
