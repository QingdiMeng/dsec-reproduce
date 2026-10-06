"""Contract tests using a task that has no shell, TB2, or Miles dependency."""

import unittest

from agent_environment import (AgentEnvironment, DSecAgentEnvironment,
                               EnvironmentAction, EnvironmentObservation,
                               EnvironmentSpec, EnvironmentVerdict)
from framework_profile import FrameworkProfile


class CounterAdapter:
    def prepare(self, task_id):
        return EnvironmentSpec(task_id, "Increase the counter", FrameworkProfile(),
                               {"cpu": 0.5}, {"initial": 2})

    async def step(self, sandbox, spec, action, policy_message):
        if action.kind != "counter.add":
            raise ValueError("Expected counter.add")
        sandbox.counter += action.payload["delta"]
        sandbox.next_step += 1
        sandbox.messages.append(policy_message)
        sandbox.messages.append({"role": "user", "content": str(sandbox.counter)})
        return EnvironmentObservation(action.step_id, action.action_id,
                                      {"counter": sandbox.counter},
                                      await sandbox.dialogue())

    async def evaluate(self, sandbox, spec):
        return EnvironmentVerdict(0.75, "counter-grader", {"counter": sandbox.counter})


class CounterSandbox:
    state = "ACTIVE"

    def __init__(self):
        self.counter = 2
        self.next_step = 0
        self.messages = []
        self.stopped = False

    async def start_dialogue(self, messages):
        self.messages = messages

    async def dialogue(self):
        return {"state": self.state, "next_step": self.next_step,
                "messages": list(self.messages), "pending": None}

    async def stop(self):
        self.stopped = True


class CounterClient:
    def __init__(self, sandbox):
        self.sandbox = sandbox
        self.create_args = None

    async def create(self, **kwargs):
        self.create_args = kwargs
        return self.sandbox


class AgentEnvironmentTest(unittest.IsolatedAsyncioTestCase):
    async def test_task_independent_episode(self):
        sandbox = CounterSandbox()
        client = CounterClient(sandbox)
        episode: AgentEnvironment = DSecAgentEnvironment(
            client, CounterAdapter(), "counter-1", "a" * 32)
        context = await episode.reset([{"role": "system", "content": "Count"}])
        self.assertEqual([message["content"] for message in context["messages"]],
                         ["Count", "Increase the counter"])
        self.assertEqual(client.create_args["resources"], {"cpu": 0.5})
        action = EnvironmentAction(0, "add-three", "counter.add", {"delta": 3})
        policy_message = {"role": "assistant", "content": "Add three", "extra": [1, 2]}
        observation = await episode.step(action, policy_message=policy_message)
        self.assertEqual(observation.result, {"counter": 5})
        self.assertIs(sandbox.messages[-2], policy_message)
        self.assertEqual(observation.dialogue["next_step"], 1)
        verdict = await episode.evaluate()
        self.assertEqual((verdict.score, verdict.evaluator), (0.75, "counter-grader"))
        await episode.stop()
        self.assertTrue(sandbox.stopped)

    async def test_rejects_repeated_reset_and_mismatched_observation(self):
        class BadAdapter(CounterAdapter):
            async def step(self, sandbox, spec, action, policy_message):
                return EnvironmentObservation(99, action.action_id, {}, {})

        episode = DSecAgentEnvironment(CounterClient(CounterSandbox()),
                                       BadAdapter(), "counter-1", "b" * 32)
        await episode.reset([])
        with self.assertRaisesRegex(RuntimeError, "already reset"):
            await episode.reset([])
        with self.assertRaisesRegex(RuntimeError, "mismatched observation"):
            await episode.step(EnvironmentAction(0, "a", "counter.add", {"delta": 1}),
                               policy_message={"role": "assistant", "content": "go"})
        await episode.stop()

    async def test_unknown_action_requires_reconcile_not_replay(self):
        class UnknownSandbox(CounterSandbox):
            state = "UNKNOWN"

            def __init__(self):
                super().__init__()
                self.reconciles = 0

            async def reconcile(self):
                self.reconciles += 1
                return {"reconciled": False}

        sandbox = UnknownSandbox()
        episode = DSecAgentEnvironment(CounterClient(sandbox), CounterAdapter(),
                                       "counter-1", "c" * 32)
        with self.assertRaisesRegex(RuntimeError, "do not replay"):
            await episode.reset([])
        self.assertEqual(sandbox.reconciles, 1)
        self.assertFalse(sandbox.stopped)
        await episode.stop()

    async def test_definite_dialogue_setup_failure_releases_sandbox(self):
        class RejectingSandbox(CounterSandbox):
            async def start_dialogue(self, messages):
                raise ValueError("invalid prefix")

        sandbox = RejectingSandbox()
        episode = DSecAgentEnvironment(CounterClient(sandbox), CounterAdapter(),
                                       "counter-1", "d" * 32)
        with self.assertRaisesRegex(ValueError, "invalid prefix"):
            await episode.reset([])
        self.assertTrue(sandbox.stopped)


if __name__ == "__main__":
    unittest.main()
