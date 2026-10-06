"""Worker-owned policy context survives trainer and worker reconnection."""

import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from pathlib import Path
from unittest.mock import patch

from framework_profile import FrameworkProfile
from request_journal import request_digest
from rollout_workerd import Rollout, RolloutWorker


class FakeTransport:
    def __init__(self):
        self.calls = 0

    def call(self, operation, sandbox_id=None, **kwargs):
        if operation == "status":
            return {"state": "RUNNING"}
        if operation == "execute":
            self.calls += 1
            return {"exit_code": 0, "output": f"observation {self.calls}\n"}
        raise AssertionError(operation)


class FakeClient:
    def __init__(self, transport):
        self._transport = transport

    async def lookup_request(self, request_id):
        return self.proof


class FakeSandbox:
    id = "sandbox-1"

    def __init__(self, transport):
        self.transport = transport

    async def run_shell(self, command, **kwargs):
        return self.transport.call("execute", self.id, command=command, **kwargs)


class DialogueRecoveryTest(unittest.IsolatedAsyncioTestCase):
    async def test_reconnect_preserves_context_and_deduplicates_command(self):
        with tempfile.TemporaryDirectory() as directory:
            transport = FakeTransport()
            first = RolloutWorker(FakeClient(transport), state_dir=Path(directory))
            rollout = Rollout("a" * 32, "task", FakeSandbox(transport),
                              FrameworkProfile(), 300, store=first.store)
            first.store.reserve(rollout.record())
            first.rollouts[rollout.id] = rollout
            seed = [{"role": "system", "content": "Use one shell command."},
                    {"role": "user", "content": "Create an answer file."}]
            started = await first.dispatch({"operation": "dialogue_start", "args": {
                "rollout_id": rollout.id, "messages": seed}})
            self.assertEqual(started["messages"], seed)
            assistant = {"role": "assistant", "content": "```bash\necho answer > /app/answer\n```"}
            args = {"rollout_id": rollout.id, "step_id": 0, "action_id": "turn-0",
                    "assistant_message": assistant, "command": "echo answer > /app/answer",
                    "timeout_ms": 120000, "output_limit": 65536}
            first_result = await first.dispatch({"operation": "agent_step", "args": args})
            self.assertEqual(transport.calls, 1)
            self.assertEqual(first_result["dialogue"]["messages"][-2:],
                             [assistant, {"role": "user", "content": "observation 1\n"}])
            first.store.lock.close()

            # A new trainer attaches to a restarted worker after the response
            # to the first trainer was lost. It sees the same sandbox/context.
            second = RolloutWorker(FakeClient(transport), state_dir=Path(directory))
            await second.initialize()
            context = await second.dispatch({"operation": "dialogue_status", "args": {
                "rollout_id": rollout.id}})
            self.assertEqual(context["sandbox_id"], FakeSandbox.id)
            self.assertEqual(context["messages"], first_result["dialogue"]["messages"])
            replay = await second.dispatch({"operation": "agent_step", "args": args})
            self.assertTrue(replay["replayed_result"])
            self.assertEqual(transport.calls, 1)
            with self.assertRaisesRegex(ValueError, "conflicts"):
                await second.dispatch({"operation": "agent_step", "args": {
                    **args, "assistant_message": {"role": "assistant", "content": "different"}}})
            second.store.lock.close()

    async def test_pending_model_reply_survives_worker_reconciliation(self):
        with tempfile.TemporaryDirectory() as directory:
            transport = FakeTransport()
            client = FakeClient(transport)
            worker = RolloutWorker(client, state_dir=Path(directory))
            rollout = Rollout("b" * 32, "task", FakeSandbox(transport),
                              FrameworkProfile(), 300, store=worker.store)
            rollout.dialogue_seed = [{"role": "user", "content": "task"}]
            message = {"role": "assistant", "content": "```bash\necho done\n```"}
            rollout.state = "UNKNOWN"
            rollout.pending = {"operation": "step", "step_id": 0, "action_id": "turn-0",
                               "command": "echo done", "timeout_ms": 120000,
                               "output_limit": 65536, "request_id": "c" * 32,
                               "assistant_message": message}
            worker.store.reserve(rollout.record())
            worker.store.lock.close()
            client.proof = {"state": "DONE", "operation": "execute",
                            "sandbox_id": FakeSandbox.id,
                            "digest": request_digest("execute", FakeSandbox.id,
                                                     {"command": "echo done",
                                                      "timeout_ms": 120000,
                                                      "output_limit": 65536}),
                            "response": {"ok": True, "result": {
                                "exit_code": 0, "output": "done\n"}}}
            recovered = RolloutWorker(client, state_dir=Path(directory))
            await recovered.initialize()
            context = await recovered.dispatch({"operation": "dialogue_status", "args": {
                "rollout_id": rollout.id}})
            self.assertEqual(context["messages"][-2:],
                             [message, {"role": "user", "content": "done\n"}])
            self.assertEqual(context["next_step"], 1)
            recovered.store.lock.close()

    async def test_official_verdict_is_required_before_reward_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "regex-log").mkdir()
            transport = FakeTransport()
            profile = FrameworkProfile(environment="erofs_layers",
                                       environment_id="tb2-regex-log")
            worker = RolloutWorker(FakeClient(transport), state_dir=root / "state",
                                   tb2_tasks_dir=root)
            rollout = Rollout("d" * 32, "regex-log", FakeSandbox(transport),
                              profile, 300, store=worker.store)
            worker.store.reserve(rollout.record())
            worker.rollouts[rollout.id] = rollout
            calls = []

            class ValidEnv:
                def __init__(self, sandbox, **kwargs):
                    self.sandbox = sandbox
                    calls.append(kwargs["task_id"])
                    self.evidence_dir = kwargs["evidence_dir"]

                async def reset(self, *, task_id):
                    return None

                async def step(self, action):
                    return SimpleNamespace(reward=0.0, observation=SimpleNamespace(
                        error="", info={"harness": "tests/test.sh",
                                        "evidence": {"directory": str(self.evidence_dir)}}))

            fake = ModuleType("tb2_microvm_env")
            fake.TB2MicroVMEnv = ValidEnv
            with patch.dict("sys.modules", {"dsec_adapters.tb2_microvm_env": fake}):
                first = await worker.dispatch({"operation": "tb2_evaluate", "args": {
                    "rollout_id": rollout.id}})
                second = await worker.dispatch({"operation": "tb2_evaluate", "args": {
                    "rollout_id": rollout.id}})
            self.assertEqual(first["reward"]["value"], 0.0)
            self.assertEqual(second["reward"], first["reward"])
            self.assertEqual(calls, ["regex-log"])
            worker.store.lock.close()

            failing = RolloutWorker(FakeClient(transport), state_dir=root / "other",
                                    tb2_tasks_dir=root)
            broken = Rollout("e" * 32, "regex-log", FakeSandbox(transport),
                             profile, 300, store=failing.store)
            failing.store.reserve(broken.record())
            failing.rollouts[broken.id] = broken

            class InvalidEnv(ValidEnv):
                async def step(self, action):
                    return SimpleNamespace(reward=None, observation=SimpleNamespace(
                        error="verifier failed", info={"harness": "tests/test.sh"}))

            fake.TB2MicroVMEnv = InvalidEnv
            with patch.dict("sys.modules", {"dsec_adapters.tb2_microvm_env": fake}):
                with self.assertRaisesRegex(RuntimeError, "no valid verdict"):
                    await failing.dispatch({"operation": "tb2_evaluate", "args": {
                        "rollout_id": broken.id}})
            self.assertEqual(broken.state, "UNKNOWN")
            self.assertIsNone(broken.reward)
            failing.store.lock.close()

            no_evidence = RolloutWorker(FakeClient(transport),
                                        state_dir=root / "missing-evidence",
                                        tb2_tasks_dir=root)
            incomplete = Rollout("f" * 32, "regex-log", FakeSandbox(transport),
                                 profile, 300, store=no_evidence.store)
            no_evidence.store.reserve(incomplete.record())
            no_evidence.rollouts[incomplete.id] = incomplete

            class NoEvidenceEnv(ValidEnv):
                async def step(self, action):
                    return SimpleNamespace(reward=0.0, observation=SimpleNamespace(
                        error="", info={"harness": "tests/test.sh"}))

            fake.TB2MicroVMEnv = NoEvidenceEnv
            with patch.dict("sys.modules", {"dsec_adapters.tb2_microvm_env": fake}):
                with self.assertRaisesRegex(RuntimeError, "not durably exported"):
                    await no_evidence.dispatch({"operation": "tb2_evaluate", "args": {
                        "rollout_id": incomplete.id}})
            self.assertEqual(incomplete.state, "UNKNOWN")
            self.assertIsNone(incomplete.reward)
            no_evidence.store.lock.close()


if __name__ == "__main__":
    unittest.main()
