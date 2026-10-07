"""Worker-owned policy context survives trainer and worker reconnection."""

import tempfile
import json
from types import ModuleType, SimpleNamespace
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_environment import format_shell_observation, SHELL_OUTPUT_CHAR_CAP
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
            self.assertEqual(first_result["dialogue"]["messages"][-2], assistant)
            self.assertEqual(first_result["dialogue"]["messages"][-1]["role"], "user")
            feedback = json.loads(first_result["dialogue"]["messages"][-1]["content"])
            self.assertEqual((feedback["exit_code"], feedback["output"]),
                             (0, "observation 1\n"))
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

    async def test_legacy_context_remains_identical_after_upgrade_and_next_step(self):
        with tempfile.TemporaryDirectory() as directory:
            transport = FakeTransport()
            worker = RolloutWorker(FakeClient(transport), state_dir=Path(directory))
            rollout = Rollout("c" * 32, "task", FakeSandbox(transport),
                              FrameworkProfile(), 300, store=worker.store)
            rollout.dialogue_seed = [{"role": "user", "content": "task"}]
            assistant = {"role": "assistant", "content": "```bash\nexit 7\n```"}
            rollout.history = [{"step_id": 0, "action_id": "turn-0", "command": "exit 7",
                                "assistant_message": assistant,
                                "result": {"exit_code": 7, "output": ""}}]
            rollout.next_step = 1
            old_record = rollout.record()
            del old_record["dialogue_feedback_version"]
            worker.store.reserve(old_record)
            worker.store.lock.close()
            recovered = RolloutWorker(FakeClient(transport), state_dir=Path(directory))
            await recovered.initialize()
            context = await recovered.dispatch({"operation": "dialogue_status", "args": {
                "rollout_id": rollout.id}})
            self.assertEqual(context["dialogue_feedback_version"], 1)
            self.assertEqual(context["messages"], rollout.dialogue_seed + [
                assistant, {"role": "user", "content": "(no output)"}])
            next_result = await recovered.dispatch({"operation": "agent_step", "args": {
                "rollout_id": rollout.id, "step_id": 1, "action_id": "turn-1",
                "assistant_message": assistant, "command": "echo done"}})
            self.assertEqual(next_result["dialogue"]["messages"][:3], context["messages"])
            self.assertEqual(next_result["dialogue"]["messages"][-1]["content"], "observation 1\n")
            self.assertEqual(recovered.store.load()[rollout.id]["dialogue_feedback_version"], 1)
            recovered.store.lock.close()

    async def test_timeout_feedback_survives_worker_restart_without_reexecution(self):
        class TimeoutSandbox(FakeSandbox):
            async def run_shell(self, command, **kwargs):
                self.transport.calls += 1
                return {"exit_code": 124, "timed_out": True,
                        "output": "", "truncated": False}

        with tempfile.TemporaryDirectory() as directory:
            transport = FakeTransport()
            worker = RolloutWorker(FakeClient(transport), state_dir=Path(directory))
            rollout = Rollout("9" * 32, "task", TimeoutSandbox(transport),
                              FrameworkProfile(), 300, store=worker.store)
            rollout.dialogue_seed = [{"role": "user", "content": "task"}]
            worker.store.reserve(rollout.record())
            worker.rollouts[rollout.id] = rollout
            args = {"rollout_id": rollout.id, "step_id": 0, "action_id": "turn-0",
                    "assistant_message": {"role": "assistant", "content": "sleep 2"},
                    "command": "sleep 2", "timeout_ms": 1000}
            first = await worker.dispatch({"operation": "agent_step", "args": args})
            feedback = json.loads(first["dialogue"]["messages"][-1]["content"])
            self.assertEqual((feedback["status"], feedback["exit_code"], feedback["timed_out"]),
                             ("timed_out", 124, True))
            worker.store.lock.close()
            recovered = RolloutWorker(FakeClient(transport), state_dir=Path(directory))
            await recovered.initialize()
            replay = await recovered.dispatch({"operation": "agent_step", "args": args})
            self.assertEqual(replay["dialogue"], first["dialogue"])
            self.assertEqual(transport.calls, 1)
            recovered.store.lock.close()

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
            self.assertEqual(context["messages"][-2], message)
            feedback = json.loads(context["messages"][-1]["content"])
            self.assertEqual((feedback["exit_code"], feedback["output"]), (0, "done\n"))
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
                        error="verifier failed", info={"harness": "tests/test.sh",
                            "verifier_stage": "validating_verdict",
                            "evidence": {"directory": str(self.evidence_dir)}}))

            fake.TB2MicroVMEnv = InvalidEnv
            with patch.dict("sys.modules", {"dsec_adapters.tb2_microvm_env": fake}):
                with self.assertRaisesRegex(RuntimeError, "no valid verdict"):
                    await failing.dispatch({"operation": "tb2_evaluate", "args": {
                        "rollout_id": broken.id}})
            self.assertEqual(broken.state, "UNKNOWN")
            self.assertIsNone(broken.reward)
            saved = json.loads((failing.store.root / (broken.id + ".json")).read_text())
            self.assertEqual(saved["verifier_failure"]["error"], "verifier failed")
            self.assertEqual(saved["verifier_failure"]["evidence"]["directory"],
                             str(failing.store.root / "evidence" / broken.id))
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


