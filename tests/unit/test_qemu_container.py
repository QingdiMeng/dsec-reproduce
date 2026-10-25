"""Controlled Container/FnCall cleanup and Edge journal regressions (no KVM)."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import io
import json
from pathlib import Path
import socket
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from dsec.contracts.sandbox import DSecContainerRunArgs, DSecFnCallRunArgs, UnsupportedCapability
from dsec.runtime.backends.docker_guest import Supervisor, bounded_shell, receive, send
from dsec.runtime.backends.qemu_container import ExecutionCatalog, QemuContainer, QemuContainerBackend
from dsec.runtime.container_edge import ContainerRuntime
from dsec.runtime.requests import RequestJournal
from dsec.sdk.client import DSecFnCallExecutor
from dsec.runtime.container_journal import ContainerLifecycleJournal
from dsec.contracts.rpc import ServiceError


class Docker:
    def __init__(self):
        self.calls = []; self.cid = 'e'*64; self.fail_cleanup = False
        self.cleaning, self.release = threading.Event(), threading.Event()
        self.block_cleanup = False

    def __call__(self, argv, *, timeout=30):
        self.calls.append(argv)
        if argv[0] == 'run':
            return self.cid
        if argv[0] == 'inspect':
            return json.dumps([dict(Id=self.cid,State=dict(Running=True))])
        if argv[0] == 'restart':
            self.cleaning.set()
            if self.block_cleanup:
                assert self.release.wait(3)
            if self.fail_cleanup:
                raise RuntimeError('cleanup failed')
            return 'dsec-task'
        if argv[:2] == ['exec', '-d']:
            return ''
        if argv[0] == 'exec':
            return json.dumps(dict(exit_code=0,output='ok',timed_out=False,truncated=False))
        raise AssertionError(argv)


class GuestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.docker = Docker()
        self.config = dict(mode='fncall',image='sha256:'+'a'*64,memory_mb=512,cpus=1)

    def supervisor(self, mode='fncall'):
        value = Supervisor(dict(self.config,mode=mode),self.temp.name,run=self.docker)
        value.start(); return value

    def request(self, rid='a'*32):
        return dict(operation='execute',request_id=rid,command='printf ok',timeout_ms=100,output_limit=32)

    def test_fncall_cleanup_before_done_same_container_and_no_replay(self):
        supervisor = self.supervisor()
        result = supervisor.dispatch(self.request())
        self.assertEqual(result['state'],'DONE')
        self.assertEqual(supervisor.status()['container_id'],self.docker.cid)
        self.assertEqual(supervisor.dispatch(self.request()),result)
        self.assertEqual(sum(argv[0]=='restart' for argv in self.docker.calls),1)
        self.assertEqual(sum(argv[0]=='exec' for argv in self.docker.calls),1)
        argv = self.docker.calls[0]
        for option in ('--read-only','--tmpfs','--cap-drop','--pids-limit','--memory'):
            self.assertIn(option,argv)
        self.assertNotIn('/var/run/docker.sock',' '.join(argv))

    def test_cleanup_is_a_linearization_boundary_not_a_reusable_slot(self):
        supervisor = self.supervisor(); self.docker.block_cleanup = True
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(supervisor.dispatch,self.request())
            self.assertTrue(self.docker.cleaning.wait(2))
            path = Path(self.temp.name)/('a'*32+'.json')
            self.assertEqual(json.loads(path.read_text())['state'],'UNKNOWN')
            second = pool.submit(supervisor.dispatch,self.request('b'*32))
            self.assertFalse(first.done()); self.assertFalse(second.done())
            self.assertEqual(sum(argv[0]=='exec' for argv in self.docker.calls),1)
            self.docker.release.set()
            self.assertEqual(first.result(3)['state'],'DONE')
            self.assertEqual(second.result(3)['state'],'DONE')

    def test_failed_cleanup_never_reuses_slot_or_returns_success(self):
        supervisor = self.supervisor(); self.docker.fail_cleanup = True
        with self.assertRaisesRegex(RuntimeError,'cleanup'):
            supervisor.dispatch(self.request())
        with self.assertRaises(RuntimeError):
            supervisor.dispatch(self.request('b'*32))
        self.assertEqual(sum(argv[0]=='exec' for argv in self.docker.calls),1)
        state = json.loads((Path(self.temp.name)/'supervisor.json').read_text())
        self.assertEqual(state['state'],'FAILED')

    def test_unknown_intent_is_not_executed_again(self):
        supervisor = self.supervisor()
        import hashlib
        value = self.request()
        digest = hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest()
        (Path(self.temp.name)/('a'*32+'.json')).write_text(json.dumps(dict(state='UNKNOWN',digest=digest)))
        self.assertEqual(supervisor.dispatch(value)['state'],'UNKNOWN')
        self.assertFalse(any(argv[0]=='exec' for argv in self.docker.calls))

    def test_container_preserves_state_and_starts_native_agent(self):
        supervisor = self.supervisor('container')
        self.assertEqual(supervisor.dispatch(self.request())['state'],'DONE')
        self.assertFalse(any(argv[0]=='restart' for argv in self.docker.calls))
        self.assertTrue(any(argv[:2]==['exec','-d'] for argv in self.docker.calls))
        self.assertNotIn('--read-only',self.docker.calls[0])

    def test_changed_container_identity_and_request_digest_fail_closed(self):
        supervisor = self.supervisor()
        supervisor.dispatch(self.request())
        with self.assertRaisesRegex(ValueError,'conflict'):
            supervisor.dispatch(dict(self.request(),command='different'))
        self.docker.cid = 'b'*64
        with self.assertRaisesRegex(RuntimeError,'identity'):
            supervisor.dispatch(self.request('b'*32))

    def test_limits_and_schema_reject_before_execute(self):
        supervisor = self.supervisor()
        for key,value in (('output_limit',262145),('timeout_ms',True),('timeout_ms',30001),
                          ('command','x'*65537),('argv',['docker','run'])):
            with self.subTest(key=key,value=str(value)[:20]), self.assertRaises(ValueError):
                supervisor.dispatch(dict(self.request(),**{key:value}))
        self.assertFalse(any(argv[0]=='exec' for argv in self.docker.calls))

    def test_bounded_shell_drains_large_output_and_kills_timeout(self):
        real_popen = __import__('subprocess').Popen
        def popen(argv,**kwargs):
            kwargs['cwd'] = self.temp.name
            return real_popen(argv,**kwargs)
        with patch('dsec.runtime.backends.docker_guest.subprocess.Popen',popen):
            result = bounded_shell('yes a | head -c 1000000',2000,17)
            self.assertEqual(len(result['output']),17); self.assertTrue(result['truncated'])
            result = bounded_shell('sleep 5',50,32)
            self.assertTrue(result['timed_out'])

    def test_bounded_protocol_and_exact_frames(self):
        value = dict(result='中文')
        class Wire(io.BytesIO):
            def sendall(self,data): self.write(data)
            def recv(self,size): return self.read(min(size,2))
        stream = Wire(); send(stream,value); stream.seek(0)
        self.assertEqual(receive(stream),value)
        with self.assertRaises(ValueError):
            receive(Wire((2**30).to_bytes(4,'big')))
        with self.assertRaises(EOFError):
            receive(Wire(b'\0\0'))


class EdgeTests(unittest.TestCase):
    def test_old_edge_contract_cannot_blindly_upgrade_or_rollback_new_execution_state(self):
        from dsec.contracts.release import COMPATIBILITY,same_contract
        current = dict(build='a'*64,version='0.1.0.dev0',compatibility=dict(COMPATIBILITY))
        old = dict(current,build='b'*64,compatibility=dict(COMPATIBILITY,edge=1))
        self.assertEqual(COMPATIBILITY['edge'],2)
        for source,target in ((old,current),(current,old)):
            with self.assertRaisesRegex(ValueError,'migration'):
                same_contract(source,target)

    def test_stopped_predecessor_never_retires_a_reused_numeric_cid(self):
        from dataclasses import asdict
        from dsec.runtime.isolation.vsock import CIDLease
        with tempfile.TemporaryDirectory() as temporary:
            lease = CIDLease(20000,'a'*32,temporary)
            replacement = CIDLease(20000,'b'*32,'/other-instance')
            pool = Mock(); pool.snapshot.return_value = {'20000':asdict(replacement)}
            backend = SimpleNamespace(pool=pool,item=dict(mode='fncall'))
            vm = SimpleNamespace(directory=Path(temporary),lock=threading.RLock(),
                status=lambda:dict(state='STOPPED',process_alive=False))
            handle = QemuContainer(backend,'a'*32,lease,vm,Mock())
            disk=Path(temporary)/'root.ext4'; disk.write_bytes(b'private data')
            self.assertTrue(handle.stop()['resource_cleanup_complete'])
            self.assertFalse(disk.exists())
            self.assertTrue((Path(temporary)/'retired.json').is_file())
            pool.retire.assert_not_called()
            vm.status = lambda:dict(state='RUNNING',process_alive=True)
            with self.assertRaisesRegex(RuntimeError,'Missing CID'):
                handle.stop()
            pool.retire.assert_not_called()

    def test_foreign_or_linked_disk_never_produces_cleanup_success(self):
        import os
        from dsec.runtime.isolation.vsock import CIDLease
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary); disk=root/'root.ext4'; other=root/'other'
            other.write_bytes(b'preserve'); disk.symlink_to(other)
            lease=CIDLease(20000,'a'*32,temporary)
            pool=Mock(); pool.snapshot.return_value={}
            vm=SimpleNamespace(directory=root,lock=threading.RLock(),
                               status=lambda:dict(state='STOPPED',process_alive=False))
            handle=QemuContainer(SimpleNamespace(pool=pool),'a'*32,lease,vm,Mock())
            with self.assertRaisesRegex(RuntimeError,'ownership'):
                handle.stop()
            self.assertEqual(other.read_bytes(),b'preserve')
            disk.unlink();os.link(other,disk)
            with self.assertRaisesRegex(RuntimeError,'ownership'):
                handle.stop()
            self.assertFalse((root/'retired.json').exists())

    def test_failed_creation_requires_explicit_terminal_proof_and_is_never_recreated(self):
        with tempfile.TemporaryDirectory() as temporary:
            journal = ContainerLifecycleJournal(temporary,edge_node_admission=True)
            effect = Mock(side_effect=RuntimeError('boot failed'))
            rid = 'a'*32
            with self.assertRaises(Exception):
                journal.execute(rid,'create',None,{},effect)
            self.assertEqual(journal.recover(rid,lambda record:None,failed=True)['state'],'UNKNOWN')
            error = dict(type='ExecutionCreateFailed',message='failed',details=dict(cleanup_complete=True))
            proof = journal.recover(rid,lambda record:error,failed=True)
            self.assertFalse(proof['response']['ok'])
            with self.assertRaises(ServiceError) as raised:
                journal.execute(rid,'create',None,{},effect)
            self.assertEqual(raised.exception.kind,'ExecutionCreateFailed')
            effect.assert_called_once()

    def test_old_container_wire_and_journal_digest_unchanged(self):
        spec = DSecContainerRunArgs()
        self.assertNotIn('execution_mode',spec.wire_spec())
        self.assertNotIn('execution_mode',spec.lifecycle_args())
        self.assertNotIn('execution_mode',spec.stop_args())
        fn = DSecFnCallRunArgs(environment_id='function-python').validate()
        self.assertEqual(fn.wire_spec()['execution_mode'],'fncall')
        self.assertEqual(fn.lifecycle_args()['execution_mode'],'fncall')

    def test_fncall_never_falls_back_to_host_docker_or_native_session(self):
        runtime = ContainerRuntime({})
        spec = DSecFnCallRunArgs()
        with self.assertRaisesRegex(UnsupportedCapability,'QEMU'):
            runtime._backend('container',spec)
        with self.assertRaisesRegex(UnsupportedCapability,'persistent'):
            with runtime.native_operation('container',spec,'a'*32,'b'*32):
                self.fail('Function acquired persistent native session')
        runtime.close()

    def test_fncall_sdk_has_invocation_and_no_session_methods(self):
        transport = Mock(); transport.call.return_value = dict(output='ok')
        fn = DSecFnCallExecutor(transport,'sandbox',DSecFnCallRunArgs())
        self.assertFalse(hasattr(fn,'open_session'))
        self.assertEqual(asyncio.run(fn.invoke('true',request_id='a'*32)),dict(output='ok'))
        args = transport.call.call_args.kwargs
        self.assertEqual(args['spec']['execution_mode'],'fncall')

    def test_host_action_proof_survives_guest_failure_and_blocks_replay(self):
        with tempfile.TemporaryDirectory() as temporary:
            vm = SimpleNamespace(directory=Path(temporary),lock=threading.RLock(),
                                 status=lambda:dict(state='RUNNING'))
            backend = SimpleNamespace(item=dict(mode='fncall'))
            control = Mock(); control.call.side_effect = EOFError('lost reply')
            handle = QemuContainer(backend,'a'*32,None,vm,control)
            handle.stop = Mock()
            with self.assertRaises(Exception):
                handle.run_shell('touch x',request_id='b'*32)
            self.assertEqual(handle.query_request('b'*32)['state'],'UNKNOWN')
            self.assertIn('lost reply',handle.query_request('b'*32)['reason'])
            self.assertEqual(handle.query_request('b'*32)['args']['command'],'touch x')
            handle.stop.assert_called_once()
            with self.assertRaises(Exception):
                handle.run_shell('touch x',request_id='b'*32)
            self.assertEqual(control.call.call_count,1)
            restored = RequestJournal(temporary)
            self.assertEqual(restored.lookup('b'*32)['state'],'UNKNOWN')


class GatewayTests(unittest.TestCase):
    def test_unknown_action_proof_with_no_response_maps_to_public_id(self):
        from dsec.control.node_gateway import public_proof
        raw=dict(state='UNKNOWN',request_id='a'*32,response=None,
                 reason='EOFError: lost reply',args=dict(command='sleep 30'))
        value=public_proof(raw,'b'*32)
        self.assertIsNone(value['response'])
        self.assertEqual(value['request_id'],'b'*32)
        self.assertEqual(value['reason'],raw['reason'])
        self.assertEqual(raw['request_id'],'a'*32)

    def fixture(self, *, fncall=False, physical=False):
        from tests.unit.test_cluster_control import MemoryStore, EdgeFixture
        from dsec.contracts.cluster import Quota
        from dsec.control.iam import IAM
        from dsec.control.node_gateway import NodeGateway
        self.token = 'admin-'+'a'*48
        self.iam = IAM(MemoryStore()); self.iam.bootstrap(self.token,Quota(8,8,8192,16384,8))
        self.edge = EdgeFixture()
        self.minimum = Quota(1,1,512,1024,0)
        actual = {'default':dict(mode='fncall' if fncall else 'container',
                  minimum=asdict(Quota(1,1,1024,2048,0)))} if physical else {}
        original = self.edge.call
        self.edge.call = lambda operation,*a,**kw: (dict(qemu_environments=actual) if operation=='health'
                                                   else original(operation,*a,**kw))
        spec = DSecContainerRunArgs(environment_id='default',execution_mode='fncall' if fncall else 'container')
        self.spec = spec
        receipt = self.iam.admit(self.token,'root','a'*32,'d'*64,'n1','container',self.minimum,
                                 args=dict(spec=spec.wire_spec()))
        node = NodeGateway('n1',self.edge,self.iam,route_key=b'r'*32,boot_id='b',backends=('container',),
            environments=('default',),minimum_demands={'default':asdict(self.minimum)})
        return node,receipt

    def test_outer_vm_minimum_cannot_be_undercharged_as_inner_container(self):
        node,receipt = self.fixture(physical=True)
        with self.assertRaises(ServiceError) as raised:
            node.create(receipt)
        self.assertEqual(raised.exception.kind,'AdmissionDenied')
        self.assertEqual(raised.exception.details['minimum']['memory_mb'],1024)
        self.assertEqual(raised.exception.details['minimum']['disk_mb'],2048)
        self.assertEqual(self.iam.lookup('root','a'*32)['state'],'RELEASED')
        self.assertEqual(self.edge.created,0)

    def test_unprepared_edge_cannot_dispatch_a_function(self):
        node,receipt = self.fixture(fncall=True)
        with self.assertRaises(UnsupportedCapability):
            node.create(receipt)
        self.assertEqual(self.edge.created,0)
        self.assertEqual(self.iam.lookup('root','a'*32)['state'],'ADMITTED')

    def test_only_canonical_failed_cleanup_proof_releases_iam_quota(self):
        from dsec.control.node_gateway import edge_request_id
        from dsec.contracts.requests import request_digest,RequestConflict
        for cleanup,digest_ok,expected in ((True,True,'RELEASED'),(False,True,'ADMITTED'),(True,False,'ADMITTED')):
            with self.subTest(cleanup=cleanup,digest=digest_ok):
                node,receipt = self.fixture()
                rid = edge_request_id('root','a'*32)
                self.edge.proofs[rid] = dict(state='DONE',request_id=rid,operation='create',sandbox_id=None,
                    digest=request_digest('create',None,self.spec.lifecycle_args()) if digest_ok else 'f'*64,
                    response=dict(ok=False,error=dict(type='ExecutionCreateFailed',message='failed',
                                                       details=dict(cleanup_complete=cleanup))))
                with self.assertRaises((ServiceError,RequestConflict)):
                    node.create(receipt)
                self.assertEqual(self.iam.lookup('root','a'*32)['state'],expected)

    def test_wrong_mode_is_rejected_before_edge_dispatch(self):
        node,receipt = self.fixture(physical=True)
        self.spec = DSecFnCallRunArgs(environment_id='default')
        receipt['args'] = dict(spec=self.spec.wire_spec())
        # Replace only the trusted fixture's stored receipt, preserving its CAS.
        stored = self.iam.store.read('admissions/root/'+'a'*32)
        self.iam.store.transact([stored],{stored.key:receipt})
        with self.assertRaisesRegex(UnsupportedCapability,'mode'):
            node.create(receipt)
        self.assertEqual(self.edge.created,0)


if __name__ == '__main__':
    unittest.main()


class FailedCreateRecoveryTests(unittest.TestCase):
    def fixture(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        root=Path(temp.name).resolve();sid='a'*32
        backend=QemuContainerBackend.__new__(QemuContainerBackend)
        backend.lock=threading.RLock();backend.handles={};backend._directory=lambda value:root/value
        backend.catalog=Mock(digest='c'*64,host_isolation=None);backend.network=None
        backend.environment_id='unit';backend.item=dict(mode='fncall',inner_memory_mb=512,inner_cpus=1)
        from dsec.runtime.isolation.vsock import CIDLease
        backend.pool=Mock();backend.pool.reserve.return_value=CIDLease(20000,'b'*32,str(root/sid))
        backend._config=Mock(return_value=None);backend.control_factory=Mock()
        return root,sid,backend

    def test_pending_cleanup_can_retry_without_restarting_edge(self):
        root,sid,backend=self.fixture();(root/sid).mkdir()
        handle=Mock();handle.vm.status.return_value=dict(state='STOPPING',process_alive=False)
        backend._handle=Mock(return_value=handle)
        handle.stop.side_effect=RuntimeError('network cleanup pending')
        with self.assertRaisesRegex(RuntimeError,'cleanup pending'):backend.prove_stopped(sid)
        handle.stop.side_effect=None
        self.assertTrue(backend.prove_stopped(sid))
        self.assertEqual(handle.stop.call_count,2)

    def test_original_failure_survives_a_second_cleanup_exception(self):
        root,sid,backend=self.fixture()
        vm=Mock(directory=root/sid,record=dict(boot_error=dict(type='RuntimeError',message='TC inspection failed')))
        vm.boot.side_effect=RuntimeError('first cleanup failure')
        with patch('dsec.runtime.backends.qemu_container.QemuVM',return_value=vm), \
                patch.object(QemuContainer,'stop',side_effect=RuntimeError('cleanup still blocked')):
            with self.assertRaisesRegex(RuntimeError,'cleanup still blocked'):
                backend.create(memory_mb=512,cpus=1,sandbox_id=sid)
        failure=json.loads((root/sid/'failed-create.json').read_text())
        self.assertEqual(failure['message'],'TC inspection failed')
        self.assertEqual(failure['cleanup_error']['message'],'cleanup still blocked')
        self.assertFalse(failure['cleanup_complete']);backend.pool.release.assert_not_called()

    def test_interrupted_create_recovers_only_after_terminal_resource_proof(self):
        root,sid,backend=self.fixture();directory=root/sid;directory.mkdir()
        (directory/'qemu.json').write_text('{}')
        handle=Mock();handle.vm.record={}
        handle.vm.status.return_value=dict(state='RUNNING',process_alive=True)
        backend._handle=Mock(return_value=handle);backend.prove_stopped=Mock()
        self.assertFalse(backend.prove_failed_create(sid));backend.prove_stopped.assert_not_called()
        handle.vm.status.return_value=dict(state='FAILED',process_alive=False)
        backend.prove_stopped.side_effect=RuntimeError('network cleanup pending')
        with self.assertRaisesRegex(RuntimeError,'cleanup pending'):backend.prove_failed_create(sid)
        path=directory/'failed-create.json'
        self.assertFalse(json.loads(path.read_text())['cleanup_complete'])
        backend.prove_stopped.side_effect=None;backend.prove_stopped.return_value=True
        self.assertTrue(backend.prove_failed_create(sid))
        self.assertTrue(json.loads(path.read_text())['cleanup_complete'])
        handle.vm.boot.assert_not_called()
