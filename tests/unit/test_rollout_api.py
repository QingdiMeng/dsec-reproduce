"""API routing over the real durable worker. VMM/coordination are fixtures."""
import asyncio
from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path
import tempfile
import shutil
import subprocess
import threading
import unittest
from unittest.mock import patch

from dsec.contracts.cluster import PERMISSIONS, NodeView, Quota
from dsec.contracts.errors import RequestOutcomeUnknown
from dsec.contracts.evaluation import EvaluationFailure
from dsec.contracts.rollout import ROLLOUT_FEATURE, private_rollout_id
from dsec.control.cluster_api import ClusterAPI
from dsec.control.iam import IAM
from dsec.control.placement import PlacementEngine
from dsec.control.watcher import Watcher
from dsec.host.cluster_node import NodeGateway, RemoteNode
from dsec.host.cluster_http import ClusterHTTPServer
from dsec.rollout.worker import RolloutWorker, error_response
from dsec.sdk import RolloutAPIClient, DSecClient, ScheduledOutcomeUnknown
from dsec.sdk.rollout_transport import RolloutServiceError, RolloutOutcomeUnknown
from dsec.sdk.sandbox_transport import ServiceError
from dsec.sdk.cluster import JSONRPC
from tests.unit.test_cluster_control import MemoryStore
from tests.unit.test_rollout_scheduler import scheduler


class SandboxFixture:
    def __init__(self, sid):
        self.id, self.state, self.commands = sid, 'RUNNING', []
        self.cleanup = True

    async def status(self):
        return dict(id=self.id, state=self.state, resource_cleanup_complete=self.cleanup)

    async def stop(self, **kwargs):
        self.state = 'STOPPED'; return await self.status()

    async def pause(self, **kwargs):
        self.state = 'PAUSED'; return await self.status()

    async def resume(self):
        self.state = 'RUNNING'; return await self.status()

    async def run_shell(self, command, **kwargs):
        self.commands.append(command)
        return dict(exit_code=0, output='3', timed_out=False, truncated=False)


class ClientFixture:
    def __init__(self):
        self.sandboxes = []

    async def run_microvm(self, *args, **kwargs):
        sandbox = SandboxFixture(f'{len(self.sandboxes)+1:012x}')
        self.sandboxes.append(sandbox); return sandbox


class WorkerBridge:
    def __init__(self, worker, loop):
        self.worker, self.loop, self.calls = worker, loop, []

    def call(self, operation, *, timeout_s=None, **args):
        self.calls.append((operation, deepcopy(args), timeout_s))
        future = asyncio.run_coroutine_threadsafe(self.worker.dispatch(
            {'operation':operation, 'args':args}), self.loop)
        try:
            return future.result(timeout=10)
        except Exception as exc:
            error = error_response(exc)["error"]
            raise RolloutServiceError(error["type"], error["message"], error.get("details")) from exc


class RPCFixture:
    def __init__(self, api):
        self.api, self.calls, self.lose = api, [], None

    def send(self, token, payload, **kwargs):
        self.calls.append(deepcopy(payload))
        try:
            result = self.api.call(token, payload)
        except RequestOutcomeUnknown:
            raise
        except Exception as exc:
            raise ServiceError(getattr(exc,"kind",type(exc).__name__), str(exc), getattr(exc,"details",None)) from exc
        if self.lose == payload['operation']:
            self.lose = None
            raise RequestOutcomeUnknown('Controlled committed reply loss', payload['request_id'])
        return result


