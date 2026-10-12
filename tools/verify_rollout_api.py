"""Manual API-only use-case acceptance; fixed responses, no GPU or model claims.

Operators provision the API/Node/worker and pinned environments first. This tool
does not install or restart services, change policies, or access a worker socket.
MBPP/TB2.1 are optional application imports. Reports preserve partial failures.
"""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

from dsec._persistence import atomic_json
from dsec.sdk import RolloutAPIClient


async def verify(args):
    root=args.out.resolve();root.mkdir(mode=0o700,parents=True,exist_ok=False)
    report=dict(status='running',scope='api-only-use-case-harness',model_sampling=False,
        checks=[],api_config_sha256=hashlib.sha256(args.api_config.read_bytes()).hexdigest(),
        tool_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
    def passed(name,**details):
        report['checks'].append(dict(name=name,**details));atomic_json(root/'report.json',report)
    try:
        from dsec_adapters.miles_dsec_agent_function import run_episode
        class Message:
            def __init__(self,content):self.content=content
            def model_dump(self,exclude_none=True):return {'role':'assistant','content':self.content}
        calls=[]
        async def policy(*arguments):
            calls.append(arguments[2])
            reply='```bash\nprintf 3 > /rl-counter\n```' if len(calls)==1 else 'TASK_COMPLETE'
            return SimpleNamespace(choices=[SimpleNamespace(message=Message(reply),finish_reason='stop')])
        settings={'DSEC_API_CONFIG':str(args.api_config.resolve()),
            'DSEC_EPISODE_REGISTRY_DIR':str(root/'miles-ownership'),
            'DSEC_MODEL_OUTPUT_DIR':str(root/'miles-fixed-replies'), 'DSEC_TITO_AUDIT_DIR':None}
        previous={key:os.environ.get(key) for key in settings}
        for key,value in settings.items():
            if value is None:os.environ.pop(key,None)
            else:os.environ[key]=value
        rid=RolloutAPIClient.new_rollout_id();report['miles_rollout_id']=rid
        atomic_json(root/'report.json',report)
        try:
            score,metrics=await run_episode(None,'fixed-harness',[],{},
                {'task_id':'counter-example','dsec_environment':'counter','dsec_rollout_id':rid},policy_call=policy)
        finally:
            for key,value in previous.items():
                if value is None:os.environ.pop(key,None)
                else:os.environ[key]=value
        assert score==1 and len(calls)==2,(score,metrics)
        passed('miles_loop_through_api',score=score,metrics=metrics)
        if args.mbpp_environment:
            from dsec_mbpp_case.acceptance import FIXTURES,TRUTH
            from dsec_mbpp_case.reward import compute_score
            for name,code,expected in FIXTURES:
                result=await compute_score('mbpp-dsec','```python\n'+code+'\n```',TRUTH,
                    api_config=str(args.api_config),environment_id=args.mbpp_environment,
                    evidence_dir=root/'mbpp-fixtures',candidate_timeout=.25)
                assert result['score']==expected,(name,result)
                passed('mbpp_'+name,**result)
            semaphore=asyncio.Semaphore(args.concurrency)
            async def sample():
                async with semaphore:
                    return await compute_score('mbpp-dsec','```python\n'+FIXTURES[-1][1]+'\n```',TRUTH,
                        api_config=str(args.api_config),environment_id=args.mbpp_environment,
                        evidence_dir=root/'mbpp-samples',candidate_timeout=.25)
            results=await asyncio.gather(*(sample() for _ in range(args.samples)))
            assert all(result['score']==1 for result in results),results
            assert len({result['dsec_rollout_id'] for result in results})==args.samples
            passed('mbpp_independent_episodes',samples=args.samples,concurrency=args.concurrency,results=results)
        if args.tb21_tasks:
            from dsec_tb21_case.tb2_dsec_environment import TB2DSecEnvironment
            from dsec.rollout.environment import DSecAgentEnvironment,EnvironmentAction
            fixture=json.loads(args.tb21_trajectory.read_text())
            client=RolloutAPIClient.from_config(args.api_config)
            async with client:
                episode=DSecAgentEnvironment(client,TB2DSecEnvironment(args.tb21_tasks,
                    environment_catalog=args.tb21_catalog),fixture['task_id'],client.new_rollout_id())
                unknown=False
                try:
                    await episode.reset([])
                    for index,command in enumerate(fixture['commands']):
                        await episode.step(EnvironmentAction.shell(step_id=index,action_id=f'fixed-{index}',command=command,
                            timeout_ms=120000),policy_message={'role':'assistant','content':'```bash\n'+command+'\n```'})
                    verdict=await episode.evaluate()
                    assert verdict.score==1,verdict
                    passed('tb21_official_verifier_through_api',rollout_id=episode.rollout_id,
                        trajectory_sha256=hashlib.sha256(args.tb21_trajectory.read_bytes()).hexdigest(),
                        score=verdict.score,evaluator=verdict.evaluator,details=verdict.details)
                except Exception:
                    from dsec.sdk import ScheduledOutcomeUnknown
                    import sys
                    unknown=isinstance(sys.exception(),ScheduledOutcomeUnknown)
                    raise
                finally:
                    if not unknown:await episode.stop()
        report['status']='passed'
    except BaseException as exc:
        report.update(status='failed',error=dict(type=type(exc).__name__,message=str(exc)))
        raise
    finally:
        atomic_json(root/'report.json',report)
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--api-config',type=Path,required=True)
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--mbpp-environment')
    parser.add_argument('--samples',type=int,default=8)
    parser.add_argument('--concurrency',type=int,default=2)
    parser.add_argument('--tb21-tasks',type=Path)
    parser.add_argument('--tb21-catalog',type=Path)
    parser.add_argument('--tb21-trajectory',type=Path)
    args=parser.parse_args()
    if not 1<=args.concurrency<=args.samples<=128:parser.error('Require 1 <= concurrency <= samples <= 128')
    if any((args.tb21_tasks,args.tb21_catalog,args.tb21_trajectory)) and not all((args.tb21_tasks,args.tb21_catalog,args.tb21_trajectory)):
        parser.error('TB2.1 needs tasks, catalog and trajectory together')
    report=asyncio.run(verify(args));print(json.dumps({'status':report['status'],'checks':len(report['checks'])}))


if __name__=='__main__':main()
