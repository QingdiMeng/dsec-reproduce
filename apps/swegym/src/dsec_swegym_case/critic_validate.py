"""Read-only value validation against a fixed prior on unseen native episodes."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import resource
import time
import traceback

import torch
from .critic_warmup import read_episode,value_metrics


def validation_summary(rows):
    if not rows:raise ValueError('Nonempty validation episodes required')
    candidate=sum(r['candidate']['mse'] for r in rows)/len(rows)
    baseline=sum(r['constant_prior']['mse'] for r in rows)/len(rows)
    if not all(math.isfinite(x) for x in (candidate,baseline)):
        raise ValueError('Nonfinite validation error')
    coverage={r['reward'] for r in rows}
    if not coverage<={0.,1.}:raise ValueError('Official binary returns required')
    reasons=[]
    if coverage!={0.,1.}:reasons.append('missing_positive_or_negative_episode')
    if candidate>baseline:reasons.append('candidate_worse_than_constant_prior')
    return dict(primary='equal episode mean of action-token MSE',candidate_mse=candidate,
        constant_prior_mse=baseline,gate_passed=not reasons,gate_reasons=reasons,
        reward_groups={str(int(reward)):dict(episodes=sum(r['reward']==reward for r in rows),
            candidate_mse=sum(r['candidate']['mse'] for r in rows if r['reward']==reward)/sum(r['reward']==reward for r in rows),
            constant_prior_mse=sum(r['constant_prior']['mse'] for r in rows if r['reward']==reward)/sum(r['reward']==reward for r in rows))
            for reward in sorted(coverage)})


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for key in ('model','adapter','manifest','prepared-from','out'):
        parser.add_argument('--'+key,type=Path,required=True)
    parser.add_argument('--prior',type=float,default=.75)
    args=parser.parse_args()
    if not math.isfinite(args.prior) or not 0<=args.prior<=1:
        parser.error('Fixed binary-reward prior must be in [0,1]')
    args.out.mkdir(parents=True,exist_ok=False,mode=0o700)
    resource.setrlimit(resource.RLIMIT_CORE,(0,0))
    report=dict(status='running',actor_loaded=False,optimizer_steps=0,actor_updates=0,
        fixed_prior=args.prior,episodes=[],scope='single-task independent critic validation; small pilot gate')
    def save():(args.out/'report.json').write_text(json.dumps(report,indent=2,allow_nan=False))
    started=time.monotonic()
    try:
        from safetensors.torch import load_file
        from .sao_verl import make_engine,adapter_state,digest,values_for
        manifest=json.loads(args.manifest.read_text())
        prepared=json.loads(args.prepared_from.read_text())
        old=[read_episode(p) for p in [*prepared['training'],prepared['reference']]]
        episodes=[read_episode(p) for p in manifest['validation']]
        if len(episodes)!=4:raise ValueError('Exactly four prespecified independent episodes required')
        all_paths=[str(Path(e['path']).resolve()) for e in [*old,*episodes]]
        if len(set(all_paths))!=len(all_paths):raise ValueError('Reused training/reference/validation episode')
        known={a['response_id'] for e in old for a in e['actions']}
        seen=set()
        for e in episodes:
            if e['signature']!=old[0]['signature']:raise ValueError('Changed validation protocol')
            ids={a['response_id'] for a in e['actions']}
            if ids&known or ids&seen:raise ValueError('Overlapping validation generation identities')
            seen.update(ids)
        report.update(manifest_sha256=hashlib.sha256(args.manifest.read_bytes()).hexdigest(),
            prepared_manifest_sha256=hashlib.sha256(args.prepared_from.read_bytes()).hexdigest(),
            adapter_file_sha256=hashlib.sha256((args.adapter/'adapter_model.safetensors').read_bytes()).hexdigest(),
            manifest=str(args.manifest),adapter=str(args.adapter),protocol_signature=old[0]['signature'])
        torch.manual_seed(0);torch.cuda.set_device(0)
        torch.distributed.init_process_group('nccl',init_method='file://'+str(args.out/'rendezvous'),rank=0,world_size=1)
        critic=make_engine(args.model,'value_model',learning_rate=5e-6,steps=1,
            adapter_path=args.adapter,forward_only=True)
        if critic.optimizer is not None or critic.lr_scheduler is not None:
            raise ValueError('Read-only validation must not construct an optimizer or scheduler')
        with critic.eval_mode():before=digest(adapter_state(critic))
        if before!=digest(load_file(args.adapter/'adapter_model.safetensors')):
            raise ValueError('Restored critic head/LoRA differs from candidate checkpoint')
        report['adapter_state_sha256']=before;save()
        all_values=[];all_returns=[]
        for index,e in enumerate(episodes):
            chunks=[]
            for j,a in enumerate(e['actions']):
                chunks.append(values_for(critic,[a]))
                print(json.dumps(dict(stage='validate',episode=index+1,segment=j+1,segments=len(e['actions']))),flush=True)
            values=torch.cat(chunks);returns=torch.full_like(values,e['reward'])
            row=dict(path=e['path'],reward=e['reward'],termination=json.loads((Path(e['path'])/'case/report.json').read_text())['termination'],
                official_sha256=e['official_sha256'],
                case_report_sha256=hashlib.sha256((Path(e['path'])/'case/report.json').read_bytes()).hexdigest(),
                candidate=value_metrics(values,returns),constant_prior=value_metrics(torch.full_like(values,args.prior),returns))
            (args.out/f'episode-{index+1}-values.json').write_text(json.dumps(dict(
                values=values.tolist(),reward=e['reward'],response_ids=[a['response_id'] for a in e['actions']],
                response_lengths=[len(a['response_ids']) for a in e['actions']]),allow_nan=False))
            report['episodes'].append(row);all_values.append(values);all_returns.append(returns);save()
        report['summary']=validation_summary(report['episodes'])
        report['pooled_token_metrics']=value_metrics(torch.cat(all_values),torch.cat(all_returns))
        with critic.eval_mode():after=digest(adapter_state(critic))
        if after!=before:raise ValueError('Validation changed critic parameters')
        report.update(status='passed',critic_unchanged=True,gate_passed=report['summary']['gate_passed'],
            peak_cuda_allocated_mib=torch.cuda.max_memory_allocated()/2**20)
    except BaseException:
        report.update(status='failed',traceback=traceback.format_exc());raise
    finally:
        report['elapsed_seconds']=time.monotonic()-started;save()
        if torch.distributed.is_initialized():torch.distributed.destroy_process_group()
    print(json.dumps(report),flush=True)


if __name__=='__main__':main()
