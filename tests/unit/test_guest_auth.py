"""Controlled unauthorized/replayed channel attempts, including native streams."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import Mock, patch

from dsec.runtime.isolation import guest_auth as auth
from dsec.runtime.backends.docker_guest import serve_connection, receive, send
from dsec.runtime.backends.qemu_container import GuestControl, ExecutionCatalog
from dsec.runtime.sessions.native import NativeChannel
from tests.unit import test_qemu_backend as qemu_tests


class GuestAuthTests(unittest.TestCase):
    key = b'a'*32

    def setUp(self):
        # Wire tests use real Unix socketpairs; no AF_VSOCK syscall on macOS.
        self.enterContext(patch.object(socket,'VMADDR_CID_HOST',2,create=True))
        self.enterContext(patch.object(socket,'AF_VSOCK',40,create=True))

    def connection(self, client, *, native=False, key=None, nonce=None):
        """Run the real guest gate; unauthorized callers must not reach effects."""
        supervisor = Mock(); supervisor.dispatch.return_value = dict(state='RUNNING')
        left, right = socket.socketpair()
        self.addCleanup(left.close); self.addCleanup(right.close)
        def server():
            try:
                with right:
                    serve_connection(supervisor, right, (socket.VMADDR_CID_HOST,0),
                                     key or self.key, native=native)
            except (OSError, EOFError, ValueError) as exc:
                return type(exc).__name__
        with ThreadPoolExecutor(max_workers=1) as pool:
            with patch.object(auth.secrets, 'token_bytes', return_value=nonce or b'n'*32), \
                    patch('dsec.runtime.backends.docker_guest.socket.socket') as target:
                future = pool.submit(server)
                try:
                    client(left)
                finally:
                    left.close()
                outcome = future.result(3)
                return supervisor, target.call_count, outcome

    def test_correct_key_control_dispatches_once_after_mutual_authentication(self):
        def client(sock):
            auth.authenticate_client(sock,self.key,5002)
            send(sock,dict(operation='status'))
            self.assertEqual(receive(sock)['result']['state'],'RUNNING')
        supervisor, target_count, outcome = self.connection(client)
        self.assertIsNone(outcome); self.assertEqual(target_count,0)
        supervisor.dispatch.assert_called_once_with(dict(operation='status'))

    def unauthorized(self, *, native=False, key=None, response=None):
        def client(sock):
            challenge = auth.read_exact(sock,len(auth.GREETING)+32)
            nonce = challenge[len(auth.GREETING):]
            sock.sendall(response or auth.proof(key or b'b'*32,5001 if native else 5002,nonce,b'client'))
            self.assertEqual(sock.recv(1),b'')
        supervisor, target_count, outcome = self.connection(client,native=native)
        supervisor.dispatch.assert_not_called()
        self.assertEqual(target_count,0,'Unauthenticated peer connected to the native agent')
        self.assertEqual(outcome,'PermissionError')

    def test_other_vm_key_cannot_dispatch_control_or_connect_to_native_agent(self):
        for native in (False,True):
            with self.subTest(native=native): self.unauthorized(native=native)

    def test_previous_challenge_response_cannot_be_replayed(self):
        response = auth.proof(self.key,5002,b'o'*32,b'client')
        self.unauthorized(response=response)

    def test_control_proof_cannot_authorize_native_port(self):
        response = auth.proof(self.key,5002,b'n'*32,b'client')
        self.unauthorized(native=True,response=response)

    def test_json_and_native_protocol_are_not_an_authentication_fallback(self):
        for native, payload in ((False,b'{"operation":"execute"}'+b'x'*32),
                                (True,b'RUN '+b'x'*40)):
            def client(sock):
                auth.read_exact(sock,len(auth.GREETING)+32)
                sock.sendall(payload[:32])
                self.assertEqual(sock.recv(1),b'')
            supervisor, count, outcome = self.connection(client,native=native)
            self.assertEqual(outcome,'PermissionError'); self.assertEqual(count,0)
            supervisor.dispatch.assert_not_called()

    def test_non_host_cid_is_rejected_before_handshake(self):
        supervisor = Mock(); sock = Mock()
        serve_connection(supervisor,sock,(1234,0),self.key)
        sock.sendall.assert_not_called(); supervisor.dispatch.assert_not_called()

    def test_server_must_prove_the_key_and_cannot_reflect_client_proof(self):
        left,right = socket.socketpair()
        self.addCleanup(left.close); self.addCleanup(right.close)
        def server():
            right.sendall(auth.GREETING+b'n'*32)
            # Echoing a client proof is not the direction-bound server proof.
            right.sendall(auth.read_exact(right,32))
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(server)
            with self.assertRaises(PermissionError): auth.authenticate_client(left,self.key,5002)
            future.result(3)

    def test_credentials_are_private_unique_and_missing_or_invalid_never_regenerated(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary); a=root/'a'; b=root/'b'; a.mkdir(); b.mkdir()
            key=auth.credential(a,create=True)
            self.assertEqual(auth.credential(a),key)
            self.assertNotEqual(key,auth.credential(b,create=True))
            with self.assertRaises(FileExistsError): auth.credential(a,create=True)
            path=a/'channel.key'
            self.assertEqual(path.stat().st_mode & 0o777,0o600)
            path.chmod(0o644)
            with self.assertRaises(PermissionError): auth.credential(a)
            path.chmod(0o600); os.link(path,a/'linked')
            with self.assertRaises(PermissionError): auth.credential(a)
            (a/'linked').unlink(); path.unlink()
            with self.assertRaises(FileNotFoundError): auth.credential(a)
            path.symlink_to(b/'channel.key')
            with self.assertRaises(OSError): auth.credential(a)
            path.unlink(); path.write_bytes(b'short'); path.chmod(0o600)
            with self.assertRaises(ValueError): auth.credential(a)

    def transport(self, port, *, stream=False):
        left,right = socket.socketpair()
        self.addCleanup(left.close); self.addCleanup(right.close)
        seen=[]
        def server():
            with right:
                auth.authenticate_server(right,self.key,port)
                if port==5002:
                    seen.append(receive(right)); send(right,dict(ok=True,result=dict(state='RUNNING')))
                else:
                    with right.makefile('rb') as reader:
                        seen.append(reader.readline(256))
                        if stream:
                            reader.read(int(seen[-1].split()[4]))
                    right.sendall(b'DSEC1 0 0 0 0 0 0 0 0\n')
        class Socket:
            def __enter__(self): return self
            def __exit__(self,*args): left.close()
            def connect(self,address): self.address=address
            def __getattr__(self,name): return getattr(left,name)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(server)
            if port==5002:
                result=GuestControl(20000,auth_key=self.key,socket_factory=lambda *a:Socket()).call('status')
                self.assertEqual(result,dict(state='RUNNING'))
            else:
                with patch('dsec.runtime.sessions.native.socket.socket',return_value=Socket()):
                    channel=NativeChannel(guest_cid=20000,auth_key=self.key)
                    if stream:
                        self.assertEqual(list(channel.stream(session_id='a'*32,command='echo hi'))[-1]['type'],'result')
                    else:
                        self.assertEqual(channel._exchange(b'CAP\n')['exit_code'],0)
            future.result(3)
        return seen

    def test_host_control_and_native_regular_and_stream_transports_authenticate(self):
        self.assertEqual(self.transport(5002),[dict(operation='status')])
        self.assertEqual(self.transport(5001),[b'CAP\n'])
        self.assertTrue(self.transport(5001,stream=True)[0].startswith(b'STREAM '))

    def test_old_catalog_rejected_before_runtime_or_artifact_effects(self):
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'catalog.json'
            path.write_text(json.dumps(dict(schema=1,cid_pool={},environments={})))
            with self.assertRaisesRegex(ValueError,'schema 2'): ExecutionCatalog(path)

    def test_handshake_deadline_does_not_restart_on_partial_reads(self):
        sock=Mock();sock.gettimeout.return_value=None;sock.recv.return_value=b'D'
        with patch.object(auth.time,'monotonic',side_effect=[0,0,.9,1.8,2.1]):
            with self.assertRaisesRegex(TimeoutError,'deadline'):
                auth.authenticate_client(sock,self.key,5002)
        self.assertEqual(sock.recv.call_count,3)
        sock.sendall.assert_not_called()
        self.assertIsNone(sock.settimeout.call_args.args[0])


class BootAuthTests(unittest.TestCase):
    """Reuse the controlled boot harness, with authentication enabled."""
    def setUp(self):
        qemu_tests.QemuTests.setUp(self)
        self.vm.auth_key=b'a'*32
        self.vm._provision_channel=Mock()

    instrument=qemu_tests.QemuTests.instrument

    def test_key_is_not_in_argv_and_changed_key_cannot_adopt(self):
        self.instrument(); self.vm.boot()
        argv=self.vm.record['argv']
        self.assertNotIn('a'*32,' '.join(argv))
        self.assertIn('virtio-serial-pci,id=dsecserial',argv)
        self.assertEqual(self.vm.record['auth_sha256'],hashlib.sha256(b'a'*32).hexdigest())
        self.vm._provision_channel.assert_called_once()
        self.vm.detach()
        from dsec.runtime.backends.qemu import QemuVM
        changed=QemuVM(self.config,self.path,auth_key=b'b'*32)
        self.addCleanup(changed.detach)
        with self.assertRaisesRegex(ValueError,'identity mismatch'): changed.adopt()


if __name__=='__main__': unittest.main()
