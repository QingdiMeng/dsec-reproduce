"""Project-record invariants: prevent lost evidence and accidental reopened work."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import unittest

from tools.check_project import affected, load, render, validate, VIEW

ROOT = Path(__file__).resolve().parents[2]


class ProjectManagementTests(unittest.TestCase):
    def setUp(self):
        self.value = load(ROOT)

    def cap(self, name='CAP-RECOVERY'):
        return next(c for c in self.value['capabilities'] if c['id'] == name)

    def task(self, name='TASK-002'):
        return next(t for t in self.value['work'] if t['id'] == name)

    def assert_error(self, message):
        self.assertTrue(any(message in x for x in validate(ROOT, self.value)),
                        validate(ROOT, self.value))

    def test_canonical_register_and_view_are_consistent(self):
        self.assertEqual(validate(ROOT, self.value), [])
        self.assertEqual((ROOT/VIEW).read_text(), render(self.value))

    def test_scoped_acceptance_requires_evidence_and_limits(self):
        self.cap()['evidence'] = []
        self.cap()['limits'] = ''
        self.assert_error('accepted capability needs scoped evidence')
        self.assert_error('limits required')

    def test_unknown_or_ambiguous_ids_and_broken_references_are_rejected(self):
        self.value['capabilities'].append(deepcopy(self.cap()))
        self.cap()['evidence'].append('EV-NOT-FOUND')
        self.cap()['sources'].append(dict(path='src/missing.py'))
        self.cap()['sources'].append(dict(path='../outside'))
        self.assert_error('duplicate CAP-RECOVERY')
        self.assert_error('unknown evidence')
        self.assert_error('missing/unsafe reference')

    def test_reference_section_and_run_date_are_reviewable(self):
        evidence = self.value['evidence'][0]
        evidence['records'][0]['section'] = 'Missing section'
        evidence['date_kind'] = 'pretend_today_was_a_rerun'
        self.assert_error('missing section')
        self.assert_error('distinguish run date from index review')

    def test_done_needs_evidence_not_just_a_passing_status_label(self):
        task = self.task()
        task.update(state='done', owner='maintainer', evidence=[])
        self.assert_error('done needs evidence')

    def test_existing_capability_requires_prior_review_before_execution(self):
        task = self.task()
        task.update(state='ready', owner='maintainer', prior_evidence=[])
        self.assert_error('review prior evidence')

    def test_feature_on_accepted_capability_needs_a_changed_boundary(self):
        task = self.task()
        task.update(kind='feature', capabilities=['CAP-RECOVERY'])
        task.pop('changed_boundary', None)
        self.assert_error('feature on accepted capability needs a changed boundary')

    def test_reopen_retains_original_completion_and_needs_rationale(self):
        task = self.task()
        task.update(reopens=['TASK-005'])
        self.assert_error('reopen needs changed boundary')
        task['changed_boundary'] = 'A new guest protocol requires a different environment claim'
        self.assertEqual(validate(ROOT, self.value), [])
        self.assertEqual(self.task('TASK-005')['state'], 'done')
        task['reopens'] = ['TASK-004']
        self.assert_error('invalid reopened task')

    def test_wip_dependency_and_owner_guards(self):
        # TASK-001 may already be closed when this regression runs later.
        a, b = self.task('TASK-001'), self.task()
        a.update(state='in_progress', owner='maintainer')
        b.update(state='in_progress', owner='maintainer')
        self.assert_error('WIP limit exceeded')
        b['state'] = 'ready'; b['owner'] = 'unassigned'
        self.assert_error('active/ready task needs owner')
        b.update(state='in_progress', owner='maintainer', depends_on=['TASK-004'])
        a['state'] = 'candidate'
        self.assert_error('unfinished dependency')

    def test_dependency_cycles_and_unknown_evidence_cannot_close_work(self):
        a, b = self.task(), self.task('TASK-003')
        a['depends_on'] = [b['id']]; b['depends_on'] = [a['id']]
        a['evidence'] = ['EV-MISSING']
        self.assert_error('dependency cycle')
        self.assert_error('unknown evidence')

    def test_blocked_work_requires_a_concrete_condition(self):
        self.task().update(state='blocked', owner='maintainer')
        self.assert_error('blocker required')

    def test_malformed_state_and_dependency_fields_fail_with_diagnostics(self):
        self.task()['depends_on'] = None
        self.cap()['state'] = []
        self.assert_error('must be ID arrays')
        self.assert_error('invalid capability state')

    def test_change_impact_is_advisory_and_preserves_historical_completion(self):
        before = deepcopy(self.value)
        rows = affected(ROOT, self.value, ['src/dsec/runtime/requests.py'])
        self.assertIn('CAP-RECOVERY', [r['capability'] for r in rows])
        self.assertTrue(all(r['tests'] for r in rows))
        self.assertEqual(self.value, before)
        self.assertEqual(affected(ROOT, self.value, ['README.md']), [])

    def test_check_cli_is_read_only_and_suggests_only_impacted_owners(self):
        before = (ROOT/'project/registry.json').read_bytes()
        completed = subprocess.run([sys.executable, str(ROOT/'tools/check_project.py'),
            '--changed', 'src/dsec/sdk/native.py'], text=True, capture_output=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertIn('CAP-NATIVE', [r['capability'] for r in result['impact']])
        self.assertEqual((ROOT/'project/registry.json').read_bytes(), before)


if __name__ == '__main__': unittest.main()
