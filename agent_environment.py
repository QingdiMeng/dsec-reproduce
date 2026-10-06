"""Framework-independent contract for a durable DSec agent environment.

The task adapter owns instruction, sandbox profile, command location, and
verifier interpretation. A training framework owns policy messages, tokens,
logprobs, and sampling. This module only joins those two sides to the durable
rollout worker; it does not know TB2, Miles, or a model's reply format.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import math
import time
from typing import Any, Protocol

from framework_profile import FrameworkProfile
from scheduled_dsec import ScheduledOutcomeUnknown


class UnresolvedAction(RuntimeError):
    """Worker cannot yet prove whether a sandbox side effect committed."""


@dataclass(frozen=True)
class EnvironmentSpec:
    task_id: str
    instruction: str
    profile: FrameworkProfile
    resources: dict[str, Any] = field(default_factory=dict)
    options: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.task_id or not isinstance(self.instruction, str):
            raise ValueError("Environment needs a task ID and instruction")
        self.profile.validate_runtime()


@dataclass(frozen=True)
class EnvironmentAction:
    step_id: int
    action_id: str
    kind: str
    payload: dict[str, Any]
    timeout_ms: int = 120000
    output_limit: int = 65536

    def __post_init__(self) -> None:
        if type(self.step_id) is not int or self.step_id < 0:
            raise ValueError("step_id must be a nonnegative integer")
        if not self.action_id or not self.kind or not isinstance(self.payload, dict):
            raise ValueError("action_id, kind, and payload are required")

    @classmethod
    def shell(cls, *, step_id: int, action_id: str, command: str,
              timeout_ms: int = 120000, output_limit: int = 65536):
        if not isinstance(command, str) or not command:
            raise ValueError("shell command is required")
        return cls(step_id, action_id, "shell", {"command": command},
                   timeout_ms, output_limit)


@dataclass(frozen=True)
class EnvironmentObservation:
    step_id: int
    action_id: str
    result: dict[str, Any]
    dialogue: dict[str, Any]


@dataclass(frozen=True)
class EnvironmentVerdict:
    score: float
    evaluator: str
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not math.isfinite(self.score) or not self.evaluator:
            raise ValueError("Environment verdict requires a finite score and evaluator")


class TaskEnvironmentAdapter(Protocol):
    """Task-specific boundary; implementations may serve any benchmark."""

    def prepare(self, task_id: str) -> EnvironmentSpec: ...

    async def step(self, sandbox: Any, spec: EnvironmentSpec,
                   action: EnvironmentAction, policy_message: dict[str, Any]
                   ) -> EnvironmentObservation: ...

    async def evaluate(self, sandbox: Any, spec: EnvironmentSpec) -> EnvironmentVerdict: ...


class AgentEnvironment(Protocol):
    """Trainer-facing episode contract, independent of task and sandbox SDK."""

    async def reset(self, policy_prefix: list[dict[str, Any]]) -> dict[str, Any]: ...

    async def dialogue(self, *, timeout_s: float = 180) -> dict[str, Any]: ...

    async def step(self, action: EnvironmentAction, *,
                   policy_message: dict[str, Any]) -> EnvironmentObservation: ...

    async def evaluate(self) -> EnvironmentVerdict: ...

    async def stop(self) -> None: ...


class DSecAgentEnvironment:
    """One reset/step/evaluate episode on a scheduler-backed sandbox.

    The worker owns the dialogue and side-effect journal. Model-facing message
    dictionaries are opaque here: this layer preserves them verbatim so each
    training framework can keep its own token/session accounting.
    """

    def __init__(self, client: Any, adapter: TaskEnvironmentAdapter,
                 task_id: str, rollout_id: str, *, ttl_running_stop: int = 3600,
                 baseline_rollout_id: str | None = None):
        self.baseline_rollout_id = baseline_rollout_id
        self.client = client
        self.adapter = adapter
        self.task_id = task_id
        self.rollout_id = rollout_id
        self.ttl_running_stop = ttl_running_stop
        self.spec: EnvironmentSpec | None = None
        self.sandbox: Any = None

    async def reset(self, policy_prefix: list[dict[str, Any]]) -> dict[str, Any]:
        if self.sandbox is not None:
            raise RuntimeError("Environment episode was already reset")
        spec = self.adapter.prepare(self.task_id)
        if spec.task_id != self.task_id:
            raise ValueError("Task adapter returned a different task ID")
        sandbox = await self.client.create(
            task_id=spec.task_id, rollout_id=self.rollout_id,
            profile=spec.profile, resources=spec.resources,
            ttl_running_stop=self.ttl_running_stop,
            **({"baseline_rollout_id": self.baseline_rollout_id}
               if self.baseline_rollout_id is not None else {}))
        self.spec, self.sandbox = spec, sandbox
        try:
            if sandbox.state == "QUEUED":
                await sandbox.wait_ready(timeout=1800)
            await sandbox.start_dialogue(
                list(policy_prefix) + [{"role": "user", "content": spec.instruction}])
            return await self.dialogue()
        except (ScheduledOutcomeUnknown, UnresolvedAction):
            # The worker may have committed the operation. Keep the stable
            # rollout available for attach/reconcile instead of destroying it.
            raise
        except Exception:
            # A definite setup failure must not leave a reserved VM behind.
            # Keep the original error even if cleanup also fails.
            try:
                await sandbox.stop()
            except Exception:
                pass
            raise

    async def dialogue(self, *, timeout_s: float = 180) -> dict[str, Any]:
        if self.sandbox is None:
            raise RuntimeError("Environment episode has not been reset")
        deadline = time.monotonic() + timeout_s
        while True:
            context = await self.sandbox.dialogue()
            state = context["state"]
            if state in ("ACTIVE", "PAUSED", "COMPLETED"):
                return context
            if state == "UNKNOWN":
                proof = await self.sandbox.reconcile()
                if proof.get("reconciled"):
                    continue
                raise UnresolvedAction("Unresolved DSec action; do not replay its command")
            if state not in ("EXECUTING", "CREATING", "QUEUED"):
                raise RuntimeError("Rollout cannot resume from " + state)
            if time.monotonic() >= deadline:
                raise TimeoutError("DSec rollout action did not settle")
            await asyncio.sleep(.2)

    async def step(self, action: EnvironmentAction, *,
                   policy_message: dict[str, Any]) -> EnvironmentObservation:
        if self.sandbox is None or self.spec is None:
            raise RuntimeError("Environment episode has not been reset")
        if policy_message.get("role") != "assistant":
            raise ValueError("policy_message must be an assistant message")
        observation = await self.adapter.step(
            self.sandbox, self.spec, action, policy_message)
        if not isinstance(observation, EnvironmentObservation) or (
                observation.step_id != action.step_id or
                observation.action_id != action.action_id):
            raise RuntimeError("Task adapter returned a mismatched observation")
        return observation

    async def evaluate(self) -> EnvironmentVerdict:
        if self.sandbox is None or self.spec is None:
            raise RuntimeError("Environment episode has not been reset")
        verdict = await self.adapter.evaluate(self.sandbox, self.spec)
        if not isinstance(verdict, EnvironmentVerdict):
            raise TypeError("Task adapter must return an EnvironmentVerdict")
        return verdict

    async def stop(self) -> None:
        if self.sandbox is not None:
            await self.sandbox.stop()


__all__ = ["EnvironmentSpec", "EnvironmentAction", "EnvironmentObservation",
           "EnvironmentVerdict", "TaskEnvironmentAdapter", "AgentEnvironment",
           "DSecAgentEnvironment",
           "ScheduledOutcomeUnknown", "UnresolvedAction"]
