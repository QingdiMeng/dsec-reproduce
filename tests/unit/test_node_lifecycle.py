"""Controlled quorum/CAS schedules; fixtures do not establish physical HA."""
from dataclasses import asdict
import asyncio
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from dsec.contracts.cluster import AuthorizationDenied, CoordinationUnavailable, NoPlacement, PERMISSIONS, Quota, NodeView, SandboxRoute, node_credential
from dsec.contracts.requests import RequestConflict
from dsec.control.iam import IAM
from dsec.control.node_lifecycle import NodeLifecycle
from dsec.control.node_gateway import NodeGateway
from dsec.control.cluster_api import ClusterAPI
from dsec.control.placement import PlacementEngine
from dsec.control.watcher import Watcher
from dsec.host.node_directory import NodeDirectory, NodeProbes
from tests.unit.test_cluster_control import MemoryStore, EdgeFixture


class FleetFixture:
    token='admin-'+'x'*48
    key=b'k'*32
    demand=Quota(1,1,512,1024,1)

    def setUp(self):
        self.store=MemoryStore();self.iam=IAM(self.store,managed_nodes=True)
        self.iam.bootstrap(self.token,Quota(20,20,20000,40000,100))
        self.iam.create_project(self.token,'root','case',Quota(10,10,10000,20000,50),{'admin':sorted(PERMISSIONS)})
        self.life=NodeLifecycle(self.iam);self.sequence=0

    def rid(self):self.sequence+=1;return f'{self.sequence:032x}'
    def spec(self,node='n1',port=18444):return dict(id=node,endpoints=[f'https://192.0.2.10:{port}'])
    def join(self,node='n1',port=18444):return self.life.join(self.token,self.rid(),self.spec(node,port))
    def proof(self,record,empty=True):return dict(node_id=record['id'],version=record['version'],boot_id='boot1',empty=empty)
    def move(self,action,node='n1',proof=True):
        record=self.life.read(node)
        return self.life.transition(self.token,self.rid(),node,record['version'],action,
                                   proof=self.proof(record) if proof else None)
    def active(self,node='n1',port=18444):self.join(node,port);return self.move('activate',node)
    def admit(self,node='n1',identity=None):
        return self.iam.admit(self.token,'case',identity or self.rid(),'d'*64,node,'microvm',self.demand,args={})


