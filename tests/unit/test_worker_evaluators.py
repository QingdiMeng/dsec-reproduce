"""Plugin verdict commits, interruption recovery and task dependency boundaries."""
import asyncio
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from dsec.contracts.evaluation import EvaluationFailure, EvaluationOutcome
from dsec.contracts.profiles import FrameworkProfile
from dsec.contracts.requests import request_digest
from dsec.rollout.worker import Rollout, RolloutWorker


class Sandbox:
    id = "sandbox-1"


class Client:
    def __init__(self):
        self._transport = self

    def call(self, operation, *args, **kwargs):
        if operation == "status":
            return {"state": "RUNNING"}
        raise AssertionError(operation)

    async def lookup_request(self, request_id):
        return self.proof


class Evaluator:
    id = "test-verifier-v1"

    def __init__(self, behavior="success"):
        self.calls = 0
        self.behavior = behavior

    def validate(self, context, parameters):
        if "target" not in parameters:
            raise ValueError("target is required")

    def accepts_reward(self, reward, parameters):
        return bool(reward and reward.get("target") == parameters["target"])

    async def evaluate(self, context, sandbox, parameters):
        self.calls += 1
        if context.evidence_directory:
            record = Path(context.evidence_directory).parents[1] / (context.rollout_id + '.json')
            saved = json.loads(record.read_text())
            assert saved['state'] == 'EXECUTING'
            assert saved['pending']['evaluator'] == self.id
        if self.behavior == "cancel":
            raise asyncio.CancelledError()
        if self.behavior == "failure":
            raise EvaluationFailure("no valid verdict", {"error": "verifier failed",
                                    "evidence": {"directory": context.evidence_directory}})
        if self.behavior == "mutated_failure":
            failure = EvaluationFailure("no valid verdict", {"error": "verifier failed"})
            failure.details['invalid'] = float('nan')
            raise failure
        if self.behavior == "invalid":
            return {"value": 1.0}
        outcome = EvaluationOutcome({"value": 1.0, "target": parameters['target']})
        if self.behavior == "mutated":
            outcome.reward['value'] = float('nan')
        if self.behavior == "mutate_parameters":
            parameters['target'] = 999
        return outcome


