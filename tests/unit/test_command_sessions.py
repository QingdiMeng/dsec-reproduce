"""Wire compatibility, no replay after lost replies, and Edge execution policy."""
import json
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from dsec.contracts.errors import CommandOutcomeUnknown, RequestOutcomeUnknown, SandboxError
from dsec.contracts.execution import ShellRequest
from dsec.runtime.backends.container import LayeredContainer, ContainerBackendError
from dsec.runtime.backends.firecracker import MicroVM
from dsec.runtime.sessions.channel import DockerCommandChannel, VsockCommandChannel
from dsec.runtime.sessions.dispatcher import ShellDispatcher


class GuestWireTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='dsec-shell-')
        self.addCleanup(self.temp.cleanup)
        self.endpoint = Path(self.temp.name)/'guest.sock'
        self.requests, self.errors, self.accepts = [], [], 0

    def guest(self, response, *, handshake=b'OK 5000\n', count=1):
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(self.endpoint)); listener.listen(); listener.settimeout(2)
        def serve():
            try:
                for _ in range(count):
                    conn, _ = listener.accept()
                    self.accepts += 1
                    with conn:
                        conn.settimeout(2)
                        with conn.makefile('rb') as reader:
                            self.assertEqual(reader.readline(128), b'CONNECT 5000\n')
                            conn.sendall(handshake)
                            if not handshake.startswith(b'OK '):
                                continue
                            timeout, limit, length = map(int, reader.readline(128).split())
                            command = reader.read(length)
                            self.assertEqual(len(command), length)
                            self.requests.append((timeout, limit, command))
                            if response:
                                # The stream may fragment both header and payload.
                                conn.sendall(response[:3]); conn.sendall(response[3:])
            except BaseException as exc:
                self.errors.append(exc)
            finally:
                listener.close()
        thread = threading.Thread(target=serve, daemon=True); thread.start()
        def finish():
            thread.join(3)
            self.assertFalse(thread.is_alive(), 'Guest wire fixture did not finish')
            if self.errors:
                raise self.errors[0]
        self.addCleanup(finish)
        return VsockCommandChannel(self.endpoint, max_timeout_ms=900000)

    def test_existing_utf8_framing_and_timeout_truncation_result(self):
        payload = '输出\n'.encode()
        channel = self.guest(f'124 1 1 {len(payload)}\n'.encode()+payload)
        result = channel.execute(ShellRequest('printf 中文', 900000, 32))
        self.assertEqual(result, {'exit_code':124, 'timed_out':True,
                                 'truncated':True, 'output':'输出\n'})
        self.assertEqual(self.requests, [(900000,32,'printf 中文'.encode())])

    def test_each_command_has_a_fresh_connection(self):
        channel = self.guest(b'0 0 0 2\nok', count=2)
        for command in ('first action', 'second action'):
            self.assertEqual(channel.execute(ShellRequest(command))['output'], 'ok')
        self.assertEqual(self.accepts, 2)
        self.assertEqual([r[2] for r in self.requests], [b'first action',b'second action'])

    def test_disconnect_after_command_is_not_replayed(self):
        channel = self.guest(b'0 0 0 8\npartial')
        with self.assertRaisesRegex(EOFError, 'Incomplete command result'):
            channel.execute(ShellRequest('effect may have committed'))
        self.assertEqual(self.accepts, 1)
        self.assertEqual(len(self.requests), 1)

    def test_lost_result_header_is_unknown_without_retry(self):
        channel = self.guest(b'')
        with self.assertRaisesRegex(EOFError, 'outcome unknown'):
            channel.execute(ShellRequest('write file'))
        self.assertEqual(len(self.requests), 1)

    def test_rejected_vsock_handshake_never_sends_command(self):
        channel = self.guest(b'', handshake=b'ERR\n')
        with self.assertRaisesRegex(EOFError, 'connection rejected'):
            channel.execute(ShellRequest('write file'))
        self.assertEqual(self.requests, [])

    def test_oversized_response_is_rejected(self):
        channel = self.guest(b'0 0 0 33\n')
        with self.assertRaisesRegex(ValueError, 'Invalid response size'):
            channel.execute(ShellRequest('read file', output_limit=32))
        self.assertEqual(len(self.requests), 1)

    def test_invalid_wire_bounds_or_unsupported_guest_journal_do_not_connect(self):
        factory = Mock(side_effect=AssertionError('must not connect'))
        channel = VsockCommandChannel(self.endpoint, socket_factory=factory)
        for request in (ShellRequest('x', timeout_ms=30001),
                        ShellRequest('x', output_limit=0), ShellRequest('x'*65537),
                        ShellRequest('x', request_id='a'*32)):
            with self.assertRaises(ValueError):
                channel.execute(request)
        factory.assert_not_called()

    def test_driver_rejects_nonrunning_state_before_transport(self):
        vm = MicroVM('/unused', Path(self.temp.name))
        with patch('dsec.runtime.backends.firecracker.socket.socket') as factory:
            with self.assertRaisesRegex(RuntimeError, 'STOPPED'):
                vm.execute('write file')
            factory.assert_not_called()


