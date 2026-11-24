"""Guest-only coding tools, reached through DSec's native HTTP capability.

This file is copied into the sandbox, never used to execute agent code on the
controller. IPython and the unmodified upstream ACI own Python/editor behavior.
The PTY preserves shell state, soft polling and actual foreground stdin/SIGINT.
"""
import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import queue
import re
import shlex
import signal
import threading
import time
import uuid

OUTPUT_CHARS = 12000


def terminal_signals():
    # A shell background job starts with SIGINT/SIGQUIT ignored. Reset them in
    # the PTY child before exec so its foreground jobs remain interruptible.
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    signal.signal(signal.SIGQUIT, signal.SIG_DFL)


class Shell:
    def __init__(self, cwd, initialize=''):
        import pexpect
        self.marker = '__DSEC_' + uuid.uuid4().hex + '__'
        self.pattern = re.compile(re.escape(self.marker) + r'(\d+):(.*?)' + re.escape(self.marker), re.S)
        environment = dict(os.environ)
        # The companion's portable Python is a tool implementation detail.
        # Task commands keep their prepared interpreter and dependencies.
        for key in ('PYTHONHOME', 'PYTHONPATH', 'LD_LIBRARY_PATH'):
            environment.pop(key, None)
        self.process = pexpect.spawn('/bin/bash', ['--noprofile', '--norc'], cwd=cwd,
                                     env=environment, encoding='utf-8', codec_errors='replace', echo=False,
                                     preexec_fn=terminal_signals)
        self.process.delaybeforesend = 0
        self.buffer = ''
        self.pending = True
        self.cwd = cwd
        prompt = self.marker + '${?}:${PWD}' + self.marker
        self.process.sendline('umask 002; unset PROMPT_COMMAND; PS1=' + shlex.quote(prompt) + "; PS2=''")
        result = self._read(10)
        if result['exit_code'] != 0:
            raise RuntimeError('Could not initialize PTY')
        if initialize:
            result = self.run(initialize, True, 30)
            if result['exit_code'] != 0:
                raise RuntimeError('Shell environment initialization failed: ' + result['content'])
            # Conda/virtualenv activation may decorate PS1. Restore our framing
            # after activation so prompt text cannot become command feedback.
            self.pending = True
            self.process.sendline('unset PROMPT_COMMAND; PS1=' + shlex.quote(prompt) + "; PS2=''")
            if self._read(10)['exit_code'] != 0:
                raise RuntimeError('Could not restore PTY framing after initialization')

    def _read(self, seconds):
        import pexpect
        deadline = time.monotonic() + seconds
        truncated = False
        while True:
            match = self.pattern.search(self.buffer)
            if match:
                content = self.buffer[:match.start()]
                self.buffer = self.buffer[match.end():]
                self.cwd = match[2]
                self.pending = False
                return dict(content=content, exit_code=int(match[1]), cwd=self.cwd, truncated=truncated)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                # Hold a possible prompt prefix across polls; return each byte
                # only once, even if the marker is split between PTY reads.
                keep = min(len(self.buffer), len(self.marker) - 1)
                content = self.buffer[:-keep] if keep else self.buffer
                self.buffer = self.buffer[-keep:] if keep else ''
                return dict(content=content, exit_code=-1, cwd=self.cwd, truncated=truncated)
            try:
                self.buffer += self.process.read_nonblocking(8192, timeout=min(.1, remaining))
            except pexpect.TIMEOUT:
                continue
            except pexpect.EOF as exc:
                raise RuntimeError('Persistent shell exited; state was not recreated') from exc
            if len(self.buffer) > OUTPUT_CHARS:
                self.buffer = self.buffer[:OUTPUT_CHARS // 2] + '\n[output truncated]\n' + self.buffer[-OUTPUT_CHARS // 2:]
                truncated = True

    def run(self, command, blocking, timeout):
        if command.lower() == 'ctrl+c':
            if not self.pending:
                return dict(content='No foreground command is running.', exit_code=0, cwd=self.cwd, truncated=False)
            self.process.sendintr()
            result = self._read(min(timeout, 5))
        elif self.pending:
            if command:
                self.process.sendline(command)
            result = self._read(timeout if blocking else min(timeout, 5))
        elif not command:
            return dict(content='No foreground command is running.', exit_code=0, cwd=self.cwd, truncated=False)
        else:
            self.pending = True
            # Submit one interactive shell command even for multiline scripts.
            # Otherwise every top-level line emits PS1 and later calls consume
            # stale completion frames. eval keeps cwd/env/aliases in this shell;
            # stdin sent to an already-running foreground process stays raw.
            self.process.sendline('eval -- ' + shlex.quote(command))
            result = self._read(timeout if blocking else min(timeout, 5))
        if blocking and result['exit_code'] == -1:
            self.process.sendintr()
            interrupted = self._read(5)
            result.update(content=result['content'] + interrupted['content'],
                          exit_code=interrupted['exit_code'], timed_out=True,
                          truncated=result['truncated'] or interrupted['truncated'])
        return result

    def close(self):
        self.process.close(force=True)


class PythonKernel:
    def __init__(self, cwd):
        from jupyter_client import KernelManager
        import sys
        self.manager = KernelManager(kernel_name='python3')
        self.manager.kernel_spec.argv = [sys.executable, '-m', 'ipykernel_launcher', '-f', '{connection_file}']
        self.manager.start_kernel(cwd=cwd)
        self.client = self.manager.client()
        self.client.start_channels()
        self.client.wait_for_ready(timeout=30)
        value = self.run('from openhands_aci.editor import file_editor', cwd, 30)
        if value['error']:
            raise RuntimeError('Could not initialize upstream file editor: ' + value['content'])

    def run(self, code, cwd, timeout):
        identity = self.client.execute('import os; os.chdir(' + repr(cwd) + ')\n' + code, store_history=True)
        deadline = time.monotonic() + timeout
        output = ''
        error = timed_out = truncated = False
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if timed_out:
                    raise RuntimeError('IPython did not settle after interrupt; outcome unknown')
                self.manager.interrupt_kernel()
                timed_out = True
                deadline = time.monotonic() + 5
                continue
            try:
                message = self.client.get_iopub_msg(timeout=min(.2, remaining))
            except queue.Empty:
                continue
            if message['parent_header'].get('msg_id') != identity:
                continue
            kind, content = message['msg_type'], message['content']
            if kind == 'status' and content['execution_state'] == 'idle':
                return dict(content=output, error=error, timed_out=timed_out, truncated=truncated)
            if kind == 'stream':
                output += content['text']
            elif kind in ('execute_result', 'display_data'):
                output += content['data'].get('text/plain', '') + '\n'
            elif kind == 'error':
                error = True
                output += '\n'.join(content.get('traceback', [])) + '\n'
            if len(output) > OUTPUT_CHARS:
                output = output[:OUTPUT_CHARS] + '\n[output truncated]\n'
                truncated = True

    def close(self):
        self.client.stop_channels()
        self.manager.shutdown_kernel(now=True)


class Tools:
    capabilities = ['persistent-bash', 'foreground-stdin', 'poll', 'interrupt', 'ipython', 'upstream-aci-editor']
    def __init__(self, config):
        self.shell = Shell(config['cwd'], config.get('shell_init', ''))
        self.python = None
        self.epoch = uuid.uuid4().hex
        self.ceiling = config['timeout_seconds']
        self.lock = threading.Lock()
        self.results = {}

    def execute(self, request):
        identity = request['id']
        if not re.fullmatch('[0-9a-f]{32}', identity) or request['epoch'] != self.epoch:
            raise ValueError('Request identity or tool-process epoch differs; do not replay')
        digest = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
        with self.lock:
            if identity in self.results:
                previous, result = self.results[identity]
                if digest != previous:
                    raise ValueError('Request ID reused with different arguments')
                return result
            if len(self.results) >= 1000:
                raise ValueError('Tool request limit reached')
            seconds = request.get('timeout_seconds', self.ceiling)
            if type(seconds) not in (int, float) or not 0 < seconds <= self.ceiling:
                raise ValueError('Tool timeout exceeds the published ceiling')
            kind = request['kind']
            if kind not in self.kinds:
                raise ValueError('Unsupported tool capability: ' + kind)
            # A failed execution is retained as UNKNOWN for this ID. Starting a
            # new tool process also changes its epoch; neither permits replay.
            self.results[identity] = (digest, dict(ok=False, error='Prior execution did not settle; outcome unknown'))
            value = self.dispatch(kind, request, seconds)
            result = dict(ok=True, result=value, cwd=self.shell.cwd)
            self.results[identity] = (digest, result)
            return result

    kinds = ('bash', 'python', 'list', 'stat', 'mkdir')

    def dispatch(self, kind, request, seconds):
        if kind == 'bash':
            value = self.shell.run(request['command'], request.get('blocking', True), seconds)
        elif kind == 'python':
            if self.python is None:
                self.python = PythonKernel(self.shell.cwd)
            value = self.python.run(request['code'], self.shell.cwd, seconds)
        elif kind == 'list':
            path = request.get('path') or self.shell.cwd
            value = dict(files=[os.path.join(path, name) for name in sorted(os.listdir(path))])
        elif kind == 'stat':
            path = request['path']
            try:
                info = os.stat(path)
                value = dict(exists=True, directory=os.path.isdir(path), mode=info.st_mode & 0o777)
            except FileNotFoundError:
                value = dict(exists=False)
        elif kind == 'mkdir':
            os.makedirs(request['path'], exist_ok=True)
            value = dict(created=True)
        else:
            raise ValueError('Unsupported tool capability: ' + kind)
        return value

    def health(self):
        return dict(epoch=self.epoch, cwd=self.shell.cwd, capabilities=self.capabilities)

    def close(self):
        if self.python:
            self.python.close()
        self.shell.close()


def serve(config, tools_factory=Tools):
    tools = tools_factory(config)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def respond(self, status, result):
            body = json.dumps(result).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path != '/health':
                self.respond(404, dict(ok=False, error='Unknown route'))
                return
            self.respond(200, tools.health())

        def do_POST(self):
            try:
                size = int(self.headers['Content-Length'])
                if not 0 < size <= 60000:
                    raise ValueError('Request exceeds tool budget')
                request = json.loads(self.rfile.read(size))
                if self.path == '/action':
                    value = tools.execute(request)
                elif self.path == '/interrupt' and hasattr(tools, 'interrupt'):
                    value = tools.interrupt(request)
                else:
                    raise ValueError('Unknown route')
                self.respond(200, value)
            except Exception as exc:
                self.respond(409, dict(ok=False, error=str(exc)))

    server = ThreadingHTTPServer(('127.0.0.1', config['port']), Handler)
    try:
        server.serve_forever()
    finally:
        tools.close()
        server.server_close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    with open(args.config) as stream:
        serve(json.load(stream))
