"""Prepared-state safety boundary and pre-resume drive rebinding."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from microvm import MicroVM
from sandbox_sdk import SandboxError
from snapshot_fork import seal_baseline, fork_baseline, identity_command, _validate_snapshot
from libdsec_compat import DSecMicroVMRunArgs
from rollout_workerd import RolloutWorker
from tests.unit.test_rollout_scheduler import Client, scheduler


class ForkTests(unittest.TestCase):
    def test_drive_and_vsock_rebound_before_resume(self):
        vm = MicroVM('/fc', '/child')
        calls = []
        vm.start_process = lambda name: calls.append(('start', name))
        vm.api = lambda *args: calls.append(args)
        vm.resume = lambda: calls.append(('resume',))
        vm.wait_ready = lambda: calls.append(('ready',))
        vm.restore_fork('/state', '/memory', '/child/disk', tap='tap0')
        load = calls[1][2]
        self.assertFalse(load['resume_vm'])
        self.assertEqual(load['vsock_override']['uds_path'], '/child/v.sock')
        self.assertEqual(load['network_overrides'], [{'iface_id': 'eth0', 'host_dev_name': 'tap0'}])
        self.assertEqual(calls[2], ('PATCH', '/drives/rootfs',
                                  {'drive_id': 'rootfs', 'path_on_host': '/child/disk'}))
        self.assertEqual(calls[3], ('resume',))

    def test_no_resume_if_drive_rebind_fails(self):
        vm = MicroVM('/fc', '/child')
        vm.start_process = Mock()
        vm.api = Mock(side_effect=[None, RuntimeError('PATCH failed')])
        vm.resume = Mock()
        with self.assertRaisesRegex(RuntimeError, 'PATCH failed'):
            vm.restore_fork('/state', '/memory', '/child/disk')
        vm.resume.assert_not_called()

    def test_invalid_or_unsealed_source_has_no_admission(self):
        manager = Mock(sandboxes={})
        with self.assertRaises(ValueError):
            fork_baseline(manager, '../bad', 300, 'default', 'baseline', None, 'local')
        with self.assertRaisesRegex(SandboxError, 'Unknown baseline'):
            fork_baseline(manager, 'a'*12, 300, 'default', 'baseline', None, 'local')
        manager._create_cold.assert_not_called()

    def test_explicit_prepare_permission(self):
        sb = Mock()
        import threading
        sb.lock = threading.RLock()
        with self.assertRaises(ValueError):
            seal_baseline(sb)
        sb.pause.assert_not_called()

    def test_corrupt_memory_rejected(self):
        import hashlib
        import json
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binary = root/'fc'
            binary.write_bytes(b'fc')
            (root/'memory').write_bytes(b'clean memory')
            (root/'state').write_bytes(b'state')
            manifest = {'id':'a'*12, 'generation':1, 'snapshot_mode':'full',
                        'binary_sha256':hashlib.sha256(b'fc').hexdigest(),
                        'guest_kernel':'/kernel', 'environment_manifest_sha256':None,
                        'files':{'memory':hashlib.sha256(b'clean memory').hexdigest(),
                                 'state':hashlib.sha256(b'state').hexdigest()}}
            (root/'manifest.json').write_text(json.dumps(manifest))
            source = SimpleNamespace(snapshot=root, id='a'*12, generation=1,
                         vm=SimpleNamespace(binary=binary), kernel='/kernel',
                         environment_manifest_sha256=None, overlaybd_store=None)
            _validate_snapshot(source)
            (root/'memory').write_bytes(b'corrupt memory')
            with self.assertRaisesRegex(SandboxError, 'integrity mismatch: memory'):
                _validate_snapshot(source)

    def test_live_connection_rejected_before_snapshot(self):
        import threading
        sb = Mock()
        sb.lock = threading.RLock()
        sb.baseline_sealed = False
        sb.state = 'RUNNING'
        sb.reserved = False
        sb.snapshot_mode = 'full'
        sb.network_mode = 'netns'
        sb.vm.execute.return_value = {'exit_code': 1, 'output': 'live TCP connection'}
        with self.assertRaisesRegex(SandboxError, 'not quiescent'):
            seal_baseline(sb, allow_prepared_state=True)
        sb.pause.assert_not_called()

    def test_entropy_command_unique_and_injection_rejected(self):
        a, b = identity_command('a'*12), identity_command('a'*12)
        self.assertNotEqual(a, b)
        self.assertIn('fcntl.ioctl(fd, 0x5207', a)
        with self.assertRaises(ValueError):
            identity_command("'; evil")

    def test_run_spec_pins_baseline_in_create_digest(self):
        plain = DSecMicroVMRunArgs().service_args()
        fork = DSecMicroVMRunArgs(baseline_id='a'*12).service_args()
        self.assertEqual(fork, {**plain, 'baseline_id': 'a'*12})


class WorkerForkTests(unittest.IsolatedAsyncioTestCase):
    async def test_restart_restores_baseline_fields_and_checks_core_seal(self):
        from framework_profile import FrameworkProfile
        from rollout_workerd import Rollout
        from rollout_store import RolloutStore
        with tempfile.TemporaryDirectory() as tmp:
            client = Client()
            source_id, child_id = 'a'*32, 'b'*32
            source_sid, child_sid = 'a'*12, 'b'*12
            client._transport.states.update({source_sid:'PAUSED', child_sid:'RUNNING'})
            original = client._transport.call
            confirmed = True
            def call(op, sandbox_id=None, **kw):
                result = original(op, sandbox_id, **kw)
                if op == 'status' and sandbox_id == source_sid:
                    result['baseline_sealed'] = confirmed
                return result
            client._transport.call = call
            store = RolloutStore(tmp)
            source = Rollout(source_id, 'test', None, FrameworkProfile(), 300,
                             sandbox_id=source_sid, store=store)
            source.state = 'PAUSED'
            source.baseline_sealed = True
            child = Rollout(child_id, 'test', None, FrameworkProfile(), 300,
                            sandbox_id=child_sid, store=store)
            child.baseline_rollout_id = source_id
            child.baseline_sandbox_id = source_sid
            store.reserve(source.record())
            store.reserve(child.record())
            store.lock.close()
            worker = RolloutWorker(client, state_dir=tmp)
            await worker.initialize()
            self.assertTrue(worker.rollouts[source_id].baseline_sealed)
            self.assertEqual(worker.rollouts[source_id].state, 'PAUSED')
            restored = worker.rollouts[child_id]
            self.assertEqual((restored.baseline_rollout_id, restored.baseline_sandbox_id),
                             (source_id, source_sid))
            worker.store.lock.close()
            confirmed = False
            worker = RolloutWorker(client, state_dir=tmp)
            await worker.initialize()
            self.assertEqual(worker.rollouts[source_id].state, 'UNKNOWN')
            worker.store.lock.close()

    async def test_seal_then_fork_starts_fresh_history_and_rejects_source_actions(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = Client()
            captured = []
            original = client.run_microvm
            async def create(spec, **kwargs):
                captured.append(spec.service_args())
                sb = await original(spec, **kwargs)
                sb.id = sb.id[:12]
                client._transport.states[sb.id] = "RUNNING"
                async def seal(**kwargs):
                    return {'state': 'PAUSED', 'baseline_sealed': True}
                sb.seal_baseline = seal
                return sb
            client.run_microvm = create
            # Configure both resource and job owners before any admission.
            budget = scheduler(cpu=2, memory_mb=1024, disk_mb=2048, api_episode_slots=2)
            worker = RolloutWorker(client, state_dir=Path(tmp), scheduler=budget)
            source_id, child_id = 'a'*32, 'b'*32
            source = await worker.dispatch({'operation':'create', 'args':{
                'task_id':'test', 'rollout_id':source_id}})
            source_record = worker.rollouts[source_id]
            source_record.dialogue_seed = [{'role':'user','content':'policy episode'}]
            with self.assertRaisesRegex(ValueError, 'before policy'):
                await worker.dispatch({'operation':'seal_baseline', 'args':{
                    'rollout_id':source_id, 'allow_prepared_state':True}})
            source_record.dialogue_seed = None
            source_record.history = [{'trusted_setup': True}]
            source_record.next_step = 1
            sealed = await worker.dispatch({'operation':'seal_baseline', 'args':{
                'rollout_id':source_id, 'allow_prepared_state':True}})
            self.assertTrue(sealed['baseline_sealed'])
            with self.assertRaisesRegex(RuntimeError, 'immutable'):
                await worker.dispatch({'operation':'step', 'args':{'rollout_id':source_id}})
            fork = await worker.dispatch({'operation':'create', 'args':{
                'task_id':'test', 'rollout_id':child_id, 'baseline_rollout_id':source_id}})
            self.assertEqual(fork['next_step'], 0)
            self.assertEqual(fork['history'], [])
            self.assertIsNone(fork['dialogue_seed'])
            self.assertIsNone(fork['reward'])
            self.assertEqual(captured[1]['baseline_id'], source['sandbox_id'])
            self.assertEqual(fork['baseline_rollout_id'], source_id)
            with self.assertRaisesRegex(ValueError, 'same task/profile'):
                await worker.dispatch({'operation':'create', 'args':{
                    'task_id':'different', 'rollout_id':'c'*32, 'baseline_rollout_id':source_id}})
            await worker.dispatch({'operation':'stop', 'args':{'rollout_id':source_id}})
            again = await worker.dispatch({'operation':'create', 'args':{
                'task_id':'test', 'rollout_id':child_id, 'baseline_rollout_id':source_id}})
            self.assertEqual(again['sandbox_id'], fork['sandbox_id'])
            self.assertEqual(len(captured), 2)
            for rid in [child_id, source_id]:
                await worker.dispatch({'operation':'stop', 'args':{'rollout_id':rid}})
            worker.store.lock.close()


if __name__ == '__main__':
    unittest.main()
