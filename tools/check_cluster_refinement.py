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
sys.path[:0] = [str(ROOT/'src'), str(ROOT)]
from dsec.control import iam
from dsec.host import cluster_node
from tests.unit import test_cluster_control as regression
from tests.unit import test_rollout_api as rollout_regression
from dsec.host import rollout_gateway

CONTROLS = (
    ('quota_guard', iam, 'IAM', 'admit',
     "if not usage.plus(Quota(**record['delegated'])).fits(Quota(**record['quota'])):",
     'if False:', 'test_concurrent_admissions_never_exceed_quota_and_keep_one_target', 'QuotaBounded'),
    ('unknown_replacement', iam, 'IAM', 'admit',
     'return old, {}\n            usage =',
     "return dict(old, node=node), {key:dict(old, node=node)}\n            usage =",
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
        report['status'] = 'passed'
    finally:
        sources = ['src/dsec/control/iam.py','src/dsec/host/cluster_node.py',
            'src/dsec/control/cluster_api.py','src/dsec/control/watcher.py',
            'tests/unit/test_cluster_control.py','verification/ClusterAdmission.tla',
            'verification/ClusterAdmission.cfg', 'tools/check_cluster_refinement.py',
            'src/dsec/control/rollout_api.py','src/dsec/host/rollout_gateway.py',
            'src/dsec/contracts/rollout.py','src/dsec/sdk/rollout_api.py',
            'tests/unit/test_rollout_api.py','verification/RolloutDispatch.tla','verification/RolloutDispatch.cfg']
        report['source_sha256'] = {name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in sources}
        (root/'report.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(dict(status=report['status'],tests=report['tests'],controls=len(report['controls']))))


if __name__ == '__main__':
    main()
