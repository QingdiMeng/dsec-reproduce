"""The non-TB2 plugin rejects verifier failures instead of training on zero."""

import unittest

from agent_environment import EnvironmentAction
from dsec_adapters.counter_dsec_environment import CounterDSecEnvironment


class Sandbox:
    def __init__(self):
        self.command = None
        self.verdict = {"value": 1.0, "expected_counter": 3,
                        "observed": "3", "verifier_exit_code": 0}

    async def agent_step(self, command, **kwargs):
        self.command = command
        return {"entry": {"result": {"exit_code": 0, "output": ""}},
                "dialogue": {"next_step": 1, "state": "ACTIVE"}}

    async def evaluate_counter(self, expected):
        self.expected = expected
        return self.verdict


class CounterPluginTest(unittest.IsolatedAsyncioTestCase):
    async def test_non_tb2_task_and_verifier(self):
        plugin = CounterDSecEnvironment(3)
        spec = plugin.prepare("counter-example")
        self.assertEqual(spec.profile.environment, "fixed_ext4")
        sandbox = Sandbox()
        observation = await plugin.step(
            sandbox, spec, EnvironmentAction.shell(
                step_id=0, action_id="write", command="printf 3 > /rl-counter",
                timeout_ms=30000),
            {"role": "assistant", "content": "write"})
        self.assertEqual(sandbox.command, "printf 3 > /rl-counter")
        self.assertEqual(observation.dialogue["next_step"], 1)
        verdict = await plugin.evaluate(sandbox, spec)
        self.assertEqual((verdict.score, verdict.evaluator),
                         (1.0, "counter-exact-value"))
        sandbox.verdict["observed"] = "2"
        sandbox.verdict["value"] = 0.0
        self.assertEqual((await plugin.evaluate(sandbox, spec)).score, 0.0)
        sandbox.verdict["verifier_exit_code"] = 1
        with self.assertRaisesRegex(RuntimeError, "no valid verdict"):
            await plugin.evaluate(sandbox, spec)
        with self.assertRaisesRegex(ValueError, "limited to 30000"):
            await plugin.step(sandbox, spec, EnvironmentAction.shell(
                step_id=1, action_id="too-long", command="true"),
                {"role": "assistant", "content": "true"})


if __name__ == "__main__":
    unittest.main()