class RolloutAPITests(unittest.IsolatedAsyncioTestCase):
    token = 'admin-' + 'a'*48
    key = b'r'*32
    resources = dict(cpu=1, memory_mb=512, disk_mb=1024, network_mbps=1, api_episode_slots=1)

    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory(prefix='dr-api-'); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.runtime = ClientFixture()
        self.worker = RolloutWorker(self.runtime, state_dir=self.root/'worker',
            scheduler=scheduler(cpu=8, memory_mb=4096, disk_mb=8192, network_mbps=8, api_episode_slots=8))
        self.addCleanup(self.worker.store.lock.close)
        await self.worker.initialize()
        self.addAsyncCleanup(self.worker.close_meters)
        self.addAsyncCleanup(self.worker.scheduler.close)
        self.bridge = WorkerBridge(self.worker, asyncio.get_running_loop())
        self.store = MemoryStore(); self.iam = IAM(self.store)
        quota = Quota(8,8,4096,8192,8)
        self.iam.bootstrap(self.token, quota)
        self.iam.create_project(self.token,'root','other',Quota(2,2,1024,2048,2),{'admin':sorted(PERMISSIONS)})
        self.node = NodeGateway('n1', object(), self.iam, route_key=self.key, boot_id='boot1',
            environments=('default',), worker=self.bridge, worker_inspection=self.bridge,
            minimum_demands={'default':asdict(Quota(1,1,512,1024,1))})
        watcher = Watcher({'n1':lambda:NodeView('n1','boot1',('microvm',),('default',),
            (ROLLOUT_FEATURE,),0,quota,0)})
        watcher.poll()
        self.api = ClusterAPI(self.iam,PlacementEngine(watcher),{'n1':self.node},route_key=self.key)

    async def client(self, project='root'):
        client = RolloutAPIClient(['https://api.example'],token=self.token,project=project)
        rpc = RPCFixture(self.api); client._transport.api.rpc = rpc
        await client.open(); return client, rpc

    async def create(self, client, rid='1'*32):
        return await client.create(task_id='counter-example',rollout_id=rid,
            profile={},resources=self.resources)

    async def test_project_namespace_and_concurrent_same_id_have_one_worker_owner(self):
        first, _ = await self.client(); second, _ = await self.client(); other, _ = await self.client('other')
        a,b = await asyncio.gather(self.create(first),self.create(second))
        c = await self.create(other)
        self.assertEqual(a.sandbox_id,b.sandbox_id); self.assertNotEqual(a.sandbox_id,c.sandbox_id)
        self.assertEqual(len(self.runtime.sandboxes),2)
        self.assertEqual(set(self.worker.rollouts),{private_rollout_id('root','1'*32),private_rollout_id('other','1'*32)})
        self.assertEqual(sum(op=='create' for op,_,_ in self.bridge.calls),2)
        await a.stop(); await c.stop()
        self.assertEqual(self.store.read('projects/root').value['usage']['concurrency'],0)
        self.assertEqual(self.store.read('projects/other').value['usage']['concurrency'],0)
        with self.assertRaises(RolloutServiceError): await self.create(first)

    async def test_lost_create_and_agent_reply_reconnect_to_same_episode_without_replay(self):
        client,rpc = await self.client(); rpc.lose='rollout.create'
        with self.assertRaises(ScheduledOutcomeUnknown): await self.create(client)
        other,_ = await self.client(); sandbox = await other.attach('1'*32)
        retry = await self.create(other)
        self.assertEqual(sandbox.sandbox_id,retry.sandbox_id)
        self.assertEqual(sum(op=='create' for op,_,_ in self.bridge.calls),1)
        await sandbox.start_dialogue([{'role':'user','content':'Write 3'}])
        other._transport.api.rpc.lose='rollout.agent_step'
        message = {'role':'assistant','content':'```bash\nprintf 3 > /rl-counter\n```'}
        with self.assertRaises(ScheduledOutcomeUnknown):
            await sandbox.agent_step('printf 3 > /rl-counter',step_id=0,action_id='step-0',assistant_message=message)
        attached = await client.attach('1'*32)
        dialogue = await attached.dialogue()
        self.assertEqual(dialogue['rollout_id'],'1'*32)
        self.assertEqual(dialogue['next_step'],1)
        self.assertEqual(dialogue['messages'][1],message)
        self.assertEqual(len(self.runtime.sandboxes),1)
        self.assertEqual(self.runtime.sandboxes[0].commands,['printf 3 > /rl-counter'])
        reward = await attached.evaluate_counter(3)
        self.assertEqual(reward['value'],1)
        await attached.stop()

    async def test_cross_project_raw_worker_ops_and_direct_sandbox_path_are_denied(self):
        client,_ = await self.client(); sandbox = await self.create(client)
        other,_ = await self.client('other')
        with self.assertRaises(RolloutServiceError): await other.attach('1'*32)
        payload = dict(project='root',request_id='2'*32,operation='rollout.admission_check',
                       args={'rollout_id':'1'*32},sandbox_id=None)
        with self.assertRaises(ValueError): self.api.call(self.token,payload)
        plain = DSecClient(['https://api.example'],token=self.token,project='root')
        plain._transport.rpc = RPCFixture(self.api)
        await plain.open()
        with self.assertRaises(ServiceError): await plain.attach(sandbox.sandbox_id)
        await sandbox.stop()

    async def test_cleanup_and_verifier_failure_require_proof_and_keep_evidence(self):
        from tests.unit.test_worker_evaluators import Evaluator
        evaluator = Evaluator('failure'); self.worker.evaluators[evaluator.id]=evaluator
        client,_ = await self.client(); sandbox=await self.create(client)
        with self.assertRaises(RolloutServiceError): await sandbox.evaluate(evaluator.id,{'target':7})
        await sandbox.refresh()
        self.assertEqual(sandbox.state,'UNKNOWN')
        private=private_rollout_id('root','1'*32)
        saved=json.loads((self.root/'worker'/(private+'.json')).read_text())
        self.assertIsNone(saved['reward']); self.assertIn('verifier_failure',saved)
        self.runtime.sandboxes[0].cleanup=False
        with self.assertRaises(ScheduledOutcomeUnknown): await sandbox.stop()
        self.assertEqual(self.iam.lookup('root','1'*32)['state'],'BOUND')
        self.runtime.sandboxes[0].cleanup=True
        await asyncio.to_thread(self.node.reconcile)
        self.assertEqual(self.iam.lookup('root','1'*32)['state'],'RELEASED')

    async def test_edge_unknown_identity_survives_worker_and_api_without_replay(self):
        client,_ = await self.client(); sandbox = await self.create(client)
        calls = []
        async def uncertain(command, **kwargs):
            calls.append(command)
            raise ServiceError('CommandOutcomeUnknown', 'Lost command reply')
        with patch.object(self.runtime.sandboxes[0], 'run_shell', side_effect=uncertain):
            with self.assertRaises(ScheduledOutcomeUnknown) as raised:
                await sandbox.run_shell('touch /effect', step_id=0, action_id='one')
        self.assertEqual(raised.exception.rollout_id, '1'*32)
        self.assertEqual(calls, ['touch /effect'])
        attached = await client.attach('1'*32)
        self.assertEqual(attached.state, 'UNKNOWN')
        self.assertEqual(self.iam.lookup('root','1'*32)['state'], 'BOUND')
        self.assertEqual(self.runtime.sandboxes[0].state, 'RUNNING')
        await attached.stop()

    async def test_worker_wire_preserves_verifier_failure_details(self):
        from dsec.sdk.rollout_transport import RolloutClient
        path = str(self.root/'wire.sock')
        response = error_response(EvaluationFailure('Verifier failed', {'log':'saved.txt'}))
        async def handle(reader, writer):
            await reader.readline()
            writer.write(json.dumps(response).encode()+b'\n')
            await writer.drain(); writer.close(); await writer.wait_closed()
        server = await asyncio.start_unix_server(handle, path=path)
        try:
            with self.assertRaises(RolloutServiceError) as raised:
                await asyncio.to_thread(RolloutClient(path).call, 'task_evaluate')
            self.assertEqual(raised.exception.kind, 'EvaluationFailure')
            self.assertEqual(raised.exception.details, {'log':'saved.txt'})
        finally:
            server.close(); await server.wait_closed()

    async def test_dispatch_gap_not_found_keeps_quota_and_never_resends_create(self):
        client,_ = await self.client()
        call = self.bridge.call
        attempts = []
        def lose(operation, **args):
            if operation == 'create':
                attempts.append(operation)
                raise RolloutOutcomeUnknown('Lost before observing worker admission')
            return call(operation, **args)
        with patch.object(self.bridge,'call',side_effect=lose):
            with self.assertRaises(ScheduledOutcomeUnknown): await self.create(client)
            with self.assertRaises(ScheduledOutcomeUnknown): await self.create(client)
            with self.assertRaises(ScheduledOutcomeUnknown): await client.attach('1'*32)
            with self.assertRaises(ScheduledOutcomeUnknown): await client.recover_create('1'*32)
        self.assertEqual(attempts,['create'])
        self.assertEqual(self.iam.lookup('root','1'*32)['state'],'DISPATCHED')
        self.assertEqual(self.store.read('projects/root').value['usage']['concurrency'],1)
        self.assertEqual(len(self.runtime.sandboxes),0)

    async def test_worker_health_failure_is_typed_and_explicit_recovery_dispatches_once(self):
        client,_ = await self.client()
        with patch.object(self.bridge,'call',side_effect=RolloutOutcomeUnknown('Restart window')):
            with self.assertRaises(ScheduledOutcomeUnknown) as raised:
                await self.create(client)
            self.assertEqual(raised.exception.rollout_id,'1'*32)
        self.assertEqual(self.iam.lookup('root','1'*32)['state'],'ADMITTED')
        self.assertEqual(len(self.runtime.sandboxes),0)
        first,second=await asyncio.gather(client.recover_create('1'*32),client.recover_create('1'*32))
        self.assertEqual(first.sandbox_id,second.sandbox_id)
        self.assertEqual(sum(op=='create' for op,_,_ in self.bridge.calls),1)
        self.assertEqual(len(self.runtime.sandboxes),1)
        await first.stop()
        with self.assertRaises(RolloutServiceError):await client.recover_create('1'*32)
        self.assertEqual(self.store.read('projects/root').value['usage']['concurrency'],0)

    async def test_unhealthy_worker_retains_grant_and_recovery_cannot_change_owner_or_arguments(self):
        client,_=await self.client()
        with patch.object(self.node.rollouts,'healthy',return_value=False):
            with self.assertRaises(ScheduledOutcomeUnknown):await self.create(client)
        self.assertEqual(self.iam.lookup('root','1'*32)['state'],'ADMITTED')
        self.assertEqual(self.store.read('projects/root').value['usage']['concurrency'],1)
        other,_=await self.client('other')
        with self.assertRaises(RolloutServiceError):await other.recover_create('1'*32)
        payload=dict(project='root',request_id='2'*32,operation='rollout.recover_create',
            sandbox_id=None,args={'rollout_id':'1'*32,'resources':dict(self.resources,cpu=0)})
        with self.assertRaises(ValueError):self.api.call(self.token,payload)
        sandbox=await client.recover_create('1'*32);await sandbox.stop()

    async def test_stale_capacity_nonadmission_waits_with_same_id_without_mutation_replay(self):
        client,rpc=await self.client()
        send=rpc.send; attempts=[]
        def blocked_once(token,payload,**kwargs):
            if payload['operation']=='rollout.create':
                attempts.append(payload['args']['rollout_id'])
                if len(attempts)==1:
                    self.assertIsNone(self.iam.lookup('root','1'*32))
                    raise ServiceError('NoPlacement','Watcher still sees a busy slot')
            return send(token,payload,**kwargs)
        with patch.object(rpc,'send',side_effect=blocked_once):
            sandbox=await self.create(client)
        self.assertEqual(attempts,['1'*32,'1'*32])
        self.assertEqual(sum(op=='create' for op,_,_ in self.bridge.calls),1)
        await sandbox.stop()

    @unittest.skipUnless(shutil.which('openssl'), 'TLS fixture needs OpenSSL')
    async def test_tls_controllers_mtls_node_and_relative_client_config(self):
        cert, key = self.root/'api.crt', self.root/'api.key'
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes',
            '-keyout',str(key),'-out',str(cert),'-days','1','-subj','/CN=localhost',
            '-addext','subjectAltName=DNS:localhost,IP:127.0.0.1'],check=True,capture_output=True)
        servers=[]
        def server(dispatch, node=False):
            options = dict(cert_file=str(cert),key_file=str(key))
            if node: options.update(client_ca_file=str(cert),node_token='internal-'+'b'*48)
            value=ClusterHTTPServer(('127.0.0.1',0),dispatch,**options)
            thread=threading.Thread(target=value.serve_forever,kwargs={'poll_interval':.01});thread.start()
            servers.append((value,thread));return f'https://localhost:{value.server_port}'
        def cleanup():
            for value,thread in reversed(servers):
                value.shutdown();thread.join(5);value.server_close()
                self.assertFalse(thread.is_alive())
        self.addCleanup(cleanup)
        remote=RemoteNode(JSONRPC([server(self.node.dispatch,True)],ca_file=str(cert),
            cert_file=str(cert),key_file=str(key)),token='internal-'+'b'*48,route_key=self.key)
        first=ClusterAPI(self.iam,self.api.placement,{'n1':remote},route_key=self.key)
        second=ClusterAPI(IAM(self.store),self.api.placement,{'n1':remote},route_key=self.key)
        endpoints=[server(first.call),server(second.call)]
        token_file=self.root/'token';token_file.write_text(self.token);token_file.chmod(0o600)
        config=self.root/'client.json';config.write_text(json.dumps(dict(
            endpoints=endpoints,project='root',token_file='token',ca_file='api.crt')))
        client=RolloutAPIClient.from_config(config);await client.open()
        def lost(stage,envelope):
            if envelope['operation']=='rollout.create':
                raise RequestOutcomeUnknown('Committed API reply lost')
        first._checkpoint=lost
        with self.assertRaises(ScheduledOutcomeUnknown):await self.create(client)
        reconnect=RolloutAPIClient([endpoints[1]],token=self.token,project='root',ca_file=str(cert))
        await reconnect.open();sandbox=await reconnect.attach('1'*32)
        result=await sandbox.run_shell('printf 3',step_id=0,action_id='one')
        self.assertEqual(result['output'],'3')
        self.assertEqual((await sandbox.evaluate_counter(3))['value'],1)
        await sandbox.stop()
        self.assertEqual(len(self.runtime.sandboxes),1)
        self.assertEqual(self.iam.lookup('root','1'*32)['state'],'RELEASED')

    async def test_restart_acceptance_uses_real_worker_dialogue_timeout_and_reward_contract(self):
        # Only service controls/VMM are fixtures. Real API/worker enforce the
        # dialogue-only agent_step path, command budget and counter reward shape.
        from types import SimpleNamespace
        from tools.verify_rollout_restart import verify
        client,rpc=await self.client()
        available=True
        original=rpc.send
        def send(*args,**kwargs):
            if not available:raise RequestOutcomeUnknown('Controlled service outage')
            return original(*args,**kwargs)
        async def shell(sandbox,command,**kwargs):
            sandbox.commands.append(command)
            if "printf 'once" in command:
                sandbox.effects=getattr(sandbox,'effects',0)+1
                output='committed\n'
            elif command=='cat /restart-once; cat /rl-counter':
                output='once\n'*sandbox.effects+'3'
            else:output='3'
            return dict(exit_code=0,output=output,timed_out=False,truncated=False)
        def control(argv,**kwargs):
            nonlocal available
            op=argv[1]
            if op in ('stop','start'):
                available=op=='start';return
            receipt=self.iam.lookup('root',argv[-1])
            sandbox=self.runtime.sandboxes[0]
            live=sandbox.state=='RUNNING'
            proof=dict(held_concurrency=self.store.read('projects/root').value['usage']['concurrency'],
                       edge_leases=int(live),admission_state=receipt['state'])
            if op=='inspect':
                proof['physical']=(dict(sandbox_id=sandbox.id,pid=321,start_ticks=1000,
                                        boot_id='fixture') if live else None)
            kwargs['stdout'].write(json.dumps(proof).encode())
        plan=self.root/'plan.json'
        plan.write_text(json.dumps(dict(inspect=['fixture','inspect','{rollout_id}'],
            inspect_outage=['fixture','quota','{rollout_id}'],phases=[dict(name='protocol_fixture',
                stop=[['fixture','stop']],start=[['fixture','start']])])))
        with patch('tools.verify_rollout_restart.RolloutAPIClient.from_config',return_value=client), \
             patch('tools.verify_rollout_restart.subprocess.run',side_effect=control), \
             patch.object(rpc,'send',side_effect=send), \
             patch.object(SandboxFixture,'run_shell',new=shell):
            result=await verify(SimpleNamespace(plan=plan,api_config=self.root/'unused-api.json',
                                               out=self.root/'acceptance'))
        self.assertEqual(result['status'],'passed')
        self.assertEqual(result['cleanup_errors'],[])
        self.assertEqual(self.runtime.sandboxes[0].effects,1)
        self.assertEqual(self.store.read('projects/root').value['usage']['concurrency'],0)


if __name__ == '__main__':
    unittest.main()
