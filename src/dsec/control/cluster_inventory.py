"""Authorized bounded browsing; quota receipts are not a VM state database."""
from copy import deepcopy
import hashlib
import hmac
import re

from dsec.contracts.cluster import AuthorizationDenied, SandboxRoute


OPERATIONS = frozenset(('project.get', 'project.list', 'admission.list', 'sandbox.list', 'audit.list'))


def browse(iam, token, project, operation, args, route_key):
    if set(args) - {'limit', 'after'}:
        raise ValueError('Unsupported inventory arguments')
    limit, after = args.get('limit', 50), args.get('after', '')
    if type(limit) is not int or not 1 <= limit <= 100 or not isinstance(after, str) or (after and not re.fullmatch(r'[0-9a-f]{64}', after)):
        raise ValueError('Invalid inventory page')

    def read(snapshot):
        identity, current, permissions = iam._authorize(snapshot, token, project, 'sandbox.read')

        def public_project(name, value, allowed):
            result = dict(project=name, parent=value['parent'], quota=value['quota'],
                delegated=value['delegated'], usage=value['usage'], permissions=sorted(allowed))
            if 'iam.policy' in allowed:
                result['grants'] = value['grants']
                result['ceiling'] = value['ceiling']
            return deepcopy(result)

        if operation == 'project.get':
            return dict(principal=identity, **public_project(project, current, permissions)), {}
        if operation == 'project.list':
            prefix = 'projects/'
            selected = lambda value: value['parent'] == project
        elif operation in ('admission.list', 'sandbox.list'):
            prefix = f'admissions/{project}/'
            selected = lambda value: operation == 'admission.list' or value['sandbox_id'] is not None
        else:
            iam._authorize(snapshot, token, project, 'iam.policy')
            prefix = 'audit/'
            selected = lambda value: any(key == 'projects/' + project or
                key.startswith(f'admissions/{project}/') or (project == 'root' and
                    'iam.credentials' in permissions and key.startswith('credentials/')) or
                (project == 'root' and 'iam.policy' in permissions and
                    key.startswith(('fleet/', 'fleet-routes/')))
                for key in value['changed_keys'])
        # etcd's scan is revision-pinned and bounded. Individual record reads
        # join the same CAS guard as the credential and ancestor policies.
        def cursor(key):
            # Hidden project names must not leak through pagination cursors.
            body = f'console-v1:{project}:{operation}:{key}'.encode()
            return hmac.new(route_key, body, hashlib.sha256).hexdigest()
        scanned = sorted((r for r in iam.store.scan(prefix) if selected(r.value)), key=lambda r:r.key)
        start = 0
        if after:
            positions = [i for i,record in enumerate(scanned) if hmac.compare_digest(cursor(record.key), after)]
            if not positions:
                raise ValueError('Inventory cursor expired or belongs to another scope; refresh the first page')
            start = positions[0] + 1
        records = scanned[start:start + limit + 1]
        items = []
        for record in records[:limit]:
            value = snapshot.read(record.key)
            if value is None or not selected(value):
                continue
            if operation == 'project.list':
                name = record.key[len(prefix):]
                try:
                    _, value, allowed = iam._authorize(snapshot, token, name, 'sandbox.read')
                except AuthorizationDenied:
                    continue
                item = public_project(name, value, allowed)
            elif operation in ('admission.list', 'sandbox.list'):
                item = {key:deepcopy(value[key]) for key in (
                    'project', 'request_id', 'node', 'backend', 'demand', 'state')}
                # Never export creation args, private IDs, commands or credentials.
                sid = value['sandbox_id']
                item['sandbox_id'] = SandboxRoute(project, value['node'], value['backend'],
                    sid, value['request_id']).encode(route_key) if sid else None
                item['quota_held'] = value['state'] != 'RELEASED'
                item['runtime_state'] = None  # Query the owning Edge explicitly.
            else:
                item = dict(id=record.key[len(prefix):], action=value['action'],
                    actor=value['actor'], timestamp=value['timestamp'], project=project)
            items.append(item)
        return dict(items=items, next_after=cursor(records[limit-1].key) if len(records)>limit else None,
                    scope='project', runtime_authority='owning-node'), {}

    return iam._change(read)
