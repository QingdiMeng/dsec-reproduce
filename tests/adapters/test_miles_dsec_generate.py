"""Missing or resumed DSec verdicts must not silently become reward zero."""

import asyncio
from dataclasses import dataclass
from enum import Enum
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

import dsec_adapters.miles_dsec_generate as guard


class Status(Enum):
    PENDING = "PENDING"
    ABORTED = "ABORTED"


class Sample:
    Status = Status

    def __init__(self, metadata):
        self.metadata = metadata
        self.status = Status.PENDING


def valid():
    return {"dsec_canonical_verdict": True, "exit_status": "completed",
            "reward": 1.0, "agent_metrics": {
                "resumed_after_trainer_exit": False,
                "verifier_diagnostic": {"harness": "tests/test.sh"}}}


class GuardTest(unittest.IsolatedAsyncioTestCase):
    async def test_qwen_thinking_coding_profile_reaches_agent_sampling_params(self):
        @dataclass(frozen=True)
        class Input:
            sampling_params: dict
            evaluation: bool = False

        observed = []
        async def base_generate(value):
            observed.append(value.sampling_params)
            return SimpleNamespace(samples=Sample(valid()))

        module = ModuleType("miles.rollout.generate_hub.agentic_tool_call")
        module.generate = base_generate
        types = ModuleType("miles.utils.types")
        types.Sample = Sample
        with patch.dict(sys.modules, {
                "miles": ModuleType("miles"),
                "miles.rollout": ModuleType("miles.rollout"),
                "miles.rollout.generate_hub": ModuleType("miles.rollout.generate_hub"),
                "miles.rollout.generate_hub.agentic_tool_call": module,
                "miles.utils": ModuleType("miles.utils"),
                "miles.utils.types": types}), patch.dict(
                    "os.environ", {"DSEC_QWEN35_THINKING_CODING_SAMPLING": "1"},
                    clear=True):
            await guard.generate(Input({"max_new_tokens": 32768}))
        self.assertEqual(observed, [{"max_new_tokens": 32768,
                                      **guard.QWEN35_THINKING_CODING_SAMPLING}])

    async def test_qwen_nonthinking_profile_reaches_agent_sampling_params(self):
        @dataclass(frozen=True)
        class Input:
            sampling_params: dict
            evaluation: bool = False

        observed = []
        async def base_generate(value):
            observed.append(value.sampling_params)
            return SimpleNamespace(samples=Sample(valid()))

        module = ModuleType("miles.rollout.generate_hub.agentic_tool_call")
        module.generate = base_generate
        types = ModuleType("miles.utils.types")
        types.Sample = Sample
        with patch.dict(sys.modules, {
                "miles": ModuleType("miles"),
                "miles.rollout": ModuleType("miles.rollout"),
                "miles.rollout.generate_hub": ModuleType("miles.rollout.generate_hub"),
                "miles.rollout.generate_hub.agentic_tool_call": module,
                "miles.utils": ModuleType("miles.utils"),
                "miles.utils.types": types}), patch.dict(
                    "os.environ", {"DSEC_QWEN35_NONTHINKING_SAMPLING": "1"}):
            await guard.generate(Input({"max_new_tokens": 2048, "temperature": 1.0}))
        self.assertEqual(observed, [{"max_new_tokens": 2048,
                                      **guard.QWEN35_NONTHINKING_SAMPLING}])

    async def test_missing_reward_and_resumed_episode_are_aborted(self):
        good = Sample(valid())
        missing = Sample({"agent_metrics": {}})
        resumed = Sample({**valid(), "agent_metrics": {
            "resumed_after_trainer_exit": True,
            "verifier_diagnostic": {"harness": "tests/test.sh"}}})
        misaligned = Sample({**valid(), "tito_session_mismatch": [
            {"type": "special_token_count"}]})
        result = SimpleNamespace(samples=[good, missing, resumed, misaligned])

        async def base_generate(_input):
            return result

        base_generate.add_arguments = lambda parser: parser
        modules = {}
        for name in ("miles", "miles.rollout", "miles.rollout.generate_hub",
                     "miles.rollout.generate_hub.agentic_tool_call", "miles.utils",
                     "miles.utils.types"):
            modules[name] = ModuleType(name)
        modules["miles.rollout.generate_hub.agentic_tool_call"].generate = base_generate
        modules["miles.utils.types"].Sample = Sample
        with patch.dict(sys.modules, modules):
            output = await guard.generate(None)
            self.assertIs(output, result)
            self.assertEqual([s.status for s in result.samples],
                             [Status.PENDING, Status.ABORTED, Status.ABORTED,
                              Status.ABORTED])
            self.assertEqual(await guard.reward_func(None, result.samples),
                             [1.0, 0.0, 0.0, 0.0])
            counter = Sample({**valid(), "agent_metrics": {
                "dsec_environment": "counter",
                "resumed_after_trainer_exit": False,
                "verifier_diagnostic": {"harness": "counter-exact-value"}}})
            self.assertTrue(guard._verified(counter.metadata))
            self.assertEqual(await guard.reward_func(None, counter), 1.0)
            counter.metadata["agent_metrics"]["verifier_diagnostic"]["harness"] = "tests/test.sh"
            self.assertFalse(guard._verified(counter.metadata))
            missing.status = Status.PENDING
            with self.assertRaisesRegex(RuntimeError, "lacks a canonical"):
                await guard.reward_func(None, missing)


if __name__ == "__main__":
    unittest.main()
