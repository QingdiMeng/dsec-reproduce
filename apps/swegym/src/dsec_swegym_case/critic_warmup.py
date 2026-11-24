"""Value-only warmup on scored, native-token episodes; no actor or sandbox owner."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import resource
import time
import traceback

import torch
from dsec_openhands.training import SampledGeneration, episode_actions


def value_metrics(values,returns):
    values=values.float();returns=returns.float()
    if values.ndim!=1 or values.shape!=returns.shape or not values.numel():
        raise ValueError('Aligned nonempty value/return vectors required')
    if not torch.isfinite(values).all() or not torch.isfinite(returns).all():
        raise ValueError('Nonfinite value prediction or return')
    errors=values-returns;variance=returns.var(unbiased=False)
    return dict(tokens=values.numel(),mean=float(values.mean()),
        mse=float(errors.square().mean()),rmse=float(errors.square().mean().sqrt()),
        mae=float(errors.abs().mean()),bias=float(errors.mean()),
        minimum=float(values.min()),maximum=float(values.max()),
        outside_unit_interval=float(((values<0)|(values>1)).float().mean()),
        explained_variance=float(1-errors.var(unbiased=False)/variance) if variance>0 else None)


def read_episode(path):
    path=Path(path);case=path/'case';read=lambda p:json.loads(p.read_text())
    verdict=read(case/'report.json');official=read(case/'verification/report.json')
    if (verdict['status']!='scored' or official['status']!='scored' or
            verdict['cleanup']!='stopped' or official['cleanup']!='stopped' or
            verdict['score']!=official['score'] or verdict['score'] not in (0,1)):
        raise ValueError('A genuinely scored, retired episode is required: '+str(path))
    completions=sorted((read(p) for p in (case/'completions').glob('*.json')),key=lambda r:float(r['timestamp']))
    identities=[c['response']['id'] for c in completions if 'response' in c]
    records=[];excluded=[]
    for p in (path/'generations').glob('*.json'):
        row=read(p)
        if 'error' in row:
            if verdict['termination'] not in ('episode_deadline','iteration_limit'):
                raise ValueError('Capture failure in an episode: '+str(p))
            excluded.append(p.name);continue
        sample=SampledGeneration(**row['sample'])
        if sample.policy_version!=0 or row['native']['obj'].get('lora_path') is not None:
            raise ValueError('Warmup corpus requires the frozen SFT behavior policy')
        if sample!=SampledGeneration.from_sglang(row['native'],row['response'],policy_version=0,purpose=sample.purpose):
            raise ValueError('Native capture mismatch')
        records.append(sample)
    actions=episode_actions(records,identities)
    signature={k:verdict.get(k) for k in ('instance_id','sdk_version','token_budgets',
        'max_iterations','episode_seconds','model','thinking_history','condenser',
        'sampling','unparsed_tool_recovery','recovery_limit','repository_baseline')}
    if signature['unparsed_tool_recovery']!='minicpm5' or signature['max_iterations']!=100:
        raise ValueError('Current fixed recovery profile and 100-iteration budget required')
    return dict(path=str(path),reward=float(verdict['score']),actions=[a.as_dict() for a in actions],
        segments=len(actions),action_tokens=sum(len(a.response_ids) for a in actions),
        max_context_tokens=max(len(a.prompt_ids)+len(a.response_ids) for a in actions),
        signature=signature,excluded_incomplete_records=excluded,
        official_sha256=hashlib.sha256((case/'verification/report.json').read_bytes()).hexdigest())


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('model','adapter','manifest','out'):parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--steps',type=int,default=10)
    parser.add_argument('--lora-lr',type=float,default=5e-6)
    parser.add_argument('--head-lr',type=float,default=1e-4)
    parser.add_argument('--reset-value-head-from-training-mean',action='store_true',
        help='Explicit initialization experiment: keep saved MLP adapters, reset only the scalar head')
    args=parser.parse_args()
    if not 1<=args.steps<=100 or not all(math.isfinite(x) and x>0 for x in (args.lora_lr,args.head_lr)):
        parser.error('Bounded steps and positive finite learning rates required')
    args.out.mkdir(mode=0o700,parents=True,exist_ok=False)
    resource.setrlimit(resource.RLIMIT_CORE,(0,0))
    report=dict(status='running',actor_loaded=False,actor_updates=0,steps=[],
        weights_only_initialization=True,automatic_actor_promotion=False,
        lora_lr=args.lora_lr,value_head_lr=args.head_lr,requested_steps=args.steps,
        model=str(args.model),initial_critic_adapter=str(args.adapter))
    def save():
        (args.out/'report.json').write_text(json.dumps(report,indent=2,allow_nan=False))
    started=time.monotonic()
    try:
        from safetensors.torch import load_file
        from .sao_verl import make_engine,values_for,update,adapter_state,digest,save_adapter
        manifest=json.loads(args.manifest.read_text())
        report['manifest_sha256']=hashlib.sha256(args.manifest.read_bytes()).hexdigest()
        training=[read_episode(p) for p in manifest['training']]
        reference=read_episode(manifest['reference'])
        if not training:raise ValueError('A nonempty training corpus is required')
        paths=[str(Path(e['path']).resolve()) for e in [*training,reference]]
        if len(set(paths))!=len(paths):raise ValueError('Reference and training episodes must be separate')
        if any(e['signature']!=reference['signature'] for e in training):
            raise ValueError('Warmup and reference protocol signatures differ')
        train_ids=[a['response_id'] for e in training for a in e['actions']]
        if len(set(train_ids))!=len(train_ids) or set(train_ids)&{a['response_id'] for a in reference['actions']}:
            raise ValueError('Overlapping training/reference response identities')
        report['corpus']=[{k:v for k,v in e.items() if k!='actions'} for e in training]
        report['reference']={k:v for k,v in reference.items() if k!='actions'}
        report['training_contains_both_rewards']=len({e['reward'] for e in training})==2
        prior=sum(e['reward'] for e in training)/len(training) if args.reset_value_head_from_training_mean else None
        report['value_head_initialization']=dict(mode='zero_weight_training_episode_mean' if prior is not None else 'checkpoint',
            requested_prior=prior,reference_used=False)
        torch.manual_seed(0);torch.cuda.set_device(0)
        torch.distributed.init_process_group('nccl',init_method='file://'+str(args.out/'rendezvous'),rank=0,world_size=1)
        critic=make_engine(args.model,'value_model',learning_rate=args.lora_lr,steps=args.steps,
            adapter_path=args.adapter,value_head_lr=args.head_lr,value_head_prior=prior,
            reset_value_head=args.reset_value_head_from_training_mean)
        with critic.eval_mode():before=digest(adapter_state(critic))
        original=load_file(args.adapter/'adapter_model.safetensors')
        original_digest=digest(original)
        if getattr(critic,'loaded_adapter_digest',None)!=original_digest:
            raise ValueError('Loaded value head/LoRA state differs from saved adapter before initialization')
        if prior is None and before!=original_digest:
            raise ValueError('Restored value head/LoRA state differs from saved adapter')
        if prior is not None:
            with critic.eval_mode():state=adapter_state(critic)
            if any(not torch.equal(v,state[k]) for k,v in original.items()
                   if not any(p in k.split('.') for p in ('score','classifier','v_head'))):
                raise ValueError('Head initialization unexpectedly changed MLP adapter state')
            report['value_head_initialization']['effective_prior']=critic.effective_value_prior
        report['checkpoint_adapter_sha256']=original_digest
        report['before_adapter_sha256']=before
        report['optimizer_groups']=[dict(name=g.get('name'),lr=g['lr'],
            parameters=sum(p.numel() for p in g['params'])) for g in critic.optimizer.param_groups]
        save()

        def evaluate(episodes,label):
            rows=[];all_values=[];all_returns=[]
            evidence=[]
            for i,e in enumerate(episodes):
                chunks=[]
                for j,action in enumerate(e['actions']):
                    chunks.append(values_for(critic,[action]))
                    print(json.dumps(dict(stage=label,episode=i+1,segment=j+1,segments=len(e['actions']))),flush=True)
                values=torch.cat(chunks);returns=torch.full_like(values,e['reward'])
                rows.append(dict(path=e['path'],reward=e['reward'],metrics=value_metrics(values,returns)))
                evidence.append(dict(path=e['path'],reward=e['reward'],values=values.tolist(),
                    response_ids=[a['response_id'] for a in e['actions']],
                    response_lengths=[len(a['response_ids']) for a in e['actions']]))
                all_values.append(values);all_returns.append(returns)
            (args.out/(label+'-values.json')).write_text(json.dumps(evidence,allow_nan=False))
            return dict(episodes=rows,pooled=value_metrics(torch.cat(all_values),torch.cat(all_returns)))

        # Fixed diagnostics span each training trajectory. Minibatches below
        # traverse other positions too; reference is never an optimizer target.
        fit_check=[]
        for e in training:
            picks=sorted({0,len(e['actions'])//2,len(e['actions'])-1})
            fit_check.append(dict(e,actions=[e['actions'][i] for i in picks]))
        report['fit_before']=evaluate(fit_check,'fit_before')
        report['reference_before']=evaluate([reference],'reference_before');save()
        for step in range(args.steps):
            actions=[];labels=[]
            for e in training:
                for k in range(2):
                    index=(step*2+k)*(len(e['actions'])-1)//max(1,args.steps*2-1)
                    a=e['actions'][index];actions.append(a)
                    labels.append(torch.full((len(a['response_ids']),),e['reward']))
            result=update(critic,actions,kind='critic',targets=torch.cat(labels))
            result.update(step=step+1,response_ids=[a['response_id'] for a in actions],
                reward_labels=[float(label[0]) for label in labels])
            report['steps'].append(result);save()
            print(json.dumps(dict(stage='warmup_step',**result)),flush=True)
        report['fit_after']=evaluate(fit_check,'fit_after')
        report['reference_after']=evaluate([reference],'reference_after')
        after=save_adapter(critic,args.out/'critic')
        report['after_adapter_sha256']=after
        report['critic_changed']=after!=before
        report['stationary_zero_gradient']=all(step['grad_norm']==0 for step in report['steps'])
        if not report['critic_changed'] and not report['stationary_zero_gradient']:
            raise RuntimeError('Nonzero warmup gradients left every critic parameter unchanged')
        report['fit_improved']=report['fit_after']['pooled']['mse']<report['fit_before']['pooled']['mse']
        report['reference_improved']=report['reference_after']['pooled']['mse']<report['reference_before']['pooled']['mse']
        report['small_pilot_gate_passed']=all(report[k] for k in ('training_contains_both_rewards','fit_improved','reference_improved'))
        report['scope']='single-task warmup; not held-out-task calibration or paper-scale value pretraining'
        torch.save(dict(optimizer=critic.optimizer.state_dict(),scheduler=critic.lr_scheduler.state_dict(),
            torch_rng=torch.get_rng_state(),cuda_rng=torch.cuda.get_rng_state_all()),args.out/'optimizer.pt')
        report.update(status='passed',peak_cuda_allocated_mib=torch.cuda.max_memory_allocated()/2**20,
            peak_cuda_reserved_mib=torch.cuda.max_memory_reserved()/2**20)
    except BaseException:
        report.update(status='failed',traceback=traceback.format_exc());raise
    finally:
        report['elapsed_seconds']=time.monotonic()-started;save()
        if torch.distributed.is_initialized():torch.distributed.destroy_process_group()
    print(json.dumps(report),flush=True)


if __name__=='__main__':main()
