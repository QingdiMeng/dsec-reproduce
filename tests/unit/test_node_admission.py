"""Durable Edge leases with real RPC/journals and instrumented VM/Docker effects."""
import asyncio
from dataclasses import asdict
import json
import shutil
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest
from unittest.mock import patch

from dsec.contracts.resources import NodeBudget, NodeDemand, NodeLeaseUncertain
from dsec.control.server import BoundedServer, Handler
from dsec.runtime.container_edge import ContainerRuntime
from dsec.runtime.lifecycle import SandboxManager
from dsec.runtime.node_admission import NodeAdmission
from dsec.runtime.requests import RequestJournal
from dsec.runtime.backends.firecracker import MicroVM
from dsec.rollout.scheduler import WorkScheduler
from dsec.rollout.worker import RolloutWorker
from dsec.sdk.client import DSecClient, DSecMicroVMRunArgs
from dsec.sdk.sandbox_transport import ServiceError, RequestOutcomeUnknown
from tests.unit.test_container_edge import Backend
from tests.unit.test_work_scheduler import FakeSampler, budget


class Process:
    pid = 999999
    def __init__(self):
        self.alive = True
    def poll(self):
        return None if self.alive else 0
    def terminate(self):
        self.alive = False
    def kill(self):
        self.alive = False
    def wait(self, timeout):
        self.alive = False
        return 0


class EdgeLeaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.sampler = FakeSampler()
        self.budget = budget(cpu=2, memory_mb=1024, disk_mb=2048)
        self.node = NodeAdmission(self.root/'vms', NodeBudget.from_resource(self.budget), self.sampler)
        self.node.activate()
        self.boots = []
        def boot(vm, *args, **kwargs):
            self.boots.append(kwargs)
            vm.process = Process()
        mock = patch.object(MicroVM, 'boot', boot)
        mock.start()
        self.addCleanup(mock.stop)
        mock = patch('dsec.runtime.lifecycle._copy_sparse', shutil.copyfile)
        mock.start()
        self.addCleanup(mock.stop)
        mock = patch('dsec.runtime.container_edge.LayeredContainerBackend', Backend)
        mock.start()
        self.addCleanup(mock.stop)
        self.template = self.root/'template.ext4'
        self.template.write_bytes(b'guest')
        self.manager = SandboxManager(self.root/'vms', self.root/'fc', self.root/'kernel',
            self.template, capacity=4, node_admission=self.node,
            tb2_templates={'tb2-test':self.template})
        self.manager.recovery_events = []
        self.runtime = ContainerRuntime({'DSEC_CONTAINER_ROOT':str(self.root/'containers'),
            'DSEC_CONTAINER_AGENT':'/agent', 'DSEC_CONTAINER_ARTIFACTS':'/artifacts',
            'DSEC_CONTAINER_IMAGE':'sha256:'+'a'*64}, node_admission=self.node)
        self.server = BoundedServer(str(self.root/'socket'), Handler, 8)
        self.server.manager = self.manager
        self.server.journal = RequestJournal(self.manager.root)
        self.server.node_admission = self.node
        self.server.admission_worker_socket = None
        self.server.container_runtime = self.runtime
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval':.01})
        self.thread.start()
        self.addCleanup(self.cleanup_runtime)
        self.client = await DSecClient(self.root/'socket').open()

    def cleanup_runtime(self):
        self.server.shutdown()
        self.thread.join(3)
        self.server.server_close()
        self.manager.close()
        self.runtime.close()
        self.node.close()

    async def worker(self):
        scheduler = WorkScheduler(self.budget, None, node_status=self.client.node_status,
                                  sample_interval=.01)
        await scheduler.refresh_node_status()
        worker = RolloutWorker(self.client, state_dir=self.root/'rollouts', scheduler=scheduler)
        # Resource instrumentation is verified separately; this fixture has no
        # actual /proc VMM and must not try to measure its deliberately fake PID.
        async def meter(*args):
            pass
        worker._start_meter = meter
        await worker.initialize()
        return worker

    async def test_busy_before_execution_keeps_request_id_retryable_and_effect_runs_once(self):
        vm = await self.client.run_microvm(request_id='a'*32)
        sandbox = self.manager.sandboxes[vm.id]
        result = dict(exit_code=0, output='once', timed_out=False, truncated=False)
        with patch.object(MicroVM, 'execute', return_value=result) as effect:
            sandbox.lock.acquire()
            try:
                with self.assertRaises(ServiceError) as refused:
                    await asyncio.to_thread(self.client._transport.call, 'execute', vm.id,
                        request_id='b'*32, command='write once', timeout_ms=5000, output_limit=65536)
                self.assertEqual(refused.exception.kind, 'ServiceBusy')
                self.assertEqual(self.server.journal.lookup('b'*32)['state'], 'NOT_FOUND')
                effect.assert_not_called()
                pending = asyncio.create_task(vm.run_shell('write once', request_id='b'*32))
                await asyncio.sleep(.1)
                self.assertFalse(pending.done())
                effect.assert_not_called()
            finally:
                sandbox.lock.release()
            self.assertEqual(await asyncio.wait_for(pending, 3), result)
            self.assertEqual(await vm.run_shell('write once', request_id='b'*32), result)
            effect.assert_called_once()
            self.assertEqual(self.server.journal.lookup('b'*32)['state'], 'DONE')
            with self.assertRaises(RuntimeError):
                self.server.journal.reject_before_effect('b'*32,operation='execute')
            self.assertEqual(self.server.journal.lookup('b'*32)['state'], 'DONE')
        await vm.stop()

    async def test_explicit_id_busy_against_legacy_service_is_not_retried(self):
        from dsec.sdk.client import DSecSandbox
        transport = unittest.mock.Mock()
        transport.call.side_effect = ServiceError('ServiceBusy', 'legacy cached rejection')
        vm = DSecSandbox(transport, '123456abcdef')
        with self.assertRaises(ServiceError):
            await vm.run_verifier_shell('write once', timeout_ms=5000, request_id='c'*32)
        transport.call.assert_called_once()

    async def test_direct_vm_and_container_share_one_budget_and_retry_only_nonadmission(self):
        vm = await self.client.run_microvm(request_id='1'*32)
        container = await self.client.run_container(request_id='2'*32)
        before = await self.client.node_status()
        self.assertEqual(before['reserved']['memory_mb'], 1024)
        with self.assertRaises(ServiceError) as busy:
            await self.client.run_microvm(request_id='3'*32)
        self.assertEqual(busy.exception.kind, 'NodeAdmissionBusy')
        self.assertIn('memory_mb_budget', busy.exception.details['reasons'])
        self.assertEqual((await self.client.lookup_request('3'*32))['state'], 'NOT_FOUND')
        self.assertEqual(len(self.boots), 1)
        await container.stop(request_id='4'*32)
        second = await self.client.run_microvm(request_id='3'*32)
        self.assertEqual(len(self.boots), 2)
        self.assertEqual((await self.client.node_status())['reserved']['memory_mb'], 1024)
        await vm.stop()
        await second.stop()
        self.assertEqual((await self.client.node_status())['reserved']['memory_mb'], 0)

    async def test_physical_lease_is_committed_before_boot_allows_concurrent_container_admission(self):
        entered, finish = threading.Event(), threading.Event()
        def boot(vm, *args, **kwargs):
            vm.process = Process()
            entered.set()
            if not finish.wait(2):
                raise RuntimeError('test boot barrier timeout')
        self.node.default_demand = NodeDemand(2, 1024, 1024, 1)
        with patch.object(MicroVM, 'boot', boot):
            creating = asyncio.create_task(self.client.run_microvm(request_id='4'*32))
            try:
                await asyncio.wait_for(asyncio.to_thread(entered.wait), 1)
                snapshot = await self.client.node_status()
                self.assertEqual(snapshot['reserved']['memory_mb'], 1024)
                self.assertEqual(next(iter(snapshot['leases'].values()))['state'], 'BOUND')
                with self.assertRaises(ServiceError) as busy:
                    await self.client.run_container(request_id='5'*32)
                self.assertEqual(busy.exception.kind, 'NodeAdmissionBusy')
                self.assertEqual(self.runtime.backends[('e1-real','local')].creates, 0)
            finally:
                finish.set()
                await asyncio.wait_for(creating, 2)
        await creating.result().stop()

    async def test_ttl_cleanup_releases_node_lease_without_worker_receipt(self):
        worker = await self.worker()
        result = await worker.dispatch({'operation':'create','args':{
            'task_id':'ttl','rollout_id':'4'*32}})
        sandbox = self.manager.sandboxes[result['sandbox_id']]
        sandbox.deadline = 0
        self.assertEqual((await worker.rollouts['4'*32].sandbox.status())['state'], 'STOPPED')
        self.assertEqual((await self.client.node_status())['reserved']['memory_mb'], 0)
        self.assertEqual(worker.scheduler.episode_quota.reserved, 1)
        await worker.dispatch({'operation':'stop','args':{'rollout_id':'4'*32}})
        self.assertEqual(worker.scheduler.episode_quota.reserved, 0)
        worker.store.lock.close()

    async def test_ready_checkout_transfers_lease_without_second_reservation(self):
        ready = self.manager.prewarm(environment_id='tb2-test', count=1)[0]
        before = await self.client.node_status()
        vm = await self.client.run_microvm(DSecMicroVMRunArgs(environment_id='tb2-test'), request_id='5'*32)
        after = await self.client.node_status()
        self.assertEqual(vm.id, ready['id'])
        self.assertEqual(after['reserved']['memory_mb'], before['reserved']['memory_mb'])
        self.assertEqual(after['reserved']['disk_mb'], before['reserved']['disk_mb'])
        self.assertEqual(before['reserved']['cpu'], .05)
        self.assertEqual(after['reserved']['cpu'], 1)
        self.assertEqual(after['lease_ids'], before['lease_ids'])
        self.assertEqual(len(self.boots), 1)
        lease = next(iter(after['leases'].values()))
        self.assertIn('5'*32, lease['requests'])
        self.assertEqual(lease['demand']['memory_mb'], 512)
        self.assertEqual(lease['configured_limits']['memory_mb'], 2048)
        await vm.stop()

    async def test_pool_directory_failure_releases_unbound_reservation(self):
        with patch('dsec.runtime.lifecycle.Sandbox', side_effect=OSError('directory full')):
            with self.assertRaisesRegex(OSError, 'directory full'):
                self.manager.prewarm(environment_id='tb2-test', count=1)
        snapshot = await self.client.node_status()
        self.assertEqual(snapshot['reserved']['memory_mb'], 0)
        self.assertEqual(snapshot['lease_ids'], [])
        self.assertEqual(self.boots, [])
        self.assertEqual(self.manager.sandboxes, {})
        ready = self.manager.prewarm(environment_id='tb2-test', count=1)[0]
        self.assertEqual(len(self.boots), 1)
        self.manager.sandboxes[ready['id']].stop()

    async def test_ready_checkout_waits_for_cpu_delta_without_consuming_vm(self):
        ready = self.manager.prewarm(environment_id='tb2-test', count=1)[0]
        container = await self.client.run_container(request_id='1'*32,
            resource_demand=NodeDemand(1.5, 512, 1024, 1))
        before = await self.client.node_status()
        with self.assertRaises(ServiceError) as busy:
            await self.client.run_microvm(DSecMicroVMRunArgs(environment_id='tb2-test'),
                                         request_id='2'*32)
        self.assertEqual(busy.exception.kind, 'NodeAdmissionBusy')
        self.assertIn('cpu_budget', busy.exception.details['reasons'])
        after = await self.client.node_status()
        self.assertEqual(after['reserved'], before['reserved'])
        self.assertEqual(set(after['lease_ids']), set(before['lease_ids']))
        self.assertTrue(self.manager.sandboxes[ready['id']].reserved)
        self.assertEqual((await self.client.lookup_request('2'*32))['state'], 'NOT_FOUND')
        await container.stop()
        vm = await self.client.run_microvm(DSecMicroVMRunArgs(environment_id='tb2-test'),
                                         request_id='2'*32)
        self.assertEqual(vm.id, ready['id'])
        self.assertEqual(len(self.boots), 1)
        await vm.stop()

    async def test_worker_waits_and_restart_restores_only_job_quota(self):
        vm = await self.client.run_microvm(request_id='6'*32)
        container = await self.client.run_container(request_id='7'*32)
        worker = await self.worker()
        create = asyncio.create_task(worker.dispatch({'operation':'create','args':{
            'task_id':'ordinary','rollout_id':'8'*32}}))
        try:
            for _ in range(100):
                if worker.scheduler.node_pending:
                    break
                await asyncio.sleep(.01)
            self.assertTrue(worker.scheduler.node_pending)
            self.assertIsNone(worker.scheduler.node_ledger)
            self.assertEqual(worker.scheduler.episode_quota.reserved, 1)
            self.assertEqual((await self.client.node_status())['reserved']['memory_mb'], 1024)
            await vm.stop()
            result = await asyncio.wait_for(create, 2)
            self.assertEqual(result['state'], 'ACTIVE')
            self.assertGreater(worker.scheduler.active['8'*32]['node_wait_seconds'], 0)
            worker.store.lock.close()
            restored = await self.worker()
            self.assertEqual(restored.scheduler.episode_quota.reserved, 1)
            self.assertIsNone(restored.scheduler.node_ledger)
            self.assertEqual((await self.client.node_status())['reserved']['memory_mb'], 1024)
            await restored.dispatch({'operation':'stop','args':{'rollout_id':'8'*32}})
            self.assertEqual((await self.client.node_status())['reserved']['memory_mb'], 512)
            self.assertEqual(restored.scheduler.episode_quota.reserved, 0)
            restored.store.lock.close()
        finally:
            if not create.done():
                create.cancel()
                await asyncio.gather(create, return_exceptions=True)
            worker.store.lock.close()
            await container.stop()

    async def test_cancelling_proven_node_wait_releases_job_without_touching_live_vms(self):
        one = await self.client.run_microvm(request_id='9'*32)
        two = await self.client.run_microvm(request_id='a'*32)
        worker = await self.worker()
        create = asyncio.create_task(worker.dispatch({'operation':'create','args':{
            'task_id':'cancel','rollout_id':'b'*32}}))
        for _ in range(100):
            if worker.scheduler.node_pending:
                break
            await asyncio.sleep(.01)
        self.assertTrue(worker.scheduler.node_pending)
        create.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await create
        self.assertEqual(worker.scheduler.episode_quota.reserved, 0)
        self.assertEqual(worker.rollouts['b'*32].state, 'FAILED')
        self.assertEqual((await self.client.node_status())['reserved']['memory_mb'], 1024)
        worker.store.lock.close()
        await one.stop()
        await two.stop()

    async def test_demand_identity_conflict_is_rejected_even_after_cached_create(self):
        hint = NodeDemand(1, 512, 1024, 1)
        vm = await self.client.run_microvm(request_id='c'*32, resource_demand=hint)
        with self.assertRaises(ServiceError) as conflict:
            await self.client.run_microvm(request_id='c'*32,
                resource_demand=NodeDemand(1, 768, 1024, 1))
        self.assertEqual(conflict.exception.kind, 'RequestConflict')
        self.assertEqual(len(self.boots), 1)
        await vm.stop()

    async def test_commit_after_boot_failure_retains_live_reservation_and_blocks_replay(self):
        # In the actual cold path a commit failure is cleaned up. Use the
        # request handler's final publication to inject after creation returned.
        original_created = self.node.created
        calls = 0
        def created(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise NodeLeaseUncertain('lost final create receipt')
            return original_created(*args, **kwargs)
        with patch.object(self.node, 'created', created):
            with self.assertRaises(RequestOutcomeUnknown):
                await self.client.run_microvm(request_id='d'*32)
        self.assertEqual(len(self.boots), 1)
        self.assertEqual((await self.client.node_status())['reserved']['memory_mb'], 512)
        self.server.journal = RequestJournal(self.manager.root)
        with self.assertRaises(ServiceError) as uncertain:
            await self.client.run_microvm(request_id='d'*32)
        self.assertEqual(uncertain.exception.kind, 'RequestUncertain')
        self.assertEqual(len(self.boots), 1)
        vm = next(iter(self.manager.sandboxes.values()))
        vm.stop()
        self.assertEqual((await self.client.node_status())['reserved']['memory_mb'], 0)

    async def test_cleanup_failure_after_stopped_keeps_lease_until_retry_finishes(self):
        vm = await self.client.run_microvm(request_id='e'*32)
        sandbox = self.manager.sandboxes[vm.id]
        shared = SimpleNamespace(release=unittest.mock.Mock(side_effect=[OSError('storage down'), None]))
        sandbox.overlaybd_store = SimpleNamespace(shared_layers=shared, source_for=lambda _:self.template)
        with self.assertRaises(ServiceError):
            await vm.stop(request_id='f'*32)
        self.assertEqual(sandbox.state, 'STOPPED')
        self.assertFalse(sandbox.resource_cleanup_complete)
        self.assertEqual((await self.client.node_status())['reserved']['memory_mb'], 512)
        await vm.stop(request_id='0'*32)
        self.assertTrue(sandbox.resource_cleanup_complete)
        self.assertEqual((await self.client.node_status())['reserved']['memory_mb'], 0)


class NodeRecoveryTests(unittest.TestCase):
    def test_restart_keeps_bound_unknown_and_over_budget_leases_without_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            b, sampler = NodeBudget.from_resource(budget()), FakeSampler()
            node = NodeAdmission(directory, b, sampler)
            node.activate()
            with node.request('microvm','1'*32,{'environment_id':'default'},
                              asdict(NodeDemand(1, 1024, 1024, 1))):
                key = node.allocate('microvm')
                node.bind(key, '123456abcdef')
            node.close()
            smaller = NodeBudget.from_resource(budget(memory_mb=512))
            recovered = NodeAdmission(directory, smaller, sampler)
            self.assertEqual(recovered.status()['reserved']['memory_mb'], 1024)
            recovered.reconcile_microvms(SimpleNamespace(sandboxes={}))
            self.assertEqual(recovered.status()['reserved']['memory_mb'], 1024)
            recovered.close()

    def test_fork_pre_admission_and_child_allocation_share_one_lease(self):
        with tempfile.TemporaryDirectory() as directory:
            node = NodeAdmission(directory, NodeBudget.from_resource(budget()), FakeSampler())
            node.activate()
            with node.request('microvm','3'*32,{'baseline_id':'123456abcdef'}):
                before_source = node.allocate('microvm')
                child = node.allocate('microvm', configured_limits={'memory_mb':2048})
                self.assertEqual(before_source, child)
                self.assertEqual(node.status()['reserved']['memory_mb'],512)
                node.bind(child, 'fedcba654321')
            self.assertEqual(node.records[child]['configured_limits'], {'memory_mb':2048})
            node.close()

    def test_failed_release_commit_never_frees_in_memory_reservation(self):
        with tempfile.TemporaryDirectory() as directory:
            node = NodeAdmission(directory, NodeBudget.from_resource(budget()), FakeSampler())
            node.activate()
            with node.request('microvm','2'*32,{}):
                key = node.allocate('microvm')
                node.bind(key,'123456abcdef')
            with patch('dsec.runtime.node_admission.atomic_json', side_effect=OSError('fsync')):
                with self.assertRaises(NodeLeaseUncertain):
                    node.stopped('microvm','123456abcdef')
            self.assertEqual(node.status()['reserved']['memory_mb'],512)
            node.stopped('microvm','123456abcdef')
            self.assertEqual(node.status()['reserved']['memory_mb'],0)
            node.close()

    def test_tb2_docker_outage_is_not_absence_proof(self):
        import tb2_backend
        with patch('tb2_backend._docker', return_value=SimpleNamespace(returncode=1,stdout='')):
            backend = tb2_backend.TB2ContainerBackend('test', 'sha256:'+'a'*64)
            self.assertFalse(backend.prove_stopped('1'*32))


if __name__ == '__main__':
    unittest.main()
