"""Task evaluation boundary; plugins cannot own worker lifecycle state."""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
from typing import Any, Protocol


@dataclass(frozen=True)
class EvaluationContext:
    rollout_id: str
    task_id: str
    backend: str
    environment_id: str | None
    evidence_directory: str | None = None


@dataclass(frozen=True)
class EvaluationOutcome:
    reward: dict[str, Any]

    def __post_init__(self):
        if not isinstance(self.reward, dict):
            raise ValueError("Evaluator reward must be an object")
        value = self.reward.get("value")
        if (isinstance(value, bool) or not isinstance(value, (int, float)) or
                not math.isfinite(value)):
            raise ValueError("Evaluator must return a finite numeric reward")
        json.dumps(self.reward, allow_nan=False)


class EvaluationFailure(RuntimeError):
    """No valid verdict; diagnostic evidence must survive in the worker journal."""

    def __init__(self, message: str, details: dict[str, Any]):
        super().__init__(message)
        if not isinstance(details, dict):
            raise ValueError("Evaluation failure details must be an object")
        json.dumps(details, allow_nan=False)
        self.details = details


class EvaluationPlugin(Protocol):
    # Change this versioned identity when the evaluator's semantics change.
    id: str

    def validate(self, context: EvaluationContext, parameters: dict[str, Any]) -> None: ...

    def accepts_reward(self, reward: dict[str, Any], parameters: dict[str, Any]) -> bool: ...

    async def evaluate(self, context: EvaluationContext, sandbox: Any,
                       parameters: dict[str, Any]) -> EvaluationOutcome: ...
