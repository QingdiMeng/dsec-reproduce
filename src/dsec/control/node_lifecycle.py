"""Quorum-owned membership; Edge/episode services retain execution authority."""
from copy import deepcopy
import hashlib
from urllib.parse import urlsplit

from dsec.contracts.cluster import NoPlacement, identifier, request_id
from dsec.contracts.requests import RequestConflict, request_digest
from dsec.contracts.release import release_ref, same_contract


PREFIX = 'fleet/'
STATES = frozenset(('UNMANAGED', 'JOINING', 'ACTIVE', 'DRAINING', 'MAINTENANCE', 'RETIRED'))


def unmanaged(node):
    return dict(id=identifier(node), state='UNMANAGED', version=0, held=0, spec=None, last=None)


def specification(value):
    if not isinstance(value, dict) or set(value) != {'id', 'endpoints'}:
        raise ValueError('Node registration requires id and HTTPS endpoints only')
    identifier(value['id'])
    endpoints = value['endpoints']
    if (not isinstance(endpoints, list) or not 1 <= len(endpoints) <= 8
            or any(not isinstance(value,str) for value in endpoints) or len(set(endpoints)) != len(endpoints)):
        raise ValueError('One to eight distinct Node endpoints required')
    for endpoint in endpoints:
        parsed = urlsplit(endpoint)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or parsed.path or not parsed.port):
            raise ValueError('Explicit verified HTTPS Node endpoint and port required')
    return deepcopy(value)


