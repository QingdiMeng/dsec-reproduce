"""Bounded QMP wire and controlled QEMU primitive lifecycle. Not VM acceptance."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import io
import json
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from dsec.contracts.execution import ShellRequest
from dsec.contracts.sandbox import UnsupportedCapability
from dsec.runtime.backends.qemu import QemuArtifact, QemuConfig, QemuVM
from dsec.runtime.backends.qmp import QMP, QMPError
from dsec.runtime.sessions.channel import VsockCommandChannel
from dsec.runtime.sessions.native import NativeChannel


class QMPTests(unittest.TestCase):
    def fixture(self, reply):
        temp = tempfile.TemporaryDirectory(prefix='dsec-qmp-'); self.addCleanup(temp.cleanup)
        endpoint = str(Path(temp.name)/'control.sock')
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(endpoint); listener.listen(); listener.settimeout(3)
        seen, errors = [], []
        def serve():
            try:
                conn, _ = listener.accept()
                with conn, conn.makefile('rb') as reader:
                    conn.settimeout(3)
                    conn.sendall(b'{"QMP":{"version":{},"capabilities":[]}}\r\n')
                    negotiation = json.loads(reader.readline(1024))
                    self.assertEqual(negotiation['execute'], 'qmp_capabilities')
                    conn.sendall(json.dumps({'return':{}, 'id':negotiation['id']}).encode()+b'\n')
                    request = json.loads(reader.readline(1024)); seen.append(request)
                    reply(conn, request)
            except BaseException as exc:
                errors.append(exc)
            finally:
                listener.close()
        thread = threading.Thread(target=serve); thread.start()
        def cleanup():
            thread.join(4); self.assertFalse(thread.is_alive())
            if errors: raise errors[0]
        self.addCleanup(cleanup)
        return QMP(endpoint), seen

    def test_negotiation_events_and_response_identity(self):
        def reply(conn, request):
            conn.sendall(b'{"event":"RESUME","data":{}}\n')
            conn.sendall(json.dumps({'return':{'running':True}, 'id':request['id']}).encode()+b'\n')
        qmp, seen = self.fixture(reply)
        self.assertEqual(qmp.execute('query-status'), {'running':True})
        self.assertEqual([r['execute'] for r in seen], ['query-status'])

    def test_lost_mutation_reply_never_reconnects_or_replays(self):
        qmp, seen = self.fixture(lambda conn, request:None)
        with self.assertRaisesRegex(QMPError, 'Incomplete'):
            qmp.execute('cont')
        self.assertEqual([r['execute'] for r in seen], ['cont'])

    def test_mismatched_response_id_rejected(self):
        qmp, _ = self.fixture(lambda conn, request:conn.sendall(b'{"return":{},"id":"wrong"}\n'))
        with self.assertRaisesRegex(QMPError, 'ID mismatch'):
            qmp.execute('stop')

    def test_oversized_frame_and_event_budget_rejected(self):
        qmp, _ = self.fixture(lambda conn, request:conn.sendall(b'x'*129))
        qmp.MAX_FRAME = 128
        with self.assertRaisesRegex(QMPError, 'Oversized'):
            qmp.execute('query-status')
        qmp, _ = self.fixture(lambda conn, request:conn.sendall(b'{"event":"STOP"}\n'*3))
        qmp.MAX_EVENTS = 1
        with self.assertRaisesRegex(QMPError, 'event budget'):
            qmp.execute('query-status')

    def test_no_arbitrary_monitor_or_host_file_commands(self):
        with patch('dsec.runtime.backends.qmp.socket.socket', side_effect=AssertionError('must not connect')):
            for command in ('blockdev-add', 'migrate', 'human-monitor-command'):
                with self.assertRaises(ValueError): QMP('/unused').execute(command)

    def test_absolute_deadline_checked_between_fragments(self):
        connection = Mock(); connection.__enter__ = Mock(return_value=connection)
        connection.__exit__ = Mock(return_value=False)
        connection.recv.side_effect = [b'{', b'"QMP"']
        with patch('dsec.runtime.backends.qmp.socket.socket', return_value=connection), \
                patch('dsec.runtime.backends.qmp.time.monotonic', side_effect=[0, 0, .9, 1.1]):
            with self.assertRaises(TimeoutError): QMP('/unused', timeout=1).execute('stop')
        self.assertEqual(connection.recv.call_count, 1)


class QemuTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='dsec-qemu-'); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        artifacts = []
        for name in ('binary', 'kernel', 'template'):
            path = self.root/name; path.write_bytes(name.encode())
            artifacts.append(QemuArtifact(str(path), hashlib.sha256(path.read_bytes()).hexdigest()))
        self.config = QemuConfig(*artifacts, guest_cid=20000)
        self.path = self.root/'vm'; self.path.mkdir(mode=0o700)
        self.vm = QemuVM(self.config, self.path); self.addCleanup(self.vm.detach)

    def instrument(self):
        self.enterContext(patch('dsec.runtime.backends.qemu._copy_sparse', side_effect=shutil.copyfile))
        process = SimpleNamespace(poll=lambda:None, terminate=Mock(), wait=Mock(), kill=Mock(), close=Mock())
        def spawn():
            self.vm.process = process
            self.vm.record['process'] = {'fixture':True}; self.vm._save()
            Path(self.vm.qmp.endpoint).touch()
        self.vm._spawn = Mock(side_effect=spawn)
        self.vm._preflight = Mock()
        self.vm.qmp.execute = Mock(side_effect=lambda op:{'enabled':True,'present':True} if op=='query-kvm' else {})
        self.vm.native.capabilities = Mock(return_value={'native-sessions-files-v1'})
        return process

    def test_production_and_invalid_config_rejected_before_directory_access(self):
        for config in (replace(self.config, production=True), replace(self.config, guest_cid=3),
                       replace(self.config, cpu_count=True), replace(self.config, memory_mb=0)):
            with self.assertRaises((ValueError, UnsupportedCapability)):
                QemuVM(config, '/not-created')

    def test_private_directory_and_artifact_identity_required(self):
        self.config.template.verify()
        Path(self.config.template.path).write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'identity mismatch'): self.config.template.verify()
        with self.assertRaises((ValueError, FileNotFoundError)): QemuArtifact(str(self.root/'alias'), 'f'*64).verify()
        self.path.chmod(0o755)
        with self.assertRaises(ValueError): QemuVM(self.config, self.path)

    def test_launch_has_kvm_no_network_and_private_raw_disk(self):
        args = self.vm._argv('fixture-uuid', self.vm._artifacts())
        self.assertIn('q35,accel=kvm', args); self.assertIn('vhost-vsock-pci,guest-cid=20000', args)
        self.assertEqual(args[args.index('-nic')+1], 'none')
        disk = json.loads(args[args.index('-blockdev')+1])
        self.assertEqual(disk['driver'], 'raw'); self.assertEqual(disk['file']['filename'], str(self.path/'root.ext4'))
        self.assertFalse(any('tcg' in value or 'docker.sock' in value for value in args))
        self.assertIn('-S', args); self.assertIn('-sandbox', args)

    def test_lost_control_reply_retains_intent_without_retry(self):
        self.instrument(); self.vm.boot()
        self.vm.qmp.execute = Mock(side_effect=EOFError('lost pause response'))
        with self.assertRaises(EOFError): self.vm.pause()
        self.assertEqual(json.loads(self.vm.record_path.read_text())['state'], 'PAUSING')
        with self.assertRaises(RuntimeError): self.vm.pause()
        self.vm.qmp.execute.assert_called_once_with('stop')
        self.assertEqual(self.vm.stop()['state'], 'STOPPED')
        with self.assertRaises(RuntimeError): self.vm.resume()

    def test_stop_serializes_after_boot_at_controlled_boundary(self):
        process = self.instrument()
        arrived, release, stop_entered = threading.Event(), threading.Event(), threading.Event()
        def ready():
            arrived.set(); self.assertTrue(release.wait(3)); return {'native-sessions-files-v1'}
        self.vm.native.capabilities = Mock(side_effect=ready)
        def stop():
            stop_entered.set(); return self.vm.stop()
        with ThreadPoolExecutor(max_workers=2) as pool:
            boot = pool.submit(self.vm.boot); self.assertTrue(arrived.wait(3))
            stopped = pool.submit(stop); self.assertTrue(stop_entered.wait(3))
            self.assertFalse(stopped.done()); release.set()
            self.assertEqual(boot.result(timeout=3)['state'], 'RUNNING')
            self.assertEqual(stopped.result(timeout=3)['state'], 'STOPPED')
        process.terminate.assert_called_once()
        with self.assertRaises(RuntimeError): self.vm.boot()
        self.assertEqual(json.loads(self.vm.record_path.read_text())['state'], 'STOPPED')

    def test_stop_before_boot_is_terminal_for_owner(self):
        self.vm.stop()
        with self.assertRaises(RuntimeError): self.vm.boot()
        with self.assertRaises(UnsupportedCapability): self.vm.checkpoint()
        self.vm.detach()
        adopted = QemuVM(self.config, self.path); self.addCleanup(adopted.detach)
        with self.assertRaises(RuntimeError): adopted.boot()
        self.assertEqual(adopted.adopt()['state'], 'STOPPED')

    def test_pause_resume_and_guest_death_are_explicit(self):
        process = self.instrument(); self.vm.boot()
        self.assertEqual(self.vm.pause()['state'], 'PAUSED')
        self.assertEqual(self.vm.resume()['state'], 'RUNNING')
        process.poll = lambda:0
        self.assertEqual(self.vm.status()['state'], 'FAILED')
        with self.assertRaises(RuntimeError): self.vm.execute('echo nope')
        self.vm.stop()

    def test_adoption_retires_pending_control_without_replaying(self):
        process = self.instrument(); self.vm.boot()
        self.vm.state = 'PAUSING'; self.vm._save(); self.vm.detach()
        adopted = QemuVM(self.config, self.path); self.addCleanup(adopted.detach)
        adopted._attach = Mock(return_value=process)
        adopted.qmp.execute = Mock(side_effect=AssertionError('No control replay'))
        self.assertEqual(adopted.adopt()['state'], 'FAILED')
        process.terminate.assert_called_once()
        adopted.qmp.execute.assert_not_called()

    def test_adoption_rejects_changed_argv_before_process_attachment(self):
        self.instrument(); self.vm.boot(); self.vm.detach()
        record = json.loads(self.vm.record_path.read_text())
        record['argv'].append('-enable-kvm'); self.vm.record_path.write_text(json.dumps(record))
        adopted = QemuVM(self.config, self.path); self.addCleanup(adopted.detach)
        adopted._attach = Mock(side_effect=AssertionError('Untrusted identity'))
        with self.assertRaisesRegex(ValueError, 'configuration identity'): adopted.adopt()
        adopted._attach.assert_not_called()

    def test_adoption_resolves_fork_to_pid_record_gap(self):
        self.instrument(); self.vm.boot()
        self.vm.state = 'STARTING'; self.vm.record['process'] = None
        self.vm._save(); self.vm.detach()
        adopted = QemuVM(self.config, self.path); self.addCleanup(adopted.detach)
        adopted._retire_unrecorded_processes = Mock()
        adopted._attach = Mock(side_effect=AssertionError('No unrecorded PID guess'))
        self.assertEqual(adopted.adopt()['state'], 'FAILED')
        adopted._retire_unrecorded_processes.assert_called_once()

    def test_terminal_record_never_attaches_old_pid(self):
        self.instrument(); self.vm.boot(); self.vm.stop(); self.vm.detach()
        self.assertIsNone(json.loads(self.vm.record_path.read_text())['process'])
        adopted = QemuVM(self.config, self.path); self.addCleanup(adopted.detach)
        adopted._attach = Mock(side_effect=AssertionError('Never open terminal old PID'))
        self.assertEqual(adopted.adopt()['state'], 'STOPPED')
        self.assertEqual(adopted.stop()['state'], 'STOPPED')
        adopted._attach.assert_not_called()

    @unittest.skipUnless(hasattr(socket, 'AF_VSOCK'), 'Linux pidfd verifier')
    def test_pidfd_shared_verifier_requires_exact_launch_arguments(self):
        from dsec.runtime.registry import AttachedProcess, identity
        binary = str(Path('/usr/bin/sleep').resolve(strict=True))
        argv = [binary, '30']
        child = subprocess.Popen(argv)
        self.addCleanup(lambda:(child.terminate(), child.wait(timeout=5)) if child.poll() is None else None)
        saved = identity(child.pid)
        with self.assertRaisesRegex(RuntimeError, 'command line'):
            AttachedProcess(saved, binary, '/unused', expected_argv=[binary, '31'])
        attached = AttachedProcess(saved, binary, '/unused', expected_argv=argv)
        self.addCleanup(attached.close)
        self.assertIsNone(attached.poll())
        attached.terminate(); attached.wait(timeout=5); child.wait(timeout=5)


class DirectVsockTests(unittest.TestCase):
    def connection(self, response):
        connection = Mock(); connection.__enter__ = Mock(return_value=connection)
        connection.__exit__ = Mock(return_value=False)
        reader = io.BytesIO(response)
        connection.makefile.return_value = reader
        return connection

    @unittest.skipUnless(hasattr(socket, 'AF_VSOCK'), 'Direct vsock is Linux-only')
    def test_direct_native_and_legacy_wire_skip_firecracker_handshake(self):
        features = b'native-sessions-files-v1'
        connection = self.connection(f'DSEC1 0 0 0 0 0 0 {len(features)} 0\n'.encode()+features)
        with patch('dsec.runtime.sessions.native.socket.socket', return_value=connection) as factory:
            self.assertIn('native-sessions-files-v1', NativeChannel(guest_cid=20000).capabilities())
            factory.assert_called_once_with(socket.AF_VSOCK, socket.SOCK_STREAM)
            connection.connect.assert_called_once_with((20000, 5001))
            connection.sendall.assert_called_once_with(b'CAP\n')
        connection = self.connection(b'0 0 0 2\nok'); factory = Mock(return_value=connection)
        self.assertEqual(VsockCommandChannel(guest_cid=20000, socket_factory=factory).execute(ShellRequest('echo ok'))['output'], 'ok')
        connection.connect.assert_called_once_with((20000, 5000))
        self.assertEqual(connection.sendall.call_count, 1)
        connection = self.connection(b'DSECE stdout 2\nokDSEC1 0 0 0 0 0 0 0 0\n')
        with patch('dsec.runtime.sessions.native.socket.socket', return_value=connection):
            events = list(NativeChannel(guest_cid=20000).stream(session_id='1'*32,
                operation_id='2'*32, command='echo ok'))
        self.assertEqual(events[0], {'type':'stdout', 'data':b'ok'})
        connection.connect.assert_called_once_with((20000, 5001))

    def test_cid_must_be_exclusive_and_bounded(self):
        for cls in (NativeChannel, VsockCommandChannel):
            for value in (True, 0, 2, 2**32-1):
                with self.assertRaises(ValueError): cls(guest_cid=value)
            with self.assertRaises(ValueError): cls('/path', guest_cid=4)
