"""Small-resource SAO actor/critic driver using the pinned native verl engine.

Model execution, activation checkpointing, LoRA, FSDP, optimizer and offload
remain verl responsibilities. This optional recipe supplies the SAO schedule
and segmented exact-context objective, not another sandbox or agent loop.
"""
import argparse
import gc
import hashlib
import json
import math
import os
from pathlib import Path
import time
import urllib.request

import torch
from peft import LoraConfig, TaskType, PeftModel, get_peft_model, get_peft_model_state_dict
from safetensors.torch import save_file
from tensordict import TensorDict

from verl.trainer.config import CheckpointConfig
from verl.utils import tensordict_utils as tu
from verl.workers.config import FSDPEngineConfig, FSDPOptimizerConfig, HFModelConfig
from verl.workers.engine.fsdp.transformer_impl import FSDPEngineWithLMHead, FSDPEngineWithValueHead

from .sao import direct_is_loss, policy_lambda, skip_observation_gae


def seed_value_head(module,prior):
    """Initialize an active scalar head, before FSDP/optimizer construction.

    Only explicit cold initialization calls this. A restored critic keeps its
    trained head unless a separate reset experiment was deliberately requested.
    """
    if not math.isfinite(prior) or not 0<=prior<=1:
        raise ValueError('A finite binary-reward prior in [0,1] is required')
    head={}
    for name,parameter in module.named_parameters():
        if parameter.requires_grad and any(p in name.split('.') for p in ('score','classifier','v_head')):
            kind=name.rsplit('.',1)[-1]
            if kind in head:raise ValueError('Ambiguous scalar value head')
            head[kind]=parameter
    if (set(head)!={'weight','bias'} or head['weight'].ndim!=2 or
            head['weight'].shape[0]!=1 or tuple(head['bias'].shape)!=(1,)):
        raise ValueError('One trainable scalar weight and bias are required')
    with torch.no_grad():
        head['weight'].zero_();head['bias'].fill_(prior)
    return float(head['bias'].detach()[0])


def install_single_rank_fused_boundary():
    """Triton needs a storage-backed Tensor, not DTensor's null data pointer.

    This compatibility boundary is intentionally limited to the single-rank,
    frozen LM head recipe. It neither changes native kernels nor claims a
    multi-rank implementation of vocab-parallel cross entropy.
    """
    from torch.distributed.tensor import DTensor
    import verl.utils.kernel.linear_cross_entropy as ce
    original=ce.linear_cross_entropy

    def local_head(hidden,weight,*args,**kwargs):
        if isinstance(weight,DTensor):
            if weight.device_mesh.size()!=1 or weight.requires_grad:
                raise ValueError('Local fused-head boundary requires one rank and a frozen LM head')
            weight=weight.to_local()
        return original(hidden,weight,*args,**kwargs)

    ce.linear_cross_entropy=local_head


class FrozenAttentionValueEngine(FSDPEngineWithValueHead):
    def _build_lora_module(self, module):
        # Native actor LoRA uses CAUSAL_LM; a token classifier needs TOKEN_CLS
        # and its scalar head saved/trained alongside the MLP adapters.
        module.enable_input_require_grads()
        adapter=getattr(self.model_config,'lora_adapter_path',None)
        if adapter:
            result=PeftModel.from_pretrained(module,adapter,is_trainable=True,autocast_adapter_dtype=False)
            if result.peft_config['default'].task_type!=TaskType.TOKEN_CLS:
                raise ValueError('A token-classification value adapter is required')
        else:
            result=get_peft_model(module,LoraConfig(task_type=TaskType.TOKEN_CLS,
                r=self.model_config.lora_rank,lora_alpha=self.model_config.lora_alpha,
                target_modules=list(self.model_config.target_modules),bias='none'))
        for name,parameter in result.named_parameters():
            if parameter.requires_grad:
                if 'self_attn' in name:
                    raise ValueError('Critic attention must remain frozen')
                parameter.data=parameter.data.to(torch.bfloat16)
        # Verify the complete restored adapter before any explicitly requested
        # re-initialization. This also includes TOKEN_CLS modules_to_save.
        if adapter:
            self.loaded_adapter_digest=digest({k:v.detach().cpu().contiguous()
                for k,v in get_peft_model_state_dict(result).items()})
        prior=getattr(self,'value_head_prior',None if adapter else .5)
        if prior is not None:
            if adapter and not getattr(self,'reset_value_head',False):
                raise ValueError('Resetting a saved value head requires the explicit reset option')
            self.effective_value_prior=seed_value_head(result,prior)
        return result

    def _build_optimizer(self,module):
        head_lr=getattr(self,'value_head_lr',None)
        if head_lr is None:
            return super()._build_optimizer(module)
        from verl.workers.config.optimizer import build_optimizer
        groups=dict(mlp_lora=[],value_head=[])
        for name,parameter in module.named_parameters():
            if not parameter.requires_grad:continue
            if 'lora_' in name:
                groups['mlp_lora'].append(parameter)
            elif any(part in name.split('.') for part in ('score','classifier','v_head')):
                groups['value_head'].append(parameter)
            else:
                raise ValueError('Unexpected trainable critic parameter: '+name)
        if not all(groups.values()):
            raise ValueError('Warmup requires both MLP adapters and a value head')
        return build_optimizer([
            dict(params=groups['mlp_lora'],lr=self.optimizer_config.lr,name='mlp_lora'),
            dict(params=groups['value_head'],lr=head_lr,name='value_head')],self.optimizer_config)


