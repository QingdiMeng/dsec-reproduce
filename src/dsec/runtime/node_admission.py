"""Edge-owned durable physical leases, including ready VMs and uncertain creates.

The existing backend/request journals remain execution truth. This journal owns
resource reservations only: no effects or requests are replayed from it.
"""
from contextlib import contextmanager
from dataclasses import asdict, replace
import fcntl
import hashlib
import json
from pathlib import Path
import re
import threading
import uuid

from dsec._persistence import atomic_json
from dsec.contracts.resources import NodeBudget, NodeDemand, NodeAdmissionBusy, NodeLeaseUncertain
from dsec.contracts.requests import RequestConflict, request_digest
from dsec.runtime.resources import NodeResourceLedger


class NodeAdmission:
    def __init__(self, root, budget: NodeBudget, sampler, *, default_demand=None, ready_demand=None):
        self.root = Path(root).resolve() / 'node-leases'
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.owner = (self.root / 'owner.lock').open('a')
        try:
            fcntl.flock(self.owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            self.owner.close()
            raise
        self.ledger = NodeResourceLedger(budget, sampler)
        self.default_demand = default_demand or NodeDemand(1, 512, 1024, 1)
        self.ready_demand = ready_demand or replace(self.default_demand,
            cpu=min(self.default_demand.cpu, .05), network_mbps=0, disk_io_mbps=0)
        self.lock = threading.RLock()
        self.local = threading.local()
        self.records = {}
        self.recovery_errors = []
        self.ready = False
        try:
            if self.ledger.too_large(self.default_demand) or self.ledger.too_large(self.ready_demand):
                raise ValueError('Node default demand exceeds node budget')
            for path in sorted(self.root.glob('*.json')):
                record = json.loads(path.read_text())
                if (record.get('version') != 1 or record.get('lease_id') != path.stem or
                        not re.fullmatch(r'[0-9a-f]{32}', path.stem) or
                        record.get('backend') not in ('microvm', 'container') or
                        record.get('state') not in ('ALLOCATING', 'BOUND', 'LIVE', 'UNKNOWN', 'RELEASED')):
                    raise ValueError('Invalid node lease record')
                requests = record.get('requests')
                if not isinstance(requests, dict):
                    raise ValueError('Invalid node request identities')
                for request_id, identity in requests.items():
                    if (not re.fullmatch(r'[0-9a-f]{32}', request_id) or
                            set(identity) != {'digest','hint'} or
                            not re.fullmatch(r'[0-9a-f]{64}', identity['digest'])):
                        raise ValueError('Invalid node request identity')
                    if identity['hint'] is not None:
                        NodeDemand(**identity['hint'])
                sid=record.get('sandbox_id')
                if sid is not None and (not isinstance(sid, str) or not re.fullmatch(
                        r'[0-9a-f]{12}' if record['backend']=='microvm' else r'[0-9a-f]{32}', sid)):
                    raise ValueError('Invalid node sandbox binding')
                demand = NodeDemand(**record['demand'])
                self.records[path.stem] = record
                if record['state'] != 'RELEASED':
                    self.ledger.restore(path.stem, demand)
        except BaseException:
            self.owner.close()
            raise

    def close(self):
        self.owner.close()

    def activate(self):
        with self.lock:
            self.ready = True

    def _for_request(self, request_id):
        return next((r for r in self.records.values() if request_id in r['requests']), None)

    def _for_sandbox(self, backend, sandbox_id):
        return next((r for r in self.records.values()
                     if r['backend'] == backend and r['sandbox_id'] == sandbox_id), None)

    def _commit(self, record):
        key = record['lease_id']
        previous = self.records.get(key)
        # Keep the conservative in-memory reservation if an admission commit
        # is uncertain. A failed release commit never frees an old reservation.
        if record['state'] != 'RELEASED':
            self.records[key] = record
            if key in self.ledger.leases:
                self.ledger.release(key)
            self.ledger.restore(key, NodeDemand(**record['demand']))
        try:
            atomic_json(self.root / (key + '.json'), record)
        except Exception as exc:
            if record['state'] != 'RELEASED':
                self.records[key] = {**record, 'state': 'UNKNOWN'}
            elif previous is not None:
                self.records[key] = previous
            raise NodeLeaseUncertain('Node lease commit is uncertain') from exc
        self.records[key] = record
        if record['state'] == 'RELEASED' and key in self.ledger.leases:
            self.ledger.release(key)

    @contextmanager
    def request(self, backend, request_id, args, hint=None):
        if not isinstance(request_id, str) or not re.fullmatch(r'[0-9a-f]{32}', request_id):
            raise ValueError('Invalid node create request ID')
        requested = NodeDemand(**hint) if hint is not None else None
        identity = {'digest': request_digest('create', None, args),
                    'hint': asdict(requested) if requested is not None else None}
        with self.lock:
            previous = self._for_request(request_id)
            if previous is not None and (previous['backend'] != backend or
                                         previous['requests'][request_id] != identity):
                raise RequestConflict('Create request reused with different node demand/arguments')
        if getattr(self.local, 'request', None) is not None:
            raise RuntimeError('Nested node create context')
        self.local.request = (backend, request_id, identity, requested)
        self.local.lease_id = None
        self.local.source_effects = False
        try:
            yield
        finally:
            try:
                key = getattr(self.local, 'lease_id', None)
                if key is not None:
                    self.release_unbound(key)
            finally:
                self.local.request = None
                self.local.lease_id = None
                self.local.source_effects = False

    def _demand(self, minimum=None):
        context = getattr(self.local, 'request', None)
        demands = [self.default_demand]
        if context is not None and context[3] is not None:
            demands.append(context[3])
        if minimum is not None:
            demands.append(minimum)
        return NodeDemand(**{name: max(getattr(d, name) for d in demands)
                            for name in NodeDemand.__dataclass_fields__})

    def allocate(self, backend, *, minimum=None, configured_limits=None):
        """Called before constructing a sandbox or allocating host handles."""
        with self.lock:
            if not self.ready:
                raise NodeAdmissionBusy(['node_startup'])
            context = getattr(self.local, 'request', None)
            if context is not None and context[0] != backend:
                raise ValueError('Node backend context mismatch')
            existing = getattr(self.local, 'lease_id', None)
            if context is not None and existing:
                # Attach child configuration to a fork's pre-admission without
                # reserving a second time after source preparation effects.
                record = self.records[existing]
                if configured_limits is not None and record['configured_limits'] is None:
                    self._commit({**record, 'configured_limits':configured_limits})
                return existing
            demand = self._demand(minimum)
            if context is None and backend == 'microvm':
                demand = NodeDemand(**{name:max(getattr(demand,name),getattr(self.ready_demand,name))
                                       for name in NodeDemand.__dataclass_fields__})
            if self.ledger.too_large(demand):
                raise ValueError('Node demand exceeds configured budget')
            blockers = self.ledger.blockers(demand, self.ledger.sampler.sample())
            if blockers:
                raise NodeAdmissionBusy(blockers)
            key = context[1] if context is not None else uuid.uuid4().hex
            if key in self.records:
                raise RequestConflict('Node create intent already exists; reconcile instead of replaying')
            record = {'version': 1, 'lease_id': key, 'backend': backend,
                      'sandbox_id': None, 'state': 'ALLOCATING', 'phase':'creating',
                      'demand': asdict(demand),
                      'configured_limits': configured_limits,
                      'requests': {context[1]: context[2]} if context is not None else {}}
            self._commit(record)
            self.local.lease_id = key
            return key

    def bind(self, lease_id, sandbox_id):
        with self.lock:
            record = self.records[lease_id]
            if record['sandbox_id'] not in (None, sandbox_id):
                raise ValueError('Node lease already bound to another sandbox')
            self._commit({**record, 'sandbox_id': sandbox_id, 'state': 'BOUND'})

    def created(self, backend, sandbox_id, *, ready=False):
        with self.lock:
            record = self._for_sandbox(backend, sandbox_id)
            if record is None or record['state'] == 'RELEASED':
                raise RuntimeError('Created sandbox has no active node lease')
            # Pool boot acquired the larger creating/ready reservation already.
            # Reducing to the ready estimate cannot reject after boot effects.
            demand = asdict(self.ready_demand) if ready else record['demand']
            phase = 'ready' if ready else 'active'
            if record['state'] != 'LIVE' or record.get('phase') != phase:
                self._commit({**record, 'state':'LIVE', 'phase':phase, 'demand':demand})

    def checkout(self, sandbox):
        """Transfer a ready VM's lease, increasing only its reservation delta."""
        with self.lock:
            context = getattr(self.local, 'request', None)
            if context is None:
                raise RuntimeError('Ready checkout requires a create request context')
            record = self._for_sandbox('microvm', sandbox.id)
            if record is None or record['state'] == 'RELEASED':
                raise RuntimeError('Ready VM has no node lease')
            demand = self._demand(NodeDemand(**record['demand']))
            old = self.ledger.release(record['lease_id'])
            try:
                sample = self.ledger.sampler.sample()
                sample = replace(sample, memory_available_mb=sample.memory_available_mb+old.memory_mb,
                                 disk_available_mb=sample.disk_available_mb+old.disk_mb)
                blockers = self.ledger.blockers(demand, sample)
            finally:
                self.ledger.restore(record['lease_id'], old)
            if blockers:
                raise NodeAdmissionBusy(blockers)
            self._commit({**record, 'demand': asdict(demand), 'state': 'LIVE', 'phase':'active',
                          'requests': {**record['requests'], context[1]: context[2]}})
            self.local.lease_id = record['lease_id']

    def release_unbound(self, lease_id):
        with self.lock:
            record = self.records[lease_id]
            # Bind is crash-durable before any VMM/container or host handle
            # allocation. An unbound lease cannot have started those effects.
            if record['sandbox_id'] is None and record['state'] != 'RELEASED':
                self._commit({**record, 'state':'RELEASED'})

    def stopped(self, backend, sandbox_id):
        """Only called after backend-owned resource cleanup has completed."""
        with self.lock:
            record = self._for_sandbox(backend, sandbox_id)
            if record is not None and record['state'] != 'RELEASED':
                self._commit({**record, 'state': 'RELEASED'})

    def reconcile_microvms(self, manager):
        # Called before monitors/background pool refill start. Missing or invalid
        # registries do not prove cleanup and never free a bound reservation.
        with self.lock:
            for record in list(self.records.values()):
                if record['sandbox_id'] is None:
                    self.release_unbound(record['lease_id'])
            for sb in manager.sandboxes.values():
                record = self._for_sandbox('microvm', sb.id)
                if record is not None:
                    if sb.state == 'STOPPED':
                        if getattr(sb, 'resource_cleanup_complete', False):
                            self.stopped('microvm', sb.id)
                        else:
                            try:
                                sb._stop('node_cleanup_reconcile')
                            except Exception as exc:
                                self.recovery_errors.append({'sandbox_id':sb.id, 'error':str(exc)})
                elif sb.state != 'STOPPED':
                    key = hashlib.sha256(('microvm:'+sb.id).encode()).hexdigest()[:32]
                    self._commit({'version':1, 'lease_id':key, 'backend':'microvm',
                                  'sandbox_id':sb.id, 'state':'LIVE', 'requests':{},
                                  'configured_limits':None,
                                  'phase':'ready' if sb.reserved else 'active',
                                  'demand':asdict(self.ready_demand if sb.reserved else self.default_demand)})

    def adopt_container(self, sandbox_id, minimum):
        with self.lock:
            if self._for_sandbox('container', sandbox_id) is None:
                key = hashlib.sha256(('container:'+sandbox_id).encode()).hexdigest()[:32]
                self._commit({'version':1, 'lease_id':key, 'backend':'container',
                              'sandbox_id':sandbox_id, 'state':'LIVE', 'requests':{},
                              'configured_limits':None, 'demand':asdict(self._demand(minimum))})

    def status(self):
        with self.lock:
            return {'authority':'edge', 'scope':'edge-instance', 'budget':asdict(self.ledger.budget),
                    'default_demand':asdict(self.default_demand), 'ready_demand':asdict(self.ready_demand),
                    'reserved':dict(self.ledger.reserved), 'lease_ids':list(self.ledger.leases),
                    'leases':{key:record for key,record in self.records.items()
                              if record['state'] != 'RELEASED'},
                    'sample':asdict(self.ledger.sampler.sample()),
                    'recovery_errors':list(self.recovery_errors)}
