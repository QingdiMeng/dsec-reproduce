"""Bind worker verifier pins to this instance's validated host configuration."""
import os


def configure(sandbox, worker_root, atomic_json):
    # The verifier must use the same independent artifact pin as sandboxd,
    # rather than inheriting a manifest from a previous source deployment.
    canonical = sandbox.get('tb2_verifier_artifact_manifest')
    if canonical:
        os.environ['DSEC_TB2_CANONICAL_VERIFIER_MANIFEST'] = canonical
    else:
        os.environ.pop('DSEC_TB2_CANONICAL_VERIFIER_MANIFEST', None)
    task_pins = {spec.split('=')[0]:spec.split('=')[1]
                 for spec in sandbox.get('tb2_task_verifier_artifact', [])}
    if task_pins:
        pin_index = worker_root/'task-verifier-pins.json'
        atomic_json(pin_index, task_pins)
        os.environ['DSEC_TB2_TASK_VERIFIER_MANIFESTS_FILE'] = str(pin_index)
    else:
        os.environ.pop('DSEC_TB2_TASK_VERIFIER_MANIFESTS_FILE', None)
