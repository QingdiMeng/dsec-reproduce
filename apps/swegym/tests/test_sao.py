import math
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
try:
    import torch
except ImportError:
    torch = None
if torch is not None:
    from dsec_swegym_case.sao import direct_is_loss, skip_observation_gae


class PilotEvidenceTests(unittest.TestCase):
    @unittest.skipUnless(importlib.util.find_spec('dsec_openhands'), 'Optional OpenHands integration not installed')
    def test_critic_initialization_is_forwarded_only_when_requested(self):
        from dsec_swegym_case.train_sao import Pilot
        from types import SimpleNamespace
        import json
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);case=root/'case.json';case.write_text(json.dumps(dict(openhands={})))
            for name,adapter,prior in (('fresh',None,None),('warm',root/'critic',None),('prior',None,.75)):
                args=SimpleNamespace(out=root/name,case_config=case,model=root/'model',
                    trainer_python=root/'trainer/bin/python',steps=2)
                if adapter is not None:args.critic_adapter=adapter
                if prior is not None:args.critic_value_prior=prior
                pilot=Pilot(args)
                with patch.object(pilot,'start',side_effect=RuntimeError('stop before spawn')) as launch:
                    with self.assertRaisesRegex(RuntimeError,'stop before spawn'):pilot.run()
                    argv=launch.call_args.args[1]
                    self.assertEqual('--critic-adapter' in argv,adapter is not None)
                    if adapter is not None:self.assertEqual(argv[-2:],['--critic-adapter',str(adapter)])
                    if prior is not None:self.assertEqual(argv[-2:],['--critic-value-prior',str(prior)])

    @unittest.skipUnless(importlib.util.find_spec('dsec_openhands'), 'Optional OpenHands integration not installed')
    def test_checkpoint_and_cold_prior_are_rejected_before_creating_output(self):
        from dsec_swegym_case.train_sao import main
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as directory:
            out=Path(directory)/'new-run';argv=['train-sao','--out',str(out)]
            for option in ('task','case-config','model','sdk-python','trainer-python','critic-adapter'):
                argv.extend(['--'+option,directory])
            argv.extend(['--critic-value-prior','.75'])
            with patch('sys.argv',argv),contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:main()
            self.assertEqual(error.exception.code,2)
            self.assertFalse(out.exists())

    @unittest.skipUnless(importlib.util.find_spec('dsec_openhands'), 'Optional OpenHands integration not installed')
    def test_allocator_fix_is_confined_to_learner_and_leaves_inference_environment_unchanged(self):
        from dsec_swegym_case.train_sao import Pilot
        from types import SimpleNamespace
        import json
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);case=root/'case.json'
            case.write_text(json.dumps(dict(openhands={})))
            pilot=Pilot(SimpleNamespace(out=root,case_config=case,trainer_python=root/'bin/python',steps=2))
            pilot.env.update(PYTORCH_ALLOC_CONF='max_split_size_mb:128',PYTORCH_CUDA_ALLOC_CONF='max_split_size_mb:64')
            with patch('dsec_swegym_case.train_sao.subprocess.Popen') as popen:
                popen.return_value.pid=123
                try:
                    pilot.start('trainer',['python','trainer'])
                    self.assertEqual(popen.call_args.kwargs['env']['PYTORCH_ALLOC_CONF'],'expandable_segments:True')
                    self.assertNotIn('PYTORCH_CUDA_ALLOC_CONF',popen.call_args.kwargs['env'])
                    pilot.start('model',['python','server'])
                    self.assertEqual(popen.call_args.kwargs['env']['PYTORCH_ALLOC_CONF'],'max_split_size_mb:128')
                    self.assertEqual(pilot.env['PYTORCH_CUDA_ALLOC_CONF'],'max_split_size_mb:64')
                finally:
                    for log in pilot.logs.values():log.close()

    @unittest.skipUnless(importlib.util.find_spec('dsec_openhands'), 'Optional OpenHands integration not installed')
    def test_evaluation_preserves_case_deadlines_and_training_records_its_wait_budget(self):
        from dsec_swegym_case.train_sao import Pilot
        from types import SimpleNamespace
        import json
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            case=root/'case.json'
            case.write_text(json.dumps(dict(openhands=dict(episode_seconds=1200,
                model_timeout_seconds=600,response_length=32768,context_length=65536,
                unparsed_tool_recovery='minicpm5',recovery_limit=3))))
            (root/'policy.json').write_text('{"version":0}')
            args=SimpleNamespace(out=root,case_config=case,model=root/'model',
                trainer_python=root/'trainer/bin/python',sdk_python=root/'sdk/bin/python',
                task=root/'task.json',max_iterations=100,steps=2)
            pilot=Pilot(args)
            with patch.object(pilot,'start'):
                for name in ('before','train-1','after'):
                    pilot.episode(name)
                    options=json.loads((root/(name+'-case.json')).read_text())['openhands']
                    self.assertEqual(options['max_iterations'],100)
                    self.assertEqual(options['response_length'],32768)
                    self.assertEqual(options['unparsed_tool_recovery'],'minicpm5')
                    self.assertEqual(options['recovery_limit'],3)
                    self.assertEqual(options['episode_seconds'],7200 if name.startswith('train-') else 1200)
                    self.assertEqual(options['model_timeout_seconds'],7200 if name.startswith('train-') else 600)

    @unittest.skipUnless(importlib.util.find_spec('dsec_openhands'), 'Optional OpenHands integration not installed')
    def test_existing_evidence_is_rejected_before_launch_and_remains_unchanged(self):
        from dsec_swegym_case.train_sao import main
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as directory:
            report=Path(directory)/'report.json'
            report.write_text('{"original":true}')
            argv=['train-sao']
            for option in ('task','case-config','model','out','sdk-python','trainer-python'):
                argv.extend(['--'+option,directory])
            with patch('sys.argv',argv),contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    main()
            self.assertEqual(error.exception.code,2)
            self.assertEqual(report.read_text(),'{"original":true}')


