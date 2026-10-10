"""Real local RPC and journals with an instrumented backend, without Docker."""
import asyncio
from dataclasses import asdict, replace
import json
import os
from pathlib import Path
from types import SimpleNamespace
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from dsec.contracts.requests import request_digest
from dsec.contracts.sandbox import DSecContainerRunArgs, DSecTB2RunArgs, UnsupportedCapability
from dsec.control.server import BoundedServer, Handler
from dsec.runtime.container_edge import ContainerRuntime
from dsec.runtime.container_journal import ContainerLifecycleJournal
from dsec.runtime.requests import RequestJournal
from dsec.sdk.client import DSecClient, DSecSandbox
from dsec.sdk.sandbox_transport import RequestOutcomeUnknown, ServiceError


class Backend:
    made = []

    def __init__(self, *, root, **configuration):
        self.root = Path(root)
        self.creates = 0
        self.runs = []
        self.started = threading.Event()
        self.release = threading.Event()
        self.configuration = configuration
        self.made.append(self)

    def path(self, sid):
        return self.root / (sid + '.mock.json')

    def create(self, *, sandbox_id, **limits):
        self.creates += 1
        self.path(sandbox_id).write_text(json.dumps({'state': 'RUNNING', 'limits': limits}))
        return Container(self, sandbox_id)

    def attach(self, sid):
        if not self.prove_running(sid):
            raise RuntimeError('not running')
        return Container(self, sid)

    def prove_running(self, sid):
        return self.path(sid).exists()

    def prove_stopped(self, sid):
        return not self.path(sid).exists()


class Container:
    def __init__(self, backend, sid):
        self.backend, self.id = backend, sid
        self.name = 'dsec-e1-' + sid

    def status(self):
        return {'id': self.id, 'backend': 'container',
                'state': 'RUNNING' if self.backend.prove_running(self.id) else 'STOPPED'}

    def stop(self):
        self.backend.path(self.id).unlink()
        return self.status()

    def run_shell(self, command, *, timeout_ms, output_limit, request_id):
        self.backend.runs.append((self.id, command, timeout_ms, output_limit, request_id))
        if command == 'hold':
            self.backend.started.set()
            assert self.backend.release.wait(3)
        return {'exit_code': 0, 'output': command}

    def query_request(self, request_id):
        return {'request_id': request_id, 'state': 'NOT_FOUND'}


class Manager:
    recovery_events = []
    errors = []

    def __init__(self):
        self.entered = self.exited = 0
        self.sandboxes = {}

    def create(self, **args):
        sandbox = MicroSandbox()
        self.sandboxes[sandbox.id] = sandbox
        return sandbox

    def warm_pool_status(self):
        return {}

    def foreground_enter(self):
        self.entered += 1

    def foreground_exit(self):
        self.exited += 1


class MicroSandbox:
    id = '012345abcdef'
    reserved = False
    state = 'RUNNING'

    def __init__(self):
        self.lock = threading.Lock()
        self.executions = 0

    def status(self):
        return {'id': self.id, 'state': self.state}

    def _persist(self):
        pass

    def execute(self, command, **args):
        self.executions += 1
        return {'exit_code': 0, 'output': command}

    def stop(self):
        self.state = 'STOPPED'


class ForbiddenJournal:
    def begin(self, *_):
        raise AssertionError('container RPC must not enter the microVM journal')


class ContainerEdgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        Backend.made = []
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        mock = patch('dsec.runtime.container_edge.LayeredContainerBackend', Backend)
        mock.start()
        self.addCleanup(mock.stop)
        self.configuration = {'DSEC_CONTAINER_ROOT': str(self.root / 'containers'),
                              'DSEC_CONTAINER_AGENT': '/edge-only/agent.py',
                              'DSEC_CONTAINER_ARTIFACTS': '/edge-only/artifacts',
                              'DSEC_CONTAINER_IMAGE': 'sha256:' + 'c' * 64}
        self.runtime = ContainerRuntime(self.configuration)
        self.server = BoundedServer(str(self.root / 'service.sock'), Handler, 8)
        self.server.manager = Manager()
        self.server.journal = ForbiddenJournal()
        self.server.admission_worker_socket = None
        self.server.container_runtime = self.runtime
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={'poll_interval': .01})
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.client = DSecClient(self.root / 'service.sock')
        await self.client.open()
        self.spec = DSecContainerRunArgs()

    def stop_server(self):
        self.server.shutdown()
        self.thread.join(3)
        self.server.server_close()
        self.runtime.close()

    async def test_native_socket_path_limit_rejects_before_backend_creation(self):
        binary = self.root / 'native-agent'
        binary.write_bytes(b'fixture')
        for root in ('/tmp/' + 'x' * 58, '/tmp/' + '中' * 20):
            runtime = ContainerRuntime(dict(self.configuration,
                DSEC_CONTAINER_ROOT=root, DSEC_NATIVE_AGENT=str(binary)))
            with self.assertRaisesRegex(UnsupportedCapability, '107-byte.*shorten DSEC_CONTAINER_ROOT'):
                runtime._container_backend(self.spec)
        self.assertEqual(Backend.made, [])
        runtime = ContainerRuntime(dict(self.configuration,
            DSEC_CONTAINER_ROOT='/tmp/' + 'x' * 57, DSEC_NATIVE_AGENT=str(binary)))
        self.assertEqual(len(os.fsencode(runtime._container_backend(self.spec).root / ('0' * 32) / 'native.sock')), 107)
        # The new limit applies to opted-in native UDS deployments only.
        legacy = ContainerRuntime(dict(self.configuration, DSEC_CONTAINER_ROOT='/tmp/' + 'x' * 58))
        legacy._container_backend(self.spec)

    async def test_client_needs_no_artifact_paths_and_close_leaves_sandbox_running(self):
        with patch.dict(os.environ, {}, clear=True):
            sandbox = await self.client.run_container(self.spec, request_id='a' * 32)
        self.assertEqual(sandbox.id, 'a' * 32)
        self.assertEqual(Backend.made[0].configuration['agent'], '/edge-only/agent.py')
        self.assertFalse(hasattr(self.client, '_container_backends'))
        self.assertEqual(await sandbox.run_shell('echo hello', timeout_ms=12345,
            output_limit=1234, request_id='b' * 32), {'exit_code': 0, 'output': 'echo hello'})
        self.assertEqual(Backend.made[0].runs[-1],
                         (sandbox.id, 'echo hello', 12345, 1234, 'b' * 32))
        self.assertEqual((await sandbox.query_request('b' * 32))['state'], 'NOT_FOUND')
        await self.client.close()
        self.assertEqual((await sandbox.status())['state'], 'RUNNING')
        await self.client.open()
        attached = await self.client.attach_container(sandbox.id, self.spec)
        self.assertEqual(attached.id, sandbox.id)
        await attached.stop(request_id='d' * 32)
        self.assertEqual((await sandbox.status())['state'], 'STOPPED')
        self.assertFalse(self.runtime.handles)
        self.assertFalse(self.runtime.operation_locks)

    async def test_worker_resource_monitor_uses_rpc_identity_without_a_local_backend(self):
        from dsec.observability.elastic import ElasticResourceMonitor
        sandbox = await self.client.run_container(self.spec, request_id='a' * 32)
        self.assertFalse(hasattr(sandbox, '_container'))
        with patch('dsec.observability.elastic._container_identity', return_value=(123, 'c' * 64)), \
                patch('dsec.observability.elastic._process_identity', return_value=(123, 9)), \
                patch('dsec.observability.elastic._cgroup_for_pid', return_value=Path('/mock/cgroup')), \
                patch('dsec.observability.elastic._cgroup_sample', return_value={
                    'cpu_seconds': 0.1, 'memory_current_bytes': 100, 'io_by_device': {}}):
            monitor = await ElasticResourceMonitor.for_sandbox(sandbox, interval_seconds=100)
            self.assertEqual(monitor.snapshot()['latest']['memory_current_bytes'], 100)
            await monitor.finish()
        await sandbox.stop(request_id='b' * 32)

    async def test_repeated_create_and_stop_keep_old_digest_without_second_effect(self):
        first = await self.client.run_container(self.spec, request_id='a' * 32)
        second = await self.client.run_container(self.spec, request_id='a' * 32)
        self.assertEqual(first.id, second.id)
        self.assertEqual(Backend.made[0].creates, 1)
        proof = await self.client.lookup_container_request('a' * 32)
        self.assertEqual(proof['operation'], 'create')
        self.assertEqual(proof['args'], self.spec.lifecycle_args())
        self.assertEqual(proof['digest'], request_digest('create', None, self.spec.lifecycle_args()))
        with self.assertRaises(ServiceError) as error:
            await self.client.run_container(replace(self.spec, memory_limit_mb=1024), request_id='a' * 32)
        self.assertEqual(error.exception.kind, 'ValueError')
        await first.stop(request_id='b' * 32)
        await second.stop(request_id='b' * 32)
        stop = await self.client.lookup_container_request('b' * 32)
        self.assertEqual(stop['digest'], request_digest('stop', first.id, self.spec.stop_args()))
        self.assertEqual(self.server.manager.entered, self.server.manager.exited)

    async def test_legacy_tb2_facade_uses_edge_and_keeps_its_lifecycle_identity(self):
        args = DSecTB2RunArgs('regex-log', 'sha256:' + 'd' * 64)
        sid = 'a' * 32
        container = SimpleNamespace(id=sid, name='dsec-tb2-' + sid,
            status=lambda: {'state': 'RUNNING', 'base_url': 'http://127.0.0.1:18000'},
            stop=lambda: {'id': sid, 'state': 'STOPPED'})
        backend = Mock()
        backend.create.return_value = container
        backend.prove_stopped.return_value = False
        with patch('tb2_backend.TB2ContainerBackend', return_value=backend) as factory:
            sandbox = await self.client.run_tb2(args, request_id=sid)
            self.assertEqual(await sandbox.base_url(), 'http://127.0.0.1:18000')
            factory.assert_called_once_with(args.task_id, args.image)
            backend.create.assert_called_once_with(memory_mb=2048, cpus=1.0, sandbox_id=sid)
            proof = await self.client.lookup_container_request(sid)
            self.assertEqual(proof['digest'], request_digest('create', None, args.lifecycle_args()))
            await sandbox.stop(request_id='b' * 32)
            backend.create.assert_called_once()

    async def test_result_commit_failure_is_unknown_then_proven_without_recreate(self):
        from dsec.runtime.container_journal import atomic_json
        def fail_result(path, record):
            if record['state'] == 'DONE':
                raise OSError('simulated fsync failure')
            return atomic_json(path, record)
        with patch('dsec.runtime.container_journal.atomic_json', side_effect=fail_result):
            with self.assertRaises(RequestOutcomeUnknown) as error:
                await self.client.run_container(self.spec, request_id='a' * 32)
        self.assertEqual(error.exception.request_id, 'a' * 32)
        self.assertEqual(Backend.made[0].creates, 1)
        proof = await self.client.lookup_container_request('a' * 32)
        self.assertEqual(proof['state'], 'DONE')
        self.assertTrue(proof['recovered_from_unknown'])
        sandbox = await self.client.attach_container(proof['response']['result']['id'], self.spec)
        await sandbox.stop(request_id='b' * 32)
        self.assertEqual(Backend.made[0].creates, 1)

    async def test_legacy_unknown_record_is_not_replayed_after_owner_restart(self):
        # Persist the exact v0.1 container schema, without a transport envelope.
        journal = self.runtime._journal()
        request_id = 'a' * 32
        args = self.spec.lifecycle_args()
        record = {'version': 1, 'state': 'PENDING', 'request_id': request_id,
                  'operation': 'create', 'sandbox_id': None, 'args': args,
                  'digest': request_digest('create', None, args)}
        (journal.root / (request_id + '.json')).write_text(json.dumps(record))
        self.runtime.close()
        self.runtime = ContainerRuntime(self.configuration)
        self.server.container_runtime = self.runtime
        self.assertEqual((await self.client.lookup_container_request(request_id))['state'], 'UNKNOWN')
        with self.assertRaises(RequestOutcomeUnknown):
            await self.client.run_container(self.spec, request_id=request_id)
        self.assertEqual(Backend.made[0].creates, 0)

    async def test_busy_command_rejects_stop_before_journaling_and_does_not_block_reads(self):
        sandbox = await self.client.run_container(self.spec, request_id='a' * 32)
        backend = Backend.made[0]
        running = asyncio.create_task(sandbox.run_shell('hold', request_id='b' * 32))
        self.assertTrue(await asyncio.to_thread(backend.started.wait, 2))
        try:
            self.assertEqual((await sandbox.status())['state'], 'RUNNING')
            with self.assertRaises(ServiceError) as error:
                await sandbox.stop(request_id='d' * 32)
            self.assertEqual(error.exception.kind, 'ServiceBusy')
            self.assertEqual((await self.client.lookup_container_request('d' * 32))['state'], 'NOT_FOUND')
        finally:
            backend.release.set()
            await running
        await sandbox.stop(request_id='d' * 32)

    async def test_two_edges_cannot_own_one_container_directory(self):
        self.runtime._journal()
        other = ContainerRuntime(self.configuration)
        self.addCleanup(other.close)
        with self.assertRaises(BlockingIOError):
            other._journal()
        self.runtime.close()
        self.assertIsInstance(other._journal(), ContainerLifecycleJournal)

    async def test_edge_scheduler_guard_denies_before_journal_and_effect(self):
        self.runtime.admission_worker_socket = str(self.root / 'missing-worker.sock')
        with self.assertRaises(ServiceError) as error:
            await self.client.run_container(self.spec, request_id='a' * 32)
        self.assertEqual(error.exception.kind, 'AdmissionDenied')
        self.assertEqual(Backend.made[0].creates, 0)
        self.assertFalse((self.runtime.journal.root / ('a' * 32 + '.json')).exists())

    async def test_existing_microvm_rpc_and_deduplication_still_use_the_original_journal(self):
        self.server.journal = RequestJournal(self.root / 'microvm')
        sandbox = await self.client.run_microvm(request_id='a' * 32)
        first = await sandbox.run_shell('read', request_id='b' * 32)
        second = await sandbox.run_shell('read', request_id='b' * 32)
        self.assertEqual(first, second)
        self.assertEqual(self.server.manager.sandboxes[sandbox.id].executions, 1)
        proof = await self.client.lookup_request('b' * 32)
        self.assertEqual(proof['digest'], request_digest('execute', sandbox.id,
            {'command': 'read', 'timeout_ms': 5000, 'output_limit': 65536}))
        await sandbox.stop(request_id='d' * 32)
        self.assertEqual((await sandbox.status())['state'], 'STOPPED')
        self.assertIsNone(self.runtime.journal)

    async def test_path_arguments_and_invalid_limits_cannot_reconfigure_edge(self):
        for extra in ({'root': '/tmp/another-instance'}, {'docker_socket': '/var/run/docker.sock'}):
            with self.assertRaises(ServiceError) as error:
                await asyncio.to_thread(self.client._transport.call, 'container_create',
                                        spec=asdict(self.spec), **extra)
            self.assertEqual(error.exception.kind, 'ValueError')
        for limits in ({'memory_limit_mb': True}, {'cpu_cores_limit': float('nan')}):
            with self.assertRaises(ValueError):
                await self.client.run_container(replace(self.spec, **limits))
        self.assertIsNone(self.runtime.journal)
        self.assertFalse(Backend.made)


class ClientBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_stable_microvm_request_id_is_not_replaced_after_service_busy(self):
        transport = unittest.mock.Mock()
        transport.call.side_effect = [ServiceError('ServiceBusy', 'not admitted'),
                                      RequestOutcomeUnknown('lost reply', 'b' * 32)]
        sandbox = DSecSandbox(transport, 'sandbox-1')
        with self.assertRaises(ServiceError):
            await sandbox.run_shell('echo hello', request_id='a' * 32)
        self.assertEqual(transport.call.call_count, 1)
        self.assertEqual(transport.call.call_args.kwargs['request_id'], 'a' * 32)

    async def test_older_service_has_no_implicit_host_docker_fallback(self):
        client = DSecClient('unused.sock')
        transport = unittest.mock.Mock()
        transport.call.return_value = {'pid': 1}
        client._transport = transport
        await client.open()
        with self.assertRaisesRegex(UnsupportedCapability, 'Upgrade sandbox service'):
            await client.run_container()
        self.assertEqual(transport.call.call_count, 1)

    def test_sdk_import_without_runtime_backends(self):
        code = '''
import importlib.abc, sys
class BlockBackends(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith(('dsec.runtime.backends', 'dsec.runtime.container_edge', 'dsec.storage.catalog', 'tb2_backend')):
            raise AssertionError('client loaded host backend: ' + fullname)
sys.meta_path.insert(0, BlockBackends())
from dsec.sdk import DSecClient
from libdsec_compat import DSecClient as LegacyClient
assert LegacyClient is DSecClient
'''
        result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
