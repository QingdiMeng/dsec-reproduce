"""Miles adapter consumes the worker transcript and drops resumed policy samples."""

import json
import os
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

import dsec_adapters.miles_dsec_agent_function as adapter
from scheduled_dsec import ScheduledOutcomeUnknown
from agent_environment import format_shell_observation


class Message:
    content = "```bash\necho answer\n```"

    def model_dump(self, exclude_none=True):
        return {"role": "assistant", "content": self.content}


class Sandbox:
    id = "a" * 32
    sandbox_id = "vm-1"
    state = "ACTIVE"

    def __init__(self, prior=False, result=None):
        self.prior = prior
        self.calls = []
        self.seed = None
        self.result = result if result is not None else {"exit_code": 0, "output": "ok\n"}

    async def start_dialogue(self, messages):
        self.seed = messages

    async def dialogue(self):
        messages = list(self.seed)
        if self.prior:
            messages += [{"role": "assistant", "content": "```bash\necho earlier\n```"},
                         {"role": "user", "content": "earlier output\n"}]
        return {"state": "ACTIVE", "next_step": int(self.prior),
                "messages": messages, "pending": None}

    async def agent_step(self, command, **kwargs):
        self.calls.append((command, kwargs))
        entry = {"step_id": kwargs["step_id"], "action_id": kwargs["action_id"],
                 "result": self.result}
        return {"entry": entry,
                "dialogue": {"state": "ACTIVE", "next_step": int(self.prior) + 1,
                             "messages": self.seed + [kwargs["assistant_message"],
                                {"role": "user", "content": format_shell_observation(entry)}]}}

    async def tb2_evaluate(self):
        return {"value": 0.0, "harness": "tests/test.sh", "task_id": "regex-log"}

    async def evaluate_counter(self, expected):
        return {"value": 1.0, "expected_counter": expected,
                "observed": str(expected), "verifier_exit_code": 0}

    async def stop(self):
        self.state = "STOPPED"


class Client:
    def __init__(self, sandbox):
        self.sandbox = sandbox

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def create(self, **kwargs):
        self.create_args = kwargs
        return self.sandbox


