"""Bind cluster model safety gates to canonical regression mutation controls.

This verifies reviewed code boundaries and behavioral regressions. It does not
claim arbitrary code/TLA equivalence or distributed linearizability. The model
checker separately exhausts bounded interleavings; this tool requires each
corresponding code guard removal to cause a behavioral assertion failure.
"""
import argparse
import hashlib
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'src'),str(ROOT/'compat/python'), str(ROOT)]
from dsec.control import iam
from dsec.control import node_gateway as cluster_node
from tests.unit import test_cluster_control as regression
from tests.unit import test_rollout_api as rollout_regression
from dsec.control import rollout_gateway
from dsec.control import node_lifecycle
from tests.unit import test_node_lifecycle as fleet_regression
from tests.unit import test_node_upgrade as upgrade_regression

CONTROLS = (
    ('quota_guard', iam, 'IAM', 'admit',
     "if not usage.plus(Quota(**record['delegated'])).fits(Quota(**record['quota'])):",
     'if False:', 'test_concurrent_admissions_never_exceed_quota_and_keep_one_target', 'QuotaBounded'),
    ('unknown_replacement', iam, 'IAM', 'admit',
     'return old, {}\n            from dsec.control import node_lifecycle',
     "return dict(old, node=node), {key:dict(old, node=node)}\n            from dsec.control import node_lifecycle",
     'test_concurrent_admissions_never_exceed_quota_and_keep_one_target', 'UniqueWritableOwner'),
    ('health_release', cluster_node, 'NodeGateway', 'reconcile',
     '# arrive. UNKNOWN/PENDING never permit quota release.\n                        continue',
     "# arrive. UNKNOWN/PENDING never permit quota release.\n                        self.iam.release(current['project'], current['request_id'], self.node_id, nonadmission=True)\n                        continue",
     'test_ttl_reconciliation_needs_stopped_proof_and_preserves_unissued_grant', 'ReleasedHasProof'),
    ('late_rejected_dispatch', cluster_node, 'NodeGateway', 'create',
     "if receipt['state'] == 'RELEASED':", 'if False:',
     'test_definitively_rejected_admission_cannot_dispatch_after_release', 'ReleasedHasProof'),
)


