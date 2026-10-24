"""Node RPC adapters: wire envelopes in, injected gateway methods out.

No admission ledger, placement decision, recovery policy or retry here.
"""
from dataclasses import asdict
import uuid

from dsec.contracts.cluster import NodeView, Quota, SandboxRoute, request_id
from dsec.contracts.rollout import ROLLOUT_READS


class NodeRPC:
    def __init__(self, gateway):
        self.gateway = gateway

    def dispatch(self, credential, envelope):
        node = self.gateway
        operation = envelope['operation']
        if operation == 'node_probe':
            return asdict(node.health())
        if operation == 'node_lifecycle_proof':
            return node.lifecycle_proof(envelope['version'])
        if operation in ('node_create', 'node_query_create'):
            receipt = envelope['receipt']
            request_id(envelope['request_id'])
            return (node.create if operation == 'node_create' else node.query_create)(receipt)
        if operation in ('node_rollout_create', 'node_rollout_call'):
            request_id(envelope['request_id'])
            return (node.create_rollout(envelope['receipt']) if operation == 'node_rollout_create'
                    else node.call_rollout(envelope['receipt'], envelope['worker_operation'], envelope['args']))
        if operation == 'node_call':
            route = SandboxRoute.decode(envelope['sandbox_id'], node.route_key)
            return node.call(route, envelope['edge_operation'], envelope['request_id'], envelope.get('args', {}))
        raise ValueError('Unknown node gateway operation')


class RemoteNode:
    """Injected controller-side node interface; verified TLS transport supplied by host."""
    def __init__(self, rpc, *, token, route_key):
        self.rpc, self.token, self.route_key = rpc, token, route_key

    def create(self, receipt):
        return self.rpc.send(self.token, dict(operation='node_create',
            request_id=receipt['request_id'], receipt=receipt))

    def create_rollout(self, receipt):
        return self.rpc.send(self.token, dict(operation='node_rollout_create',
            request_id=receipt['request_id'], receipt=receipt), timeout=1810)

    def call_rollout(self, receipt, operation, args):
        return self.rpc.send(self.token, dict(operation='node_rollout_call',
            request_id=uuid.uuid4().hex, receipt=receipt, worker_operation=operation, args=args),
            readonly=operation in ROLLOUT_READS, timeout=12110)

    def query_create(self, receipt):
        return self.rpc.send(self.token, dict(operation='node_query_create',
            request_id=uuid.uuid4().hex, receipt=receipt), readonly=True)

    def call(self, route, operation, operation_id, args):
        return self.rpc.send(self.token, dict(operation='node_call',
            request_id=operation_id, sandbox_id=route.encode(self.route_key),
            edge_operation=operation, args=args), readonly=operation in (
                'status', 'container_status', 'query_request', 'container_query_request', 'container_query_action', 'native_events'))

    def probe(self):
        value = self.rpc.send(self.token, dict(operation='node_probe', request_id=uuid.uuid4().hex), readonly=True, timeout=5)
        return NodeView(value['node_id'], value['boot_id'], tuple(value['backends']),
            tuple(value['environments']), tuple(value['capabilities']), value['load'],
            Quota(**value['available']), value['active'])

    def lifecycle_proof(self, version):
        return self.rpc.send(self.token, dict(operation='node_lifecycle_proof',
            request_id=uuid.uuid4().hex, version=version), readonly=True, timeout=10)

    def security(self):
        return self.rpc.send(self.token,dict(operation='node_security',request_id=uuid.uuid4().hex),readonly=True,timeout=10)