class MilesAdapterTest(unittest.IsolatedAsyncioTestCase):
    async def test_episode_budget_zero_preserves_tito_and_finishes_without_next_action(self):
        for phase in ("policy", "command"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as directory:
                clock = [0.0]

                class TimedSandbox(Sandbox):
                    async def agent_step(self, command, **kwargs):
                        clock[0] += kwargs["timeout_ms"] / 1000 + 0.1
                        return await super().agent_step(command, **kwargs)

                    async def evaluate_counter(self, expected):
                        raise AssertionError("Budget zero must not pretend to be a verifier score")

                class QueuedClient(Client):
                    async def create(self, **kwargs):
                        clock[0] += 100  # Scheduling wait is outside agent budget.
                        return await super().create(**kwargs)

                sandbox = TimedSandbox(result={"exit_code": 124, "timed_out": True, "output": ""})

                async def policy_call(*_args):
                    clock[0] += 6 if phase == "policy" else 1
                    return SimpleNamespace(choices=[SimpleNamespace(
                        message=Message(), finish_reason="stop")])

                async def fetch(_url):
                    return {"session_id": "test", "records": [{
                        "request": {"messages": [], "input_ids": [1]},
                        "response": {"choices": [{"message": {"role": "assistant", "content": Message.content},
                            "finish_reason": "stop", "meta_info": {
                                "completion_tokens": 1, "output_token_logprobs": [[-0.1, 2, None]]}}]}}]}

                with patch.dict(os.environ, {
                        "DSEC_ROLLOUT_WORKER_SOCKET": "/tmp/unused.sock",
                        "DSEC_AGENT_STEP_TIMEOUT_MS": "30000", "OPENENV_MAX_TURNS": "2",
                        "DSEC_QWEN35_THINKING": "0", "OPENENV_MAX_ROLLOUT_TIME_SECONDS": "5",
                        "DSEC_TITO_AUDIT_DIR": directory}), patch.object(
                            adapter, "time", SimpleNamespace(monotonic=lambda: clock[0])):
                    reward, metrics = await adapter.run_episode(
                        SimpleNamespace(base_url="http://session/sessions/test/v1"), "model", [], {}, {
                            "task_id": "counter-example", "dsec_environment": "counter"},
                        client_factory=lambda _: QueuedClient(sandbox), policy_call=policy_call,
                        tito_fetch=fetch)
                result = {"reward": reward, "exit_status": "timeout", "agent_metrics": metrics}
                accepted = adapter.canonical_training_result(result, {"dsec_environment": "counter"})
                self.assertTrue(accepted["dsec_budget_verdict"])
                self.assertEqual(reward, 0.0)
                self.assertEqual(metrics["end_reason"], "episode_timeout")
                self.assertTrue(metrics["trajectory_complete"])
                self.assertIsNone(metrics["verifier_diagnostic"]["harness"])
                self.assertEqual(metrics["reset_time"], 100)
                self.assertLess(metrics["agent_elapsed_seconds"], 10)
                self.assertEqual(len(sandbox.calls), int(phase == "command"))
                if sandbox.calls:
                    self.assertEqual(sandbox.calls[0][1]["timeout_ms"], 4000)
                self.assertEqual(sandbox.state, "STOPPED")
                evidence = json.loads(Path(metrics["tito_path"]).read_text())
                self.assertEqual(len(evidence["records"]), 1)
                self.assertEqual(evidence["records"][0]["response"]["output_token_logprobs"],
                                 [[-0.1, 2, None]])
                metrics["trajectory_complete"] = False
                self.assertIsNone(adapter.canonical_training_result(result, {"dsec_environment": "counter"}))

    async def test_empty_failure_and_timeout_reach_next_policy_request(self):
        for result, status in [({"exit_code": 7, "output": "", "timed_out": False}, "failed"),
                               ({"exit_code": 124, "output": "", "timed_out": True}, "timed_out")]:
            with self.subTest(status=status):
                sandbox = Sandbox(result=result)
                seen = []

                async def policy_call(_policy, _model, messages, _kwargs):
                    seen.append(messages)
                    message = Message()
                    if len(seen) > 1:
                        feedback = json.loads(messages[-1]["content"])
                        self.assertEqual(feedback["status"], status)
                        self.assertEqual(feedback["exit_code"], result["exit_code"])
                        self.assertEqual(feedback["timed_out"], result["timed_out"])
                        message.content = "TASK_COMPLETE"
                    return SimpleNamespace(choices=[SimpleNamespace(
                        message=message, finish_reason="stop")])

                with patch.dict(os.environ, {
                        "DSEC_ROLLOUT_WORKER_SOCKET": "/tmp/unused.sock",
                        "DSEC_AGENT_STEP_TIMEOUT_MS": "30000", "OPENENV_MAX_TURNS": "2",
                        "DSEC_QWEN35_THINKING": "0"}):
                    await adapter.run_episode(None, "model", [], {}, {
                        "task_id": "counter-example", "dsec_environment": "counter"},
                        client_factory=lambda _: Client(sandbox), policy_call=policy_call)
                self.assertEqual(len(seen), 2)
                self.assertEqual(len(sandbox.calls), 1)

    async def test_records_job_ownership_before_failed_create(self):
        class FailingClient(Client):
            async def create(self, **kwargs):
                record = Path(directory) / (kwargs["rollout_id"] + ".json")
                self_record = json.loads(record.read_text())
                if self_record["worker_socket"] != "/tmp/formal.sock":
                    raise AssertionError("Ownership was not bound to the worker")
                raise RuntimeError("lost create reply")

        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {
                    "DSEC_ROLLOUT_WORKER_SOCKET": "/tmp/formal.sock",
                    "DSEC_EPISODE_REGISTRY_DIR": directory,
                    "DSEC_AGENT_STEP_TIMEOUT_MS": "30000"}):
                with self.assertRaisesRegex(RuntimeError, "lost create reply"):
                    await adapter.run_episode(
                        None, "model", [], {}, {
                            "task_id": "counter-example", "dsec_environment": "counter",
                            "dsec_rollout_id": "a" * 32},
                        client_factory=lambda _: FailingClient(Sandbox()))
            self.assertEqual(len(list(Path(directory).glob("*.json"))), 1)

    def test_first_final_bash_fence_is_one_turn(self):
        self.assertEqual(adapter._strip_fence("```bash\necho ok\n```"), "echo ok")
        self.assertEqual(adapter._strip_fence(
            "I will run this:\n```bash\necho ok\n```\n```bash\necho later\n```"),
            "echo ok")
        self.assertIsNone(adapter._strip_fence("<think>echo bad</think>"))

    def test_qwen_thinking_prefill_exposes_only_final_answer(self):
        raw = "I should inspect the file.\n</think>\n```bash\ncat /app/regex.txt\n```"
        with patch.dict(os.environ, {"DSEC_QWEN35_THINKING": "1"}):
            final = adapter._final_reply(raw)
            self.assertEqual(final, "```bash\ncat /app/regex.txt\n```")
            self.assertEqual(adapter._strip_fence(final), "cat /app/regex.txt")
            self.assertEqual(adapter._final_reply("thought</think>more</think>answer"),
                             "answer")

    def test_repeated_thinking_boundaries_never_discard_candidate_actions(self):
        with patch.dict(os.environ, {"DSEC_QWEN35_THINKING": "1"}):
            for raw in (
                    "thought</think>```bash\necho first\n```</think>```bash\necho second\n```",
                    "thought</think>```bash\nprintf '</think>'\n```",
                    "thought</think><tool_call>first</tool_call></think>```bash\necho second\n```",
                    "thought</think>TASK_COMPLETE</think>```bash\necho second\n```",
                    "thought</think>more</think><think>unfinished"):
                with self.subTest(raw=raw):
                    self.assertIsNone(adapter._final_reply(raw))
            # A server that already separated reasoning owns that boundary.
            self.assertEqual(adapter._final_reply("plain answer", "reasoning"),
                             "plain answer")

    async def test_duplicate_thinking_close_keeps_raw_context_and_diagnostic(self):
        raw = ("Review the implementation.</think>\nLet me fix it.\n</think>\n"
               "```bash\necho repaired\n```")
        sandbox = Sandbox()
        seen = []

        async def policy_call(_policy, _model, messages, _kwargs):
            seen.append(messages)
            message = Message()
            message.content = raw if len(seen) == 1 else "TASK_COMPLETE"
            return SimpleNamespace(choices=[SimpleNamespace(
                message=message, finish_reason="stop")])

        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {
                    "DSEC_ROLLOUT_WORKER_SOCKET": "/tmp/unused.sock",
                    "DSEC_AGENT_STEP_TIMEOUT_MS": "30000", "OPENENV_MAX_TURNS": "2",
                    "DSEC_QWEN35_THINKING": "1", "DSEC_MODEL_OUTPUT_DIR": directory}):
                reward, metrics = await adapter.run_episode(None, "model", [], {}, {
                    "task_id": "counter-example", "dsec_environment": "counter"},
                    client_factory=lambda _: Client(sandbox), policy_call=policy_call)
            record = json.loads(next(Path(directory).glob("*/0000.json")).read_text())
            self.assertEqual(record["reply"], raw)
            self.assertEqual(record["thinking_end_tag_count"], 2)
            self.assertTrue(record["thinking_boundary_normalized"])
        self.assertEqual(reward, 1.0)
        self.assertEqual(metrics["end_reason"], "task_complete")
        self.assertEqual(metrics["normalized_thinking_steps"], [0])
        self.assertEqual(len(sandbox.calls), 1)
        self.assertEqual(sandbox.calls[0][0], "echo repaired")
        self.assertEqual(seen[1][-2]["content"], raw)
        self.assertEqual(sandbox.state, "STOPPED")

    async def test_thinking_multiblock_executes_first_and_echoes_raw_history(self):
        raw = ("I should write a file.</think>\nFirst draft:\n"
               "```bash\necho first\n```\nA later idea:\n"
               "```bash\necho second\n```")

        class ReplyMessage:
            def __init__(self, content):
                self.content = content
                self.reasoning_content = None

            def model_dump(self, exclude_none=True):
                return {"role": "assistant", "content": self.content}

        with tempfile.TemporaryDirectory() as directory:
            task = Path(directory) / "regex-log"
            (task / "tests").mkdir(parents=True)
            (task / "environment").mkdir()
            (task / "environment" / "Dockerfile").write_text("FROM scratch\nWORKDIR /app\n")
            (task / "task.toml").write_text("")
            (task / "tests" / "test.sh").write_text("")
            (task / "instruction.md").write_text("Do the task.")
            sandbox = Sandbox()
            seen_messages = []

            async def policy_call(_policy, _model, messages, _kwargs):
                seen_messages.append(messages)
                content = raw if len(seen_messages) == 1 else "TASK_COMPLETE"
                return SimpleNamespace(choices=[SimpleNamespace(
                    message=ReplyMessage(content), finish_reason="stop")])

            env = {"OPENENV_TB2_TASKS_DIR": directory,
                   "DSEC_ROLLOUT_WORKER_SOCKET": "/tmp/unused.sock",
                   "DSEC_QWEN35_THINKING": "1",
                   "OPENENV_MAX_TURNS": "2"}
            with patch.dict(os.environ, env):
                reward, metrics = await adapter.run_episode(
                    None, "model", [], {}, {"task_id": "regex-log"},
                    client_factory=lambda _: Client(sandbox),
                    policy_call=policy_call)
            self.assertEqual(reward, 0.0)
            self.assertEqual(metrics["tool_calls"], 1)
            self.assertEqual(sandbox.calls[0][0], "cd /app && echo first")
            self.assertEqual(sandbox.calls[0][1]["assistant_message"]["content"], raw)
            self.assertEqual(seen_messages[1][-2]["content"], raw)
            feedback = json.loads(seen_messages[1][-1]["content"])
            self.assertEqual((feedback["status"], feedback["exit_code"], feedback["output"]),
                             ("succeeded", 0, "ok\n"))

    async def test_lost_agent_step_reply_recovers_without_replaying_command(self):
        class LostReplySandbox(Sandbox):
            def __init__(self):
                super().__init__()
                self.completed_step = False

            async def agent_step(self, command, **kwargs):
                self.calls.append((command, kwargs))
                self.completed_step = True
                raise ScheduledOutcomeUnknown(self.id, "agent_step")

            async def dialogue(self):
                return {"state": "ACTIVE", "next_step": int(self.completed_step),
                        "messages": list(self.seed), "pending": None}

        with tempfile.TemporaryDirectory() as directory:
            task = Path(directory) / "regex-log"
            (task / "tests").mkdir(parents=True)
            (task / "environment").mkdir()
            (task / "environment" / "Dockerfile").write_text("FROM scratch\nWORKDIR /app\n")
            (task / "task.toml").write_text("")
            (task / "tests" / "test.sh").write_text("")
            (task / "instruction.md").write_text("Do the task.")
            sandbox = LostReplySandbox()

            async def policy_call(*_args):
                return SimpleNamespace(choices=[SimpleNamespace(
                    message=Message(), finish_reason="stop")])

            env = {"OPENENV_TB2_TASKS_DIR": directory,
                   "DSEC_ROLLOUT_WORKER_SOCKET": "/tmp/unused.sock",
                   "OPENENV_MAX_TURNS": "1",
                   "DSEC_AGENT_STEP_TIMEOUT_MS": "30000"}
            with patch.dict(os.environ, env):
                reward, metrics = await adapter.run_episode(
                    None, "model", [], {}, {"task_id": "regex-log"},
                    client_factory=lambda _: Client(sandbox), policy_call=policy_call)
            self.assertEqual(reward, 0.0)
            self.assertEqual(metrics["tool_calls"], 1)
            self.assertEqual(len(sandbox.calls), 1)

    async def test_miles_entry_marks_only_complete_fresh_verdicts(self):
        fake = ModuleType("openenv_agent_function")
        answer = {"reward": 0.0, "exit_status": "completed",
                  "agent_metrics": {"resumed_after_trainer_exit": False,
                                    "verifier_diagnostic": {"harness": "tests/test.sh"}}}

        async def run_for_training(*_args, **kwargs):
            self.assertTrue(kwargs["manages_episode_budget"])
            return dict(answer)

        fake.run_for_training = run_for_training
        with patch.dict(sys.modules, {"dsec_adapters.openenv_agent_function": fake}):
            result = await adapter.run("http://session", "prompt")
            self.assertTrue(result["dsec_canonical_verdict"])
            answer["exit_status"] = "timeout"
            self.assertIsNone(await adapter.run("http://session", "prompt"))
            answer["exit_status"] = "completed"
            answer["agent_metrics"]["resumed_after_trainer_exit"] = True
            self.assertIsNone(await adapter.run("http://session", "prompt"))

    async def test_miles_counter_episode_uses_non_tb2_plugin(self):
        sandbox = Sandbox()
        client = Client(sandbox)

        async def policy_call(_policy, _model, messages, _kwargs):
            self.assertIn("/rl-counter", messages[-1]["content"])
            message = SimpleNamespace(
                content="```bash\nprintf 3 > /rl-counter\n```",
                model_dump=lambda exclude_none=True: {
                    "role": "assistant", "content": "```bash\nprintf 3 > /rl-counter\n```"})
            return SimpleNamespace(choices=[SimpleNamespace(
                message=message, finish_reason="stop")])

        with patch.dict(os.environ, {
                "DSEC_ROLLOUT_WORKER_SOCKET": "/tmp/unused.sock",
                "OPENENV_MAX_TURNS": "1",
                "DSEC_AGENT_STEP_TIMEOUT_MS": "30000"}):
            reward, metrics = await adapter.run_episode(
                None, "model", [], {},
                {"task_id": "counter-example", "dsec_environment": "counter"},
                client_factory=lambda _: client, policy_call=policy_call)
        self.assertEqual(reward, 1.0)
        self.assertEqual(metrics["dsec_environment"], "counter")
        self.assertEqual(metrics["verifier_diagnostic"]["harness"],
                         "counter-exact-value")
        self.assertEqual(client.create_args["profile"].environment, "fixed_ext4")
        self.assertEqual(sandbox.calls[0][0], "printf 3 > /rl-counter")
        self.assertEqual(sandbox.state, "STOPPED")

    async def test_miles_entry_rejects_cross_plugin_verdict(self):
        fake = ModuleType("openenv_agent_function")

        async def run_for_training(*_args, **_kwargs):
            return {"reward": 1.0, "exit_status": "completed",
                    "agent_metrics": {"dsec_environment": "counter",
                                      "resumed_after_trainer_exit": False,
                                      "verifier_diagnostic": {
                                          "harness": "counter-exact-value"}}}

        fake.run_for_training = run_for_training
        with patch.dict(sys.modules, {"dsec_adapters.openenv_agent_function": fake}):
            metadata = {"dsec_environment": "counter", "task_id": "counter-example"}
            result = await adapter.run("http://session", "prompt", metadata=metadata)
            self.assertTrue(result["dsec_canonical_verdict"])
            self.assertIsNone(await adapter.run("http://session", "prompt",
                                               metadata={"task_id": "regex-log"}))

    async def test_fresh_episode_returns_official_reward(self):
        with tempfile.TemporaryDirectory() as directory:
            task = Path(directory) / "regex-log"
            (task / "tests").mkdir(parents=True)
            (task / "environment").mkdir()
            (task / "environment" / "Dockerfile").write_text("FROM scratch\nWORKDIR /workspace\n")
            (task / "task.toml").write_text("")
            (task / "tests" / "test.sh").write_text("")
            (task / "instruction.md").write_text("Do the task.")
            sandbox = Sandbox()
            client = Client(sandbox)
            calls = []

            async def policy_call(_policy, _model, messages, _kwargs):
                calls.append(messages)
                return SimpleNamespace(choices=[SimpleNamespace(
                    message=Message(), finish_reason="stop")])

            env = {"OPENENV_TB2_TASKS_DIR": directory,
                   "DSEC_ROLLOUT_WORKER_SOCKET": "/tmp/unused.sock",
                   "OPENENV_MAX_TURNS": "1",
                   "DSEC_AGENT_STEP_TIMEOUT_MS": "30000"}
            with patch.dict(os.environ, env):
                reward, metrics = await adapter.run_episode(
                    None, "model", [{"role": "system", "content": "system"}],
                    {}, {"task_id": "regex-log"},
                    client_factory=lambda _: client, policy_call=policy_call)
            self.assertEqual(reward, 0.0)
            self.assertEqual(calls[0][-1]["content"], "Do the task.")
            self.assertEqual(sandbox.calls[0][0], "cd /workspace && echo answer")
            self.assertEqual(sandbox.calls[0][1]["step_id"], 0)
            self.assertEqual(sandbox.calls[0][1]["timeout_ms"], 30000)
            self.assertEqual(metrics["verifier_diagnostic"]["harness"], "tests/test.sh")
            self.assertEqual(sandbox.state, "STOPPED")

            resumed = Sandbox(prior=True)
            with patch.dict(os.environ, env):
                reward, metrics = await adapter.run_episode(
                    None, "model", [{"role": "system", "content": "system"}],
                    {}, {"task_id": "regex-log", "dsec_rollout_id": "a" * 32},
                    client_factory=lambda _: Client(resumed), policy_call=policy_call)
            self.assertIsNone(reward)
            self.assertTrue(metrics["resumed_after_trainer_exit"])
            self.assertEqual(resumed.state, "STOPPED")

    async def test_length_limited_reply_is_saved_before_verifier(self):
        with tempfile.TemporaryDirectory() as directory:
            task = Path(directory) / "regex-log"
            (task / "tests").mkdir(parents=True)
            (task / "environment").mkdir()
            (task / "environment" / "Dockerfile").write_text("FROM scratch\nWORKDIR /app\n")
            (task / "task.toml").write_text("")
            (task / "tests" / "test.sh").write_text("")
            (task / "instruction.md").write_text("Do the task.")
            sandbox = Sandbox()

            async def policy_call(*_args):
                return SimpleNamespace(choices=[SimpleNamespace(
                    message=Message(), finish_reason="length")])

            output_dir = Path(directory) / "model_outputs"
            tito_dir = Path(directory) / "tito"
            seen_urls = []

            async def tito_fetch(url):
                seen_urls.append(url)
                return {"session_id": "s1", "metadata": {"tito_session_mismatch": []},
                        "records": [{"request": {"messages": [
                            {"role": "system", "content": "one command"}],
                            "input_ids": [1, 2], "temperature": 0.7},
                            "response": {"choices": [{"message": {
                                "role": "assistant", "content": Message.content},
                                "finish_reason": "length", "meta_info": {
                                    "completion_tokens": 2,
                                    "output_token_logprobs": [[-0.1, 3], [-0.2, 4]]}}],
                                "usage": {"prompt_tokens": 2,
                                          "completion_tokens": 2}}}]}

            env = {"OPENENV_TB2_TASKS_DIR": directory,
                   "DSEC_ROLLOUT_WORKER_SOCKET": "/tmp/unused.sock",
                   "DSEC_MODEL_OUTPUT_DIR": str(output_dir),
                   "DSEC_TITO_AUDIT_DIR": str(tito_dir),
                   "OPENENV_MAX_TURNS": "1"}
            with patch.dict(os.environ, env):
                reward, metrics = await adapter.run_episode(
                    SimpleNamespace(base_url="http://localhost:32200/sessions/s1/v1/"),
                    "model", [], {}, {"task_id": "regex-log"},
                    client_factory=lambda _: Client(sandbox),
                    policy_call=policy_call, tito_fetch=tito_fetch)
            self.assertIsNone(reward)
            self.assertEqual(metrics["end_reason"], "length")
            self.assertEqual(metrics["tool_calls"], 0)
            record = output_dir / metrics["rollout_id"] / "0000.json"
            import json
            self.assertEqual(json.loads(record.read_text())["reply"], Message.content)
            self.assertEqual(json.loads(record.read_text())["finish_reason"], "length")
            self.assertEqual(record.stat().st_mode & 0o777, 0o600)
            self.assertEqual(seen_urls, ["http://localhost:32200/sessions/s1"])
            tito = Path(metrics["tito_path"])
            self.assertEqual(json.loads(tito.read_text())["records"][0]["request"]["input_ids"],
                             [1, 2])
            self.assertEqual(json.loads(tito.read_text())["records"][0]["response"][
                "output_token_logprobs"], [[-0.1, 3], [-0.2, 4]])
            self.assertEqual(tito.stat().st_mode & 0o777, 0o600)

    async def test_unparsed_thinking_text_never_reaches_shell(self):
        with tempfile.TemporaryDirectory() as directory:
            task = Path(directory) / "regex-log"
            (task / "tests").mkdir(parents=True)
            (task / "environment").mkdir()
            (task / "environment" / "Dockerfile").write_text(
                "FROM scratch\nWORKDIR /app\n")
            (task / "task.toml").write_text("")
            (task / "tests" / "test.sh").write_text("")
            (task / "instruction.md").write_text("Do the task.")
            sandbox = Sandbox()

            async def policy_call(*_args):
                return SimpleNamespace(choices=[SimpleNamespace(
                    message=SimpleNamespace(content="thinking text",),
                    finish_reason="stop")])

            with patch.dict(os.environ, {
                    "OPENENV_TB2_TASKS_DIR": directory,
                    "DSEC_ROLLOUT_WORKER_SOCKET": "/tmp/unused.sock",
                    "OPENENV_MAX_TURNS": "1"}):
                reward, metrics = await adapter.run_episode(
                    None, "model", [], {}, {"task_id": "regex-log"},
                    client_factory=lambda _: Client(sandbox),
                    policy_call=policy_call)
            self.assertEqual(reward, 0.0)
            self.assertEqual(metrics["end_reason"], "invalid_format")
            self.assertFalse(metrics["format_valid"])
            self.assertEqual(sandbox.calls, [])

    async def test_plain_terminal_summary_preserves_real_positive_and_zero_scores(self):
        with tempfile.TemporaryDirectory() as directory:
            task = Path(directory) / "regex-log"
            (task / "tests").mkdir(parents=True)
            (task / "environment").mkdir()
            (task / "environment" / "Dockerfile").write_text("FROM scratch\nWORKDIR /app\n")
            (task / "task.toml").write_text("")
            (task / "tests" / "test.sh").write_text("")
            (task / "instruction.md").write_text("Do the task.")
            for score in (0.0, 1.0):
                with self.subTest(score=score):
                    class GradedSandbox(Sandbox):
                        async def tb2_evaluate(self):
                            return {"value": score, "harness": "tests/test.sh", "task_id": "regex-log"}

                    sandbox = GradedSandbox()
                    calls = []

                    async def policy_call(*_args):
                        calls.append(1)
                        text = Message.content if len(calls) == 1 else "All requirements are complete."
                        message = SimpleNamespace(content=text, model_dump=lambda **_kwargs: {
                            "role": "assistant", "content": text})
                        return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="stop")])

                    with patch.dict(os.environ, {
                            "OPENENV_TB2_TASKS_DIR": directory,
                            "DSEC_ROLLOUT_WORKER_SOCKET": "/tmp/unused.sock",
                            "OPENENV_MAX_TURNS": "2"}):
                        reward, metrics = await adapter.run_episode(
                            None, "model", [], {}, {"task_id": "regex-log"},
                            client_factory=lambda _: Client(sandbox), policy_call=policy_call)
                    self.assertEqual(reward, score)
                    self.assertEqual(metrics["end_reason"], "invalid_format")
                    self.assertFalse(metrics["format_valid"])
                    self.assertEqual(len(sandbox.calls), 1)
                    self.assertEqual(sandbox.calls[0][0], "cd /app && echo answer")
                    self.assertEqual(sandbox.state, "STOPPED")

    async def test_ambiguous_thinking_boundary_still_excludes_training_and_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            task = Path(directory) / "regex-log"
            (task / "tests").mkdir(parents=True)
            (task / "environment").mkdir()
            (task / "environment" / "Dockerfile").write_text("FROM scratch\nWORKDIR /app\n")
            (task / "task.toml").write_text("")
            (task / "tests" / "test.sh").write_text("")
            (task / "instruction.md").write_text("Do the task.")
            sandbox = Sandbox()

            async def policy_call(*_args):
                return SimpleNamespace(choices=[SimpleNamespace(
                    message=SimpleNamespace(content="thought</think>```bash\necho discarded\n```</think>```bash\necho bad\n```"),
                    finish_reason="stop")])

            with patch.dict(os.environ, {
                    "OPENENV_TB2_TASKS_DIR": directory,
                    "DSEC_ROLLOUT_WORKER_SOCKET": "/tmp/unused.sock",
                    "DSEC_QWEN35_THINKING": "1", "OPENENV_MAX_TURNS": "1"}):
                reward, metrics = await adapter.run_episode(
                    None, "model", [], {}, {"task_id": "regex-log"},
                    client_factory=lambda _: Client(sandbox), policy_call=policy_call)
            self.assertIsNone(reward)
            self.assertEqual(metrics["end_reason"], "invalid_thinking_format")
            self.assertFalse(metrics["format_valid"])
            self.assertEqual(sandbox.calls, [])


if __name__ == "__main__":
    unittest.main()
