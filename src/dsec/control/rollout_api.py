"""Stateless authenticated rollout routing through the existing IAM receipts."""
from dataclasses import asdict

from dsec.contracts.cluster import Quota, request_id
from dsec.contracts.profiles import FrameworkProfile
from dsec.contracts.requests import request_digest
from dsec.contracts.resources import ResourceDemand
from dsec.contracts.rollout import ROLLOUT_FEATURE, ROLLOUT_PERMISSIONS, ROLLOUT_FIELDS, is_rollout


def dispatch(api, credential, project, operation_id, operation, args):
    action = operation.removeprefix('rollout.')
    if action not in ROLLOUT_PERMISSIONS:
        raise ValueError('Unsupported rollout API operation')
    api.iam.authorize(credential, project, ROLLOUT_PERMISSIONS[action])
    rid = request_id(args.get('rollout_id'))
    receipt = api.iam.lookup(project, rid)
    if action == 'create':
        if set(args) - {'task_id','rollout_id','profile','resources','ttl_running_stop','baseline_rollout_id'}:
            raise ValueError('Unexpected rollout creation arguments')
        if not isinstance(args.get('task_id'), str) or not 1 <= len(args['task_id']) <= 128:
            raise ValueError('Task identity required')
        profile = FrameworkProfile.from_dict(args.get('profile')).validate_runtime()
        if not isinstance(args.get('resources'), dict):
            raise ValueError('Explicit rollout resources required')
        resources = ResourceDemand(**args['resources'])
        if resources.disk_io_mbps:
            raise ValueError('Cluster disk-IO quota is not supported')
        demand = Quota(1, resources.cpu, resources.memory_mb, resources.disk_mb, resources.network_mbps)
        canonical = dict(args, profile=profile.as_dict(), resources=asdict(resources))
        baseline = args.get('baseline_rollout_id')
        if baseline is not None:
            api.iam.authorize(credential, project, 'sandbox.snapshot')
            source = api.iam.lookup(project, request_id(baseline))
            if source is None or not is_rollout(source) or source['state'] != 'BOUND':
                raise ValueError('Baseline must belong to this project and have a bound owner')
            candidate = source['node']
        elif receipt is None:
            candidate = api.placement.choose(profile.backend, profile.environment_id or 'default',
                                             demand, capabilities=(ROLLOUT_FEATURE,))
        else:
            candidate = receipt['node']
        digest = request_digest(operation, None, canonical)
        receipt = api.iam.admit(credential, project, rid, digest, candidate, profile.backend,
                                demand, args={'rollout':canonical})
        result = api.nodes[receipt['node']].create_rollout(receipt)
    elif action == 'recover_create':
        if set(args) != {'rollout_id'} or receipt is None or not is_rollout(receipt):
            raise ValueError('Recovery requires an existing rollout identity only')
        if receipt['node'] not in api.nodes:
            raise ValueError('Owning Node unavailable; re-placement prohibited')
        if receipt['args']['rollout'].get('baseline_rollout_id') is not None:
            api.iam.authorize(credential, project, 'sandbox.snapshot')
        # The Node gate decides from the current durable receipt: ADMITTED can
        # send once, DISPATCHED/BOUND can only inspect the original worker.
        result = api.nodes[receipt['node']].create_rollout(receipt)
    else:
        if set(args) - (ROLLOUT_FIELDS[action] | {'rollout_id'}):
            raise ValueError('Unexpected rollout operation arguments')
        if receipt is None or not is_rollout(receipt):
            raise ValueError('Rollout has no admission in this project')
        if receipt['node'] not in api.nodes:
            raise ValueError('Owning Node unavailable; re-placement prohibited')
        result = api.nodes[receipt['node']].call_rollout(receipt, action, args)
    api._checkpoint('result_before_reply', dict(project=project, request_id=operation_id,
                                               operation=operation, args=args))
    return result