def run(root, method=None, test_class=regression.ClusterTests):
    root.mkdir()
    suite = (unittest.defaultTestLoader.loadTestsFromTestCase(test_class)
        if method is None else unittest.TestSuite([test_class(method)]))
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    (root/'regression.log').write_text(stream.getvalue())
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve(); root.mkdir(parents=True, exist_ok=False)
    report = dict(status='failed', controls=[], scope='reviewed code guards and bounded-model correspondence; no general equivalence proof')
    try:
        result = run(root/'canonical')
        if not result.wasSuccessful() or result.skipped:
            raise AssertionError('Canonical cluster regressions must pass without skips')
        report['tests'] = result.testsRun
        for fault,module,cls,method,before,after,test,invariant in CONTROLS:
            source = Path(module.__file__).read_text()
            if source.count(before) != 1:
                raise AssertionError(f'Code/model boundary changed: {fault}; review correspondence')
            mutated = source.replace(before, after, 1)
            if fault == 'late_rejected_dispatch':
                # Release is defended at three Node boundaries. Removing only
                # the first guard is correctly caught by the later two gates.
                changes = (
                    ("if proof['state'] != 'NOT_FOUND' or receipt['state'] != 'ADMITTED':",
                     "if proof['state'] != 'NOT_FOUND':"),
                    ("receipt = self.iam.begin_dispatch(receipt['project'], receipt['request_id'], self.node_id)",
                     'receipt = receipt'),
                )
                for previous,replacement in changes:
                    if mutated.count(previous) != 1:
                        raise AssertionError('Release/dispatch correspondence changed; review all gates')
                    mutated = mutated.replace(previous,replacement,1)
            case = root/fault; case.mkdir()
            path = case/(module.__name__+'.py'); path.write_text(mutated)
            namespace = dict(module.__dict__)
            exec(compile(mutated, str(path), 'exec'), namespace)
            with patch.object(getattr(module, cls), method, getattr(namespace[cls], method)):
                result = run(case/'test', test)
            passed = len(result.failures) == 1 and not result.errors and not result.skipped
            report['controls'].append(dict(fault=fault,invariant=invariant,regression=test,passed=passed))
            if not passed:
                raise AssertionError(f'Required code mutation did not produce a behavioral failure: {fault}')
        canonical_rollout = run(root/'rollout-canonical', test_class=rollout_regression.RolloutAPITests)
        if not canonical_rollout.wasSuccessful() or canonical_rollout.skipped:
            raise AssertionError('Canonical rollout regressions must pass without skips')
        controls = (
            ('rollout_unready_dispatch', 'create',
             "if not self.healthy():\n                raise RequestOutcomeUnknown('Worker not ready; original admission retained', receipt['request_id'])",
             "if False:\n                raise RequestOutcomeUnknown('Worker not ready; original admission retained', receipt['request_id'])",
             'test_unhealthy_worker_retains_grant_and_recovery_cannot_change_owner_or_arguments', 'DispatchRequiresAcceptedHealth'),
            ('rollout_replay', 'create',
             "if receipt['state'] == 'ADMITTED':\n                self.node.iam.begin_dispatch(receipt['project'], receipt['request_id'], self.node.node_id)",
             'if True:\n                pass',
             'test_lost_create_and_agent_reply_reconnect_to_same_episode_without_replay', 'NoCreateReplay'),
            ('rollout_unproved_release', '_observe',
             "if (status.get('id') != sid or status.get('state') != 'STOPPED'\n                        or status.get('resource_cleanup_complete') is False):",
             'if False:',
             'test_cleanup_and_verifier_failure_require_proof_and_keep_evidence', 'ReleasedHasCleanupProof'),
        )
        for fault, method, before, after, test, invariant in controls:
            source = Path(rollout_gateway.__file__).read_text()
            if source.count(before) != 1:
                raise AssertionError('Rollout code/model boundary changed: ' + fault)
            case = root/fault; case.mkdir()
            path = case/'rollout_gateway.py'; path.write_text(source.replace(before,after,1))
            namespace = dict(rollout_gateway.__dict__)
            exec(compile(path.read_text(),str(path),'exec'),namespace)
            with patch.object(rollout_gateway.RolloutGateway,method,getattr(namespace['RolloutGateway'],method)):
                result = run(case/'test',test,test_class=rollout_regression.RolloutAPITests)
            passed = len(result.failures) == 1 and not result.errors and not result.skipped
            report['controls'].append(dict(fault=fault,invariant=invariant,regression=test,passed=passed))
            if not passed:
                raise AssertionError('Rollout code mutation did not produce its required failure: ' + fault)
        report['rollout_tests'] = canonical_rollout.testsRun
        canonical_fleet = run(root/'fleet-canonical',test_class=fleet_regression.NodeLifecycleTests)
        if not canonical_fleet.wasSuccessful() or canonical_fleet.skipped:
            raise AssertionError('Canonical fleet regressions must pass without skips')
        fleet_controls = (
            ('node_stale_admission', 'admit',
             "if record['state'] not in ('ACTIVE', 'UNMANAGED') or (required and record['state'] != 'ACTIVE'):",
             'if False:', 'test_drain_wins_against_a_controller_with_stale_active_snapshot', 'AdmissionRequiresActive'),
            ('node_missing_held', '_transition',
             "if action in ('maintain', 'retire', 'withdraw', 'upgrade', 'commit', 'rollback') and current['held'] != 0:",
             'if False:', 'test_unknown_dispatch_and_unreachable_node_do_not_authorize_retirement', 'QuiescentRequiresNoWork'),
            ('node_stale_transition', '_transition',
             "if current['version'] != version:", 'if False:',
             'test_empty_proof_and_version_are_both_required', 'RetiredIsFinal'),
        )
        for fault,method,before,after,test,invariant in fleet_controls:
            source=Path(node_lifecycle.__file__).read_text()
            if source.count(before)!=1:raise AssertionError('Fleet code/model boundary changed: '+fault)
            mutated=source.replace(before,after,1)
            if fault=='node_stale_transition':
                gate='if state not in allowed[action]:'
                if mutated.count(gate)!=1:raise AssertionError('Fleet transition state gate changed')
                mutated=mutated.replace(gate,'if False:',1)
            case=root/fault;case.mkdir();path=case/'node_lifecycle.py';path.write_text(mutated)
            namespace=dict(node_lifecycle.__dict__);exec(compile(mutated,str(path),'exec'),namespace)
            target=node_lifecycle if method=='admit' else node_lifecycle.NodeLifecycle
            replacement=namespace['admit'] if method=='admit' else namespace['NodeLifecycle']._transition
            with patch.object(target,method,replacement):
                result=run(case/'test',test,test_class=fleet_regression.NodeLifecycleTests)
            passed=len(result.failures)==1 and not result.errors and not result.skipped
            report['controls'].append(dict(fault=fault,invariant=invariant,regression=test,passed=passed))
            if not passed:raise AssertionError('Fleet guard removal did not produce the required regression failure: '+fault)
        report['fleet_tests']=canonical_fleet.testsRun
        upgraded=run(root/'upgrade-canonical',test_class=upgrade_regression.NodeUpgradeTests)
        if not upgraded.wasSuccessful() or upgraded.skipped:raise AssertionError('Upgrade regressions must pass')
        upgrade_controls=(
            ('upgrade_unproved',"if observed != expected:",'if False:',
             'test_rollback_requires_old_runtime_and_delayed_commit_cannot_undo_it','CompletionHasLoadedProof'),
            ('upgrade_pending_resume',"if pending and action not in ('commit','rollback'):",'if False:',
             'test_pending_upgrade_fences_resume_retirement_and_competing_plans','PendingBlocksAdmission'),
        )
        for fault,before,after,test,invariant in upgrade_controls:
            source=Path(node_lifecycle.__file__).read_text()
            if source.count(before)!=1:raise AssertionError('Upgrade correspondence changed: '+fault)
            mutated=source.replace(before,after,1);case=root/fault;case.mkdir()
            namespace=dict(node_lifecycle.__dict__);exec(compile(mutated,str(case/'node_lifecycle.py'),'exec'),namespace)
            with patch.object(node_lifecycle.NodeLifecycle,'_transition',namespace['NodeLifecycle']._transition):
                result=run(case/'test',test,test_class=upgrade_regression.NodeUpgradeTests)
            passed=len(result.failures)==1 and not result.errors and not result.skipped
            report['controls'].append(dict(fault=fault,invariant=invariant,regression=test,passed=passed))
            if not passed:raise AssertionError('Upgrade mutation did not fail its regression: '+fault)
        report['upgrade_tests']=upgraded.testsRun
        report['status'] = 'passed'
    finally:
        sources = ['src/dsec/control/iam.py','src/dsec/control/node_gateway.py','src/dsec/host/cluster_node.py',
            'src/dsec/control/node_lifecycle.py','src/dsec/control/node_admin.py',
            'src/dsec/host/node_directory.py','src/dsec/host/node_polling.py',
            'src/dsec/host/cluster.py','src/dsec/contracts/cluster.py','src/dsec/rollout/episodes.py',
            'tests/unit/test_node_lifecycle.py','verification/NodeLifecycle.tla','verification/NodeLifecycle.cfg',
            'tests/unit/test_node_upgrade.py','verification/NodeUpgrade.tla','verification/NodeUpgrade.cfg',
            'src/dsec/contracts/release.py','src/dsec/_release.py','src/dsec/host/releases.py','src/dsec/host/release_artifacts.py',
            'src/dsec/host/node_transport.py','src/dsec/contracts/rpc.py',
            'src/dsec/control/cluster_api.py','src/dsec/control/watcher.py',
            'tests/unit/test_cluster_control.py','verification/ClusterAdmission.tla',
            'verification/ClusterAdmission.cfg', 'tools/check_cluster_refinement.py',
            'src/dsec/control/rollout_api.py','src/dsec/control/rollout_gateway.py','src/dsec/host/rollout_gateway.py',
            'src/dsec/contracts/rollout.py','src/dsec/sdk/rollout_api.py',
            'tests/unit/test_rollout_api.py','verification/RolloutDispatch.tla','verification/RolloutDispatch.cfg']
        report['source_sha256'] = {name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in sources}
        (root/'report.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(dict(status=report['status'],tests=report['tests'],controls=len(report['controls']))))


if __name__ == '__main__':
    main()
