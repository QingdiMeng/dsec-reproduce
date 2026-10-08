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
        from dsec.compat.applications import require_tb21
        return require_tb21("commands").execution_command(command)
    return command


def configured_evaluators(tb2_tasks_dir):
    if tb2_tasks_dir is None:
        return {}
    from dsec.compat.applications import require_tb21
    TB21Evaluator = require_tb21("worker_evaluator").TB21Evaluator
    evaluator = TB21Evaluator(tb2_tasks_dir)
    return {evaluator.id: evaluator}


def configure_worker_environment(sandbox, worker_root, atomic_json):
    if (sandbox.get("tb2_verifier_artifact_manifest") or
            sandbox.get("tb2_task_verifier_artifact")):
        from dsec.compat.applications import require_tb21
        require_tb21("worker_configuration").configure(sandbox, worker_root, atomic_json)
    else:
        # Clear legacy inherited pins even for a generic deployment.
        import os
        os.environ.pop("DSEC_TB2_CANONICAL_VERIFIER_MANIFEST", None)
        os.environ.pop("DSEC_TB2_TASK_VERIFIER_MANIFESTS_FILE", None)


def validate_verifier_dax(store, environment_id):
    from dsec.compat.applications import require_tb21
    require_tb21("verifier_artifact").validate_dax(store, environment_id)
