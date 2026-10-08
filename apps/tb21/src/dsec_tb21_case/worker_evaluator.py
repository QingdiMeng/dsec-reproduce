"""Official TB2.1 verifier plugin; the worker owns state and verdict commits."""
from pathlib import Path
import re
from types import SimpleNamespace

from dsec.contracts.evaluation import EvaluationContext, EvaluationFailure, EvaluationOutcome


class TB21Evaluator:
    id = "tb21-canonical-v1"

    def __init__(self, tasks_dir):
        self.tasks_dir = Path(tasks_dir).resolve()

    def _task_directory(self, context):
        directory = (self.tasks_dir / context.task_id).resolve()
        if directory.parent != self.tasks_dir or directory.is_symlink():
            raise ValueError("TB2 task path escapes the pinned task directory")
        return directory

    def validate(self, context: EvaluationContext, parameters):
        if parameters:
            raise ValueError("TB2.1 canonical evaluator does not accept verifier overrides")
        if context.backend != "microvm" or not re.fullmatch(
                r"[a-z0-9][a-z0-9.-]{0,127}", context.task_id):
            raise ValueError("TB2 evaluation requires a pinned microVM task")
        if context.environment_id != "tb2-" + context.task_id:
            raise ValueError("TB2 task and sandbox template do not match")
        self._task_directory(context)

    def accepts_reward(self, reward, parameters):
        return bool(reward and reward.get("harness") == "tests/test.sh" and not parameters)

    async def evaluate(self, context, sandbox, parameters):
        from dsec_adapters.tb2_microvm_env import TB2MicroVMEnv

        evidence_dir = (Path(context.evidence_directory)
                        if context.evidence_directory is not None else None)
        env = TB2MicroVMEnv(sandbox, task_id=context.task_id,
                           task_dir=self._task_directory(context), verifier_mode="canonical",
                           evidence_dir=evidence_dir)
        await env.reset(task_id=context.task_id)
        result = await env.step(SimpleNamespace(action_type="evaluate"))
        info = result.observation.info
        if (result.reward not in (0.0, 1.0) or result.observation.error or
                info.get("harness") != "tests/test.sh" or
                (evidence_dir is not None and
                 info.get("evidence", {}).get("directory") != str(evidence_dir))):
            diagnostic = (str(result.observation.error) or
                          "Verifier evidence was not durably exported")
            details = {"error": diagnostic, "stage": info.get("verifier_stage"),
                       "evidence": info.get("evidence", {}),
                       "evidence_error": info.get("evidence_error")}
            if len(diagnostic) > 1800:
                diagnostic = diagnostic[:120] + " ... " + diagnostic[-1660:]
            raise EvaluationFailure("TB2 canonical verifier produced no valid verdict: " +
                                    diagnostic, details)
        return EvaluationOutcome({"value": float(result.reward), "harness": "tests/test.sh",
                                  "verifier_mode": "canonical", "task_id": context.task_id,
                                  "evidence": info.get("evidence", {})})
