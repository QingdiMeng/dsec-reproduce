"""Exercise real PEFT restoration and native verl optimizer groups on CPU."""
import importlib.util
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

AVAILABLE = all(importlib.util.find_spec(name) for name in ('verl','peft','torch'))


@unittest.skipUnless(AVAILABLE,'Optional pinned native verl environment required')
class CriticEngineTests(unittest.TestCase):
    def test_saved_signals_are_the_exact_tensors_used_by_optimizer_objectives(self):
        import contextlib
        import json
        import torch
        from unittest.mock import patch
        import dsec_swegym_case.sao_verl as application
        optim=SimpleNamespace(state_dict=lambda:{})
        actor=SimpleNamespace(eval_mode=contextlib.nullcontext,optimizer=optim,lr_scheduler=optim)
        critic=SimpleNamespace(optimizer=optim,lr_scheduler=optim)
        actions=[dict(response_id=str(i),response_ids=[3],policy_version=0) for i in range(2)]
        used=[]
        def update(engine,segments,**kw):
            used.append({k:v.clone() if torch.is_tensor(v) else v for k,v in kw.items()})
            return dict(loss=0.,grad_norm=1.)
        with tempfile.TemporaryDirectory() as directory:
            out=Path(directory)
            with patch.object(application,'values_for',side_effect=[torch.tensor([-.1,.2]),torch.tensor([.3,.4])]), \
                 patch.object(application,'update',side_effect=update), \
                 patch.object(application,'adapter_state',return_value={}), \
                 patch.object(application,'save_adapter',return_value='new'):
                application.train_episode(actor,critic,dict(actions=actions,reward=1),out)
            saved=json.loads((out/'learning-signals.json').read_text())
            self.assertEqual(saved['response_ids'],['0','1'])
            self.assertEqual(saved['critic_targets'],used[0]['targets'].tolist())
            self.assertEqual(saved['critic_targets'],used[1]['targets'].tolist())
            self.assertEqual(saved['advantages'],used[2]['advantages'].tolist())
            self.assertEqual(saved['values_before'],torch.tensor([-.1,.2]).tolist())
            self.assertEqual(saved['values_after'],torch.tensor([.3,.4]).tolist())

    def test_checkpoint_restores_head_and_adapters_and_keeps_attention_frozen(self):
        import torch
        from peft import get_peft_model_state_dict
        from transformers import Qwen2Config,Qwen2ForTokenClassification
        from dsec_swegym_case.sao_verl import FrozenAttentionValueEngine
        from verl.workers.config import FSDPOptimizerConfig

        def engine(adapter=None):
            result=FrozenAttentionValueEngine.__new__(FrozenAttentionValueEngine)
            result.model_config=SimpleNamespace(lora_adapter_path=adapter,lora_rank=2,
                lora_alpha=4,target_modules=['gate_proj','up_proj','down_proj'])
            result.optimizer_config=FSDPOptimizerConfig(lr=5e-6,total_training_steps=10,
                weight_decay=0.,lr_scheduler_type='constant')
            result.rank=0
            result.value_head_lr=1e-4
            return result

        def model():
            return Qwen2ForTokenClassification(Qwen2Config(vocab_size=32,hidden_size=16,
                intermediate_size=32,num_hidden_layers=1,num_attention_heads=2,
                num_key_value_heads=1,num_labels=1)).to(dtype=torch.bfloat16)

        torch.manual_seed(7)
        first=engine()._build_lora_module(model())
        for tokens in ([[1,2,3]],[[7,8,9]]):
            with torch.no_grad():value=first(input_ids=torch.tensor(tokens)).logits
            torch.testing.assert_close(value,torch.full_like(value,.5),rtol=0,atol=0)
        # Zero projection is not a dead critic: the head receives a gradient
        # immediately; MLP gradients start after the projection becomes nonzero.
        value=first(input_ids=torch.tensor([[1,2,3]])).logits
        ((value.float().flatten()-torch.tensor([0.,1.,0.]))**2).mean().backward()
        self.assertTrue(any(p.grad is not None and p.grad.abs().max()>0
            for n,p in first.named_parameters() if 'score' in n))
        self.assertTrue(all(p.grad is None or torch.count_nonzero(p.grad)==0
            for n,p in first.named_parameters() if 'lora_' in n))
        first.zero_grad(set_to_none=True)
        with torch.no_grad():
            for name,p in first.named_parameters():
                if p.requires_grad:p.add_(.125)
        expected=get_peft_model_state_dict(first)
        self.assertTrue(any('score' in name for name in expected))
        with tempfile.TemporaryDirectory() as directory:
            first.save_pretrained(directory)
            restored_engine=engine(directory)
            restored=restored_engine._build_lora_module(model())
            actual=get_peft_model_state_dict(restored)
            self.assertEqual(set(expected),set(actual))
            for name,value in expected.items():
                torch.testing.assert_close(actual[name],value,rtol=0,atol=0)
            self.assertFalse(any(p.requires_grad and 'self_attn' in n for n,p in restored.named_parameters()))
            optimizer=restored_engine._build_optimizer(restored)
            scheduler=restored_engine._build_lr_scheduler(optimizer)
            self.assertEqual([(g['name'],g['lr']) for g in optimizer.param_groups],
                [('mlp_lora',5e-6),('value_head',1e-4)])
            optimizer.zero_grad()
            output=restored(input_ids=torch.tensor([[1,2,3]])).logits
            output.float().square().mean().backward();optimizer.step();scheduler.step()
            self.assertEqual([g['lr'] for g in optimizer.param_groups],[5e-6,1e-4])
            trainable={id(p) for p in restored.parameters() if p.requires_grad}
            grouped=[id(p) for g in optimizer.param_groups for p in g['params']]
            self.assertEqual(len(grouped),len(set(grouped)))
            self.assertEqual(set(grouped),trainable)
            # A true saved critic is preserved. Reset is a distinct, opt-in
            # experiment and must not modify the restored MLP adapters.
            guard=engine(directory);guard.value_head_prior=.75
            with self.assertRaisesRegex(ValueError,'explicit reset'):
                guard._build_lora_module(model())
            reset=engine(directory);reset.value_head_prior=.75;reset.reset_value_head=True
            initialized=reset._build_lora_module(model())
            state=get_peft_model_state_dict(initialized)
            for name,value in expected.items():
                if 'score' not in name.split('.'):
                    torch.testing.assert_close(state[name],value,rtol=0,atol=0)
            with torch.no_grad():value=initialized(input_ids=torch.tensor([[1,2,3]])).logits
            torch.testing.assert_close(value,torch.full_like(value,.75),rtol=0,atol=0)
            self.assertEqual(reset.effective_value_prior,.75)

    def test_invalid_prior_or_ambiguous_head_is_rejected(self):
        import torch
        from dsec_swegym_case.sao_verl import seed_value_head
        module=torch.nn.Module();module.score=torch.nn.Linear(16,1)
        for prior in (float('nan'),float('inf'),-.1,1.1):
            with self.assertRaises(ValueError):seed_value_head(module,prior)
        module.classifier=torch.nn.Linear(16,1)
        with self.assertRaisesRegex(ValueError,'Ambiguous'):seed_value_head(module,.5)


if __name__=='__main__':unittest.main()