class WorkerEvaluatorTest(unittest.IsolatedAsyncioTestCase):
    def worker(self, evaluator, root=None, client=None):
        worker = RolloutWorker(client or Client(), state_dir=root,
                               evaluators={evaluator.id: evaluator})
        if worker.store is not None:
            self.addCleanup(worker.store.lock.close)
        return worker

    def add_rollout(self, worker):
        rollout = Rollout('a' * 32, 'custom-task', Sandbox(), FrameworkProfile(),
                          300, store=worker.store)
        worker.rollouts[rollout.id] = rollout
        if worker.store is not None:
            worker.store.reserve(rollout.record())
        return rollout

    async def evaluate(self, worker, rollout, *, target=7, evaluator=None):
        return await worker.dispatch({'operation': 'task_evaluate', 'args': {
            'rollout_id': rollout.id, 'evaluator': evaluator or 'test-verifier-v1',
            'parameters': {'target': target}}})

    async def test_completed_verdict_is_cached_and_identity_cannot_change(self):
        evaluator = Evaluator()
        worker = self.worker(evaluator)
        rollout = self.add_rollout(worker)
        first = await self.evaluate(worker, rollout)
        second = await self.evaluate(worker, rollout)
        self.assertEqual(first['reward'], second['reward'])
        self.assertEqual(evaluator.calls, 1)
        with self.assertRaisesRegex(ValueError, 'conflicts'):
            await self.evaluate(worker, rollout, target=8)
        replacement = Evaluator()
        replacement.id = 'another-verifier-v1'
        worker.evaluators[replacement.id] = replacement
        with self.assertRaisesRegex(ValueError, 'conflicts'):
            await self.evaluate(worker, rollout, evaluator=replacement.id)
        self.assertEqual(replacement.calls, 0)

    async def test_completed_verdict_survives_worker_restart_without_reexecution(self):
        with tempfile.TemporaryDirectory() as directory:
            evaluator = Evaluator()
            worker = self.worker(evaluator, directory)
            rollout = self.add_rollout(worker)
            first = await self.evaluate(worker, rollout)
            worker.store.lock.close()
            recovered = self.worker(evaluator, directory)
            await recovered.initialize()
            result = await self.evaluate(recovered, recovered.rollouts[rollout.id])
            self.assertEqual(result['reward'], first['reward'])
            self.assertEqual(result['evaluation'], first['evaluation'])
            self.assertEqual(evaluator.calls, 1)

    async def test_interrupted_verifier_stays_unknown_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            evaluator = Evaluator('cancel')
            worker = self.worker(evaluator, directory)
            rollout = self.add_rollout(worker)
            with self.assertRaises(asyncio.CancelledError):
                await self.evaluate(worker, rollout)
            self.assertEqual(rollout.state, 'UNKNOWN')
            worker.store.lock.close()
            recovered = self.worker(evaluator, directory)
            await recovered.initialize()
            restored = recovered.rollouts[rollout.id]
            with self.assertRaisesRegex(RuntimeError, 'UNKNOWN'):
                await self.evaluate(recovered, restored)
            self.assertFalse((await recovered._reconcile(restored))['reconciled'])
            self.assertIsNone(restored.reward)
            self.assertEqual(evaluator.calls, 1)

    async def test_invalid_outcome_and_failure_evidence_never_become_model_zero(self):
        for behavior in ('invalid', 'mutated', 'failure', 'mutated_failure'):
            with self.subTest(behavior=behavior), tempfile.TemporaryDirectory() as directory:
                evaluator = Evaluator(behavior)
                worker = self.worker(evaluator, directory)
                rollout = self.add_rollout(worker)
                with self.assertRaises((ValueError, RuntimeError)):
                    await self.evaluate(worker, rollout)
                saved = json.loads((worker.store.root / (rollout.id + '.json')).read_text())
                self.assertEqual(saved['state'], 'UNKNOWN')
                self.assertIsNone(saved['reward'])
                if behavior == 'failure':
                    self.assertEqual(saved['verifier_failure']['error'], 'verifier failed')
                    self.assertEqual(saved['verifier_failure']['evidence']['directory'],
                                     str(worker.store.root / 'evidence' / rollout.id))
                worker.store.lock.close()

    async def test_plugin_cannot_mutate_persisted_parameters(self):
        evaluator = Evaluator('mutate_parameters')
        worker = self.worker(evaluator)
        rollout = self.add_rollout(worker)
        result = await self.evaluate(worker, rollout)
        self.assertEqual(result['evaluation']['parameters'], {'target': 7})
        self.assertEqual(result['reward']['target'], 7)

    async def test_unregistered_rpc_cannot_select_python_code(self):
        evaluator = Evaluator()
        worker = self.worker(evaluator)
        rollout = self.add_rollout(worker)
        with self.assertRaisesRegex(RuntimeError, 'not configured'):
            await self.evaluate(worker, rollout, evaluator='os:system')
        self.assertEqual(rollout.state, 'ACTIVE')
        self.assertEqual(evaluator.calls, 0)

    async def test_invalid_parameters_are_rejected_before_verifier_submission(self):
        evaluator = Evaluator()
        worker = self.worker(evaluator)
        rollout = self.add_rollout(worker)
        for parameters in ({}, {"target": float('nan')}, []):
            with self.subTest(parameters=parameters), self.assertRaises(ValueError):
                await worker.dispatch({'operation': 'task_evaluate', 'args': {
                    'rollout_id': rollout.id, 'evaluator': evaluator.id,
                    'parameters': parameters}})
            self.assertEqual(rollout.state, 'ACTIVE')
            self.assertIsNone(rollout.pending)
        self.assertEqual(evaluator.calls, 0)

    async def test_reconcile_uses_original_transformed_command_after_policy_change(self):
        client = Client()
        worker = RolloutWorker(client, command_transform=lambda *_: (_ for _ in ()).throw(
            AssertionError('must not reinterpret an already submitted command')))
        rollout = self.add_rollout(worker)
        rollout.state = 'UNKNOWN'
        rollout.pending = {'operation': 'step', 'step_id': 0, 'action_id': 'read',
                           'command': 'cat /data', 'execution_command': 'OLD=1 cat /data',
                           'request_id': 'b' * 32}
        client.proof = {'state': 'DONE', 'operation': 'execute', 'sandbox_id': rollout.sandbox_id,
                        'digest': request_digest('execute', rollout.sandbox_id, {
                            'command': 'OLD=1 cat /data', 'timeout_ms': 5000, 'output_limit': 65536}),
                        'response': {'ok': True, 'result': {'exit_code': 0, 'output': 'ok'}}}
        self.assertTrue((await worker._reconcile(rollout))['reconciled'])
        self.assertEqual(rollout.history[0]['execution_command'], 'OLD=1 cat /data')
        self.assertEqual(rollout.history[0]['command'], 'cat /data')


class WorkerDependencyTest(unittest.TestCase):
    def test_generic_worker_import_and_configuration_do_not_load_task_code(self):
        code = '''
import importlib.abc, sys, tempfile
from pathlib import Path
class BlockTasks(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith(('dsec_tb21_case', 'dsec_adapters.tb2', 'tb2_backend')):
            raise ModuleNotFoundError('task package unavailable', name=fullname)
sys.meta_path.insert(0, BlockTasks())
from dsec.rollout.worker import RolloutWorker
RolloutWorker(None)
with tempfile.TemporaryDirectory() as directory:
    root = Path(directory) / 'state'
    try:
        RolloutWorker(None, state_dir=root, tb2_tasks_dir=directory)
    except RuntimeError as exc:
        assert 'optional application' in str(exc)
    else:
        raise AssertionError('missing task package must be reported')
    assert not root.exists(), 'plugin precheck must run before allocating worker state'
'''
        subprocess.run([sys.executable, '-c', code], check=True, capture_output=True)