class NodeLifecycleTests(FleetFixture, unittest.TestCase):
    def test_enrollment_activation_and_new_request_gate(self):
        with self.assertRaises(NoPlacement):self.admit()
        joined=self.join()
        with self.assertRaises(NoPlacement):self.admit()
        self.assertEqual(joined['state'],'JOINING')
        active=self.move('activate');self.assertEqual(active['version'],2)
        admitted=self.admit()
        self.assertTrue(admitted['node_accounted'])
        self.assertEqual(self.life.read('n1')['held'],1)
        self.move('drain',proof=False)
        with self.assertRaises(NoPlacement):self.admit()
        self.assertEqual(len(self.iam.admissions('n1')),1)

    def test_admitted_work_can_finish_while_draining_without_replacement(self):
        self.active();receipt=self.admit()
        self.move('drain',proof=False)
        edge=EdgeFixture()
        gateway=NodeGateway('n1',edge,self.iam,route_key=self.key,boot_id='boot1',environments=['default'],
            minimum_demands={'default':asdict(self.demand)})
        result=gateway.create(receipt)
        self.assertEqual(edge.created,1)
        with self.assertRaises(NoPlacement):self.move('maintain')
        gateway.call(SandboxRoute.decode(result['id'],self.key),'stop',self.rid(),{})
        self.assertEqual(self.life.read('n1')['held'],0)
        self.assertEqual(self.move('maintain')['state'],'MAINTENANCE')
        self.assertEqual(edge.states['000000000001'],'STOPPED')

    def test_maintenance_resume_retirement_and_tombstone(self):
        self.active();self.move('drain',proof=False)
        self.assertEqual(self.move('maintain')['state'],'MAINTENANCE')
        self.assertEqual(self.move('resume')['state'],'ACTIVE')
        self.move('drain',proof=False)
        self.assertEqual(self.move('retire')['state'],'RETIRED')
        with self.assertRaises(RequestConflict):self.join()
        with self.assertRaises(NoPlacement):self.admit()
        # The physical endpoint can be reassigned to a new identity, while old
        # node IDs remain tombstones and old sandbox routes retain their owner.
        self.assertEqual(self.join('n2')['state'],'JOINING')

    def test_endpoint_claims_and_unactivated_withdrawal_are_atomic(self):
        self.join()
        with self.assertRaises(RequestConflict):self.join('n2')
        self.assertIsNone(self.life.read('n2'))
        self.assertEqual(self.move('withdraw',proof=False)['state'],'RETIRED')
        with self.assertRaises(RequestConflict):self.move('activate')
        self.assertEqual(self.join('n2')['state'],'JOINING')

    def test_unknown_dispatch_and_unreachable_node_do_not_authorize_retirement(self):
        self.active();receipt=self.admit()
        self.iam.begin_dispatch('case',receipt['request_id'],'n1')
        self.move('drain',proof=False)
        for action in ('maintain','retire'):
            with self.assertRaises(NoPlacement):self.move(action)
        self.assertEqual(self.life.read('n1')['held'],1)
        self.assertEqual(self.iam.lookup('case',receipt['request_id'])['state'],'DISPATCHED')

    def test_empty_proof_and_version_are_both_required(self):
        self.active();drained=self.move('drain',proof=False)
        with self.assertRaises(NoPlacement):self.move('maintain',proof=False)
        with self.assertRaises(NoPlacement):
            self.life.transition(self.token,self.rid(),'n1',drained['version'],'retire',proof=self.proof(drained,False))
        maintenance=self.move('maintain');old=self.rid()
        self.life.transition(self.token,old,'n1',maintenance['version'],'resume',proof=self.proof(maintenance))
        self.move('drain',proof=False);self.move('retire')
        with self.assertRaises(RequestConflict):
            self.life.transition(self.token,old,'n1',maintenance['version'],'resume',proof=self.proof(maintenance))
        self.assertEqual(self.life.read('n1')['state'],'RETIRED')

    def test_same_transition_reconciles_lost_reply_and_conflicting_reuse_rejects(self):
        identity=self.rid();spec=self.spec()
        first=self.life.join(self.token,identity,spec)
        self.assertEqual(self.life.join(self.token,identity,spec),first)
        with self.assertRaises(RequestConflict):self.life.join(self.token,identity,self.spec(port=18445))
        self.move('activate');receipt=self.admit();self.move('drain',proof=False)
        self.iam.release('case',receipt['request_id'],'n1',nonadmission=True)
        self.iam.release('case',receipt['request_id'],'n1',nonadmission=True)
        self.assertEqual(self.life.read('n1')['held'],0)

    def test_tenant_policy_and_quorum_loss_cannot_change_fleet(self):
        other='tenant-'+'t'*48
        self.iam.issue_credential(self.token,'tenant',new_token=other)
        self.iam.set_policy(self.token,'case',{'admin':sorted(PERMISSIONS),'tenant':sorted(PERMISSIONS)})
        with self.assertRaises(AuthorizationDenied):self.life.join(other,self.rid(),self.spec())
        self.assertIsNone(self.life.read('n1'))
        self.store.unavailable=True
        with self.assertRaises(CoordinationUnavailable):self.join()

    def test_drain_wins_against_a_controller_with_stale_active_snapshot(self):
        self.active();entered=threading.Event();release=threading.Event();errors=[]
        identity=self.rid()
        def before(reads,writes):
            if f'admissions/case/{identity}' in writes:
                entered.set();self.assertTrue(release.wait(5))
        self.store.before_commit=before
        def run():
            try:self.admit(identity=identity)
            except Exception as exc:errors.append(exc)
        thread=threading.Thread(target=run);thread.start()
        try:
            self.assertTrue(entered.wait(5));self.move('drain',proof=False)
        finally:release.set();thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors),1);self.assertIsInstance(errors[0],NoPlacement)
        self.assertIsNone(self.iam.lookup('case',identity))
        self.assertEqual(self.life.read('n1')['held'],0)

    def test_admission_wins_then_drain_preserves_its_count(self):
        self.active();entered=threading.Event();release=threading.Event();errors=[];identity=self.rid()
        def before(reads,writes):
            item=writes.get('fleet/n1')
            if item and item['state']=='DRAINING':entered.set();self.assertTrue(release.wait(5))
        self.store.before_commit=before
        def run():
            try:self.life.transition(self.token,identity,'n1',2,'drain')
            except Exception as exc:errors.append(exc)
        thread=threading.Thread(target=run);thread.start()
        try:
            self.assertTrue(entered.wait(5));self.admit()
        finally:release.set();thread.join(5)
        self.assertFalse(thread.is_alive());self.assertEqual(errors,[])
        self.assertEqual(self.life.read('n1')['state'],'DRAINING')
        self.assertEqual(self.life.read('n1')['held'],1)
        with self.assertRaises(NoPlacement):self.move('retire')

    def test_dynamic_directory_watcher_and_api_discover_without_reconstruction(self):
        class Peer:
            def __init__(inner,endpoints,**tls):inner.endpoints=tuple(endpoints)
            def send(inner,token,payload,**options):
                self.assertEqual(token,node_credential(self.key,'n1'))
                if payload['operation']=='node_probe':
                    return asdict(NodeView('n1','boot1',('microvm',),('default',),(),0,self.demand,0))
                member=self.life.read('n1')
                return self.proof(member)
        directory=NodeDirectory({},self.life,dict(ca_file='ca',cert_file='client',key_file='key'),self.key,rpc_factory=Peer)
        watcher=Watcher(NodeProbes(directory))
        placement=PlacementEngine(watcher,membership=self.life)
        api=ClusterAPI(self.iam,placement,directory,route_key=self.key)
        def call(operation,args,project='root'):
            return api.call(self.token,dict(project=project,request_id=self.rid(),operation=operation,args=args))
        self.assertEqual(list(directory),[])
        joined=call('node.join',dict(spec=self.spec()))
        watcher.poll()
        with self.assertRaises(NoPlacement):placement.choose('microvm','default',self.demand)
        call('node.activate',dict(node='n1',version=joined['version']))
        self.assertEqual(placement.choose('microvm','default',self.demand),'n1')
        call('node.drain',dict(node='n1',version=2))
        public=call('node.list',{})['nodes'][0]
        self.assertFalse(public['schedulable'])
        self.assertEqual(set(public['membership']),{'state','version','held'})
        self.assertTrue(call('audit.list',{})['items'])
        self.assertFalse(any(item['action'].startswith('node.') for item in call('audit.list',{},project='case')['items']))
        self.assertEqual(call('node.maintain',dict(node='n1',version=3))['state'],'MAINTENANCE')
        with self.assertRaises(RequestConflict):call('node.resume',dict(node='n1',version=1))
        with self.assertRaises(AuthorizationDenied):call('node.get',dict(node='n1'),project='case')

    def test_node_proof_needs_empty_edge_and_episode_services(self):
        self.active();self.move('drain',proof=False)
        edge=Mock();worker=Mock()
        gateway=NodeGateway('n1',edge,self.iam,route_key=self.key,boot_id='boot1',environments=['default'],
            minimum_demands={'default':asdict(self.demand)},worker=worker)
        state=dict(leases=[],recovery_errors=[]);boxes=[];episode=dict(empty=True)
        edge.call.side_effect=lambda op: state if op=='node_status' else ({} if op=='health' else boxes)
        worker.call.side_effect=lambda *args,**kwargs:episode
        with patch.object(gateway,'health',return_value=NodeView('n1','boot1',('microvm',),('default',),(),0,self.demand,0)):
            self.assertTrue(gateway.lifecycle_proof(3)['empty'])
            state['leases']=['held'];self.assertFalse(gateway.lifecycle_proof(3)['empty']);state['leases']=[]
            boxes.append(dict(state='STOPPED',resource_cleanup_complete=False))
            self.assertFalse(gateway.lifecycle_proof(3)['empty']);boxes.clear()
            episode['empty']=False;self.assertFalse(gateway.lifecycle_proof(3)['empty'])
            with self.assertRaises(RequestConflict):gateway.lifecycle_proof(2)

    def test_episode_drain_status_keeps_unknown_pending_and_reserved_work_visible(self):
        from dsec.rollout.episodes import EpisodeService
        service=EpisodeService(None)
        async def status():return await service.dispatch(dict(operation='drain_status'))
        self.assertTrue(asyncio.run(status())['empty'])
        for state,held,pending in (('UNKNOWN',True,{}),('STOPPED',True,None),('STOPPED',False,{'operation':'create'})):
            service.rollouts={'r':SimpleNamespace(state=state,lease_held=held,pending=pending)}
            self.assertFalse(asyncio.run(status())['empty'])
        service.rollouts={'r':SimpleNamespace(state='STOPPED',lease_held=False,pending=None)}
        self.assertTrue(asyncio.run(status())['empty'])


if __name__=='__main__':unittest.main()
