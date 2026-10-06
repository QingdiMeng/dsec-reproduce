"""Step options are part of durable identity and unknown-result recovery."""

import unittest

from framework_profile import FrameworkProfile
from request_journal import request_digest
from rollout_workerd import Rollout, RolloutWorker


class FakeSandbox:
    id = "sandbox-1"

    async def run_shell(self, command, **kwargs):
        self.last = (command, kwargs)
        return {"exit_code": 0, "output": "ok\n"}


class FakeBackend:
    async def lookup_request(self, request_id):
        return self.proof


class StepOptionsTest(unittest.IsolatedAsyncioTestCase):
    async def test_tb2_microvm_restores_image_path(self):
        sandbox = FakeSandbox()
        profile = FrameworkProfile(environment="erofs_layers",
                                   environment_id="tb2-kv-store-grpc",
                                   verifier_storage="local")
        rollout = Rollout("b" * 32, "kv-store-grpc", sandbox, profile, 300)
        worker = RolloutWorker(FakeBackend())
        await worker._step(rollout, {"step_id": 0, "action_id": "pip",
                                     "command": "command -v pip"})
        actual = sandbox.last[0]
        self.assertTrue(actual.startswith("export PATH=/usr/local/sbin:/usr/local/bin:"))
        self.assertTrue(actual.endswith("command -v pip"))
        self.assertEqual(rollout.history[0]["command"], "command -v pip")

    async def test_options_are_passed_and_replay_must_match(self):
        sandbox = FakeSandbox()
        rollout = Rollout("a" * 32, "task", sandbox, FrameworkProfile(), 300)
        worker = RolloutWorker(FakeBackend())
        result = await worker._step(rollout, {"step_id": 0, "action_id": "read",
                                              "command": "cat /data", "timeout_ms": 12000,
                                              "output_limit": 4096})
        self.assertEqual(sandbox.last[1]["timeout_ms"], 12000)
        self.assertEqual(result["entry"]["output_limit"], 4096)
        replay = await worker._step(rollout, {"step_id": 0, "action_id": "read",
                                              "command": "cat /data", "timeout_ms": 12000,
                                              "output_limit": 4096})
        self.assertTrue(replay["replayed_result"])
        with self.assertRaisesRegex(ValueError, "conflicts"):
            await worker._step(rollout, {"step_id": 0, "action_id": "read",
                                         "command": "cat /data", "timeout_ms": 5000,
                                         "output_limit": 4096})

    async def test_reconcile_uses_persisted_options(self):
        backend = FakeBackend()
        sandbox = FakeSandbox()
        rollout = Rollout("a" * 32, "task", sandbox, FrameworkProfile(), 300)
        rollout.state = "UNKNOWN"
        rollout.pending = {"operation": "step", "step_id": 0, "action_id": "read",
                           "command": "cat /data", "timeout_ms": 12000,
                           "output_limit": 4096, "request_id": "b" * 32}
        backend.proof = {"state": "DONE", "operation": "execute",
                         "sandbox_id": sandbox.id,
                         "digest": request_digest("execute", sandbox.id,
                                                  {"command": "cat /data",
                                                   "timeout_ms": 12000,
                                                   "output_limit": 4096}),
                         "response": {"ok": True,
                                      "result": {"exit_code": 0, "output": "ok\n"}}}
        worker = RolloutWorker(backend)
        recovered = await worker._reconcile(rollout)
        self.assertTrue(recovered["reconciled"])
        self.assertEqual(rollout.history[0]["timeout_ms"], 12000)
        self.assertEqual(rollout.next_step, 1)


if __name__ == "__main__":
    unittest.main()