@unittest.skipIf(torch is None, 'Optional external trainer requires PyTorch')
class SAOTests(unittest.TestCase):
    def test_observation_values_do_not_change_action_targets(self):
        mask = torch.tensor([1, 0, 0, 1, 0, 1], dtype=torch.bool)
        values = torch.tensor([.2, 10000., -9999., .4, 5555., .6])
        rewards = torch.tensor([0., 9999., 8888., 0., 7777., 1.])
        actual, returns = skip_observation_gae(values, rewards, mask, lam=.9)
        expected, targets = skip_observation_gae(values[mask], rewards[mask], torch.ones(3), lam=.9)
        torch.testing.assert_close(actual[mask], expected)
        torch.testing.assert_close(returns[mask], targets)
        self.assertTrue(torch.equal(actual[~mask], torch.zeros(3)))

    def test_strict_dis_boundaries_and_detached_ratio_gradient(self):
        # Exact log-space endpoints must be rejected, not inclusively clipped.
        x = torch.tensor([math.log(.2), 0., math.log(4.), 1000.], requires_grad=True)
        behavior = torch.zeros(4)
        loss, metrics = direct_is_loss(x, behavior, torch.ones(4), denominator=4)
        loss.backward()
        torch.testing.assert_close(x.grad, torch.tensor([0., -.25, 0., 0.]))
        self.assertEqual(metrics['kept'], 1)
        self.assertTrue(torch.isfinite(loss))

    def test_terminal_reward_propagates_only_over_actions(self):
        advantages, targets = skip_observation_gae(torch.zeros(4), torch.tensor([0.,0.,0.,1.]),
                                                   torch.tensor([1,0,1,1]), lam=1.)
        torch.testing.assert_close(advantages, torch.tensor([1.,0.,1.,1.]))
        torch.testing.assert_close(targets, advantages)


if __name__ == '__main__':
    unittest.main()
