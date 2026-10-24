"""API extension over the existing durable worker, not a second episode model."""
import hashlib

from dsec.contracts.cluster import identifier, request_id

ROLLOUT_FEATURE = 'rollout-api-v1'
ROLLOUT_PERMISSIONS = {
    'create':'sandbox.create', 'recover_create':'sandbox.create', 'status':'sandbox.read', 'dialogue_status':'sandbox.read',
    'resource_status':'sandbox.read', 'step':'sandbox.execute', 'agent_step':'sandbox.execute',
    'dialogue_start':'sandbox.execute', 'evaluate':'sandbox.execute', 'tb2_evaluate':'sandbox.execute',
    'task_evaluate':'sandbox.execute', 'reconcile':'sandbox.execute',
    'pause':'sandbox.snapshot', 'seal_baseline':'sandbox.snapshot', 'stop':'sandbox.stop',
}
ROLLOUT_READS = frozenset(('status', 'dialogue_status', 'resource_status'))
ROLLOUT_FIELDS = {
    'status':set(), 'recover_create':set(), 'dialogue_status':set(), 'resource_status':set(), 'stop':set(),
    'pause':set(), 'reconcile':set(), 'tb2_evaluate':set(),
    'seal_baseline':{'allow_prepared_state'}, 'dialogue_start':{'messages'},
    'evaluate':{'expected_counter'}, 'task_evaluate':{'evaluator','parameters'},
    'step':{'step_id','action_id','command','timeout_ms','output_limit'},
    'agent_step':{'step_id','action_id','command','timeout_ms','output_limit','assistant_message'},
}


def private_rollout_id(project, rollout_id):
    identifier(project); request_id(rollout_id)
    return hashlib.sha256(f'dsec-rollout-v1:{project}:{rollout_id}'.encode()).hexdigest()[:32]


def is_rollout(receipt):
    return isinstance(receipt.get('args'), dict) and set(receipt['args']) == {'rollout'}