class NodeLifecycle:
    def __init__(self, iam):
        self.iam = iam

    def read(self, node):
        return self.iam.store.read(PREFIX + identifier(node)).value

    def records(self):
        return [record.value for record in self.iam.store.scan(PREFIX)]

    def active_nodes(self):
        return {record['id'] for record in self.records() if record['state'] == 'ACTIVE'}

    def join(self, token, operation_id, spec):
        spec = specification(spec)
        return self._transition(token, operation_id, spec['id'], 0, 'join', spec=spec)

    def transition(self, token, operation_id, node, version, action, *, proof=None, release=None):
        if action not in ('activate', 'drain', 'maintain', 'resume', 'retire', 'withdraw', 'upgrade', 'commit', 'rollback'):
            raise ValueError('Unsupported Node lifecycle transition')
        return self._transition(token, operation_id, node, version, action, proof=proof, release=release)

    def _transition(self, token, operation_id, node, version, action, *, spec=None, proof=None, release=None):
        identifier(node); request_id(operation_id)
        if type(version) is not int or version < 0:
            raise ValueError('Explicit nonnegative expected Node version required')
        arguments = dict(version=version, spec=spec)
        if action == 'upgrade':arguments['release'] = release_ref(release)
        elif release is not None:raise ValueError('Only upgrade accepts a target release')
        digest = request_digest('node.'+action, node, arguments)
        key = PREFIX + node

        def change(snapshot):
            # Fleet authority belongs to root policy administrators, never a
            # delegated tenant policy. Authorization joins the same CAS gate.
            self.iam._authorize(snapshot, token, 'root', 'iam.policy')
            old = snapshot.read(key)
            current = old or unmanaged(node)
            if current['last'] and current['last']['request_id'] == operation_id:
                if current['last']['digest'] != digest:
                    raise RequestConflict('Node transition ID reused with different arguments')
                return current, {}
            if current['version'] != version:
                raise RequestConflict('Node version changed; inspect before another transition')
            state = current['state']
            pending = current.get('upgrade')
            if pending and action not in ('commit','rollback'):
                raise RequestConflict('Complete or roll back the pending upgrade before another lifecycle transition')
            if action == 'join':
                if state != 'UNMANAGED' or current['held']:
                    raise RequestConflict('Node identity already enrolled, retired or still owns admissions')
                # Pre-upgrade grants lack the new node counter marker. A read
                # of this Node key fences admissions from upgraded controllers;
                # old binaries must be drained before enabling this workflow.
                if any(not receipt.get('node_accounted') for receipt in self.iam.admissions(node)):
                    raise RequestConflict('Unmigrated Node admissions must finish before enrollment')
                target = 'JOINING'
            else:
                allowed = dict(activate=('JOINING',), drain=('ACTIVE',), maintain=('DRAINING',),
                               resume=('MAINTENANCE',), retire=('DRAINING', 'MAINTENANCE'),withdraw=('JOINING',),
                               upgrade=('MAINTENANCE',),commit=('MAINTENANCE',),rollback=('MAINTENANCE',))
                if state not in allowed[action]:
                    raise RequestConflict('Node transition is not allowed from '+state)
                target = dict(activate='ACTIVE', drain='DRAINING', maintain='MAINTENANCE',
                              resume='ACTIVE', retire='RETIRED',withdraw='RETIRED',
                              upgrade='MAINTENANCE',commit='MAINTENANCE',rollback='MAINTENANCE')[action]
                if action in ('maintain', 'retire', 'withdraw', 'upgrade', 'commit', 'rollback') and current['held'] != 0:
                    raise NoPlacement('Node still owns admitted, running or unknown work')
                if action not in ('drain','withdraw'):
                    if (not isinstance(proof, dict) or proof.get('node_id') != node
                            or proof.get('version') != version or not proof.get('boot_id')):
                        raise NoPlacement('Current owning Node proof required; health expiry is not cleanup')
                    if action in ('maintain', 'retire', 'upgrade', 'commit', 'rollback') and proof.get('empty') is not True:
                        raise NoPlacement('Owning Node has not proved Edge and episode services empty')
            software = {}
            if action in ('upgrade','commit','rollback'):
                observed = release_ref(proof.get('release'))
                if action == 'upgrade':
                    target_release = same_contract(observed, arguments['release'])
                    if observed == target_release:
                        raise RequestConflict('Target release is already running')
                    if current.get('active_release') not in (None, observed):
                        raise RequestConflict('Running release differs from the committed Node release')
                    software = dict(active_release=observed,upgrade=dict(source=observed,target=target_release))
                else:
                    if pending is None:
                        raise RequestConflict('No pending upgrade')
                    expected = pending['target' if action == 'commit' else 'source']
                    if observed != expected:
                        raise NoPlacement('Owning services have not proved the requested release running')
                    software = dict(active_release=expected,upgrade=None)
            if action == 'resume' and current.get('active_release') is not None and proof.get('release') != current['active_release']:
                raise NoPlacement('Committed release must be running before resuming admissions')
            result = dict(current, state=target, version=version+1,
                spec=spec if action == 'join' else current['spec'],
                last=dict(request_id=operation_id, digest=digest))
            result.update(software)
            if proof is not None:
                result['observed_boot_id'] = proof['boot_id']
            writes={key:result}
            if action in ('join','retire','withdraw'):
                for endpoint in result['spec']['endpoints']:
                    route='fleet-routes/'+hashlib.sha256(endpoint.encode()).hexdigest()
                    previous=snapshot.read(route)
                    if action=='join':
                        if previous and previous['owner'] is not None:
                            raise RequestConflict('Node endpoint already belongs to another enrollment')
                        writes[route]=dict(owner=node)
                    else:
                        if not previous or previous['owner'] != node:
                            raise RequestConflict('Node endpoint ownership changed')
                        writes[route]=dict(owner=None)
            return result, writes

        return self.iam._change(change, action='node.'+action)


def admit(snapshot, node, *, required):
    """Same transaction as quota admission, so stale placement cannot bypass drain."""
    record = snapshot.read(PREFIX + node)
    if record is None:
        if required:
            raise NoPlacement('Node has not joined the managed fleet')
        record = unmanaged(node)
    if record['state'] not in ('ACTIVE', 'UNMANAGED') or (required and record['state'] != 'ACTIVE'):
        raise NoPlacement('Node is not active for new admissions')
    return dict(record, held=record['held']+1)


def release(snapshot, node):
    record = snapshot.read(PREFIX + node)
    if record is None or record['held'] <= 0:
        raise RequestConflict('Node admission accounting is missing or inconsistent')
    return dict(record, held=record['held']-1)
