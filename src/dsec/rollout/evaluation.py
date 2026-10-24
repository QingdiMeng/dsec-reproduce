"""Evaluator registration, input validation and isolated plugin invocation.

Only EpisodeService commits pending/completed/unknown states. Plugins receive a
context and sandbox handle, never the episode record or its durable store.
"""
from dataclasses import dataclass
import json
import re
from dsec.contracts.evaluation import EvaluationContext, EvaluationOutcome
from dsec.contracts.requests import request_digest


@dataclass(frozen=True)
class PreparedEvaluation:
    evaluator: object
    context: EvaluationContext
    parameters: dict
    encoded_parameters: str
    identity: dict


class EvaluationService:
    def __init__(self, evaluators=None, *, counter_evaluator=None):
        self.evaluators = dict(evaluators or {})
        for key, evaluator in self.evaluators.items():
            if (not isinstance(key, str) or
                    not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,127}", key) or
                    getattr(evaluator, "id", None) != key or
                    any(not callable(getattr(evaluator, method, None))
                        for method in ("validate", "accepts_reward", "evaluate"))):
                raise ValueError("Invalid evaluator registration")
        self._counter = counter_evaluator

    def _require_counter(self):
        if self._counter is None:
            raise RuntimeError("Legacy counter evaluator is not configured")
        return self._counter

    def expected_counter(self, args):
        return self._require_counter().expected(args)

    @property
    def counter_command(self):
        return self._require_counter().command

    def counter_reward(self, result, expected):
        return self._require_counter().reward_from_result(result, expected)

    def context(self, rollout, store):
        evidence = (str(store.root / "evidence" / rollout.id)
                    if store is not None else None)
        return EvaluationContext(rollout.id, rollout.task_id, rollout.profile.backend,
                                 rollout.profile.environment_id, evidence)

    def prepare(self, rollout, evaluator_id, parameters, store):
        if not isinstance(evaluator_id, str) or evaluator_id not in self.evaluators:
            raise RuntimeError("Evaluator is not configured: " + str(evaluator_id))
        if not isinstance(parameters, dict):
            raise ValueError("Evaluation parameters must be an object")
        encoded_parameters = json.dumps(parameters, allow_nan=False, sort_keys=True)
        parameters = json.loads(encoded_parameters)
        evaluator = self.evaluators[evaluator_id]
        context = self.context(rollout, store)
        evaluator.validate(context, json.loads(encoded_parameters))
        identity = {"evaluator": evaluator_id, "parameters": parameters,
                    "digest": request_digest("task_evaluate", rollout.sandbox_id,
                                             {"evaluator": evaluator_id, "parameters": parameters})}
        return PreparedEvaluation(evaluator, context, parameters, encoded_parameters, identity)

    async def execute(self, prepared, sandbox):
        outcome = await prepared.evaluator.evaluate(
            prepared.context, sandbox, json.loads(prepared.encoded_parameters))
        if not isinstance(outcome, EvaluationOutcome):
            raise ValueError("Evaluator returned no validated outcome")
        # Freeze plugin-owned dictionaries before the coordinator commits them.
        return EvaluationOutcome(json.loads(json.dumps(outcome.reward, allow_nan=False)))
