"""Node-local worker bridge. IAM routes; the worker remains episode truth."""
from copy import deepcopy

from dsec.contracts.cluster import AuthorizationDenied, Quota, SandboxRoute
from dsec.contracts.errors import RequestOutcomeUnknown
from dsec.contracts.requests import RequestConflict
from dsec.contracts.rollout import ROLLOUT_PERMISSIONS, is_rollout, private_rollout_id
from dsec.sdk.rollout_transport import RolloutOutcomeUnknown, RolloutServiceError


class RolloutGateway:
    def __init__(self, node, worker, inspection):
        self.node, self.worker, self.inspection = node, worker, inspection

    def _call(self, transport, operation, **args):
        try:
            return transport.call(operation, **args)
        except RolloutOutcomeUnknown as exc:
            raise RequestOutcomeUnknown('Worker reply unknown; original rollout owner retained') from exc
        except RolloutServiceError as exc:
            if exc.kind in ('RequestOutcomeUnknown','CommandOutcomeUnknown','RolloutOutcomeUnknown'):
                raise RequestOutcomeUnknown('Worker execution uncertain; do not replay') from exc
            raise

    def healthy(self):
        if self.worker is None:
            return False
        health = self._call(self.inspection, 'health', timeout_s=3)
        expected = getattr(self.node.edge, 'socket_path', None)
        return (health.get('durable_journal') is True and health.get('scheduler_enabled') is True
                and (expected is None or health.get('edge_socket') == expected))

    def _receipt(self, receipt):
        current = self.node._receipt(receipt)
        if not is_rollout(current) or self.worker is None:
            raise AuthorizationDenied('Not an admitted rollout owner')
        return current

    def _observe(self, receipt, view):
        receipt = self._receipt(receipt)
        args = receipt['args']['rollout']
        private = private_rollout_id(receipt['project'], receipt['request_id'])
        if view.get('rollout_id') != private:
            raise AuthorizationDenied('Worker returned another rollout')
        if view.get('state') == 'NOT_FOUND':
            raise RequestOutcomeUnknown('Dispatched rollout may still arrive; creation is not replayed', receipt['request_id'])
        if (view.get('task_id') != args['task_id'] or view.get('profile') != args['profile']
                or view.get('resource_demand') != args['resources']):
            raise RequestConflict('Worker creation identity differs from admission')
        sid = view.get('sandbox_id')
        if sid is not None:
            if receipt['sandbox_id'] not in (None, sid):
                raise AuthorizationDenied('Rollout sandbox identity changed')
            if receipt['state'] != 'RELEASED':
                receipt = self.node.iam.bind(receipt['project'], receipt['request_id'], self.node.node_id, sid)
        if view['state'] == 'STOPPED' and receipt['state'] != 'RELEASED':
            if sid is None:
                raise RequestOutcomeUnknown('Unbound creation retirement unproved; quota retained', receipt['request_id'])
            else:
                status = self._call(self.inspection, 'sandbox_status', rollout_id=private, timeout_s=3)
                if (status.get('id') != sid or status.get('state') != 'STOPPED'
                        or status.get('resource_cleanup_complete') is False):
                    raise RequestOutcomeUnknown('Cleanup unproved; quota retained', receipt['request_id'])
                self.node.iam.release(receipt['project'], receipt['request_id'], self.node.node_id, sandbox_id=sid)
        return view

    def _public(self, receipt, result):
        value = deepcopy(result)
        def convert(view):
            if view.get('rollout_id') is not None:
                if view['rollout_id'] != private_rollout_id(receipt['project'], receipt['request_id']):
                    raise AuthorizationDenied('Worker response belongs to another rollout')
                view['rollout_id'] = receipt['request_id']
            if view.get('sandbox_id') is not None:
                view['sandbox_id'] = SandboxRoute(receipt['project'], self.node.node_id,
                    receipt['backend'], view['sandbox_id'], receipt['request_id']).encode(self.node.route_key)
            if view.get('baseline_rollout_id') is not None:
                view['baseline_rollout_id'] = receipt['args']['rollout'].get('baseline_rollout_id')
            view.pop('baseline_sandbox_id', None)
        convert(value)
        for key in ('rollout','dialogue'):
            if isinstance(value.get(key), dict):
                convert(value[key])
        return value

    def create(self, receipt):
        with self.node._admission((receipt['project'], receipt['request_id'])):
            receipt = self._receipt(receipt)
            if receipt['state'] == 'RELEASED':
                raise RequestConflict('Released rollout cannot be dispatched')
            args = dict(receipt['args']['rollout'])
            env = args['profile'].get('environment_id') or 'default'
            minimum = self.node.minimum_demands.get(env)
            if (receipt['backend'] not in self.node.backends or minimum is None
                    or not minimum.fits(Quota(**receipt['demand']))):
                if receipt['state'] == 'ADMITTED':
                    self.node.iam.release(receipt['project'], receipt['request_id'], self.node.node_id, nonadmission=True)
                raise ValueError('Rollout does not meet Node environment minimum')
            if not self.healthy():
                raise RequestOutcomeUnknown('Worker not ready; original admission retained', receipt['request_id'])
            args['rollout_id'] = private_rollout_id(receipt['project'], receipt['request_id'])
            if args.get('baseline_rollout_id') is not None:
                source = self.node.iam.lookup(receipt['project'], args['baseline_rollout_id'])
                if source is None or source['node'] != self.node.node_id or not is_rollout(source):
                    raise AuthorizationDenied('Baseline belongs to another Node')
                args['baseline_rollout_id'] = private_rollout_id(receipt['project'], args['baseline_rollout_id'])
            if receipt['state'] == 'ADMITTED':
                self.node.iam.begin_dispatch(receipt['project'], receipt['request_id'], self.node.node_id)
                result = self._call(self.worker, 'create', timeout_s=1800, **args)
            else:
                result = self._call(self.inspection, 'lookup', rollout_id=args['rollout_id'], timeout_s=3)
            receipt = self.node.iam.lookup(receipt['project'], receipt['request_id'])
            self._observe(receipt, result)
            return self._public(receipt, result)

    def call(self, receipt, operation, args):
        if operation not in ROLLOUT_PERMISSIONS or operation == 'create':
            raise ValueError('Unsupported worker operation')
        receipt = self._receipt(receipt)
        values = dict(args)
        if values.get('rollout_id') != receipt['request_id']:
            raise AuthorizationDenied('Rollout identity differs from admission')
        values['rollout_id'] = private_rollout_id(receipt['project'], receipt['request_id'])
        if receipt['state'] == 'RELEASED' and operation not in ('status','dialogue_status','resource_status','stop'):
            raise RequestConflict('Released rollout is terminal')
        if not self.healthy():
            raise RequestOutcomeUnknown('Owning worker unavailable or bound to another Edge')
        if operation != 'status':
            prior = self._call(self.inspection,'lookup',rollout_id=values['rollout_id'],timeout_s=3)
            self._observe(receipt,prior)  # creation/physical owner checked before guest effects
        # API and Node deadlines cover the official task budget. Caller values
        # cannot inject timeout/host settings into the worker's operation args.
        result = (self._call(self.inspection, 'lookup', timeout_s=3, **values) if operation == 'status'
                  else self._call(self.worker, operation, timeout_s=12100, **values))
        if result.get('state') == 'NOT_FOUND':
            raise RequestOutcomeUnknown('Rollout not observed yet; original owner retained', receipt['request_id'])
        view = result.get('rollout') if isinstance(result.get('rollout'), dict) else result
        if 'profile' in view:
            self._observe(receipt, view)
        return self._public(receipt, result)

    def reconcile(self, receipt):
        receipt = self._receipt(receipt)
        value = self._call(self.inspection, 'lookup', rollout_id=private_rollout_id(
            receipt['project'], receipt['request_id']), timeout_s=3)
        if value.get('state') != 'NOT_FOUND':
            self._observe(receipt, value)
