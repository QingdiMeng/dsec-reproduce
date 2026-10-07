"""MBPP reward integrity, candidate deadlines and isolated episode cleanup."""

import asyncio
import json
import importlib.util
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from dsec_mbpp_case import compare, dataset, reward, train
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
    def test_epoch_plan_rejects_dropped_tail_and_prepared_data_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            files = {}
            for split in ("train", "validation"):
                low, high = dataset.SPLITS[split]
                for suffix in ("jsonl", "parquet"):
                    data = "".join(json.dumps({"extra_info": {"task_id": i}}) + "\n" for i in range(low, high+1))
                    p = root / (split + "." + suffix)
                    p.write_text(data)
                    import hashlib
                    files[p.name] = hashlib.sha256(p.read_bytes()).hexdigest()
            (root / "manifest.json").write_text(json.dumps(dict(source_sha256=dataset.SOURCE_SHA256,
                reference_solutions_included=False, files=files, counts={"train":374,"validation":90})))
            args = SimpleNamespace(data=root, evaluation_split="validation", epochs=1,
                batch_size=32, steps=None, validation_samples=90, seed=42,
                response_length=8192, generation_concurrency=32)
            with self.assertRaisesRegex(ValueError, "drops"):
                train.plan(args)
            args.batch_size = 2
            self.assertEqual(train.plan(args)["steps"], 187)
            self.assertEqual(train.plan(args)["response_length"], 8192)
            self.assertEqual(train.plan(args)["generation_concurrency"], 32)
            args.prompt_length = 4096
            args.model, args.out = root, root / "run"
            args.worker_socket, args.environment_id = "worker.sock", "python-mbpp"
            args.evaluation_batch_size, args.checkpoint_every = 32, 20
            args.evaluate_before_train = True
            settings = dict(item.split("=", 1) for item in train.overrides(args, root / "agent.json"))
            self.assertEqual(settings["data.max_response_length"], "8192")
            self.assertEqual(settings["actor_rollout_ref.rollout.response_length"], "8192")
            self.assertEqual(settings["actor_rollout_ref.rollout.max_model_len"], "12288")
            self.assertEqual(settings["actor_rollout_ref.rollout.max_num_seqs"], "32")
            (root / "train.jsonl").write_text("changed")
            with self.assertRaisesRegex(ValueError, "changed"):
                train.plan(args)

    def test_comparison_keeps_zeros_and_rejects_incomplete_sample_groups(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "scores.jsonl"
            rows = [dict(gts=json.dumps(dict(source_sha256=dataset.SOURCE_SHA256,task_id=i)),
                         score=int(i == 11 and n == 0)) for i in [11,12] for n in range(8)]
            path.write_text("".join(json.dumps(r)+"\n" for r in rows))
            scores, _ = compare.read_scores(path, [11,12], 8)
            result = compare.summarize(scores)
            self.assertEqual(result["pass_at_1"], 1/16)
            self.assertEqual(result["pass_at_8"], .5)
            path.write_text("".join(json.dumps(r)+"\n" for r in rows[:-1]))
            with self.assertRaisesRegex(ValueError, "coverage"):
                compare.read_scores(path, [11,12], 8)

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
    async def test_concurrent_generation_failures_keep_their_own_request_evidence(self):
        class Server:
            async def generate(self, **kwargs):
                await asyncio.sleep(.02 if kwargs["prompt_ids"] == [1] else 0)
                raise RuntimeError("generation failed")
        class Parent:
            def __init__(self, server_manager):
                self.server_manager = server_manager
            async def run(self, sampling_params, **kwargs):
                return await self.server_manager.generate(
                    prompt_ids=kwargs["prompt_ids"], sampling_params=sampling_params)
        name = "verl.experimental.agent_loop.single_turn_agent_loop"
        spec = importlib.util.spec_from_file_location(
            "test_mbpp_verl_failed_agent", Path(reward.__file__).with_name("verl_agent.py"))
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {name: SimpleNamespace(SingleTurnAgentLoop=Parent)}):
            spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as tmp:
            agent = module.MBPPSingleTurnAgentLoop(Server(), evidence_dir=tmp)
            failures = await asyncio.gather(agent.run({}, prompt_ids=[1]),
                                            agent.run({}, prompt_ids=[2]), return_exceptions=True)
            self.assertTrue(all(isinstance(x, RuntimeError) for x in failures))
            records = [json.loads(p.read_text()) for p in Path(tmp).glob("*.json")]
            self.assertCountEqual([x["request"]["prompt_ids"] for x in records], [[1], [2]])

    async def test_native_loop_extension_preserves_parent_token_output_and_sampling_input(self):
        tokens, mask, logprobs = [1, 2], [1, 1], [-.2, -.3]
        calls = []
        class Server:
            async def generate(self, **kwargs):
                calls.append(kwargs)
                return SimpleNamespace(token_ids=tokens, log_probs=logprobs,
                                       stop_reason="length", extra_fields={})
        class Parent:
            def __init__(self, server_manager):
                self.server_manager = server_manager
            async def run(self, sampling_params, **kwargs):
                output = await self.server_manager.generate(prompt_ids=[3, 4], sampling_params=sampling_params)
                return SimpleNamespace(prompt_ids=[3, 4], response_ids=output.token_ids, response_mask=mask,
                                       response_logprobs=output.log_probs, extra_fields=output.extra_fields)
        name = "verl.experimental.agent_loop.single_turn_agent_loop"
        path = Path(reward.__file__).with_name("verl_agent.py")
        spec = importlib.util.spec_from_file_location("test_mbpp_verl_agent", path)
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {name: SimpleNamespace(SingleTurnAgentLoop=Parent)}):
            spec.loader.exec_module(module)
        params = {"temperature": .7, "top_p": .8, "top_k": 20}
        with tempfile.TemporaryDirectory() as tmp:
            result = await module.MBPPSingleTurnAgentLoop(Server(), evidence_dir=tmp).run(params)
            saved = json.loads(next(Path(tmp).glob('*.json')).read_text())
            self.assertEqual(saved['response_ids'], tokens)
            self.assertEqual(saved['response_mask'], mask)
            self.assertEqual(saved['response_logprobs'], logprobs)
            self.assertEqual(saved['generation_id'], result.extra_fields['dsec_generation_id'])
        self.assertEqual(params, {"temperature": .7, "top_p": .8, "top_k": 20})
        self.assertEqual(calls[0]["sampling_params"]["presence_penalty"], 1.5)
        self.assertEqual(calls[0]["sampling_params"]["min_p"], 0)
        self.assertIs(result.response_ids, tokens)
        self.assertIs(result.response_mask, mask)
        self.assertIs(result.response_logprobs, logprobs)
        self.assertEqual(result.extra_fields["dsec_finish_reason"], "length")

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
