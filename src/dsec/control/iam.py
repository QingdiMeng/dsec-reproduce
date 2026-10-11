"""Quorum-backed identity, bounded project delegation and durable quota grants.

Grant records own quota and dispatch identity, never guest execution results.
The Edge journal remains the only source of execution/recovery truth.
"""
from dataclasses import asdict
import hashlib
import re
import secrets
import uuid
from datetime import datetime, timezone

from dsec.contracts.cluster import (AuthorizationDenied, CoordinationUnavailable,
    PERMISSIONS, Quota, QuotaExceeded, ZERO_QUOTA, identifier, request_id)
from dsec.contracts.requests import RequestConflict


def credential_key(token):
    if not isinstance(token, str) or len(token) < 32 or len(token) > 512:
        raise AuthorizationDenied('Invalid credential')
    return 'credentials/' + hashlib.sha256(token.encode()).hexdigest()


class Snapshot:
    def __init__(self, store):
        self.store, self.records = store, {}

    def read(self, key):
        if key not in self.records:
            self.records[key] = self.store.read(key)
        return self.records[key].value

    def commit(self, writes):
        return self.store.transact(tuple(self.records.values()), writes)


class IAM:
    def __init__(self, store):
        self.store = store

    def _change(self, function, *, action='coordination.write', actor=None):
        audit_key = 'audit/' + uuid.uuid4().hex
        for _ in range(32):
            snapshot = Snapshot(self.store)
            value, writes = function(snapshot)
            if writes:
                # Audit the authorization/quota transaction atomically, without
                # storing raw credentials, commands or execution results.
                snapshot.read(audit_key)
                principal = actor or next((record.value['principal']
                    for key,record in snapshot.records.items()
                    if key.startswith('credentials/') and record.value), 'provisioner')
                writes = dict(writes, **{audit_key:dict(action=action, actor=principal,
                    changed_keys=sorted(writes), timestamp=datetime.now(timezone.utc).isoformat())})
            # An empty write set still validates every policy/credential read.
            if snapshot.commit(writes):
                return value
        raise CoordinationUnavailable('IAM compare contention; request was not acknowledged')

    def bootstrap(self, token, quota):
        """Administrator provisioning only; never exposed to unauthenticated RPC."""
        key = credential_key(token)
        def change(snapshot):
            project = snapshot.read('projects/root')
            credential = snapshot.read(key)
            expected = dict(principal='admin', revoked=False)
            if project is not None:
                if credential != expected:
                    raise AuthorizationDenied('IAM already bootstrapped with another identity')
                if project['quota'] != asdict(quota):
                    raise RequestConflict('Bootstrap quota changed; use an explicit administration workflow')
                return 'root', {}
            if credential is not None:
                raise RequestConflict('Bootstrap credential already exists')
            record = dict(parent=None, quota=asdict(quota), delegated=asdict(ZERO_QUOTA),
                usage=asdict(ZERO_QUOTA), grants={'admin':sorted(PERMISSIONS)},
                ceiling=sorted(PERMISSIONS))
            return 'root', {'projects/root':record, key:expected}
        return self._change(change, action='bootstrap')

    def _authorize(self, snapshot, token, project, action):
        if action not in PERMISSIONS:
            raise AuthorizationDenied('Unknown permission')
        identifier(project)
        credential = snapshot.read(credential_key(token))
        if credential is None or credential.get('revoked'):
            raise AuthorizationDenied('Invalid or revoked credential')
        principal = credential['principal']
        record = snapshot.read('projects/' + project)
        if record is None:
            raise AuthorizationDenied('Project not authorized')
        permissions = set(record['grants'].get(principal, []))
        current = record
        visited = {project}
        while current is not None:
            permissions.intersection_update(current['ceiling'])
            parent = current['parent']
            if parent is None:
                break
            if parent in visited or len(visited) >= 16:
                raise AuthorizationDenied('Invalid or overdeep project hierarchy')
            visited.add(parent)
            current = snapshot.read('projects/' + identifier(parent))
            if current is None:
                raise AuthorizationDenied('Project parent unavailable')
        if action not in permissions:
            raise AuthorizationDenied('Permission denied')
        return principal, record, permissions

    def authorize(self, token, project, action):
        def check(snapshot):
            principal, _, permissions = self._authorize(snapshot, token, project, action)
            return dict(principal=principal, project=project, permissions=sorted(permissions)), {}
        return self._change(check)

    def issue_credential(self, token, principal, *, new_token=None):
        identifier(principal)
        new_token = new_token or secrets.token_urlsafe(48)
        key = credential_key(new_token)
        def change(snapshot):
            self._authorize(snapshot, token, 'root', 'iam.credentials')
            old = snapshot.read(key)
            value = dict(principal=principal, revoked=False)
            if old is not None and old != value:
                raise RequestConflict('Credential identity conflict')
            if old == value:
                return new_token, {}
            return new_token, {key:value}
        return self._change(change, action='credential.issue')

    def revoke(self, token, target_token):
        key = credential_key(target_token)
        def change(snapshot):
            self._authorize(snapshot, token, 'root', 'iam.credentials')
            old = snapshot.read(key)
            if old is None:
                raise AuthorizationDenied('Unknown credential')
            if old.get('revoked'):
                return None, {}
            return None, {key:dict(old, revoked=True)}
        return self._change(change, action='credential.revoke')

    def create_project(self, token, parent, project, quota, grants):
        identifier(parent); identifier(project)
        if not isinstance(grants, dict) or not grants:
            raise ValueError('Project permission grants required')
        normalized = {}
        for principal, permissions in grants.items():
            identifier(principal)
            if not isinstance(permissions, (list, tuple, set)) or not set(permissions) <= PERMISSIONS:
                raise ValueError('Invalid project permissions')
            normalized[principal] = sorted(set(permissions))
        def change(snapshot):
            _, record, allowed = self._authorize(snapshot, token, parent, 'iam.delegate')
            if sum(key.startswith('projects/') for key in snapshot.records) >= 16:
                raise ValueError('Project hierarchy exceeds sixteen levels')
            ceiling = set().union(*(set(v) for v in normalized.values()))
            if not ceiling <= allowed:
                raise AuthorizationDenied('Cannot delegate permissions caller does not possess')
            key = 'projects/' + project
            old = snapshot.read(key)
            if old is not None:
                if (old['parent'] != parent or old['quota'] != asdict(quota)
                        or old['grants'] != normalized):
                    raise RequestConflict('Project identity already used')
                return project, {}
            # Delegation consumes a portion of the parent's budget. Parent
            # direct usage and all sibling quotas share this single CAS gate.
            delegated = Quota(**record['delegated']).plus(quota)
            if not delegated.plus(Quota(**record['usage'])).fits(Quota(**record['quota'])):
                raise QuotaExceeded('Delegation exceeds remaining parent quota')
            child = dict(parent=parent, quota=asdict(quota), delegated=asdict(ZERO_QUOTA),
                usage=asdict(ZERO_QUOTA), grants=normalized, ceiling=sorted(ceiling))
            return project, {'projects/' + parent:dict(record, delegated=asdict(delegated)), key:child}
        return self._change(change, action='project.create')

    def set_policy(self, token, project, grants):
        """Changing an ancestor ceiling immediately bounds descendant permissions."""
        if not isinstance(grants, dict) or not grants:
            raise ValueError('Nonempty policy required')
        normalized = {}
        for principal, permissions in grants.items():
            identifier(principal)
            if not isinstance(permissions, (list, tuple, set)) or not set(permissions) <= PERMISSIONS:
                raise ValueError('Invalid policy permissions')
            normalized[principal] = sorted(set(permissions))
        def change(snapshot):
            _, record, allowed = self._authorize(snapshot, token, project, 'iam.policy')
            ceiling = set().union(*(set(v) for v in normalized.values()))
            if not ceiling <= allowed:
                raise AuthorizationDenied('Policy would elevate caller permissions')
            if record['grants'] == normalized and record['ceiling'] == sorted(ceiling):
                return None, {}
            return None, {'projects/' + project:dict(record, grants=normalized, ceiling=sorted(ceiling))}
        return self._change(change, action='project.policy')

    def admit(self, token, project, operation_id, digest, node, backend, demand, *, args=None):
        identifier(project); identifier(node); request_id(operation_id)
        if not re.fullmatch(r'[0-9a-f]{64}', digest) or backend not in ('microvm', 'container'):
            raise ValueError('Invalid admission identity')
        if demand.concurrency != 1:
            raise ValueError('One sandbox per quota admission required')
        if not isinstance(args, dict):
            raise ValueError('Canonical creation arguments required')
        key = f'admissions/{project}/{operation_id}'
        def change(snapshot):
            principal, record, _ = self._authorize(snapshot, token, project, 'sandbox.create')
            old = snapshot.read(key)
            if old is not None:
                if (old['digest'] != digest or old['demand'] != asdict(demand)
                        or old['backend'] != backend or old['args'] != args):
                    raise RequestConflict('Admission ID reused with different request')
                # The existing target wins, even if another API chose a different
                # candidate. Losing a response never authorizes re-placement.
                return old, {}
            usage = Quota(**record['usage']).plus(demand)
            if not usage.plus(Quota(**record['delegated'])).fits(Quota(**record['quota'])):
                raise QuotaExceeded('Project quota exceeded')
            receipt = dict(project=project, request_id=operation_id, principal=principal,
                digest=digest, node=node, backend=backend, demand=asdict(demand),
                state='ADMITTED', sandbox_id=None, args=args)
            return receipt, {key:receipt, 'projects/' + project:dict(record, usage=asdict(usage))}
        return self._change(change, action='sandbox.admit')

    def lookup(self, project, operation_id):
        identifier(project); request_id(operation_id)
        return self.store.read(f'admissions/{project}/{operation_id}').value

    def admissions(self, node):
        identifier(node)
        return [record.value for record in self.store.scan('admissions/')
                if record.value['node'] == node and record.value['state'] != 'RELEASED']

    def begin_dispatch(self, project, operation_id, node):
        """Durable send boundary; NOT_FOUND after this point is not non-admission."""
        identifier(project); identifier(node); request_id(operation_id)
        key = f'admissions/{project}/{operation_id}'
        def change(snapshot):
            old = snapshot.read(key)
            if old is None or old['node'] != node or old['state'] != 'ADMITTED':
                raise RequestConflict('Admission cannot begin another dispatch')
            result = dict(old, state='DISPATCHED')
            return result, {key:result}
        return self._change(change, action='sandbox.dispatch', actor='node:' + node)

    def bind(self, project, operation_id, node, sandbox_id):
        """Trusted gateway after an authoritative create result; not a user API."""
        identifier(project); identifier(node); request_id(operation_id)
        if not isinstance(sandbox_id, str) or not re.fullmatch(r'[0-9a-f]{12}|[0-9a-f]{32}', sandbox_id):
            raise ValueError('Invalid local Edge sandbox ID')
        key = f'admissions/{project}/{operation_id}'
        def change(snapshot):
            old = snapshot.read(key)
            if old is None or old['node'] != node or old['state'] == 'RELEASED':
                raise RequestConflict('Admission target is not bindable')
            if old['sandbox_id'] not in (None, sandbox_id):
                raise RequestConflict('Admission already bound to a different sandbox')
            if old['state'] == 'BOUND' and old['sandbox_id'] == sandbox_id:
                return old, {}
            return dict(old, state='BOUND', sandbox_id=sandbox_id), {
                key:dict(old, state='BOUND', sandbox_id=sandbox_id)}
        return self._change(change, action='sandbox.bind', actor='node:' + node)

    def release(self, project, operation_id, node, *, sandbox_id=None, nonadmission=False):
        """Trusted Edge proof only. Health expiry/unknown outcomes cannot call this."""
        identifier(project); identifier(node); request_id(operation_id)
        key = f'admissions/{project}/{operation_id}'
        def change(snapshot):
            old = snapshot.read(key)
            if old is None or old['node'] != node:
                raise RequestConflict('Release target does not match admission')
            if old['state'] == 'RELEASED':
                return old, {}
            if nonadmission:
                if old['state'] not in ('ADMITTED', 'DISPATCHED') or old['sandbox_id'] is not None:
                    raise RequestConflict('Bound sandbox cannot be called non-admitted')
            elif sandbox_id is None or sandbox_id != old['sandbox_id']:
                raise RequestConflict('Authoritative stopped sandbox identity required')
            record = snapshot.read('projects/' + project)
            usage = Quota(**record['usage']).minus(Quota(**old['demand']))
            result = dict(old, state='RELEASED')
            return result, {key:result, 'projects/' + project:dict(record, usage=asdict(usage))}
        return self._change(change, action='sandbox.release', actor='node:' + node)
