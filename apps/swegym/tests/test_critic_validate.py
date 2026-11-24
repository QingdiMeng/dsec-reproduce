"""Independent validation must not hide a missing class or overweight long episodes."""
import importlib.util
import unittest

AVAILABLE=all(importlib.util.find_spec(x) for x in ('torch','dsec_openhands'))
if AVAILABLE:
    from dsec_swegym_case.critic_validate import validation_summary


@unittest.skipUnless(AVAILABLE,'Optional trainer dependencies required')
class CriticValidationTests(unittest.TestCase):
    def row(self,reward,mse,baseline,tokens):
        return dict(reward=reward,candidate=dict(mse=mse,tokens=tokens),constant_prior=dict(mse=baseline))

    def test_long_failure_cannot_hide_bad_short_successes(self):
        result=validation_summary([self.row(0.,0.,.5625,100000),self.row(1.,1.,.0625,1)])
        self.assertEqual(result['candidate_mse'],.5)
        self.assertFalse(result['gate_passed'])
        self.assertEqual(result['gate_reasons'],['candidate_worse_than_constant_prior'])

    def test_single_class_is_insufficient_even_with_perfect_prediction(self):
        result=validation_summary([self.row(1.,0.,.0625,10)]*4)
        self.assertFalse(result['gate_passed'])
        self.assertIn('missing_positive_or_negative_episode',result['gate_reasons'])

    def test_mixed_not_worse_gate_and_nonfinite_rejection(self):
        self.assertTrue(validation_summary([self.row(0.,.1,.5625,3),self.row(1.,.1,.0625,3)])['gate_passed'])
        with self.assertRaises(ValueError):validation_summary([])
        with self.assertRaises(ValueError):validation_summary([self.row(0.,float('nan'),.5625,3)])
