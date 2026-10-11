"""Controlled cluster interleavings; MemoryStore/Edge fixtures are not HA evidence."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import asdict
import json
import threading
import unittest

from dsec.contracts.cluster import (AuthorizationDenied, CoordinationUnavailable,
    NoPlacement, NodeView, PERMISSIONS, Quota, QuotaExceeded, SandboxRoute)
from dsec.contracts.errors import RequestOutcomeUnknown
from dsec.contracts.requests import RequestConflict, request_digest
from dsec.contracts.sandbox import DSecContainerRunArgs
from dsec.control.cluster_api import ClusterAPI
from dsec.control.coordination import EtcdStore, Record
from dsec.control.iam import IAM
from dsec.control.placement import PlacementEngine
from dsec.control.watcher import Watcher
from dsec.host.cluster_http import ClusterHTTPServer
from dsec.host.cluster_node import NodeGateway, edge_request_id
from dsec.sdk.cluster import ClusterTransport, JSONRPC
from dsec.sdk.sandbox_transport import ServiceError


class MemoryStore:
    """CAS fixture only; production construction always requires real etcd."""
    def __init__(self):
        self.lock, self.records, self.revision = threading.Lock(), {}, 0
        self.before_commit, self.unavailable = None, False

    def read(self, key):
        if self.unavailable:
            raise CoordinationUnavailable('fixture quorum unavailable')
        with self.lock:
            value, revision = self.records.get(key, (None, 0))
            return Record(key, deepcopy(value), revision)

    def transact(self, reads, writes):
        if self.unavailable:
            raise CoordinationUnavailable('fixture quorum unavailable')
        if self.before_commit:
            self.before_commit(reads, writes)
        with self.lock:
            if any(self.records.get(r.key, (None, 0))[1] != r.revision for r in reads):
                return False
            if writes:
                self.revision += 1
                for key, value in writes.items():
                    self.records[key] = (deepcopy(value), self.revision)
            return True

    def scan(self, prefix):
        with self.lock:
            return [Record(key,deepcopy(value),revision)
                    for key,(value,revision) in self.records.items() if key.startswith(prefix)]


class EdgeFixture:
    def __init__(self):
        self.created = 0
        self.proofs, self.states = {}, {}
        self.drop_after_create = False

    def call(self, operation, sandbox_id=None, *, request_id=None, resource_demand=None, **args):
        if operation in ('query_request', 'container_query_request'):
            return deepcopy(self.proofs.get(args['lookup_id'], {'state':'NOT_FOUND'}))
        if operation == 'create':
            self.created += 1
            sid = f'{self.created:012x}'
            self.states[sid] = 'RUNNING'
            result = {'id':sid, 'state':'RUNNING'}
            self.proofs[request_id] = dict(state='DONE', sandbox_id=None,
                operation='create', digest=request_digest('create',None,args),
                response=dict(ok=True, result=result))
            if self.drop_after_create:
                raise RequestOutcomeUnknown('fixture lost committed reply', request_id)
            return result
        if operation == 'status':
            return dict(id=sandbox_id, state=self.states[sandbox_id], resource_cleanup_complete=True)
        if operation == 'stop':
            self.states[sandbox_id] = 'STOPPED'
            return dict(id=sandbox_id, state='STOPPED')
        if operation == 'execute':
            self.proofs[request_id] = dict(state='DONE', sandbox_id=sandbox_id,
                response=dict(ok=True, result={'stdout':'fixture command'}))
            return {'stdout':'fixture command'}
        raise ValueError(operation)


class ClusterTests(unittest.TestCase):
    token = 'admin-' + 'a'*48
    key = b'r'*32
    demand = Quota(1, 1, 512, 1024, 1)
    quota = Quota(3, 3, 1536, 3072, 3)

    def setUp(self):
        self.store = MemoryStore()
        self.iam = IAM(self.store)
        self.iam.bootstrap(self.token, Quota(10, 10, 5120, 10240, 10))
        self.iam.create_project(self.token, 'root', 'case', self.quota, {'admin':sorted(PERMISSIONS)})

    def admission(self, index=1, *, node='n1', iam=None):
        return (iam or self.iam).admit(self.token, 'case', f'{index:032x}', 'd'*64,
            node, 'microvm', self.demand, args={})

    def view(self, node='n1', *, load=0, environments=('default',), capabilities=()):
        return NodeView(node, 'boot-1', ('microvm',), environments, capabilities,
                        load, self.quota, 0)

    def test_nested_delegation_consumes_parent_budget_and_prevents_elevation(self):
        token = self.iam.issue_credential(self.token, 'agent')
        grants = {'agent':['sandbox.create', 'sandbox.read', 'iam.delegate']}
        self.iam.create_project(self.token, 'case', 'child', self.demand, grants)
        self.iam.create_project(token, 'child', 'grandchild', self.demand, {'agent':['sandbox.read']})
        with self.assertRaises(AuthorizationDenied):
            self.iam.create_project(token, 'child', 'evil', self.demand, {'agent':['sandbox.stop']})
        with self.assertRaises(QuotaExceeded):
            self.iam.create_project(self.token, 'case', 'over', self.quota, grants)
        self.assertEqual(self.store.read('projects/case').value['delegated']['concurrency'], 1)

    def test_concurrent_admissions_never_exceed_quota_and_keep_one_target(self):
        def attempt(index):
            try:
                return self.admission(index)
            except QuotaExceeded:
                return None
        with ThreadPoolExecutor(max_workers=12) as executor:
            admitted = [r for r in executor.map(attempt, range(1, 13)) if r]
        self.assertEqual(len(admitted), 3)
        self.assertEqual(self.store.read('projects/case').value['usage']['concurrency'], 3)
        receipt = admitted[0]
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(lambda n:self.admission(int(receipt['request_id'],16), node='n'+str(n)), range(1,9)))
        self.assertEqual({r['node'] for r in results}, {'n1'})

    def test_parallel_delegation_and_parent_direct_usage_share_one_budget(self):
        self.admission()
        def delegate(index):
            try:
                return self.iam.create_project(self.token, 'case', 'sub'+str(index), self.demand,
                                               {'admin':['sandbox.create']})
            except QuotaExceeded:
                return None
        with ThreadPoolExecutor(max_workers=8) as executor:
            self.assertEqual(sum(v is not None for v in executor.map(delegate, range(8))), 2)
        with self.assertRaises(QuotaExceeded):
            self.admission(2)

    def test_policy_or_credential_revocation_wins_before_admission_commit(self):
        arrived, release = threading.Event(), threading.Event()
        target = 'admissions/case/' + '1'*32
        def barrier(reads, writes):
            if target in writes:
                arrived.set()
                if not release.wait(5):
                    raise AssertionError('IAM admission barrier timed out')
        self.store.before_commit = barrier
        errors = []
        def admit():
            try:
                self.iam.admit(self.token, 'case', '1'*32, 'd'*64,
                               'n1', 'microvm', self.demand, args={})
            except Exception as exc:
                errors.append(exc)
        thread = threading.Thread(target=admit)
        thread.start()
        try:
            self.assertTrue(arrived.wait(5))
            self.iam.set_policy(self.token, 'case', {'admin':['sandbox.read', 'iam.policy']})
        finally:
            release.set(); thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], AuthorizationDenied)
        self.assertIsNone(self.iam.lookup('case', '1'*32))

    def test_revocation_and_parent_ceiling_are_seen_by_other_iam_replica(self):
        token = self.iam.issue_credential(self.token, 'agent')
        self.iam.create_project(self.token, 'case', 'child', self.demand,
                               {'agent':['sandbox.read', 'sandbox.execute']})
        replica = IAM(self.store)
        replica.authorize(token, 'child', 'sandbox.execute')
        self.iam.set_policy(self.token, 'case', {'admin':['iam.policy', 'sandbox.read']})
        with self.assertRaises(AuthorizationDenied):
            replica.authorize(token, 'child', 'sandbox.execute')
        self.iam.revoke(self.token, token)
        with self.assertRaises(AuthorizationDenied):
            replica.authorize(token, 'child', 'sandbox.read')

    def test_release_is_idempotent_requires_owner_and_never_rebinds(self):
        receipt = self.admission()
        self.iam.bind('case', receipt['request_id'], 'n1', 'a'*12)
        with self.assertRaises(RequestConflict):
            self.iam.release('case', receipt['request_id'], 'n2', sandbox_id='a'*12)
        with self.assertRaises(RequestConflict):
            self.iam.release('case', receipt['request_id'], 'n1', nonadmission=True)
        for _ in range(2):
            self.iam.release('case', receipt['request_id'], 'n1', sandbox_id='a'*12)
        self.assertEqual(self.store.read('projects/case').value['usage']['concurrency'], 0)
        with self.assertRaises(RequestConflict):
            self.iam.bind('case', receipt['request_id'], 'n1', 'a'*12)

    def test_quorum_loss_fails_closed_without_local_mutation(self):
        before = deepcopy(self.store.records)
        self.store.unavailable = True
        with self.assertRaises(CoordinationUnavailable):
            self.admission()
        self.assertEqual(before, self.store.records)
        with self.assertRaises(ValueError):
            EtcdStore(['http://192.168.0.110:2379'], allow_loopback_http=True)

    def test_stale_watcher_completion_cannot_revive_failed_node_or_release_quota(self):
        self.admission()
        entered, release = threading.Event(), threading.Event()
        attempts = 0
        def probe():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                entered.set()
                if not release.wait(5):
                    raise AssertionError('Watcher barrier timed out')
                return self.view()
            raise ConnectionError('node disappeared')
        watcher = Watcher({'n1':probe})
        thread = threading.Thread(target=watcher.probe, args=('n1',))
        thread.start()
        try:
            self.assertTrue(entered.wait(5))
            self.assertFalse(watcher.probe('n1'))
        finally:
            release.set(); thread.join(5)
        self.assertEqual(watcher.snapshot(), {})
        self.assertEqual(self.store.read('projects/case').value['usage']['concurrency'], 1)

    def test_power_of_k_filters_and_recent_overlay_and_expiry(self):
        clock = [0]
        watcher = Watcher({'n1':lambda:self.view(load=.1), 'n2':lambda:self.view('n2', load=.2)},
                          clock=lambda:clock[0], max_age=5)
        watcher.poll()
        engine = PlacementEngine(watcher, sampler=lambda views,k:views[:k], clock=lambda:clock[0])
        self.assertEqual(engine.choose('microvm', 'default', self.demand), 'n1')
        self.assertEqual(engine.choose('microvm', 'default', self.demand), 'n2')
        with self.assertRaises(NoPlacement):
            engine.choose('microvm', 'missing', self.demand)
        with self.assertRaises(NoPlacement):
            engine.choose('microvm', 'default', self.demand, capabilities=('gpu',))
        clock[0] = 6
        with self.assertRaises(NoPlacement):
            engine.choose('microvm', 'default', self.demand)
        # A separate watcher has no durable snapshot and reconstructs by probe.
        replacement = Watcher(watcher.probes)
        self.assertEqual(replacement.snapshot(), {})
        replacement.poll()
        self.assertEqual(set(replacement.snapshot()), {'n1','n2'})

    def controller(self, edge=None):
        edge = edge or EdgeFixture()
        node = NodeGateway('n1', edge, self.iam, route_key=self.key, boot_id='boot1',
            environments=('default',), max_sandboxes=3,
            minimum_demands={'default':asdict(self.demand)})
        watcher = Watcher({'n1':lambda:self.view()})
        watcher.poll()
        api = ClusterAPI(IAM(self.store), PlacementEngine(watcher), {'n1':node}, route_key=self.key)
        return api, node, edge

    def envelope(self, operation='create', *, sandbox_id=None, request_id='1'*32, args=None):
        value = dict(project='case', operation=operation, request_id=request_id,
                     sandbox_id=sandbox_id, args=args or {})
        if operation == 'create':
            value['resource_demand'] = {k:v for k,v in asdict(self.demand).items() if k != 'concurrency'}
        return value

    def test_duplicate_controller_creates_reuse_one_edge_request_and_quota(self):
        api, node, edge = self.controller()
        second = ClusterAPI(IAM(self.store), api.placement, {'n1':node}, route_key=self.key)
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda c:c.call(self.token,self.envelope()), [api,second]))
        self.assertEqual(results[0]['id'], results[1]['id'])
        self.assertEqual(edge.created, 1)
        self.assertEqual(self.store.read('projects/case').value['usage']['concurrency'], 1)

    def test_lost_reply_recovers_on_other_controller_without_create_replay(self):
        edge = EdgeFixture(); edge.drop_after_create = True
        api, node, edge = self.controller(edge)
        with self.assertRaises(RequestOutcomeUnknown):
            api.call(self.token, self.envelope())
        self.assertEqual(self.iam.lookup('case', '1'*32)['state'], 'DISPATCHED')
        other = ClusterAPI(IAM(self.store), api.placement, {'n1':node}, route_key=self.key)
        proof = other.call(self.token, self.envelope('query_request', request_id='2'*32, args={'lookup_id':'1'*32}))
        route = proof['response']['result']['id']
        self.assertEqual(other.call(self.token, self.envelope('status', sandbox_id=route))['state'], 'RUNNING')
        self.assertEqual(edge.created, 1)
        other.call(self.token, self.envelope('stop', sandbox_id=route, request_id='3'*32))
        self.assertEqual(self.store.read('projects/case').value['usage']['concurrency'], 0)
        # Re-querying creation after stop cannot re-bind or re-create it.
        other.call(self.token, self.envelope('query_request', args={'lookup_id':'1'*32}))
        self.assertEqual(edge.created, 1)

    def test_route_tampering_and_cross_project_access_are_denied(self):
        api, _, _ = self.controller()
        route = api.call(self.token, self.envelope())['id']
        with self.assertRaises(AuthorizationDenied):
            api.call(self.token, self.envelope('status', sandbox_id=route.replace('n1','n2')))
        self.iam.create_project(self.token, 'root', 'other', self.demand, {'admin':['sandbox.read']})
        envelope = self.envelope('status', sandbox_id=route)
        envelope['project'] = 'other'
        with self.assertRaises(AuthorizationDenied):
            api.call(self.token, envelope)

    def test_ttl_reconciliation_needs_stopped_proof_and_preserves_unissued_grant(self):
        api, node, edge = self.controller()
        route = api.call(self.token, self.envelope())['id']
        decoded = SandboxRoute.decode(route,self.key)
        self.admission(2)
        node.reconcile()
        self.assertEqual(self.store.read('projects/case').value['usage']['concurrency'],2)
        edge.states[decoded.local_id] = 'STOPPED'
        node.reconcile(); node.reconcile()
        self.assertEqual(self.store.read('projects/case').value['usage']['concurrency'],1)
        self.assertEqual(self.iam.lookup('case', f'{2:032x}')['state'],'ADMITTED')

    def test_definitively_rejected_admission_cannot_dispatch_after_release(self):
        class RejectingEdge(EdgeFixture):
            def call(self, operation, sandbox_id=None, *, request_id=None, **args):
                if operation == 'create':
                    self.created += 1
                    raise ServiceError('ServiceBusy','not admitted')
                return super().call(operation,sandbox_id,request_id=request_id,**args)
        api, node, edge = self.controller(RejectingEdge())
        with self.assertRaises(ServiceError):
            api.call(self.token,self.envelope())
        receipt = self.iam.lookup('case','1'*32)
        self.assertEqual(receipt['state'],'RELEASED')
        self.assertEqual(self.store.read('projects/case').value['usage']['concurrency'],0)
        try:
            node.create(receipt)
        except RequestConflict:
            pass
        except ServiceError:
            self.fail('Released admission reached the Edge again')
        else:
            self.fail('Released admission was accepted again')
        self.assertEqual(edge.created,1)

    def test_identical_public_ids_in_distinct_projects_do_not_share_edge_effects(self):
        api, node, edge = self.controller()
        self.iam.create_project(self.token, 'root', 'other', self.quota,
                                {'admin': sorted(PERMISSIONS)})
        left = self.envelope()
        right = dict(left, project='other')
        first, second = api.call(self.token, left), api.call(self.token, right)
        self.assertNotEqual(SandboxRoute.decode(first['id'], self.key).local_id,
                            SandboxRoute.decode(second['id'], self.key).local_id)
        self.assertEqual(edge.created, 2)
        rid = 'a'*32
        for project, sid in [('case', first['id']), ('other', second['id'])]:
            command = self.envelope('execute', sandbox_id=sid, request_id=rid,
                                    args={'command': 'fixture'})
            command['project'] = project
            api.call(self.token, command)
            query = self.envelope('query_request', sandbox_id=sid,
                                  args={'lookup_id': rid})
            query['project'] = project
            proof = api.call(self.token, query)
            self.assertEqual(proof['request_id'], rid)
            self.assertEqual(proof['sandbox_id'], sid)
        self.assertEqual(len(edge.proofs), 4)

    def test_audit_is_atomic_private_and_idempotent(self):
        receipt = self.admission()
        audits = [r.value for r in self.store.scan('audit/')]
        admission = [r for r in audits if r['action'] == 'sandbox.admit']
        self.assertEqual(len(admission), 1)
        self.assertEqual(admission[0]['actor'], 'admin')
        self.assertNotIn(self.token, json.dumps(audits))
        self.admission()
        self.assertEqual(len(self.store.scan('audit/')), len(audits))
        self.store.unavailable = True
        with self.assertRaises(CoordinationUnavailable):
            self.iam.release('case', receipt['request_id'], 'n1', nonadmission=True)
        self.store.unavailable = False
        self.assertEqual(self.iam.lookup('case', receipt['request_id'])['state'], 'ADMITTED')

    def test_dispatch_not_yet_visible_in_edge_journal_never_resends_or_releases(self):
        class DelayedEdge(EdgeFixture):
            def call(self, operation, sandbox_id=None, *, request_id=None, **args):
                if operation == 'create':
                    self.created += 1
                    raise RequestOutcomeUnknown('send accepted; journal intent not yet visible', request_id)
                return super().call(operation, sandbox_id, request_id=request_id, **args)
        api,node,edge = self.controller(DelayedEdge())
        for _ in range(2):
            with self.assertRaises(RequestOutcomeUnknown):
                api.call(self.token,self.envelope())
        node.reconcile()
        proof = api.call(self.token,self.envelope('query_request',args={'lookup_id':'1'*32}))
        self.assertEqual(proof['state'],'UNKNOWN')
        self.assertEqual(edge.created,1)
        self.assertEqual(self.iam.lookup('case','1'*32)['state'],'DISPATCHED')
        self.assertEqual(self.store.read('projects/case').value['usage']['concurrency'],1)

    def test_unrelated_done_edge_result_cannot_be_bound_as_creation(self):
        api,node,edge = self.controller()
        edge.proofs[edge_request_id('case','1'*32)] = dict(state='DONE',
            operation='stop', sandbox_id='a'*12,
            digest=request_digest('stop','a'*12,{}),
            response=dict(ok=True,result={'id':'a'*12,'state':'STOPPED'}))
        with self.assertRaises(RequestConflict):
            api.call(self.token,self.envelope())
        self.assertEqual(edge.created,0)
        self.assertIsNone(self.iam.lookup('case','1'*32)['sandbox_id'])

    def test_native_cancel_returns_public_target_operation_id(self):
        class CancelEdge(EdgeFixture):
            def call(self, operation, sandbox_id=None, *, request_id=None, **args):
                if operation=='native_cancel':
                    return dict(operation_id=args['lookup_id'],state='DONE')
                return super().call(operation,sandbox_id,request_id=request_id,**args)
        api,node,edge=self.controller(CancelEdge())
        sid=api.call(self.token,self.envelope())['id']
        result=api.call(self.token,self.envelope('native_cancel',sandbox_id=sid,
            request_id='2'*32,args={'backend':'microvm','lookup_id':'3'*32,'session_id':'4'*32}))
        self.assertEqual(result['operation_id'],'3'*32)

    def test_container_limits_cannot_understate_project_quota(self):
        spec=asdict(DSecContainerRunArgs(environment_id='default',memory_limit_mb=4096))
        receipt=self.iam.admit(self.token,'case','a'*32,'d'*64,'n1','container',
                               self.demand,args={'spec':spec})
        edge=EdgeFixture()
        node=NodeGateway('n1',edge,self.iam,route_key=self.key,boot_id='b',
            backends=('container',),environments=('default',),
            minimum_demands={'default':asdict(self.demand)})
        with self.assertRaises(ServiceError) as denied:
            node.create(receipt)
        self.assertEqual(denied.exception.kind,'AdmissionDenied')
        self.assertEqual(denied.exception.details['minimum']['memory_mb'],4096)
        self.assertEqual(self.iam.lookup('case','a'*32)['state'],'RELEASED')
        self.assertEqual(self.store.read('projects/case').value['usage']['concurrency'],0)
        self.assertEqual(edge.created,0)

    def test_other_sandbox_query_is_not_a_project_information_leak(self):
        api, _, edge = self.controller()
        first = api.call(self.token, self.envelope())['id']
        second = api.call(self.token, self.envelope(request_id='2'*32))['id']
        api.call(self.token, self.envelope('execute', sandbox_id=second, request_id='3'*32))
        with self.assertRaises(AuthorizationDenied):
            api.call(self.token, self.envelope('query_request', sandbox_id=first, args={'lookup_id':'3'*32}))

    def server(self, dispatch):
        server = ClusterHTTPServer(('127.0.0.1',0), dispatch, allow_loopback_http=True)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval':.01})
        thread.start()
        def cleanup():
            server.shutdown(); thread.join(5); server.server_close()
        self.addCleanup(cleanup)
        return server

    def test_read_failover_and_mutation_transport_loss_has_no_hidden_retry(self):
        calls = []
        server = self.server(lambda token,payload:calls.append(payload['operation']) or {'protocol':'dsec-cluster-v1'})
        endpoints = ['http://127.0.0.1:1', f'http://127.0.0.1:{server.server_port}']
        rpc = JSONRPC(endpoints, allow_loopback_http=True)
        self.assertEqual(rpc.send(self.token, {'operation':'health','request_id':'1'*32}, readonly=True, timeout=1)['protocol'], 'dsec-cluster-v1')
        with self.assertRaises(RequestOutcomeUnknown):
            rpc.send(self.token, {'operation':'execute','request_id':'2'*32}, timeout=1)
        self.assertEqual(calls, ['health'])
        with self.assertRaises(ValueError):
            JSONRPC(['http://192.168.0.110:8000'], allow_loopback_http=True)

    def test_two_http_ingresses_route_create_query_stop_without_local_registry(self):
        api, node, edge = self.controller()
        second = ClusterAPI(IAM(self.store), api.placement, {'n1':node}, route_key=self.key)
        servers = [self.server(controller.call) for controller in (api, second)]
        transports = [ClusterTransport([f'http://127.0.0.1:{s.server_port}'], token=self.token,
            project='case', allow_loopback_http=True) for s in servers]
        hint = {k:v for k,v in asdict(self.demand).items() if k != 'concurrency'}
        result = transports[0].call('create', request_id='1'*32, resource_demand=hint)
        proof = transports[1].query_request('1'*32)
        self.assertEqual(proof['response']['result']['id'], result['id'])
        transports[1].call('execute', result['id'], request_id='3'*32, command='fixture')
        proof = transports[0].query_request('3'*32, sandbox_id=result['id'])
        self.assertEqual(proof['sandbox_id'], result['id'])
        transports[1].call('stop', result['id'])
        self.assertEqual(edge.created, 1)
        self.assertEqual(self.store.read('projects/case').value['usage']['concurrency'], 0)


if __name__ == '__main__':
    unittest.main()