class ShellFeedbackTest(unittest.TestCase):
    def feedback(self, **result):
        return json.loads(format_shell_observation({
            "step_id": 0, "action_id": "turn-0", "result": result}))

    def test_empty_success_failure_and_timeout_are_distinguishable(self):
        for result, status in [({"exit_code": 0}, "succeeded"),
                               ({"exit_code": 7}, "failed"),
                               ({"exit_code": 124, "timed_out": True}, "timed_out")]:
            with self.subTest(status=status):
                feedback = self.feedback(output="", **result)
                self.assertEqual(feedback["status"], status)
                self.assertEqual(feedback["exit_code"], result["exit_code"])
                self.assertEqual(feedback["output"], "")

    def test_missing_status_is_unknown_and_exit_124_alone_is_not_timeout(self):
        feedback = self.feedback(output="no status")
        self.assertEqual(feedback["status"], "unknown")
        for key in ("exit_code", "timed_out", "capture_truncated"):
            self.assertIsNone(feedback[key])
        self.assertEqual(self.feedback(exit_code=124, output="")["status"], "failed")
        self.assertEqual(self.feedback(exit_code=True, output="")["status"], "unknown")

    def test_long_output_keeps_terminal_error_and_reports_both_truncations(self):
        output = "BEGIN\\n" + "x" * 10000 + "\\nFATAL: final error"
        feedback = self.feedback(exit_code=2, timed_out=False, truncated=True, output=output)
        self.assertTrue(feedback["output"].startswith("BEGIN\\n"))
        self.assertTrue(feedback["output"].endswith("FATAL: final error"))
        self.assertLessEqual(len(feedback["output"]), SHELL_OUTPUT_CHAR_CAP)
        self.assertTrue(feedback["capture_truncated"])
        self.assertTrue(feedback["feedback_truncated"])
        self.assertEqual(feedback["captured_output_chars"], len(output))
        self.assertGreater(feedback["feedback_omitted_chars"], 6000)

    def test_exact_output_cap_does_not_truncate_or_modify_control_characters(self):
        output = 'quote=" newline=\n control=\x00 unicode=错误 '
        output += "x" * (SHELL_OUTPUT_CHAR_CAP - len(output))
        feedback = self.feedback(exit_code=0, timed_out=False, truncated=False, output=output)
        self.assertEqual(feedback["output"], output)
        self.assertFalse(feedback["feedback_truncated"])
        self.assertEqual(feedback["feedback_omitted_chars"], 0)


if __name__ == "__main__":
    unittest.main()
