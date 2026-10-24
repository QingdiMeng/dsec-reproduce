"""Small non-TB2 task plugin using the worker's exact-value verifier."""

from __future__ import annotations

from agent_environment import (EnvironmentAction, EnvironmentObservation,
                               EnvironmentSpec, EnvironmentVerdict)
from framework_profile import FrameworkProfile


class CounterDSecEnvironment:
    def __init__(self, expected: int = 3):
        if type(expected) is not int or expected < 0:
            raise ValueError("expected must be a nonnegative integer")
        self.expected = expected

    def prepare(self, task_id: str) -> EnvironmentSpec:
        if task_id != "counter-example":
            raise ValueError("Unknown counter task")
        return EnvironmentSpec(
            task_id=task_id,
            instruction=f"Write the decimal integer {self.expected} to /rl-counter.",
            profile=FrameworkProfile(),
            resources={"cpu": 1.0, "memory_mb": 512, "disk_mb": 1024,
                       "network_mbps": 1.0, "api_episode_slots": 1})

    async def step(self, sandbox, spec: EnvironmentSpec,
                   action: EnvironmentAction,
                   policy_message: dict) -> EnvironmentObservation:
        if action.kind != "shell" or not isinstance(action.payload.get("command"), str):
            raise ValueError("Counter task expects a shell action")
        if action.timeout_ms > 30000:
            raise ValueError("Generic microVM commands are limited to 30000 ms")
        response = await sandbox.agent_step(
            action.payload["command"], step_id=action.step_id,
            action_id=action.action_id, assistant_message=policy_message,
            timeout_ms=action.timeout_ms, output_limit=action.output_limit)
        return EnvironmentObservation(
            action.step_id, action.action_id, response["entry"]["result"],
            response["dialogue"])

    async def evaluate(self, sandbox, spec: EnvironmentSpec) -> EnvironmentVerdict:
        record = await sandbox.evaluate_counter(self.expected)
        if (record.get("expected_counter") != self.expected or
                record.get("verifier_exit_code") != 0 or
                record.get("value") not in (0.0, 1.0)):
            raise RuntimeError("Counter verifier returned no valid verdict")
        return EnvironmentVerdict(
            score=float(record["value"]), evaluator="counter-exact-value",
            details=dict(record))
