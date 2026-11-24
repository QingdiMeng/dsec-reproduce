"""Sample a real multi-turn repository trajectory through the public DSec API.

This optional scaffold is outside DSec's sandbox/model-management boundary.
It records behavior tokens for a later trainer; it does not implement SAO.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import shlex
import time
import traceback
import uuid

from dsec._credentials import secret_file
from dsec.contracts.rpc import ServiceError
from dsec.rollout.environment import format_shell_observation
from dsec.sdk import DSecClient,DSecContainerRunArgs
from .acceptance import verify
from .dataset import public_task,validate_task
from .policy import SGLangPolicy,ToolFormatError,parse_calls
from .patch import capture_command
from .common import write_json, task_shell


async def rollout(task,config,out,policy,*,max_steps=30,episode_seconds=1200,evaluator=verify):
    validate_task(task)
    if type(max_steps) is not int or not 1<=max_steps<=300 or not 1<=episode_seconds<=7200:
        raise ValueError('Bounded episode limits required')
    from dsec.contracts.execution import command_timeout
    shell_timeout=command_timeout(config.get('command_timeout_ms',30000))
    out=Path(out);out.mkdir(mode=0o700,parents=True,exist_ok=False)
    began=time.monotonic();deadline=began+episode_seconds;box=None;client=None;patch=None;error=None
    context=None
    report=dict(schema='dsec.swegym.agent.v1',instance_id=task['instance_id'],status='running',
        score=None,cleanup='not_created',policy_version=policy.version,thinking=policy.thinking,
        max_steps=max_steps,episode_seconds=episode_seconds,command_timeout_ms=shell_timeout,turns=[],actions=[],termination=None)
    def save():write_json(out/'report.json',report)
    write_json(out/'task-input.json',public_task(task));save()
    async def command(name,text):
        rid=uuid.uuid4().hex;record=dict(name=name,request_id=rid,command=text,state='intent',started=time.time())
        budget=shell_timeout if name!='model_shell' else max(1,min(shell_timeout,int((deadline-time.monotonic())*1000)))
        record['timeout_ms']=budget
        report['actions'].append(record);save()
        result=await box.run_shell(text,timeout_ms=budget,output_limit=262144,request_id=rid)
        record.update(state='done',completed=time.time(),result=result);save();return result,rid
    try:
        write_json(out/'policy.json',policy.metadata())
        context=policy.context(task['problem_statement'])
        api=json.loads(Path(config['api_config']).read_text());client=DSecClient(**api['client'],token=secret_file(api['token_file']))
        await client.open();rid=uuid.uuid4().hex;report.update(create_request_id=rid,cleanup='creation_pending');save()
        box=await client.run_container(DSecContainerRunArgs(**config['spec']),request_id=rid,resource_demand=config['resource_demand'])
        report.update(sandbox_id=box.id,cleanup='pending');save()
        result,_=await command('repository_identity','git config --global --add safe.directory /testbed && cd /testbed && git rev-parse HEAD && id -u')
        if result.get('exit_code')!=0 or result.get('output','').splitlines()!=[task['base_commit'],'1000']:
            raise RuntimeError('Prepared image base commit or runtime user differs')
        probe="import sys,typing_extensions; assert sys.prefix == '/opt/miniconda3/envs/testbed'; print(sys.executable)"
        result,_=await command('task_environment',task_shell('python -c '+shlex.quote(probe)))
        if result.get('exit_code')!=0 or result.get('timed_out') or result.get('truncated'):
            raise RuntimeError('Prepared task Python environment failed preflight')
        for index in range(max_steps):
            remaining=deadline-time.monotonic()
            if remaining<=0:report['termination']='episode_deadline';break
            turn_dir=out/f'turn-{index:03d}';turn_dir.mkdir(mode=0o700)
            try:request=policy.request(context)
            except OverflowError:report['termination']='context_limit';break
            turn=dict(index=index,state='generation_intent',request_id=request['rid'],started=time.time())
            report['turns'].append(turn);write_json(turn_dir/'request.json',request);save()
            value=await policy.generate(request,timeout=remaining)
            write_json(turn_dir/'response.json',value);context.generated(value,policy.version)
            turn.update(state='generated',completed=time.time(),response_tokens=len(value['output_ids']),
                        prompt_tokens=len(request['input_ids']),finish_reason=value['meta_info']['finish_reason'])
            write_json(out/'trajectory.json',context.export());save()
            reason=value['meta_info']['finish_reason'].get('type')
            if reason=='length':report['termination']='response_length';break
            if reason not in ('stop','matched','eos'):
                raise RuntimeError('Unusable generation finish reason: '+str(reason))
            if time.monotonic()>=deadline:report['termination']='episode_deadline';break
            try:calls=parse_calls(value['text'],thinking=policy.thinking)
            except ToolFormatError as exc:
                turn['format_error']=str(exc);context.observe([json.dumps(dict(error='tool_format',message=str(exc)))])
                write_json(out/'trajectory.json',context.export());save();continue
            turn['calls']=calls;save()
            if calls[0]['name']=='finish':report['termination']='finished';break
            observations=[]
            for call in calls:
                if time.monotonic()>=deadline:report['termination']='episode_deadline';break
                text=task_shell(call['arguments']['command'])
                result,rid=await command('model_shell',text)
                entry=dict(step_id=sum(a['name']=='model_shell' for a in report['actions'])-1,action_id=rid,result=result)
                observations.append(format_shell_observation(entry))
            if report['termination']:break
            context.observe(observations);turn['state']='observed'
            write_json(out/'trajectory.json',context.export());save()
        if report['termination'] is None:report['termination']='step_limit'
        result,_=await command('capture_candidate',task_shell(capture_command()))
        if result.get('exit_code')!=0 or result.get('timed_out') or result.get('truncated'):
            raise RuntimeError('Candidate patch capture incomplete')
        patch=await box.read_file('/work/solution.patch',max_bytes=8*1024**2)
        (out/'solution.patch').write_bytes(patch);os.chmod(out/'solution.patch',0o600)
    except BaseException as exc:
        error=exc;report.update(status='error',error=dict(type=type(exc).__name__,message=str(exc)),traceback=traceback.format_exc())
        if isinstance(exc,ServiceError):
            report['error'].update(kind=exc.kind,details=exc.details)
            if box is None and exc.kind=='ExecutionCreateFailed' and exc.details.get('cleanup_complete') is True:
                report['cleanup']='not_created_proved'
    finally:
        if context is not None:write_json(out/'trajectory.json',context.export())
        if box is not None:
            rid=uuid.uuid4().hex;report['stop_request_id']=rid;save()
            try:
                stopped=await box.stop(request_id=rid)
                if stopped.get('state')!='STOPPED':raise RuntimeError('Sandbox stop not proved')
                report['cleanup']='stopped'
            except BaseException as exc:
                report['cleanup']='unknown';report['cleanup_error']=str(exc)
                if error is None:error=exc;report['status']='error'
        if client is not None:
            try:await client.close()
            except BaseException as exc:
                report['client_close_error']=str(exc)
                if error is None:error=exc;report['status']='error'
        report['rollout_elapsed_s']=time.monotonic()-began;save()
    if error is None and patch is not None:
        # Fresh verifier sandbox: model-edited runtime/process state is not trusted.
        # A bounded incomplete agent episode still receives its actual test score;
        # infrastructure/unknown failures remain unscored.
        try:
            verdict=await evaluator(task,config,out/'verification',mode='patch',patch=patch.decode())
            report.update(status='scored',score=verdict['score'],verification_cleanup=verdict['cleanup'])
        except BaseException as exc:
            error=exc;report.update(status='error',verification_error=str(exc))
    report['elapsed_s']=time.monotonic()-began;save()
    if error is not None:raise RuntimeError('SWE-Gym rollout failed; evidence: '+str(out/'report.json')) from error
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('task','config','model','out'):parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--endpoint',default='http://127.0.0.1:31026')
    parser.add_argument('--policy-version',required=True)
    parser.add_argument('--max-steps',type=int,default=30);parser.add_argument('--episode-seconds',type=int,default=1200)
    parser.add_argument('--response-length',type=int,default=32768);parser.add_argument('--context-length',type=int,default=65536)
    parser.add_argument('--non-thinking',action='store_true')
    args=parser.parse_args()
    from transformers import AutoTokenizer
    tokenizer=AutoTokenizer.from_pretrained(args.model,local_files_only=True)
    policy=SGLangPolicy(args.endpoint,tokenizer,policy_version=args.policy_version,
        thinking=not args.non_thinking,response_length=args.response_length,context_length=args.context_length)
    report=asyncio.run(rollout(json.loads(args.task.read_text()),json.loads(args.config.read_text()),args.out,
        policy,max_steps=args.max_steps,episode_seconds=args.episode_seconds))
    print(json.dumps({k:report[k] for k in ('status','score','termination','cleanup','elapsed_s')}))


if __name__=='__main__':main()
