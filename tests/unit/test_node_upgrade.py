"""Release changes reuse the membership CAS and the owning Node's proofs."""
from copy import deepcopy
import threading
import unittest
from unittest.mock import Mock, patch

from dsec.contracts.cluster import NoPlacement
from dsec.contracts.requests import RequestConflict
from dsec.contracts.release import COMPATIBILITY
from dsec.host.releases import permitted_release
from dsec.control.node_gateway import NodeGateway
from tests.unit.test_node_lifecycle import FleetFixture


class NodeUpgradeTests(FleetFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.source=dict(build='a'*64,version='0.1.0.dev0',compatibility=COMPATIBILITY)
        self.target=dict(build='b'*64,version='0.1.0.dev1',compatibility=COMPATIBILITY)
        self.active();self.move('drain',proof=False);self.move('maintain')

    def change(self,action,release=None,observed=None,identity=None,version=None):
        current=self.life.read('n1');proof=dict(self.proof(current),release=observed or self.source)
        return self.life.transition(self.token,identity or self.rid(),'n1',current['version'] if version is None else version,
                                   action,proof=proof,release=release)

    def test_upgrade_requires_current_empty_maintenance_and_compatible_state(self):
        bad=deepcopy(self.target);bad['compatibility']['edge']=2
        with self.assertRaises(ValueError):self.change('upgrade',bad)
        self.assertIsNone(self.life.read('n1').get('upgrade'))
        current=self.life.read('n1');proof=dict(self.proof(current,False),release=self.source)
        with self.assertRaises(NoPlacement):
            self.life.transition(self.token,self.rid(),'n1',current['version'],'upgrade',release=self.target,proof=proof)
        self.move('resume')
        with self.assertRaises(RequestConflict):self.change('upgrade',self.target)

    def test_pending_upgrade_fences_resume_retirement_and_competing_plans(self):
        identity=self.rid();current=self.life.read('n1');first=self.change('upgrade',self.target,identity=identity)
        again=self.change('upgrade',self.target,identity=identity,version=current['version'])
        self.assertEqual(first,again)
        for action in ('resume','retire','upgrade'):
            with self.assertRaises(RequestConflict):self.change(action,self.target if action=='upgrade' else None)
        with self.assertRaises(NoPlacement):self.admit()
        self.assertEqual(self.life.read('n1')['state'],'MAINTENANCE')

    def test_commit_requires_loaded_target_and_resume_requires_committed_release(self):
        self.change('upgrade',self.target)
        with self.assertRaises(NoPlacement):self.change('commit')
        done=self.change('commit',observed=self.target)
        self.assertEqual(done['active_release'],self.target);self.assertIsNone(done['upgrade'])
        with self.assertRaises(NoPlacement):self.change('resume')
        self.assertEqual(self.change('resume',observed=self.target)['state'],'ACTIVE')

    def test_rollback_requires_old_runtime_and_delayed_commit_cannot_undo_it(self):
        pending=self.change('upgrade',self.target)
        with self.assertRaises(NoPlacement):self.change('rollback',observed=self.target)
        done=self.change('rollback');self.assertEqual(done['active_release'],self.source)
        with self.assertRaises(RequestConflict):self.change('commit',observed=self.target,version=pending['version'])
        self.assertEqual(self.life.read('n1'),done)

    def test_commit_and_rollback_are_ordered_by_one_expected_version(self):
        pending=self.change('upgrade',self.target);entered=threading.Event();release=threading.Event();errors=[]
        def before(reads,writes):
            item=writes.get('fleet/n1')
            if item and item.get('active_release')==self.target and item.get('upgrade') is None:
                entered.set();self.assertTrue(release.wait(5))
        self.store.before_commit=before
        def commit():
            try:self.change('commit',observed=self.target,version=pending['version'])
            except Exception as exc:errors.append(exc)
        thread=threading.Thread(target=commit);thread.start()
        try:
            self.assertTrue(entered.wait(5));self.change('rollback')
        finally:release.set();thread.join(5)
        self.assertFalse(thread.is_alive());self.assertEqual(len(errors),1)
        self.assertIsInstance(errors[0],RequestConflict)
        self.assertEqual(self.life.read('n1')['active_release'],self.source)

    def test_launch_and_runtime_gate_reject_stale_release_after_commit(self):
        self.change('upgrade',self.target)
        permitted_release(self.life.read('n1'),self.target)
        self.change('commit',observed=self.target)
        with self.assertRaises(ValueError):permitted_release(self.life.read('n1'),self.source)
        node=Mock(iam=self.iam,node_id='n1')
        node._running_release.return_value=self.source
        with self.assertRaises(RequestConflict):NodeGateway._check_committed_release(node)

    def test_gateway_edge_and_episode_must_report_same_release(self):
        node=Mock();node.inspection.call.return_value={'release':self.source}
        node.rollouts.worker=object();node.rollouts._call.return_value={'release':self.target}
        with patch('dsec.control.node_gateway.installed_release',return_value=self.source):
            with self.assertRaises(ValueError):NodeGateway._running_release(node)

    def test_bound_owner_queries_do_not_require_a_new_software_admission_probe(self):
        from dataclasses import asdict
        from tests.unit.test_cluster_control import EdgeFixture
        self.change('upgrade',self.target);self.change('commit',observed=self.target)
        self.change('resume',observed=self.target)
        edge=EdgeFixture();node=NodeGateway('n1',edge,self.iam,route_key=self.key,boot_id='boot1',
            environments=['default'],minimum_demands={'default':asdict(self.demand)})
        receipt=self.admit()
        with patch.object(node,'_running_release',return_value=self.target):node.create(receipt)
        with patch.object(node,'_running_release',side_effect=RuntimeError('probe unavailable')):
            self.assertEqual(node.query_create(receipt)['state'],'DONE')
            with self.assertRaises(RuntimeError):node.create(self.admit())