def make_engine(model, role, *, learning_rate, steps, adapter_path=None,value_head_lr=None,
                value_head_prior=None,reset_value_head=False,forward_only=False):
    config=HFModelConfig(path=str(model),model_type=role,load_tokenizer=False,
        lora_rank=8,lora_alpha=16,target_modules=['gate_proj','up_proj','down_proj'],
        enable_gradient_checkpointing=True,use_remove_padding=True,
        use_fused_kernels=(role=='language_model'),fused_kernel_options={'impl_backend':'triton'},
        lora_adapter_path=str(adapter_path) if adapter_path else None)
    engine_config=FSDPEngineConfig(strategy='fsdp2',model_dtype='bf16',
        forward_only=forward_only,
        param_offload=True,optimizer_offload=True,use_torch_compile=False,
        use_dynamic_bsz=False,micro_batch_size_per_gpu=1,
        infer_micro_batch_size_per_gpu=1,fsdp_size=1)
    optimizer=FSDPOptimizerConfig(lr=learning_rate,total_training_steps=steps,
        weight_decay=0.,clip_grad=1.,lr_scheduler_type='constant')
    cls=FSDPEngineWithLMHead if role=='language_model' else FrozenAttentionValueEngine
    engine=cls(config,engine_config,optimizer,CheckpointConfig())
    if role=='value_model':
        engine.value_head_lr=value_head_lr
        engine.value_head_prior=value_head_prior if value_head_prior is not None else (None if adapter_path else .5)
        engine.reset_value_head=reset_value_head
    engine.initialize()
    return engine


def segment_batch(segment, *, fused):
    prompt=segment['prompt_ids'];response=segment['response_ids']
    tokens=torch.tensor(prompt+response,dtype=torch.long)
    positions=torch.arange(len(tokens),dtype=torch.long)
    mask=torch.zeros(len(tokens),dtype=torch.bool)
    # Native engine output at position i predicts token i+1, and value V(s_i)
    # conditions on exactly the tokens preceding that sampled token.
    mask[len(prompt)-1:len(prompt)+len(response)-1]=True
    nested=lambda value:torch.nested.as_nested_tensor([value],layout=torch.jagged)
    data=TensorDict(dict(input_ids=nested(tokens),position_ids=nested(positions),
                         loss_mask=nested(mask)),batch_size=[1])
    tu.assign_non_tensor(data,temperature=1.,use_remove_padding=True,
        use_fused_kernels=fused,use_dynamic_bsz=False,micro_batch_size_per_gpu=1,
        return_model_output=False)
    return data


def action_slice(output, key, segment):
    start=len(segment['prompt_ids'])-1
    return output[key].values()[start:start+len(segment['response_ids'])]


def values_for(engine, segments):
    result=[]
    with engine.eval_mode():
        for segment in segments:
            output=engine.forward_backward_batch(segment_batch(segment,fused=False),None,forward_only=True)
            result.append(action_slice(output['model_output'],'values',segment).float().cpu())
    torch.cuda.empty_cache()
    return torch.cat(result)


