"""A passed validation can be reused without rerunning or cherry-picking episodes."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

AVAILABLE=importlib.util.find_spec('dsec_openhands') is not None
if AVAILABLE:
    from dsec_swegym_case.train_sao import Pilot,validated_before


@unittest.skipUnless(AVAILABLE,'Optional OpenHands integration required')
class EvaluationTests(unittest.TestCase):
    def fixture(self,root):
        adapter=root/'critic';adapter.mkdir();(adapter/'adapter_model.safetensors').write_bytes(b'fixed checkpoint')
        digest=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
        rows=[]
        for i,reward in enumerate((1,0,1,0)):
            p=root/str(i);case=p/'case';(case/'verification').mkdir(parents=True)
            verdict=case/'report.json';official=case/'verification/report.json'
            verdict.write_text(json.dumps(dict(status='scored',cleanup='stopped',score=reward,termination='finished')))
            official.write_bytes(verdict.read_bytes())
            rows.append(dict(path=str(p),reward=reward,case_report_sha256=digest(verdict),official_sha256=digest(official)))
        manifest=root/'manifest.json';manifest.write_text(json.dumps(dict(validation=[r['path'] for r in rows])))
        report=root/'validation.json';report.write_text(json.dumps(dict(status='passed',gate_passed=True,
            critic_unchanged=True,optimizer_steps=0,actor_updates=0,adapter_file_sha256=digest(adapter/'adapter_model.safetensors'),
            manifest=str(manifest),manifest_sha256=digest(manifest),episodes=rows,protocol_signature={})))
        return adapter,report

    def test_reuse_keeps_all_scores_and_rejects_changed_model_or_evidence(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);adapter,report=self.fixture(root)
            rows,_=validated_before(report,adapter,4)
            self.assertEqual([r['score'] for r,_ in rows],[1,0,1,0])
            with self.assertRaises(ValueError):validated_before(report,adapter,3)
            model=adapter/'adapter_model.safetensors';original=model.read_bytes();model.write_bytes(b'other model')
            with self.assertRaises(ValueError):validated_before(report,adapter,4)
            model.write_bytes(original);(root/'0/case/report.json').write_text('{}')
            with self.assertRaises(ValueError):validated_before(report,adapter,4)

    def test_rejected_gate_cannot_be_reused(self):
        with tempfile.TemporaryDirectory() as d:
            adapter,report=self.fixture(Path(d));r=json.loads(report.read_text());r['gate_passed']=False
            report.write_text(json.dumps(r))
            with self.assertRaisesRegex(ValueError,'Passed read-only'):validated_before(report,adapter,4)

    def test_published_repository_policy_and_observed_identity_can_differ_in_metadata(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);adapter,report=self.fixture(root)
            repository=dict(base_commit='base',tree='tree',objects=10,commits=2,
                object_set_sha256='objects',commit_set_sha256='commits',history='baseline_and_all_ancestors')
            r=json.loads(report.read_text());r['protocol_signature']=dict(instance_id='task',
                repository_baseline=dict(repository,clean_worktree=True),max_iterations=100,episode_seconds=1200,
                token_budgets=dict(context=65536,response=32768),unparsed_tool_recovery='minicpm5',recovery_limit=3)
            report.write_text(json.dumps(r))
            config=root/'case.json';config.write_text(json.dumps(dict(openhands=dict(unparsed_tool_recovery='minicpm5'),
                repository_baseline=dict(repository,schema='v2',future_fix_objects_absent=True))))
            task=root/'task.json';task.write_text('{"instance_id":"task"}')
            args=SimpleNamespace(out=root/'run',case_config=config,trainer_python=root/'bin/python',
                model=root/'model',steps=2,critic_adapter=adapter,before_validation=report,eval_episodes=4,
                task=task,max_iterations=100)
            pilot=Pilot(args)
            with patch.object(pilot,'start',side_effect=RuntimeError('stop before spawn')):
                with self.assertRaisesRegex(RuntimeError,'stop before spawn'):pilot.run()

    def test_four_episode_evaluation_and_reuse_do_not_drop_zero_rewards(self):
        pilot=Pilot.__new__(Pilot);pilot.args=SimpleNamespace(eval_episodes=4)
        child=SimpleNamespace(poll=lambda:0)
        pilot.episode=Mock(side_effect=[(child,Path(str(i))) for i in range(4)])
        pilot.wait=Mock();results=[dict(score=r,termination='finished') for r in (0,1,0,1)]
        pilot.scored=Mock(side_effect=results)
        value=pilot.evaluate('after')
        self.assertEqual(value['score'],.5)
        self.assertEqual([r['score'] for r in value['episodes']],[0,1,0,1])
        self.assertEqual([c.args[0] for c in pilot.episode.call_args_list],['after-1','after-2','after-3','after-4'])
        pilot.episode.reset_mock()
        value=pilot.evaluate('before',[(r,Path(str(i))) for i,r in enumerate(results)])
        pilot.episode.assert_not_called();self.assertEqual(value['count'],4)
