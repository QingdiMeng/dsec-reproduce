"""Legacy worker configuration bridge; application imports stay opt-in."""
from __future__ import annotations

LEGACY_EVALUATION_OPERATIONS = {"tb2_evaluate": "tb21-canonical-v1"}


def execution_command(context, command):
    """Retain historical OCI PATH restoration outside the generic worker."""
    # Also accept the old public helper's rollout argument.
    profile = getattr(context, "profile", context)
    if (profile.backend == "microvm" and
            isinstance(profile.environment_id, str) and
            profile.environment_id.startswith("tb2-")):
        return ("export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:"
                "/usr/bin:/sbin:/bin; " + command)
    return command


def configured_evaluators(tb2_tasks_dir):
    if tb2_tasks_dir is None:
        return {}
    try:
        from dsec_tb21_case.worker_evaluator import TB21Evaluator
    except ModuleNotFoundError as exc:
        if exc.name in ("dsec_tb21_case", "dsec_tb21_case.worker_evaluator"):
            raise RuntimeError(
                "TB2.1 worker evaluation requires the optional application; "
                "install ./apps/tb21 from the same checkout") from exc
        raise
    evaluator = TB21Evaluator(tb2_tasks_dir)
    return {evaluator.id: evaluator}