def adapter_state(engine):
    state=get_peft_model_state_dict(engine.module,state_dict=engine.module.state_dict(),
                                    save_embedding_layers=False)
    return {name:(value.full_tensor() if hasattr(value,'full_tensor') else value).detach().cpu().contiguous()
            for name,value in state.items()}


def digest(state):
    h=hashlib.sha256()
    for name,value in sorted(state.items()):
        h.update(name.encode()+b'\0'+value.view(torch.uint8).numpy().tobytes())
    return h.hexdigest()


def save_adapter(engine,path):
    path=Path(path);path.mkdir(parents=True,exist_ok=False)
    with engine.eval_mode():
        state=adapter_state(engine)
        save_file(state,str(path/'adapter_model.safetensors'))
        engine.module.peft_config['default'].save_pretrained(path)
    torch.cuda.empty_cache()
    return digest(state)


def update(engine,segments,*,kind,targets=None,advantages=None):
    total=sum(len(s['response_ids']) for s in segments)
    offset=0;loss_total=0.;kept=0;expandable=False
    with engine.train_mode():
        engine.optimizer_zero_grad()
        for index,segment in enumerate(segments):
            length=len(segment['response_ids'])
            target=targets[offset:offset+length].cuda() if targets is not None else None
            advantage=advantages[offset:offset+length].cuda() if advantages is not None else None
            behavior=torch.tensor(segment['behavior_logprobs'],dtype=torch.float32,device='cuda')

            def loss_function(model_output,data,dp_group):
                nonlocal kept
                if kind=='critic':
                    predictions=action_slice(model_output,'values',segment).float()
                    loss=.5*(predictions-target).square().sum()/total
                else:
                    predictions=action_slice(model_output,'log_probs',segment).float()
                    loss,metrics=direct_is_loss(predictions,behavior,advantage,denominator=total)
                    kept+=metrics['kept']
                return loss,{}

            result=engine.forward_backward_batch(segment_batch(segment,fused=(kind=='actor')),
                                                  loss_function,forward_only=False)
            if index==0:
                expandable=any(s.get('is_expandable',False) for s in torch.cuda.memory_snapshot())
            loss_total+=sum(result['loss'])
            offset+=length
            print(json.dumps(dict(stage=kind,segment=index+1,segments=len(segments),
                                   loss=loss_total,gpu_allocated_mib=torch.cuda.memory_allocated()/2**20)),flush=True)
        norm=engine.optimizer_step()
        if not math.isfinite(norm):
            raise RuntimeError('Native optimizer refused a nonfinite gradient')
        engine.lr_scheduler_step()
    torch.cuda.empty_cache()
    return dict(loss=loss_total,grad_norm=norm,kept=kept,tokens=total,
                allocator_expandable_segment_observed=expandable)


def train_episode(actor,critic,episode,out):
    from dsec_openhands.sglang_training import write_json
    segments=episode['actions']
    total=sum(len(x['response_ids']) for x in segments)
    reward=float(episode['reward'])
    if reward not in (0.,1.):
        raise ValueError('Official binary task reward required')
    values=values_for(critic,segments)
    rewards=torch.zeros(total);rewards[-1]=reward
    _,targets=skip_observation_gae(values,rewards,torch.ones(total),lam=1.)
    if not torch.isfinite(values).all() or not torch.isfinite(targets).all():
        raise RuntimeError('Nonfinite critic values or targets before update')
    signals=dict(stage='before_critic',reward=reward,
        response_ids=[s['response_id'] for s in segments],
        response_lengths=[len(s['response_ids']) for s in segments],
        behavior_versions=[s['policy_version'] for s in segments],
        values_before=values.tolist(),critic_targets=targets.tolist())
    signals_path=out/'learning-signals.json';write_json(signals_path,signals)
    critic_steps=[update(critic,segments,kind='critic',targets=targets) for _ in range(2)]
    values=values_for(critic,segments)
    advantages,_=skip_observation_gae(values,rewards,torch.ones(total),lam=policy_lambda(total))
    if not torch.isfinite(values).all() or not torch.isfinite(advantages).all():
        raise RuntimeError('Nonfinite critic values or policy advantages after critic update')
    signals.update(stage='before_actor',values_after=values.tolist(),advantages=advantages.tolist(),
                   policy_lambda=policy_lambda(total))
    write_json(signals_path,signals)
    with actor.eval_mode():
        before=digest(adapter_state(actor))
    actor_step=update(actor,segments,kind='actor',advantages=advantages)
    after=save_adapter(actor,out/'actor')
    save_adapter(critic,out/'critic')
    # Optimizer states stay offloaded; keep both states and schedule for resume.
    torch.save(dict(actor_optimizer=actor.optimizer.state_dict(),
        critic_optimizer=critic.optimizer.state_dict(),actor_scheduler=actor.lr_scheduler.state_dict(),
        critic_scheduler=critic.lr_scheduler.state_dict(),torch_rng=torch.get_rng_state()),out/'optimizers.pt')
    return dict(critic_updates=critic_steps,actor_update=actor_step,
        learning_signals_sha256=hashlib.sha256(signals_path.read_bytes()).hexdigest(),
        actor_before_sha256=before,actor_after_sha256=after,actor_changed=(before!=after),
        reward=reward,action_tokens=total,policy_lambda=policy_lambda(total),
        behavior_versions=sorted({s['policy_version'] for s in segments}))