class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        manager = SimpleNamespace(command_timeout_ms=lambda _:1000,
                                  verifier_timeout_ms=lambda _:10000,
                                  egress_proxy_url='configured', egress_proxy_bypass_hosts=('mirror',))
        self.sandbox = SimpleNamespace(manager=manager, environment_id='generic',
            lock=threading.RLock(), reserved=False, state='RUNNING', vm=SimpleNamespace(execute=Mock()))
        self.sandbox._check = lambda: self.events.append('check')
        def restore():
            self.events.append('restore'); self.sandbox.state='RUNNING'
        self.sandbox._restore = restore
        def fail(reason):
            self.events.append(reason); self.sandbox.state='FAILED'
        self.sandbox._fail = fail
        self.sandbox._touch = lambda: self.events.append('touch')
        self.proxy = Mock(side_effect=lambda command,*_: 'prepared: '+command)
        self.dispatcher = ShellDispatcher(proxy_command=self.proxy)

    def test_validation_precedes_resume_and_uses_separate_verifier_budget(self):
        self.sandbox.state='PAUSED'
        for args in ({'timeout_ms':1001}, {'timeout_ms':True}, {'output_limit':False},
                     {'execution_scope':'unknown'}):
            with self.assertRaises(ValueError):
                self.dispatcher.execute(self.sandbox, 'write file', **args)
        self.assertEqual(self.events, [])
        self.sandbox.vm.execute.assert_not_called()
        self.dispatcher.execute(self.sandbox, 'verify', timeout_ms=10000, execution_scope='verifier')
        self.assertEqual(self.events, ['check','restore','touch'])
        self.sandbox.vm.execute.assert_called_once_with('prepared: verify',10000,65536)

    def test_proxy_expansion_overflow_rejected_before_any_vm_effect(self):
        self.proxy.side_effect = lambda *_:'x'*65537
        with self.assertRaisesRegex(ValueError, 'after proxy'):
            self.dispatcher.execute(self.sandbox, 'short command')
        self.assertEqual(self.events, [])
        self.sandbox.vm.execute.assert_not_called()

    def test_reserved_instance_rejects_command_without_touch_or_resume(self):
        self.sandbox.reserved = True
        with self.assertRaisesRegex(SandboxError, 'reserved'):
            self.dispatcher.execute(self.sandbox, 'write file', timeout_ms=500)
        self.assertEqual(self.events, [])
        self.sandbox.vm.execute.assert_not_called()

    def test_transport_failure_marks_unknown_and_touches_once_without_replay(self):
        self.sandbox.vm.execute.side_effect = EOFError('lost reply')
        with self.assertRaises(CommandOutcomeUnknown):
            self.dispatcher.execute(self.sandbox, 'write file', timeout_ms=500)
        self.sandbox.vm.execute.assert_called_once()
        self.assertEqual(self.events, ['check','command_transport_failed','touch'])
        self.assertEqual(self.sandbox.state, 'FAILED')

    def test_command_failure_or_timeout_is_execution_evidence_not_transport_failure(self):
        result={'exit_code':124,'timed_out':True,'truncated':True,'output':'partial stderr'}
        self.sandbox.vm.execute.return_value = result
        self.assertIs(self.dispatcher.execute(self.sandbox,'slow command',timeout_ms=500),result)
        self.assertEqual(self.sandbox.state,'RUNNING')
        self.assertEqual(self.events,['check','touch'])

    def test_two_commands_share_the_existing_sandbox_lock(self):
        started, release, second_attempted = threading.Event(), threading.Event(), threading.Event()
        errors = []
        def execute(command,*_):
            if command.endswith('first'):
                started.set()
                if not release.wait(2):
                    raise AssertionError('blocked command never released')
            return {'exit_code':0,'output':command,'truncated':False}
        self.sandbox.vm.execute.side_effect = execute
        def call(command):
            try:
                if command=='second': second_attempted.set()
                self.dispatcher.execute(self.sandbox,command,timeout_ms=500)
            except BaseException as exc: errors.append(exc)
        first=threading.Thread(target=call,args=('first',)); second=threading.Thread(target=call,args=('second',))
        first.start()
        try:
            self.assertTrue(started.wait(1)); second.start()
            self.assertTrue(second_attempted.wait(1))
            self.assertEqual(self.sandbox.vm.execute.call_count,1)
        finally:
            release.set(); first.join(3)
            if second.ident is not None: second.join(3)
        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertEqual(errors,[])
        self.assertEqual(self.sandbox.vm.execute.call_count,2)


