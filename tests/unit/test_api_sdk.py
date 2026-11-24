"""Product SDK ingress and TLS; fixture responses are not real VM acceptance."""
import base64
from copy import deepcopy
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from dsec.contracts.cluster import AuthorizationDenied, PROTOCOL
from dsec.contracts.errors import RequestOutcomeUnknown
from dsec.contracts.native import (NATIVE_FEATURE, STREAM_FEATURE, SESSION_FILE_FEATURE,
                                   REQUIREMENTS_FEATURE, execution_requirements)
from dsec.contracts.resources import NodeDemand
from dsec.contracts.sandbox import UnsupportedCapability, DSecContainerRunArgs
from dsec.host.cluster_http import ClusterHTTPServer, Handler
from dsec.sdk import DSecClient, ClusterClient, DSecFullVMRunArgs
from dsec.sdk.client import DSecClient as CanonicalClient
from dsec.sdk.development import DevEdgeClient
from dsec.sdk.cluster import ClusterTransport
from dsec.sdk.sandbox_transport import ServiceError
from dsec.contracts.environments import ENVIRONMENT_REQUIREMENTS_FEATURE


class APIClientTests(unittest.IsolatedAsyncioTestCase):
    token = 'test-' + 'a'*48
    health = dict(protocol=PROTOCOL, protocol_features=[PROTOCOL, NATIVE_FEATURE,
        STREAM_FEATURE, SESSION_FILE_FEATURE, 'node-admission-v1'])

    def client(self):
        return DSecClient(['https://api.example:8443'], token=self.token, project='case')

    def test_product_and_compatibility_names_have_explicit_distinct_transports(self):
        import dsec.sdk
        from libdsec_compat import DSecClient as LegacyClient
        self.assertIs(DSecClient, CanonicalClient)
        self.assertIs(ClusterClient, DSecClient)
        self.assertIs(LegacyClient, DevEdgeClient)
        self.assertIsNot(DevEdgeClient, DSecClient)
        self.assertFalse(hasattr(dsec.sdk, 'SandboxClient'))
        self.assertFalse(hasattr(dsec.sdk, 'ScheduledDSecClient'))
        with patch.dict(os.environ, {'DSEC_SOCKET':'/should/not/be/used.sock'}), \
                patch('dsec.sdk.development.SandboxClient', side_effect=AssertionError('No UDS fallback')):
            self.assertIsInstance(self.client()._transport, ClusterTransport)
            with self.assertRaises(TypeError): DSecClient()

    def test_private_paths_http_and_missing_credentials_rejected(self):
        for endpoints in ('/srv/edge/service.sock', ['unix:///srv/edge/service.sock'],
                          ['http://127.0.0.1:8443'], ['http://api.example:8443'], [], [1]):
            with self.subTest(endpoints=endpoints), self.assertRaises(ValueError):
                DSecClient(endpoints, token=self.token, project='case')
        with self.assertRaises(TypeError): DSecClient(socket_path='/srv/edge/service.sock')
        with self.assertRaises(TypeError):
            DSecClient(['https://api.example'], token=self.token, project='case', allow_loopback_http=True)
        for token in ('', None, 'x\ny'):
            with self.assertRaises(ValueError): DSecClient(['https://api.example'], token=token, project='case')

    async def test_fullvm_is_explicit_and_never_falls_back_to_container(self):
        client = self.client()
        spec = DSecFullVMRunArgs(environment_id='desktop')
        demand = NodeDemand(1,512,512,0)
        health = dict(self.health, protocol_features=[*self.health['protocol_features'],'container-rpc-v1'])
        client._transport.rpc.send = Mock(return_value=health)
        await client.open()
        with self.assertRaises(UnsupportedCapability):
            await client.run_fullvm(spec,resource_demand=demand)
        with self.assertRaises(ValueError):
            await client.run_container(spec,resource_demand=demand)
        self.assertEqual(client._transport.rpc.send.call_count,1)

        health['protocol_features'].append('fullvm-rpc-v1')
        client._transport.rpc.send = Mock(side_effect=[health,{'id':'signed-fullvm'},{'session_id':'a'*32}])
        await client.open()
        box = await client.run_fullvm(spec,resource_demand=demand)
        self.assertEqual(box.backend,'fullvm')
        sent = client._transport.rpc.send.call_args.args[1]
        self.assertEqual(sent['args']['spec']['execution_mode'],'fullvm')
        await box.open_session()
        self.assertEqual(client._transport.rpc.send.call_args.args[1]['args']['backend'],'fullvm')

    async def test_missing_admission_demand_and_legacy_tb2_reject_before_send(self):
        client = self.client(); client._transport.rpc.send = Mock(return_value=self.health)
        await client.open()
        with self.assertRaisesRegex(ValueError, 'resource_demand'): await client.run_microvm()
        with self.assertRaises(UnsupportedCapability): await client.run_tb2(None)
        self.assertEqual(client._transport.rpc.send.call_count, 1)

    async def test_execution_requirements_reject_old_api_before_create(self):
        client = self.client(); client._transport.rpc.send = Mock(return_value=self.health)
        await client.open()
        with self.assertRaises(UnsupportedCapability):
            await client.run_microvm(resource_demand=NodeDemand(1,512,1024,0),
                                    required_capabilities=[STREAM_FEATURE])
        self.assertEqual(client._transport.rpc.send.call_count, 1)

    async def test_requirements_use_existing_create_contract_for_both_backends(self):
        health = dict(self.health, protocol_features=[*self.health['protocol_features'],
            REQUIREMENTS_FEATURE, ENVIRONMENT_REQUIREMENTS_FEATURE, 'container-rpc-v1'])
        for backend in ('microvm', 'container'):
            with self.subTest(backend=backend):
                client = self.client()
                client._transport.rpc.send = Mock(side_effect=[health, {'id':'owned'}])
                await client.open()
                method = client.run_microvm if backend == 'microvm' else client.run_container
                args = {} if backend == 'microvm' else {'args':DSecContainerRunArgs()}
                await method(**args, resource_demand=NodeDemand(1,512,1024,0),
                    required_capabilities=[STREAM_FEATURE, STREAM_FEATURE], request_id='1'*32)
                payload = client._transport.rpc.send.call_args.args[1]
                self.assertEqual(payload['args']['required_capabilities'],
                                 list(execution_requirements([STREAM_FEATURE])))
                self.assertEqual(payload['request_id'], '1'*32)

    async def test_portable_version_requires_new_api_and_uses_the_existing_create(self):
        from dsec.contracts.environments import ARTIFACT_VERSION_FEATURE
        client = self.client()
        version = 'a'*64
        demand = NodeDemand(1,512,1024,0)
        client._transport.rpc.send = Mock(return_value=self.health)
        await client.open()
        with self.assertRaises(UnsupportedCapability):
            await client.run_microvm(resource_demand=demand, artifact_version=version)
        self.assertEqual(client._transport.rpc.send.call_count, 1)
        health = dict(self.health, protocol_features=[*self.health['protocol_features'], ARTIFACT_VERSION_FEATURE])
        client._transport.rpc.send = Mock(side_effect=[health, {'id':'owned'}])
        await client.open()
        await client.run_microvm(resource_demand=demand, artifact_version=version)
        sent = client._transport.rpc.send.call_args.args[1]
        self.assertEqual(sent['args']['artifact_version'], version)

    async def test_empty_requirements_preserve_wire_and_invalid_requirements_never_send(self):
        client = self.client()
        client._transport.rpc.send = Mock(side_effect=[self.health, {'id':'owned'}])
        await client.open()
        for invalid in ('native-stream-v1', None, ['gpu'], [True], [STREAM_FEATURE]*5):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                await client.run_microvm(resource_demand=NodeDemand(1,512,1024,0),
                                        required_capabilities=invalid)
        self.assertEqual(client._transport.rpc.send.call_count, 1)
        await client.run_microvm(resource_demand=NodeDemand(1,512,1024,0))
        self.assertNotIn('required_capabilities', client._transport.rpc.send.call_args.args[1]['args'])

    async def test_mutation_loss_retains_id_without_replay_or_uds_downgrade(self):
        client = self.client()
        client._transport.rpc.send = Mock(side_effect=[self.health,
            RequestOutcomeUnknown('Lost API create reply', '1'*32)])
        await client.open()
        with patch('dsec.sdk.development.SandboxClient', side_effect=AssertionError('No UDS fallback')):
            with self.assertRaises(RequestOutcomeUnknown):
                await client.run_microvm(request_id='1'*32, resource_demand=NodeDemand(1,512,1024,0))
        self.assertEqual(client._transport.rpc.send.call_count, 2)
        payload = client._transport.rpc.send.call_args.args[1]
        self.assertEqual(payload['request_id'], '1'*32)
        self.assertEqual(payload['operation'], 'create')

    async def test_unknown_api_protocol_never_opens_client(self):
        client = self.client(); client._transport.rpc.send = Mock(return_value={'protocol':'wrong'})
        with self.assertRaises(RuntimeError): await client.open()
        self.assertFalse(client._opened)

    @unittest.skipUnless(shutil.which('openssl'), 'TLS fixture needs OpenSSL')
    async def test_verified_tls_carries_shell_files_stream_and_queries_through_one_ingress(self):
        temp = tempfile.TemporaryDirectory(prefix='dsec-api-sdk-'); self.addCleanup(temp.cleanup)
        root = Path(temp.name); cert, key = root/'api.crt', root/'api.key'
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes',
            '-keyout',str(key),'-out',str(cert),'-days','1','-subj','/CN=localhost',
            '-addext','subjectAltName=DNS:localhost,IP:127.0.0.1'],
            check=True, capture_output=True)
        key.chmod(0o600)
        seen = []
        closed_tls = []
        finish = Handler.finish
        def observed_finish(handler):
            finish(handler)
            closed_tls.append(handler.request.fileno())
        self.enterContext(patch.object(Handler, 'finish', observed_finish))
        def dispatch(token, payload):
            if token != self.token or payload['project'] != 'case':
                raise AuthorizationDenied('Wrong principal or project')
            seen.append(deepcopy(payload))
            op, args = payload['operation'], payload['args']
            if op == 'health': return self.health
            if op == 'create': return {'id':'api-owned-sandbox', 'state':'RUNNING'}
            if op == 'execute': return {'exit_code':0, 'output':'api-shell'}
            if op == 'native':
                if args['action'] == 'open': return {'session_id':args['session_id']}
                if args['action'] == 'read':
                    return {'data':base64.b64encode(b'\x00binary').decode(), 'version':'v1', 'eof':True}
                return {'ok':True}
            if op == 'native_stream': return {'operation_id':payload['request_id']}
            if op == 'native_events':
                return dict(state='DONE', cursor=1, drained=True, events=[
                    {'type':'stdout','data':base64.b64encode(b'api-stream').decode()}],
                    response={'ok':True,'result':{'exit_code':0}})
            if op == 'query_request': return {'state':'DONE','sandbox_id':payload['sandbox_id']}
            if op == 'stop': return {'state':'STOPPED'}
            raise ValueError(op)
        server = ClusterHTTPServer(('127.0.0.1',0), dispatch, cert_file=str(cert), key_file=str(key))
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval':.01}); thread.start()
        def cleanup():
            server.shutdown(); thread.join(5); server.server_close()
            self.assertFalse(thread.is_alive())
            self.assertGreaterEqual(len(closed_tls), len(seen))
            self.assertTrue(all(fd == -1 for fd in closed_tls), 'TLS descriptor retained after request')
        self.addCleanup(cleanup)
        endpoints = [f'https://localhost:{server.server_port}']
        client = DSecClient(endpoints, token=self.token, project='case', ca_file=str(cert))
        with patch.dict(os.environ, {'DSEC_SOCKET':'/unused.sock'}), \
                patch('dsec.sdk.development.SandboxClient', side_effect=AssertionError('No direct Edge')):
            async with client:
                sandbox = await client.run_microvm(request_id='1'*32, resource_demand=NodeDemand(1,512,1024,0))
                result = await sandbox.run_shell('printf api-shell', request_id='2'*32)
                self.assertEqual(result['output'], 'api-shell')
                async with await sandbox.open_session(request_id='3'*32) as session:
                    await session.write_file('/tmp/input', b'\x00binary', request_id='4'*32)
                    self.assertEqual(await session.read_file('/tmp/input'), b'\x00binary')
                    events = [event async for event in session.stream('printf api-stream', request_id='5'*32)]
                    self.assertEqual(events[0]['data'], b'api-stream')
                    self.assertEqual(events[-1]['type'], 'result')
                self.assertEqual((await client.lookup_request('2'*32, sandbox_id=sandbox.id))['state'], 'DONE')
                await sandbox.stop(request_id='6'*32)
        operations = [payload['operation'] for payload in seen]
        self.assertEqual(operations[:3], ['health','create','execute'])
        self.assertIn('native_events', operations)
        self.assertEqual(operations[-1], 'stop')
        self.assertTrue(all(payload['project']=='case' for payload in seen))
        before = len(seen)
        wrong = DSecClient(endpoints, token='wrong', project='case', ca_file=str(cert))
        with self.assertRaises(ServiceError) as raised: await wrong.open()
        self.assertEqual(raised.exception.kind, 'AuthorizationDenied')
        self.assertEqual(len(seen), before)


if __name__ == '__main__':
    unittest.main()