def serve_queue(actor,critic,queue,gateway_root,steps):
    from dsec_openhands.sglang_training import gpu_lease, write_json
    from dsec_openhands.training import SampledGeneration
    queue.mkdir(parents=True,exist_ok=True)
    write_json(queue/'ready.json',dict(status='ready',native_verl_engines_initialized=True,
        critic_attention_frozen=not any(p.requires_grad and 'self_attn' in n
                                        for n,p in critic.module.named_parameters())))
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def control(method,payload):
        request=urllib.request.Request('http://127.0.0.1:31026/'+method,
            data=json.dumps(payload).encode(),headers={'Content-Type':'application/json'})
        with opener.open(request,timeout=120) as response:
            content=response.read()
            return json.loads(content) if content else None

    results=[]
    for step in range(1,steps+1):
        request_path=queue/'requests'/f'{step}.json'
        # First admission includes API startup, before-evaluation and a bounded
        # 7,200-second training episode, not merely model generation time.
        deadline=time.monotonic()+10800
        while not request_path.exists():
            if (queue/'stop').exists():
                raise RuntimeError('Training stopped before the requested updates completed')
            if time.monotonic()>deadline:
                raise TimeoutError('No completed rollout arrived for training')
            time.sleep(.2)
        request=json.loads(request_path.read_text())
        episode=json.loads(Path(request['episode_file']).read_text())
        for record in episode['actions']:
            sample=SampledGeneration(**record)
            if sample.purpose!='agent':
                raise ValueError('Auxiliary summary cannot be a policy training target')
        out=queue/f'step-{step}'
        out.mkdir(exist_ok=False)
        with gpu_lease(gateway_root,'update:'+str(step)):
            state=json.loads((gateway_root/'policy.json').read_text())
            if state['version']!=step-1:
                raise ValueError('Policy publication version is not sequential')
            write_json(gateway_root/'policy.json',dict(state,phase='updating'))
            try:
                control('release_memory_occupation',dict(tags=['weights','kv_cache']))
                torch.cuda.reset_peak_memory_stats()
                result=train_episode(actor,critic,episode,out)
                result['peak_cuda_allocated_mib']=torch.cuda.max_memory_allocated()/2**20
                torch.cuda.synchronize()
                control('resume_memory_occupation',dict(tags=['weights','kv_cache']))
                adapter='policy-v'+str(step)
                loaded=control('load_lora_adapter',dict(lora_name=adapter,lora_path=str(out/'actor')))
                if loaded is not None and loaded.get('success') is False:
                    raise RuntimeError('SGLang refused updated LoRA adapter')
                write_json(gateway_root/'policy.json',dict(version=step,adapter=adapter,phase='ready',
                    actor_sha256=result['actor_after_sha256']))
                result.update(step=step,episode_file=request['episode_file'],published_adapter=adapter,
                              completed=time.time(),status='passed')
                write_json(queue/'results'/f'{step}.json',result)
                results.append(result)
            except BaseException as exc:
                write_json(gateway_root/'policy.json',dict(state,phase='failed',error_type=type(exc).__name__))
                raise
    return results


