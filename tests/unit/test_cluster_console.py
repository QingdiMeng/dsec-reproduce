"""Console browsing authorization, uncertainty and optional HTTP boundaries."""
from copy import deepcopy
from dataclasses import asdict
import json
import threading
import unittest
import urllib.error
import urllib.request

from dsec.contracts.cluster import AuthorizationDenied, PERMISSIONS, Quota, SandboxRoute
from dsec.contracts.errors import RequestOutcomeUnknown
from dsec.control.cluster_api import ClusterAPI
from dsec.control.iam import IAM
from dsec.control.watcher import Watcher
from dsec.host.cluster_http import ClusterHTTPServer
from dsec.sdk.cluster import ClusterTransport
from tests.unit import test_cluster_control as fixtures


class ConsoleTests(unittest.TestCase):
    token, key, demand, quota = fixtures.ClusterTests.token, fixtures.ClusterTests.key, fixtures.ClusterTests.demand, fixtures.ClusterTests.quota
    setUp, controller, envelope, view = fixtures.ClusterTests.setUp, fixtures.ClusterTests.controller, fixtures.ClusterTests.envelope, fixtures.ClusterTests.view

    def call(self, api, operation, *, credential=None, project='case', **args):
        return api.call(credential or self.token, dict(project=project, request_id='e'*32,
            operation=operation, args=args))

    def test_inventory_is_tenant_scoped_and_redacts_args_policy_and_audit_keys(self):
        api, _, _ = self.controller()
        other = self.iam.issue_credential(self.token, 'reader')
        self.iam.create_project(self.token, 'root', 'secret', self.demand,
            {'admin':sorted(PERMISSIONS)})
        self.iam.set_policy(self.token, 'case', {'admin':sorted(PERMISSIONS), 'reader':['sandbox.read']})
        api.call(self.token, self.envelope(args={'command_secret':'never-export-this'}))
        info = self.call(api, 'project.get', credential=other)
        self.assertNotIn('grants', info)
        records = self.call(api, 'admission.list', credential=other)['items']
        self.assertEqual(len(records), 1)
        self.assertNotIn('never-export-this', json.dumps(records))
        self.assertNotIn('principal', records[0])
        with self.assertRaises(AuthorizationDenied):
            self.call(api, 'admission.list', project='secret', credential=other)
        with self.assertRaises(AuthorizationDenied):
            self.call(api, 'audit.list', credential=other)
        audit = self.call(api, 'audit.list')['items']
        self.assertTrue(audit)
        self.assertFalse(any('changed_keys' in item for item in audit))
        self.assertNotIn('credentials/', json.dumps(audit))
        # Credential audit changes belong to the root operator, not case pages.
        self.assertFalse(any(item['action']=='credential.issue' for item in audit))
        root_audit = self.call(api, 'audit.list', project='root')['items']
        self.assertTrue(any(item['action']=='credential.issue' for item in root_audit))
        self.assertNotIn('credentials/', json.dumps(root_audit))

    def test_direct_children_browse_does_not_leak_unauthorized_projects(self):
        api, _, _ = self.controller()
        self.iam.create_project(self.token, 'case', 'public', self.demand,
            {'admin':['sandbox.read'], 'reader':['sandbox.read']})
        self.iam.create_project(self.token, 'case', 'private', self.demand, {'admin':['sandbox.read']})
        reader = self.iam.issue_credential(self.token, 'reader')
        self.iam.set_policy(self.token, 'case', {'admin':sorted(PERMISSIONS), 'reader':['sandbox.read']})
        records = self.call(api, 'project.list', credential=reader, limit=1)
        # The hidden child's key advances the bounded cursor, even on an empty page.
        self.assertEqual(records['items'], [])
        self.assertIsNotNone(records['next_after'])
        self.assertNotIn('private', records['next_after'])
        records = self.call(api, 'project.list', credential=reader, limit=1, after=records['next_after'])
        self.assertEqual([r['project'] for r in records['items']], ['public'])
        self.assertIsNone(records['next_after'])

    def test_revocation_between_scan_and_return_rejects_inventory(self):
        api, _, _ = self.controller()
        api.call(self.token, self.envelope())
        def revoke_before_commit(*_):
            self.store.before_commit = None
            self.iam.revoke(self.token, self.token)
        self.store.before_commit = revoke_before_commit
        with self.assertRaises(AuthorizationDenied):
            self.call(api, 'sandbox.list')

    def test_bound_is_not_running_released_history_and_lost_reply_no_replay(self):
        edge = fixtures.EdgeFixture(); edge.drop_after_create = True
        api, node, edge = self.controller(edge)
        with self.assertRaises(RequestOutcomeUnknown):
            api.call(self.token, self.envelope())
        receipt = self.call(api, 'admission.list')['items'][0]
        self.assertEqual(receipt['state'], 'DISPATCHED')
        self.assertIsNone(receipt['runtime_state'])
        self.assertTrue(receipt['quota_held'])
        other = ClusterAPI(IAM(self.store), api.placement, {'n1':node}, route_key=self.key)
        proof = self.call(other, 'query_request', lookup_id='1'*32)
        sid = proof['response']['result']['id']
        row = self.call(other, 'sandbox.list')['items'][0]
        self.assertEqual(row['sandbox_id'], sid)
        self.assertEqual(row['state'], 'BOUND')
        self.assertIsNone(row['runtime_state'])
        self.assertEqual(SandboxRoute.decode(sid, self.key).project, 'case')
        other.call(self.token, self.envelope('stop', sandbox_id=sid, request_id='2'*32))
        row = self.call(api, 'sandbox.list')['items'][0]
        self.assertEqual(row['state'], 'RELEASED')
        self.assertFalse(row['quota_held'])
        self.assertEqual(edge.created, 1)

    def test_stale_unreachable_and_unobserved_nodes_remain_visible_without_admission(self):
        now = [1]
        failed = [False]
        def probe():
            if failed[0]:
                raise RuntimeError('private diagnostic')
            return self.view()
        watcher = Watcher({'n1':probe, 'n2':lambda:self.view('n2')}, max_age=5, clock=lambda:now[0])
        self.assertEqual(watcher.inventory()[0]['state'], 'UNOBSERVED')
        watcher.probe('n1')
        self.assertEqual(watcher.inventory()[0]['valid_for_seconds'], 5)
        now[0] = 7
        rows = watcher.inventory()
        self.assertEqual(rows[0]['state'], 'STALE')
        self.assertEqual(rows[0]['last_view']['active'], 0)
        self.assertFalse(rows[0]['schedulable'])
        self.assertEqual(rows[0]['valid_for_seconds'], 0)
        failed[0] = True; watcher.probe('n1')
        self.assertEqual(watcher.snapshot(), {})
        row = watcher.inventory()[0]
        self.assertEqual(row['state'], 'UNREACHABLE')
        self.assertEqual(row['error'], 'RuntimeError')
        self.assertNotIn('private diagnostic', json.dumps(row))
        failed[0] = False; watcher.probe('n1')
        self.assertEqual(watcher.inventory()[0]['state'], 'HEALTHY')

    def test_page_limits_and_arguments_rejected(self):
        api, _, _ = self.controller()
        for args in ({'limit':0}, {'limit':101}, {'limit':True}, {'after':[]}, {'after':'x'*201}, {'token':'secret'}):
            with self.assertRaises(ValueError):
                self.call(api, 'admission.list', **args)
        with self.assertRaises(ValueError):
            self.call(api, 'node.list', limit=1)

    def test_inventory_readonly_transport_can_failover(self):
        transport = ClusterTransport(['https://api.invalid'], token=self.token, project='case')
        for operation in ('project.get', 'project.list', 'admission.list', 'sandbox.list', 'audit.list', 'node.list'):
            def send(token, payload, *, readonly):
                self.assertTrue(readonly)
                return {'operation':payload['operation']}
            transport.rpc.send = send
            self.assertEqual(transport.call(operation)['operation'], operation)

    def test_console_opt_in_exact_assets_security_headers_and_rpc_auth(self):
        api, _, _ = self.controller()
        for enabled in (False, True):
            server = ClusterHTTPServer(('127.0.0.1', 0), api.call,
                allow_loopback_http=True, console=enabled)
            thread = threading.Thread(target=server.serve_forever); thread.start()
            origin = 'http://127.0.0.1:' + str(server.server_port)
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            try:
                for path in ('/console/', '/console/console.js', '/console/console.css'):
                    if not enabled:
                        with self.assertRaises(urllib.error.HTTPError) as caught:
                            opener.open(origin+path)
                        self.assertEqual(caught.exception.code, 404)
                    else:
                        with opener.open(origin+path) as response:
                            self.assertGreater(len(response.read()), 100)
                            self.assertEqual(response.headers['Cache-Control'], 'no-store')
                            self.assertEqual(response.headers['X-Frame-Options'], 'DENY')
                            self.assertIn("connect-src 'self'", response.headers['Content-Security-Policy'])
                for path in ('/console/../cluster.py', '/console/index.html', '/console/?token=secret'):
                    with self.assertRaises(urllib.error.HTTPError):
                        opener.open(origin+path)
                payload = json.dumps(self.envelope('admission.list')).encode()
                request = urllib.request.Request(origin+'/v1/call', payload, {'Content-Type':'application/json'})
                with opener.open(request) as response:
                    result = json.load(response)
                    self.assertFalse(result['ok'])
                    self.assertEqual(response.headers['Cache-Control'], 'no-store')
            finally:
                server.shutdown(); thread.join(5); server.server_close()
        with self.assertRaises(ValueError):
            ClusterHTTPServer(('127.0.0.1',0), api.call, allow_loopback_http=True, console='yes')


if __name__ == '__main__':
    unittest.main()
