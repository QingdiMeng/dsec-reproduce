"""Stateless cluster ingress; dependencies are injected service interfaces.

No sandbox registry, no backend drivers and no guest execution journal here.
"""
from dataclasses import asdict

from dsec.contracts.cluster import (AuthorizationDenied, PROTOCOL, Quota, SandboxRoute,
                                   identifier, request_id)
from dsec.contracts.requests import request_digest
from dsec.contracts.native import NATIVE_FEATURE, STREAM_FEATURE


def permission(operation, args):
    if operation in ('create', 'container_create'):
        return 'sandbox.create'
    if operation in ('stop', 'container_stop'):
        return 'sandbox.stop'
    if operation in ('pause', 'resume', 'recover', 'seal_baseline'):
        return 'sandbox.snapshot'
    if operation in ('status', 'container_status', 'query_request', 'container_query_request',
                     'native_events', 'health', 'node_status'):
        return 'sandbox.read'
    if operation == 'native' and args.get('action') in ('capabilities', 'read'):
        return 'sandbox.read'
    if operation in ('native', 'native_stream', 'native_cancel', 'execute', 'container_execute'):
        return 'sandbox.execute'
    raise ValueError('Unsupported cluster operation')


class ClusterAPI:
    def __init__(self, iam, placement, nodes, *, route_key):
        self.iam, self.placement, self.nodes = iam, placement, dict(nodes)
        if not isinstance(route_key, bytes) or len(route_key) < 32:
            raise ValueError('Strong shared route key required')
        self.route_key = route_key

    def _checkpoint(self, stage, envelope):
        """No-op boundary for controlled controller-loss acceptance."""

    def call(self, credential, envelope):
        if not isinstance(envelope, dict) or set(envelope) - {
                'project', 'request_id', 'operation', 'sandbox_id', 'args', 'resource_demand'}:
            raise ValueError('Invalid cluster envelope')
        project = identifier(envelope['project'])
        operation_id = request_id(envelope['request_id'])
        operation = envelope['operation']
        args = envelope.get('args', {})
        if not isinstance(args, dict):
            raise ValueError('Object arguments required')
        if operation == 'project.create':
            if args['parent'] != project:
                raise AuthorizationDenied('Delegation parent must match request project')
            return self.iam.create_project(credential, args['parent'], args['project'],
                Quota(**args['quota']), args['grants'])
        if operation == 'project.policy':
            if args.get('project', project) != project:
                raise AuthorizationDenied('Policy target must match request project')
            return self.iam.set_policy(credential, project, args['grants'])
        if operation == 'credential.issue':
            # The caller generates and retains the token before sending. Its
            # hash is stored, so a lost response cannot strand an unknown secret.
            self.iam.issue_credential(credential, args['principal'], new_token=args['new_token'])
            return {'principal':args['principal'], 'issued':True}
        if operation == 'credential.revoke':
            return self.iam.revoke(credential, args['token'])
        self.iam.authorize(credential, project, permission(operation, args))
        if operation == 'health':
            return dict(protocol=PROTOCOL, protocol_features=[PROTOCOL, 'container-rpc-v1',
                'busy-nonadmission-v1', 'node-admission-v1', NATIVE_FEATURE, STREAM_FEATURE])
        if operation == 'node_status':
            return {'scope':'cluster-derived-view', 'nodes':{
                name:asdict(view) for name, view in self.placement.watcher.snapshot().items()}}
        if operation in ('create', 'container_create'):
            if envelope.get('sandbox_id') is not None:
                raise ValueError('Creation cannot provide a sandbox owner')
            if args.get('baseline_id') is not None or args.get('kind') == 'tb2':
                raise ValueError('Cluster baseline routing and legacy TB2 backend are not supported')
            backend = 'microvm' if operation == 'create' else 'container'
            raw_demand = envelope.get('resource_demand')
            if not isinstance(raw_demand, dict) or set(raw_demand) - {
                    'cpu', 'memory_mb', 'disk_mb', 'network_mbps', 'disk_io_mbps'}:
                raise ValueError('Explicit physical sandbox demand required for project quota')
            if raw_demand.get('disk_io_mbps', 0):
                raise ValueError('Cluster disk-IO quota is not yet supported')
            demand = Quota(concurrency=1, **{k:v for k,v in raw_demand.items() if k != 'disk_io_mbps'})
            environment = args.get('environment_id', 'default') if backend == 'microvm' else args.get('spec', {}).get('environment_id')
            digest = request_digest(operation, None, dict(args=args, demand=asdict(demand)))
            receipt = self.iam.lookup(project, operation_id)
            if receipt is None:
                candidate = self.placement.choose(backend, environment, demand)
            else:
                candidate = receipt['node']
            # IAM rechecks permission at the quota transaction's linearization
            # boundary; an earlier API check alone cannot race a revocation.
            receipt = self.iam.admit(credential, project, operation_id, digest,
                candidate, backend, demand, args=args)
            self._checkpoint('admitted_before_dispatch', envelope)
            result = self.nodes[receipt['node']].create(receipt)
            self._checkpoint('result_before_reply', envelope)
            return result
        sandbox_id = envelope.get('sandbox_id')
        if sandbox_id is None and operation in ('query_request', 'container_query_request'):
            receipt = self.iam.lookup(project, request_id(args['lookup_id']))
            if receipt is None:
                raise ValueError('Provide the sandbox ID for a non-creation request query')
            return self.nodes[receipt['node']].query_create(receipt)
        route = SandboxRoute.decode(sandbox_id, self.route_key)
        if route.project != project:
            raise AuthorizationDenied('Sandbox belongs to another project')
        if route.node not in self.nodes:
            raise ValueError('Owning node is not configured; re-placement is prohibited')
        if operation != 'query_request' and (route.backend == 'container') != (operation.startswith('container_')
                or args.get('backend') == 'container'):
            raise ValueError('Operation/backend mismatch')
        result = self.nodes[route.node].call(route, operation, operation_id, args)
        self._checkpoint('result_before_reply', envelope)
        return result
