"""Value calibration and native-token corpus boundaries, without a GPU."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

AVAILABLE = all(importlib.util.find_spec(name) for name in ('torch','dsec_openhands'))
if AVAILABLE:
    import torch
    from dsec_openhands.training import SampledGeneration
    from dsec_swegym_case.critic_warmup import read_episode, value_metrics


@unittest.skipUnless(AVAILABLE,'Optional PyTorch/OpenHands training boundary required')
class CriticWarmupTests(unittest.TestCase):
    def test_constant_returns_have_no_explained_variance(self):
        result=value_metrics(torch.tensor([-1.,1.,3.]),torch.ones(3))
        self.assertAlmostEqual(result['mse'],8/3,places=6)
        self.assertAlmostEqual(result['bias'],0.)
        self.assertIsNone(result['explained_variance'])
        self.assertAlmostEqual(result['outside_unit_interval'],2/3,places=6)

    def test_mixed_returns_and_invalid_values(self):
        result=value_metrics(torch.tensor([0.,1.]),torch.tensor([0.,1.]))
        self.assertEqual(result['explained_variance'],1.)
        for values,returns in ((torch.tensor([float('nan')]),torch.zeros(1)),
                               (torch.zeros(2),torch.zeros(1)),
                               (torch.zeros(0),torch.zeros(0))):
            with self.assertRaises(ValueError):value_metrics(values,returns)

    def write_episode(self,root):
        case=root/'case';(case/'verification').mkdir(parents=True)
        (case/'completions').mkdir();(root/'generations').mkdir()
        report=dict(status='scored',cleanup='stopped',score=0,termination='iteration_limit',
            max_iterations=100,unparsed_tool_recovery='minicpm5')
        for p in (case/'report.json',case/'verification/report.json'):
            p.write_text(json.dumps(report))
        for index,(identity,purpose) in enumerate((('action','agent'),('summary','summary'))):
            response=dict(id=identity,choices=[dict(response_token_ids=[2,3])])
            native=dict(obj=dict(input_ids=[1],lora_path=None),out=dict(output_ids=[2,3],
                meta_info=dict(id=identity,completion_tokens=2,output_token_logprobs=[[-1.,2],[-2.,3]])))
            sample=SampledGeneration.from_sglang(native,response,policy_version=0,purpose=purpose)
            (root/'generations'/f'{index}.json').write_text(json.dumps(dict(
                sample=sample.as_dict(),native=native,response=response)))
            (case/'completions'/f'{index}.json').write_text(json.dumps(dict(timestamp=index,response=response)))
        return case

    def test_real_zero_retains_exact_actions_but_excludes_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);self.write_episode(root)
            episode=read_episode(root)
            self.assertEqual(episode['reward'],0.)
            self.assertEqual([a['response_id'] for a in episode['actions']],['action'])
            self.assertEqual(episode['actions'][0]['response_ids'],[2,3])

    def test_capture_corruption_or_unscored_verdict_cannot_be_a_failure_label(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);case=self.write_episode(root)
            record=root/'generations/0.json';original=record.read_text()
            row=json.loads(original);row['native']['out']['output_ids']=[9,3]
            record.write_text(json.dumps(row))
            with self.assertRaises(ValueError):read_episode(root)
            record.write_text(original)
            verdict=case/'verification/report.json';row=json.loads(verdict.read_text())
            row.update(status='failed',score=None);verdict.write_text(json.dumps(row))
            with self.assertRaises(ValueError):read_episode(root)


if __name__=='__main__':unittest.main()
