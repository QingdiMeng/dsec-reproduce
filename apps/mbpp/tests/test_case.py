"""MBPP reward integrity, candidate deadlines and isolated episode cleanup."""

import asyncio
import json
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from dsec_mbpp_case import dataset, reward
from scheduled_dsec import ScheduledOutcomeUnknown


def truth():
    return {"schema": "dsec.mbpp.tests.v1", "task_id": 601,
            "source_sha256": dataset.SOURCE_SHA256, "test_setup_code": "",
            "test_list": ["assert add(1, 2) == 3", "assert add(-1, 1) == 0",
                          "assert add(0, 0) == 0"]}


def good_result():
    d = {"schema": "dsec.mbpp.verdict.v1", "task_id": 601, "score": 1,
         "candidate_exit_code": 0, "candidate_timed_out": False,
         "tests_completed": True, "test_count": 3, "output": ""}
    return {"exit_code": 0, "timed_out": False, "truncated": False,
            "output": reward.MARKER + json.dumps(d) + "\n"}


class CaseTests(unittest.TestCase):
    def test_rows_exclude_reference_solutions_and_preserve_tests(self):
        d = dataset.row({"task_id": 601, "text": "Add two integers",
                         "code": "REFERENCE_NOT_FOR_POLICY", "test_setup_code": "",
                         "test_list": truth()["test_list"]})
        self.assertNotIn("REFERENCE_NOT_FOR_POLICY", json.dumps(d))
        self.assertEqual(json.loads(d["reward_model"]["ground_truth"])["test_list"], truth()["test_list"])
        self.assertEqual(dataset.SPLITS["train"], (601, 974))
        self.assertEqual(dataset.SPLITS["test"], (11, 510))

    def test_dataset_drift_fails_before_creating_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "mbpp.jsonl"
            source.write_text('{"task_id": 1}\n')
            output = Path(tmp) / "staged"
            with self.assertRaisesRegex(ValueError, "pinned"):
                dataset.stage(source, output)
            self.assertFalse(output.exists())

    def test_parser_excludes_thinking_and_rejects_ambiguous_or_partial_code(self):
        self.assertEqual(reward.extract_code('<think>```python\nbad\n```</think>\n```python\ngood\n```'), 'good')
        for text in ['<think>unfinished', '```python\nx', '```python\nx\n```\n```python\ny\n```']:
            self.assertIsNone(reward.extract_code(text))

    @unittest.skipUnless(sys.platform.startswith("linux"), "guest driver requires Linux resource limits")
    def test_real_driver_success_assertion_failure_timeout_and_early_exit(self):
        # Deliberately small trusted fixtures, never downloaded model programs on the host.
        cases = [('def add(a,b): return a+b', 1, False),
                 ('def add(a,b): return 0', 0, False),
                 ('while True: pass', 0, True),
                 ('raise SystemExit(0)', 0, False),
                 ('import os; os._exit(0)', 0, False)]
        for code, score, timed_out in cases:
            with self.subTest(code=code):
                args = shlex.split(reward.verifier_command(code, truth(), .25))
                args[0] = sys.executable
                p = subprocess.run(args, capture_output=True, text=True, timeout=5)
                result = {"exit_code": p.returncode, "output": p.stdout,
                          "timed_out": False, "truncated": False}
                d = reward.read_verdict(result, 601)
                self.assertEqual(d["score"], score)
                self.assertEqual(d["candidate_timed_out"], timed_out)

    def test_missing_mismatched_or_truncated_evidence_is_not_zero(self):
        for changes in [{"timed_out": True}, {"truncated": True},
                        {"exit_code": 127}, {"output": ""}]:
            with self.subTest(changes=changes), self.assertRaises(RuntimeError):
                reward.read_verdict(dict(good_result(), **changes), 601)
        with self.assertRaises(RuntimeError):
            reward.read_verdict(good_result(), 602)


class FakeSandbox:
    state = "ACTIVE"

    def __init__(self, owner):
        self.owner = owner
        self.stopped = False

    async def run_shell(self, command, **kwargs):
        self.owner.actions.append(kwargs)
        if self.owner.failure:
            raise self.owner.failure
        return good_result()

    async def stop(self):
        self.stopped = True


class FakeClient:
    instances = []
    failure = None

    def __init__(self, socket):
        self.actions = []
        self.sandbox = FakeSandbox(self)
        self.instances.append(self)

    @staticmethod
    def new_rollout_id():
        import uuid
        return uuid.uuid4().hex

    async def open(self):
        return self

    async def create(self, **kwargs):
        self.create_args = kwargs
        return self.sandbox

    async def close(self):
        pass


class RewardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        FakeClient.instances = []
        FakeClient.failure = None

    async def score(self, path, solution='```python\ndef add(a,b): return a+b\n```'):
        return await reward.compute_score('mbpp-dsec', solution, truth(), {"task_id": 601},
                                          worker_socket='/test.sock', environment_id='python-mbpp',
                                          evidence_dir=path)

    async def test_samples_use_separate_episodes_and_cleanup_with_receipts(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(reward, 'ScheduledDSecClient', FakeClient):
            a, b = await asyncio.gather(self.score(tmp), self.score(tmp))
            self.assertNotEqual(a['dsec_rollout_id'], b['dsec_rollout_id'])
            self.assertEqual([c.create_args['task_id'] for c in FakeClient.instances], ['mbpp-601'] * 2)
            self.assertTrue(all(c.sandbox.stopped for c in FakeClient.instances))
            self.assertEqual(len(list(Path(tmp).glob('*.json'))), 2)
            self.assertEqual(a['score'], 1)

    async def test_invalid_model_format_records_zero_without_executing_thinking(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(reward, 'ScheduledDSecClient', FakeClient):
            d = await self.score(tmp, '<think>unfinished')
            self.assertEqual(d['score'], 0)
            self.assertEqual(d['dsec_reward_source'], 'model_format')
            self.assertEqual(FakeClient.instances, [])

    async def test_unknown_action_preserves_id_and_does_not_retry_or_hide_as_zero(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(reward, 'ScheduledDSecClient', FakeClient):
            FakeClient.failure = ScheduledOutcomeUnknown('a' * 32, 'step')
            with self.assertRaises(ScheduledOutcomeUnknown):
                await self.score(tmp)
            client = FakeClient.instances[0]
            self.assertEqual(len(client.actions), 1)
            self.assertFalse(client.sandbox.stopped)
            receipt = json.loads(next(Path(tmp).glob('*.json')).read_text())
            self.assertIn('cleanup_deferred', receipt)
            self.assertNotIn('score', receipt)

    async def test_infrastructure_failure_releases_vm_and_preserves_error(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(reward, 'ScheduledDSecClient', FakeClient):
            FakeClient.failure = RuntimeError('verifier failed')
            with self.assertRaisesRegex(RuntimeError, 'verifier failed'):
                await self.score(tmp)
            self.assertTrue(FakeClient.instances[0].sandbox.stopped)
            receipt = json.loads(next(Path(tmp).glob('*.json')).read_text())
            self.assertIn('verifier failed', receipt['error'])
            self.assertNotIn('score', receipt)


if __name__ == '__main__':
    unittest.main()
