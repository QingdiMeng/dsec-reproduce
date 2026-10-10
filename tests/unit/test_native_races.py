"""Controlled interleavings of real journals, lifecycle and native C commands.

Events hold a specific boundary; timeouts only detect a hung test. The optional
TLC runner replays these observations against the original specifications.
Docker/KVM drivers are instrumented here, never claimed as Linux acceptance.
"""
from contextlib import ExitStack
from dataclasses import asdict
import json
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from dsec.contracts.errors import CommandOutcomeUnknown, ServiceBusy
from dsec.contracts.sandbox import DSecContainerRunArgs
from dsec.runtime.container_edge import ContainerRuntime
from dsec.runtime.sessions import service as service_module
from dsec.runtime.sessions.native import NativeChannel
from tests.unit import test_native_sdk as sdk_fixture
from tests.unit import test_edge_assembly as edge_fixture
from tests.unit import test_container_edge as container_fixture


class NativeRaceTests(unittest.TestCase):
    traces = {}

    @classmethod
    def setUpClass(cls):
        sdk_fixture.NativeSDKTests.setUpClass()

    @classmethod
    def tearDownClass(cls):
        sdk_fixture.NativeSDKTests.tearDownClass()

    def sdk(self):
        fixture = sdk_fixture.NativeSDKTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        return fixture

    def edge(self):
        fixture = edge_fixture.EdgeAssemblyTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture, fixture.open()

    def save(self, name, model, observations):
        self.traces[name] = dict(model=model, observed=observations)

    def test_queued_cancel_prevents_dispatch_and_survives_query(self):
        f = self.sdk()
        sid = f.session()
        jobs = f.server.stream_jobs()
        op = 'a' * 32
        arrived, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        effect = f.root / 'queued-effect'
        observations, reply = [], 'NONE'
        queued_cancel = False
        def observe(job):
            proof = f.server.journal.lookup(op)
            response = proof.get('response') or {}
            result = response.get('result', {})
            observations.append(dict(phase=job['phase'], intent=job['cancel_requested'].is_set(),
                reply=reply, queuedCancel=queued_cancel, executed=effect.exists(),
                result=('CANCELLED' if result.get('cancelled') else 'SUCCESS')
                    if proof['state'] == 'DONE' else proof['state']))
        def checkpoint(operation, stage, job):
            nonlocal queued_cancel
            if operation != op:
                return
            if stage == 'cancel_intent':
                queued_cancel = job['phase'] == 'QUEUED'
            observe(job)
            if stage == 'before_dispatch':
                arrived.set()
                if not release.wait(5):
                    raise AssertionError('dispatch barrier timed out')
        original_stream = NativeChannel.stream
        def observed_stream(channel, **values):
            for event in original_stream(channel, **values):
                if values.get('operation_id') == op:
                    observe(jobs.active[op])
                yield event
        with patch.object(jobs, '_checkpoint', side_effect=checkpoint), patch.object(NativeChannel, 'stream', observed_stream):
            jobs.start(dict(request_id=op, operation='native_stream', sandbox_id=f.sb.id,
                args=dict(backend='microvm', action='stream', session_id=sid,
                          command=f"printf x > '{effect}'")))
            self.assertTrue(arrived.wait(5))
            job = jobs.active[op]
            try:
                response = jobs.cancel(f.sb.id, dict(lookup_id=op, session_id=sid), 'b' * 32)
                reply = str(response['cancel_requested']).upper()
                observe(job)
                durable = f.server.journal.lookup(op).get('cancel_requested', False)
            finally:
                release.set()
            job['thread'].join(5)
            self.assertFalse(job['thread'].is_alive())
        proof = f.server.journal.lookup(op)
        self.save('queued-cancel', 'QueueCancellation', observations)
        self.assertTrue(response['cancel_requested'])
        self.assertTrue(durable)
        self.assertEqual(proof['state'], 'DONE')
        self.assertTrue(proof['response']['result']['cancelled'])
        self.assertFalse(effect.exists())
        # Stable ID reattachment never executes the cancelled command.
        jobs.start(dict(request_id=op, operation='native_stream', sandbox_id=f.sb.id,
            args=dict(backend='microvm', action='stream', session_id=sid,
                      command=f"printf x > '{effect}'")))
        self.assertFalse(effect.exists())
        self.assertEqual(f.channel.call('run', session_id=sid, command='printf alive')['stdout'], 'alive')

    def test_cancel_retries_exact_id_when_dispatch_is_in_transit(self):
        f = self.sdk()
        sid = f.session()
        jobs = f.server.stream_jobs()
        op = 'c' * 32
        arrived, release, missed = [threading.Event() for _ in range(3)]
        self.addCleanup(release.set)
        original_stream, original_call = NativeChannel.stream, NativeChannel.call
        calls = []
        def delayed(channel, **values):
            if values.get('operation_id') == op:
                arrived.set()
                if not release.wait(5):
                    raise AssertionError('wire barrier timed out')
            yield from original_stream(channel, **values)
        def cancelling(channel, action, **values):
            if action == 'cancel':
                calls.append(values['operation_id'])
            try:
                return original_call(channel, action, **values)
            except FileNotFoundError:
                missed.set()
                raise
        with patch.object(NativeChannel, 'stream', delayed), patch.object(NativeChannel, 'call', cancelling):
            jobs.start(dict(request_id=op, operation='native_stream', sandbox_id=f.sb.id,
                args=dict(backend='microvm', action='stream', session_id=sid,
                          command='printf started; sleep 10', timeout_ms=20000)))
            self.assertTrue(arrived.wait(5))
            job = jobs.active[op]
            try:
                response = jobs.cancel(f.sb.id, dict(lookup_id=op, session_id=sid), 'd' * 32)
                self.assertTrue(missed.wait(5), 'cancel must reach the pre-admission window')
            finally:
                release.set()
            job['thread'].join(5)
        self.assertFalse(job['thread'].is_alive())
        self.assertTrue(response['cancel_requested'])
        self.assertGreaterEqual(len(calls), 2)
        self.assertEqual(set(calls), {op})
        proof = f.server.journal.lookup(op)
        self.assertTrue(proof['response']['result']['cancelled'])
        self.assertFalse(jobs.active)
        self.assertFalse(f.sb.native_inflight)

    def test_committed_result_cannot_be_replaced_by_cancel_or_cleanup_error(self):
        f = self.sdk()
        jobs, sid = f.server.stream_jobs(), f.session()
        op = 'e' * 32
        effect = f.root / 'committed-effect'
        observed = []
        def checkpoint(operation, stage, job):
            if operation == op and stage == 'committed':
                observed.append(f.server.journal.lookup(op))
                raise OSError('injected cleanup after durable commit')
        request = dict(request_id=op, operation='native_stream', sandbox_id=f.sb.id,
            args=dict(backend='microvm', action='stream', session_id=sid,
                      command=f"printf x > '{effect}'"))
        with patch.object(jobs, '_checkpoint', side_effect=checkpoint):
            jobs.start(request)
            jobs.close()
        before = f.server.journal.lookup(op)
        self.assertEqual(before['state'], 'DONE')
        self.assertTrue(observed)
        self.assertFalse(jobs.cancel(f.sb.id, dict(lookup_id=op, session_id=sid), 'f'*32)['cancel_requested'])
        self.assertEqual(f.server.journal.lookup(op), before)
        self.assertEqual(effect.read_bytes(), b'x')

    def callback_case(self, replace):
        f, manager = self.edge()
        sb = manager.create()
        if replace:
            sb.pause()
            sb.resume()
        base_epoch = sb.native_incarnation
        admitted, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        observations, exceptions = [], []
        stopped, unknown, stale_retired = False, False, False
        op_epoch = base_epoch
        def observe():
            observations.append(dict(life=sb.state, epoch=sb.native_incarnation-base_epoch,
                opEpoch=op_epoch-base_epoch, active=bool(sb.native_inflight), stopped=stopped,
                unknown=unknown, obsolete=unknown and sb.native_incarnation != op_epoch,
                staleRetired=stale_retired))
        observe()
        def checkpoint(stage, **details):
            nonlocal unknown, stale_retired
            if stage == 'outcome_unknown':
                unknown = True
                stale_retired = (sb.native_incarnation != op_epoch
                    and observations[-1]['life'] == 'RUNNING' and sb.state == 'FAILED')
            observe()
        sb._native_checkpoint = checkpoint
        server = SimpleNamespace(manager=manager)
        def execute():
            try:
                with service_module.native_operation(server, sb.id,
                        dict(backend='microvm', action='run', session_id='1'*32, command='hold'), '2'*32):
                    admitted.set()
                    if not release.wait(5):
                        raise AssertionError('callback barrier timed out')
                    raise CommandOutcomeUnknown('controlled lost reply')
            except Exception as exc:
                exceptions.append(exc)
        with patch.object(NativeChannel, 'capabilities', return_value=[]):
            thread = threading.Thread(target=execute)
            thread.start()
            try:
                self.assertTrue(admitted.wait(5))
                if replace:
                    with sb.lock:
                        sb._fail('controlled old process retirement')
                    observe()
                    sb.recover(allow_rollback=True)
                    observe()
                    replacement = sb.vm.process
                else:
                    sb.stop()
                    stopped = True
                    observe()
            finally:
                release.set()
                thread.join(5)
        name = 'old-incarnation' if replace else 'stop-before-callback'
        self.save(name, 'NativeCallbackFence', observations)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(exceptions), 1)
        self.assertIsInstance(exceptions[0], CommandOutcomeUnknown)
        self.assertEqual(sb.state, 'RUNNING' if replace else 'STOPPED')
        self.assertFalse(sb.native_inflight)
        if replace:
            self.assertTrue(replacement.alive)
            persisted = json.loads((sb.directory/'registry.json').read_text())
            self.assertEqual(persisted['native_incarnation'], base_epoch+1)
            self.assertEqual(persisted['native_inflight'], {})
            manager.detach()
            adopted = f.open().sandboxes[sb.id]
            self.assertEqual(adopted.state, 'RUNNING')
            self.assertEqual(adopted.native_incarnation, base_epoch+1)
            self.assertFalse(adopted.native_inflight)
            self.assertTrue(replacement.alive)
        else:
            self.assertTrue(sb.resource_cleanup_complete)
            with sb.lock:
                sb._fail('another late failure')
            self.assertEqual(sb.state, 'STOPPED')

    def test_stop_is_final_despite_late_unknown_callback(self):
        self.callback_case(False)

    def test_old_incarnation_callback_cannot_stop_replacement(self):
        self.callback_case(True)

    def container(self):
        import tempfile
        temp = tempfile.TemporaryDirectory(prefix='dsec-race-', dir='/tmp')
        self.addCleanup(temp.cleanup)
        backend_patch = patch('dsec.runtime.container_edge.LayeredContainerBackend', container_fixture.Backend)
        backend_patch.start()
        self.addCleanup(backend_patch.stop)
        runtime = ContainerRuntime(dict(DSEC_CONTAINER_ROOT=str(Path(temp.name)/'containers'),
            DSEC_CONTAINER_AGENT='/fixture/agent', DSEC_CONTAINER_ARTIFACTS='/fixture/artifacts',
            DSEC_CONTAINER_IMAGE='sha256:'+'c'*64))
        self.addCleanup(runtime.close)
        spec = DSecContainerRunArgs()
        args = dict(spec=asdict(spec), kind='container')
        sid = '1'*32
        runtime.dispatch('container_create', None, args, sid)
        return runtime, spec, args, sid

    def test_container_native_readers_exclude_stop_and_owner_close(self):
        runtime, spec, args, sid = self.container()
        readers = set()
        observations = []
        stopping, rejected = False, False
        def observe():
            backend = runtime._backend('container', spec)
            observations.append(dict(active=sorted(readers), stopping=stopping,
                stopped=backend.prove_stopped(sid), rejected=rejected))
        observe()
        def checkpoint(stage, sandbox_id, **details):
            nonlocal rejected
            if stage == 'admitted':
                readers.add(int(details['request_id'][0]))
            elif stage == 'released':
                # Observe the actual token map after removal, not a model step.
                readers.intersection_update(int(r[0]) for r in runtime.native_activity.get(sid, {}).values())
            elif stage == 'stop_rejected':
                rejected = True
            observe()
        runtime._native_checkpoint = checkpoint
        entry = runtime._entry('container', spec, sid)
        stop = entry.sandbox.stop
        def stopping_backend():
            nonlocal stopping
            stopping = True
            observe()
            result = stop()
            stopping = False
            observe()
            return result
        with patch.object(entry.sandbox, 'stop', side_effect=stopping_backend):
            with ExitStack() as stack:
                stack.enter_context(runtime.native_operation('container', spec, sid, '1'*32))
                stack.enter_context(runtime.native_operation('container', spec, sid, '2'*32))
                self.assertEqual(len(runtime.native_activity[sid]), 2)
                busy = False
                try:
                    runtime.dispatch('container_stop', sid, args, '3'*32)
                except ServiceBusy:
                    busy = True
                with self.assertRaises(ServiceBusy):
                    runtime.close()
                owner_held = runtime.owner_lock is not None and not runtime.owner_lock.closed
                proof = runtime.lookup_request('3'*32)
            runtime.dispatch('container_stop', sid, args, '4'*32)
        self.save('container-native-first', 'ContainerNativeGate', observations)
        self.assertTrue(busy)
        self.assertTrue(owner_held)
        self.assertEqual(proof['state'], 'NOT_FOUND')
        self.assertFalse(runtime.native_activity)
        with self.assertRaises(RuntimeError):
            with runtime.native_operation('container', spec, sid, '5'*32):
                self.fail('stopped container admitted work')

    def test_container_stop_first_rejects_new_native_admission(self):
        runtime, spec, args, sid = self.container()
        entry = runtime._entry('container', spec, sid)
        stop = entry.sandbox.stop
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        observations = [dict(active=[], stopping=False, stopped=False, rejected=False)]
        errors = []
        def backend_stop():
            observations.append(dict(active=[], stopping=True, stopped=False, rejected=False))
            entered.set()
            if not release.wait(5):
                raise AssertionError('stop barrier timed out')
            result = stop()
            observations.append(dict(active=[], stopping=False, stopped=True, rejected=True))
            return result
        def stop_thread():
            try:
                runtime.dispatch('container_stop', sid, args, '3'*32)
            except Exception as exc:
                errors.append(exc)
        with patch.object(entry.sandbox, 'stop', side_effect=backend_stop):
            thread = threading.Thread(target=stop_thread)
            thread.start()
            try:
                self.assertTrue(entered.wait(5))
                with self.assertRaises(ServiceBusy):
                    with runtime.native_operation('container', spec, sid, '1'*32):
                        self.fail('work entered while stop held admission')
                observations.append(dict(active=[], stopping=True, stopped=False, rejected=True))
            finally:
                release.set()
                thread.join(5)
        self.save('container-stop-first', 'ContainerNativeGate', observations)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertFalse(runtime.native_activity)


if __name__ == '__main__':
    unittest.main()
