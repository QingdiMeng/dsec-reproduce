"""Authenticated cluster adapter for the existing private Edge protocol.

No KVM/Docker/storage driver here. The node's Edge retains all execution truth.
"""
from contextlib import contextmanager
from dataclasses import asdict
import hashlib
import threading
import uuid

from dsec.contracts.cluster import (AuthorizationDenied, NodeView, Quota,
                                   SandboxRoute, identifier, request_id)
from dsec.contracts.errors import RequestOutcomeUnknown
from dsec.contracts.requests import RequestConflict, RequestPending, request_digest
from dsec.contracts.sandbox import DSecContainerRunArgs
from dsec.sdk.sandbox_transport import ServiceError


def edge_request_id(project, operation_id):
    """Tenant namespace for an Edge's globally keyed journal and native jobs."""
    identifier(project); request_id(operation_id)
    return hashlib.sha256(f'dsec-cluster-v1:{project}:{operation_id}'.encode()).hexdigest()[:32]


def public_proof(proof, operation_id):
    value = dict(proof, request_id=operation_id)
    if 'response' in value:
        value['response'] = dict(value['response'], request_id=operation_id)
    return value


class NodeGateway:
    def __init__(self, node_id, edge, iam, *, route_key, boot_id, environments,
                 backends=('microvm',), capabilities=(), max_sandboxes=1,
                 minimum_demands=None, inspection=None):
        self.node_id, self.edge, self.iam = identifier(node_id), edge, iam
        self.inspection = inspection or edge
        if not isinstance(route_key, bytes) or len(route_key) < 32:
            raise ValueError('Strong route key required')
        self.route_key, self.boot_id = route_key, boot_id
        self.environments, self.backends = tuple(environments), tuple(backends)
        self.capabilities = tuple(capabilities)
        if type(max_sandboxes) is not int or max_sandboxes <= 0:
            raise ValueError('Explicit Edge capacity required')
        self.max_sandboxes = max_sandboxes
        self.minimum_demands = {name:Quota(**value) for name,value in (minimum_demands or {}).items()}
        if any(name not in self.minimum_demands for name in self.environments):
            raise ValueError('Every advertised environment requires a trusted minimum quota demand')
        self.lock, self.requests = threading.Lock(), {}

    @contextmanager
    def _admission(self, operation_id):
        # The node service holds an OS owner lock in its composition entrypoint.
        # Only create/bind and stop/release share this gate; native readers and
        # commands still use the existing Edge admission/concurrency mechanisms.
        with self.lock:
            entry = self.requests.setdefault(operation_id, [threading.Lock(), 0])
            entry[1] += 1
        entry[0].acquire()
        try:
            yield
        finally:
            entry[0].release()
            with self.lock:
                entry[1] -= 1
                if entry[1] == 0:
                    self.requests.pop(operation_id)

    def _receipt(self, receipt):
        current = self.iam.lookup(receipt['project'], receipt['request_id'])
        if current is None or current['node'] != self.node_id or (
                current['digest'] != receipt['digest'] or current['args'] != receipt['args']):
            raise AuthorizationDenied('Quota admission/target mismatch')
        return current

    def _lookup(self, backend, project, operation_id):
        operation = 'query_request' if backend == 'microvm' else 'container_query_request'
        return public_proof(self.inspection.call(operation,
            lookup_id=edge_request_id(project, operation_id)), operation_id)

    def _result(self, receipt, result):
        local_id = result['id']
        self.iam.bind(receipt['project'], receipt['request_id'], self.node_id, local_id)
        route = SandboxRoute(receipt['project'], self.node_id, receipt['backend'], local_id, receipt['request_id'])
        return dict(result, id=route.encode(self.route_key))

    def _creation_proof(self, receipt):
        proof = self._lookup(receipt['backend'], receipt['project'], receipt['request_id'])
        if proof['state'] != 'NOT_FOUND':
            args = receipt['args'] if receipt['backend'] == 'microvm' else (
                DSecContainerRunArgs(**receipt['args']['spec']).lifecycle_args())
            if (proof.get('operation') != 'create' or proof.get('sandbox_id') is not None
                    or proof.get('digest') != request_digest('create', None, args)):
                raise RequestConflict('Edge request ID belongs to a different operation/arguments')
        return proof

    def create(self, receipt):
        with self._admission((receipt['project'], receipt['request_id'])):
            receipt = self._receipt(receipt)
            if receipt['state'] == 'RELEASED':
                raise RequestConflict('Released admission cannot create another sandbox')
            backend, args = receipt['backend'], receipt['args']
            environment = args.get('environment_id', 'default') if backend == 'microvm' else args.get('spec', {}).get('environment_id')
            minimum = self.minimum_demands.get(environment)
            if backend not in self.backends or minimum is None:
                raise ValueError('Node does not provide this backend/environment')
            demand = Quota(**receipt['demand'])
            if backend == 'container':
                spec = DSecContainerRunArgs(**args['spec']).validate()
                minimum = Quota(1, max(minimum.cpu,spec.cpu_cores_limit),
                    max(minimum.memory_mb,spec.memory_limit_mb),minimum.disk_mb,minimum.network_mbps)
            if not minimum.fits(demand):
                # This request has not been sent. Fence its admission before
                # returning quota, so a later duplicate cannot dispatch it.
                if receipt['state']=='ADMITTED' and self._lookup(backend,
                        receipt['project'],receipt['request_id'])['state']=='NOT_FOUND':
                    self.iam.release(receipt['project'],receipt['request_id'],self.node_id,nonadmission=True)
                raise ServiceError('AdmissionDenied','Requested quota understates configured environment resources',
                    {'minimum':asdict(minimum),'requested':asdict(demand)})
            if backend == 'container' and args.get('kind', 'container') != 'container':
                raise ValueError('Legacy TB2 runtime is not a cluster backend')
            proof = self._creation_proof(receipt)
            if proof['state'] == 'DONE':
                response = proof['response']
                if not response['ok']:
                    raise ServiceError(response['error']['type'], response['error']['message'])
                return self._result(receipt, response['result'])
            if proof['state'] == 'UNKNOWN':
                raise RequestOutcomeUnknown('Edge create outcome unknown; no replay or re-placement', receipt['request_id'])
            if proof['state'] == 'PENDING':
                raise RequestPending('Edge creation is pending; query this same node')
            if proof['state'] != 'NOT_FOUND' or receipt['state'] != 'ADMITTED':
                # A queued/delayed request may not yet be visible in the Edge
                # journal. Once a send was possible, never send it a second time.
                raise RequestOutcomeUnknown('Dispatch may have reached Edge; NOT_FOUND cannot authorize replay', receipt['request_id'])
            hint = {name:value for name,value in receipt['demand'].items() if name != 'concurrency'}
            receipt = self.iam.begin_dispatch(receipt['project'], receipt['request_id'], self.node_id)
            try:
                result = self.edge.call('create' if backend == 'microvm' else 'container_create',
                    request_id=edge_request_id(receipt['project'], receipt['request_id']), resource_demand=hint, **args)
            except ServiceError as exc:
                # Only explicit non-admission AND an absent Edge intent permits
                # returning quota. An arbitrary failure/timeout never does.
                if exc.kind in ('ServiceBusy', 'NodeAdmissionBusy', 'AdmissionDenied'):
                    if self._lookup(backend, receipt['project'], receipt['request_id'])['state'] == 'NOT_FOUND':
                        self.iam.release(receipt['project'], receipt['request_id'],
                                         self.node_id, nonadmission=True)
                raise
            return self._result(receipt, result)

    def query_create(self, receipt):
        with self._admission((receipt['project'], receipt['request_id'])):
            receipt = self._receipt(receipt)
            proof = self._creation_proof(receipt)
            if proof['state'] == 'NOT_FOUND' and receipt['state'] == 'DISPATCHED':
                return dict(proof, state='UNKNOWN', reason='dispatch_may_be_delayed')
            if proof['state'] == 'DONE' and proof['response']['ok']:
                # Quota release is final. Re-querying old DONE creation cannot
                # re-bind or restore a released sandbox's quota.
                result = proof['response']['result']
                route = SandboxRoute(receipt['project'], self.node_id, receipt['backend'],
                                     result['id'], receipt['request_id'])
                if receipt['state'] != 'RELEASED':
                    self.iam.bind(receipt['project'], receipt['request_id'], self.node_id, result['id'])
                proof = dict(proof, response=dict(proof['response'], result=dict(result, id=route.encode(self.route_key))))
            return proof

    def call(self, route, operation, operation_id, args):
        request_id(operation_id)
        receipt = self.iam.lookup(route.project, route.admission_id)
        if (route.node != self.node_id or receipt is None or receipt['node'] != self.node_id
                or receipt['sandbox_id'] != route.local_id or receipt['backend'] != route.backend):
            raise AuthorizationDenied('Sandbox route does not match its admitted owner')
        if route.backend == 'container' and args.get('spec', receipt['args'].get('spec')) != receipt['args'].get('spec'):
            raise RequestConflict('Container specification changed from original admission')
        values = dict(args)
        private_id = edge_request_id(route.project, operation_id)
        lookup_id = values.get('lookup_id')
        if lookup_id is not None:
            values['lookup_id'] = edge_request_id(route.project, lookup_id)
        if route.backend == 'container' and operation not in ('native', 'native_stream', 'native_events', 'native_cancel', 'container_query_request'):
            values['spec'] = receipt['args']['spec']
        if operation in ('query_request', 'container_query_request'):
            proof = public_proof(self.edge.call(operation, route.local_id,
                request_id=private_id, **values), lookup_id)
            if proof['state'] != 'NOT_FOUND' and proof.get('sandbox_id') != route.local_id:
                raise AuthorizationDenied('Request belongs to another sandbox')
            return dict(proof, **({'sandbox_id':route.encode(self.route_key)} if proof['state'] != 'NOT_FOUND' else {}))
        if operation in ('stop', 'container_stop'):
            with self._admission((route.project, route.admission_id)):
                result = self.edge.call(operation, route.local_id, request_id=private_id, **values)
                # A terminal stopped result is necessary; a response saying
                # only "accepted" is not quota release evidence.
                status_operation = 'status' if route.backend == 'microvm' else 'container_status'
                status_args = {} if route.backend == 'microvm' else {'spec':receipt['args']['spec']}
                status = self.edge.call(status_operation, route.local_id, **status_args)
                if status.get('state') != 'STOPPED' or status.get('resource_cleanup_complete') is False:
                    raise RequestOutcomeUnknown('Stop not attested; quota remains held', operation_id)
                self.iam.release(route.project, route.admission_id, self.node_id, sandbox_id=route.local_id)
        else:
            result = self.edge.call(operation, route.local_id, request_id=private_id, **values)
        if isinstance(result, dict) and result.get('operation_id') == private_id:
            result = dict(result, operation_id=operation_id)
        elif isinstance(result, dict) and lookup_id is not None and result.get('operation_id') == values['lookup_id']:
            result = dict(result, operation_id=lookup_id)
        if isinstance(result, dict) and result.get('id') == route.local_id:
            result = dict(result, id=route.encode(self.route_key))
        return result

    def health(self):
        health = self.inspection.call('health')
        if 'node-admission-v1' not in health.get('protocol_features', []):
            raise RuntimeError('Cluster node requires Edge-owned physical admission')
        state = self.inspection.call('node_status')
        if state.get('recovery_errors'):
            raise RuntimeError('Node resource recovery is uncertain')
        budget, reserved, sample = state['budget'], state['reserved'], state['sample']
        active = len(state['leases'])
        available = Quota(max(0, self.max_sandboxes-active),
            max(0, budget['cpu']-reserved['cpu']),
            max(0, min(budget['memory_mb']-reserved['memory_mb'], sample['memory_available_mb']-budget['min_memory_free_mb'])),
            max(0, min(budget['disk_mb']-reserved['disk_mb'], sample['disk_available_mb']-budget['min_disk_free_mb'])),
            max(0, budget['network_mbps']-reserved['network_mbps']))
        load = max(sample['cpu_utilization'], sample['disk_busy'], active/self.max_sandboxes)
        return NodeView(self.node_id, self.boot_id, self.backends, self.environments,
                        self.capabilities, load, available, active)

    def reconcile(self):
        """TTL/controller-crash reconciliation from Edge proofs, never health expiry."""
        for receipt in self.iam.admissions(self.node_id):
            with self._admission((receipt['project'], receipt['request_id'])):
                current = self.iam.lookup(receipt['project'], receipt['request_id'])
                if current['state'] == 'RELEASED':
                    continue
                if current['state'] in ('ADMITTED', 'DISPATCHED'):
                    proof = self._creation_proof(current)
                    if proof['state'] != 'DONE' or not proof['response']['ok']:
                        # NOT_FOUND cannot prove a delayed dispatch will never
                        # arrive. UNKNOWN/PENDING never permit quota release.
                        continue
                    self._result(current, proof['response']['result'])
                    current = self.iam.lookup(current['project'], current['request_id'])
                operation = 'status' if current['backend']=='microvm' else 'container_status'
                args = {} if current['backend']=='microvm' else {'spec':current['args']['spec']}
                status = self.inspection.call(operation, current['sandbox_id'], **args)
                if status.get('state') == 'STOPPED' and status.get('resource_cleanup_complete') is not False:
                    self.iam.release(current['project'], current['request_id'], self.node_id,
                                     sandbox_id=current['sandbox_id'])

    def dispatch(self, credential, envelope):
        operation = envelope['operation']
        if operation == 'node_probe':
            return asdict(self.health())
        if operation in ('node_create', 'node_query_create'):
            receipt = envelope['receipt']
            request_id(envelope['request_id'])
            return (self.create if operation == 'node_create' else self.query_create)(receipt)
        if operation == 'node_call':
            route = SandboxRoute.decode(envelope['sandbox_id'], self.route_key)
            return self.call(route, envelope['edge_operation'], envelope['request_id'], envelope.get('args', {}))
        raise ValueError('Unknown node gateway operation')


class RemoteNode:
    """Injected controller-side node interface; verified TLS transport supplied by host."""
    def __init__(self, rpc, *, token, route_key):
        self.rpc, self.token, self.route_key = rpc, token, route_key

    def create(self, receipt):
        return self.rpc.send(self.token, dict(operation='node_create',
            request_id=receipt['request_id'], receipt=receipt))

    def query_create(self, receipt):
        return self.rpc.send(self.token, dict(operation='node_query_create',
            request_id=uuid.uuid4().hex, receipt=receipt), readonly=True)

    def call(self, route, operation, operation_id, args):
        return self.rpc.send(self.token, dict(operation='node_call',
            request_id=operation_id, sandbox_id=route.encode(self.route_key),
            edge_operation=operation, args=args), readonly=operation in (
                'status', 'container_status', 'query_request', 'container_query_request', 'native_events'))

    def probe(self):
        value = self.rpc.send(self.token, dict(operation='node_probe', request_id=uuid.uuid4().hex), readonly=True, timeout=5)
        return NodeView(value['node_id'], value['boot_id'], tuple(value['backends']),
            tuple(value['environments']), tuple(value['capabilities']), value['load'],
            Quota(**value['available']), value['active'])