def main():
    # This application owns its model processes. Preserve textual diagnostics
    # without letting a failed CUDA probe fill the host disk with a core dump.
    import resource
    resource.setrlimit(resource.RLIMIT_CORE,(0,0))
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model',type=Path,required=True)
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--episode',type=Path)
    parser.add_argument('--queue',type=Path)
    parser.add_argument('--gateway-root',type=Path)
    parser.add_argument('--steps',type=int,default=2)
    parser.add_argument('--probe',action='store_true')
    parser.add_argument('--critic-adapter',type=Path)
    parser.add_argument('--critic-value-prior',type=float)
    args=parser.parse_args()
    if args.critic_adapter and args.critic_value_prior is not None:
        parser.error('A restored critic preserves its value head; do not supply an initialization prior')
    if args.critic_value_prior is not None and not 0<=args.critic_value_prior<=1:
        parser.error('Binary-reward value prior must be finite and in [0,1]')
    args.out.mkdir(parents=True,exist_ok=False)
    torch.cuda.set_device(0)
    torch.distributed.init_process_group('nccl',init_method='file://'+str((args.out/'rendezvous').resolve()),
                                          rank=0,world_size=1)
    report=dict(status='running',framework='native verl FSDP2 engine',synthetic_probe=args.probe,
        allocator_backend=torch.cuda.memory.get_allocator_backend(),
        allocator_config=os.environ.get('PYTORCH_ALLOC_CONF',os.environ.get('PYTORCH_CUDA_ALLOC_CONF')))
    started=time.monotonic()
    try:
        install_single_rank_fused_boundary()
        actor=make_engine(args.model,'language_model',learning_rate=1e-6,steps=args.steps)
        critic=make_engine(args.model,'value_model',learning_rate=5e-6,steps=2*args.steps,
                           adapter_path=args.critic_adapter,value_head_prior=args.critic_value_prior)
        report['critic_initialization']=dict(mode='checkpoint' if args.critic_adapter else 'zero_weight_prior',
            effective_prior=getattr(critic,'effective_value_prior',None))
        if args.critic_adapter:
            from safetensors.torch import load_file
            with critic.eval_mode():restored=digest(adapter_state(critic))
            expected=digest(load_file(args.critic_adapter/'adapter_model.safetensors'))
            if restored!=expected:raise ValueError('Restored critic state differs from its checkpoint')
            report['initial_critic_adapter_sha256']=restored
        if args.queue:
            if not args.gateway_root:
                raise ValueError('Queue training requires the inference GPU-lease directory')
            report['updates']=serve_queue(actor,critic,args.queue,args.gateway_root,args.steps)
        elif args.probe:
            # This proves real-model forward/backward feasibility, not a task reward.
            from transformers import AutoTokenizer
            tokenizer=AutoTokenizer.from_pretrained(args.model,local_files_only=True)
            prompt=tokenizer.encode('Compute 1+1. Answer:')
            response=tokenizer.encode('2',add_special_tokens=False)
            segment=dict(prompt_ids=prompt,response_ids=response,behavior_logprobs=[-1.]*len(response),
                         policy_version=0)
            with actor.eval_mode():
                reference=actor.forward_backward_batch(segment_batch(segment,fused=True),None,forward_only=True)
                segment['behavior_logprobs']=action_slice(reference['model_output'],'log_probs',segment).float().cpu().tolist()
            values=values_for(critic,[segment])
            target=torch.ones_like(values)
            report['critic_probe']=update(critic,[segment],kind='critic',targets=target)
            report['actor_probe']=update(actor,[segment],kind='actor',advantages=target)
            report['actor_export_sha256']=save_adapter(actor,args.out/'actor-probe')
            report['critic_export_sha256']=save_adapter(critic,args.out/'critic-probe')
        else:
            episode=json.loads(args.episode.read_text())
            report.update(train_episode(actor,critic,episode,args.out))
        report['status']='passed'
        report['peak_cuda_allocated_mib']=torch.cuda.max_memory_allocated()/2**20
        report['peak_cuda_reserved_mib']=torch.cuda.max_memory_reserved()/2**20
    except BaseException as exc:
        import traceback
        report.update(status='failed',error_type=type(exc).__name__,error=str(exc),traceback=traceback.format_exc())
        raise
    finally:
        report['elapsed_s']=time.monotonic()-started
        (args.out/'report.json').write_text(json.dumps(report,indent=2))
        torch.distributed.destroy_process_group()
        print(json.dumps(report),flush=True)


if __name__=='__main__':
    main()
