"""Historical worker constructor and task aliases; no episode implementation."""
from pathlib import Path
from dsec.rollout.episodes import EpisodeService
from dsec.compat.counter_evaluator import CounterEvaluator
from dsec.compat.task_plugins import (
    LEGACY_EVALUATION_OPERATIONS, configured_evaluators, execution_command)


class RolloutWorker(EpisodeService):
    def __init__(self, sandbox_client, state_dir=None, scheduler=None, tb2_tasks_dir=None,
                 *, evaluators=None, command_transform=None):
        registrations = dict(evaluators or {})
        for key, evaluator in configured_evaluators(tb2_tasks_dir).items():
            if key in registrations:
                raise ValueError("Conflicting evaluator configuration: " + key)
            registrations[key] = evaluator
        super().__init__(sandbox_client, state_dir, scheduler,
                         evaluators=registrations,
                         command_transform=command_transform or execution_command,
                         counter_evaluator=CounterEvaluator,
                         evaluation_aliases=LEGACY_EVALUATION_OPERATIONS)
        self.tb2_tasks_dir = Path(tb2_tasks_dir).resolve() if tb2_tasks_dir else None

    async def _tb2_evaluate(self, rollout):
        """Compatibility entry point; task implementation belongs to the application."""
        return await self._plugin_evaluate(
            rollout, LEGACY_EVALUATION_OPERATIONS["tb2_evaluate"], {}, operation="tb2_evaluate")