class DockerChannelTests(unittest.TestCase):
    def setUp(self):
        self.run = Mock()
        self.channel = DockerCommandChannel('dsec-private-instance',run=self.run,error=ContainerBackendError)
        self.request = ShellRequest('write file', 12345, 32, 'a'*32)

    def response(self, proof, code=0):
        self.run.return_value=SimpleNamespace(stdout=json.dumps(proof),stderr='',returncode=code)

    def test_journaled_result_preserves_exact_fields_and_existing_agent_arguments(self):
        result={'exit_code':1,'timed_out':False,'truncated':True,'output':'stderr'}
        self.response({'request_id':'a'*32,'state':'DONE','response':{'ok':True,'result':result}})
        self.assertEqual(self.channel.execute(self.request),result)
        self.run.assert_called_once_with(['docker','exec','dsec-private-instance','python3','-B',
            '/dsec-agent.py','request-shell','a'*32,'12345','32','--','write file'],timeout=20.345,check=False)

    def test_journal_pending_unknown_conflict_or_failed_result_never_replays(self):
        for state in ('PENDING','UNKNOWN','CONFLICT','DONE'):
            with self.subTest(state=state):
                self.run.reset_mock()
                self.response({'request_id':'a'*32,'state':state,'response':{'ok':False}})
                with self.assertRaises(ValueError if state=='CONFLICT' else RequestOutcomeUnknown):
                    self.channel.execute(self.request)
                self.assertEqual(self.run.call_count,1)

    def test_docker_timeout_preserves_request_identity_without_retry(self):
        self.run.side_effect=subprocess.TimeoutExpired('docker',20)
        with self.assertRaises(RequestOutcomeUnknown) as caught:
            self.channel.execute(self.request)
        self.assertEqual(caught.exception.request_id,'a'*32)
        self.run.assert_called_once()

    def test_corrupt_or_wrong_identity_reply_is_unknown(self):
        for payload in ('not JSON',json.dumps({'request_id':'b'*32})):
            with self.subTest(payload=payload):
                self.run.reset_mock()
                self.run.return_value=SimpleNamespace(stdout=payload,stderr='transport failed',returncode=0)
                with self.assertRaises(RequestOutcomeUnknown) as caught:
                    self.channel.execute(self.request)
                self.assertEqual(caught.exception.request_id,'a'*32)
                self.run.assert_called_once()

    def test_legacy_result_does_not_invent_timeout_evidence(self):
        self.run.return_value=SimpleNamespace(stdout='abcdef',stderr='',returncode=1)
        result=self.channel.execute(ShellRequest('read file',500,3))
        self.assertEqual(result,{'exit_code':1,'output':'abc','truncated':True})
        self.assertNotIn('timed_out',result)

    def test_legacy_transport_stderr_is_unknown(self):
        self.run.return_value=SimpleNamespace(stdout='',stderr='daemon unreachable',returncode=1)
        with self.assertRaises(RequestOutcomeUnknown):
            self.channel.execute(ShellRequest('write file'))
        self.run.assert_called_once()

    def test_backend_validates_limits_and_status_before_transport(self):
        container=LayeredContainer.__new__(LayeredContainer)
        container.name='dsec-private-instance'; container.status=Mock(return_value={'state':'RUNNING'})
        with patch('dsec.runtime.backends.container._run',self.run):
            for kwargs in ({'timeout_ms':30001},{'output_limit':False}):
                with self.assertRaises(ValueError): container.run_shell('write file',**kwargs)
            container.status.assert_not_called(); self.run.assert_not_called()
            container.status.return_value={'state':'STOPPED'}
            with self.assertRaises(ContainerBackendError): container.run_shell('write file')
            self.run.assert_not_called()

    def test_query_uses_same_pinned_container_and_checks_reply_identity(self):
        self.response({'request_id':'a'*32,'state':'UNKNOWN'})
        self.assertEqual(self.channel.query_request('a'*32)['state'],'UNKNOWN')
        self.run.assert_called_once_with(['docker','exec','dsec-private-instance','python3','-B',
                                         '/dsec-agent.py','request-query','a'*32],check=False)
        self.response({'request_id':'b'*32})
        with self.assertRaisesRegex(ContainerBackendError,'ID mismatch'):
            self.channel.query_request('a'*32)


if __name__ == '__main__':
    unittest.main()
