"""One real multi-step repository episode through the public DSec API.

Baseline/oracle modes validate plumbing, not model performance or RL training.
Each evaluation gets a fresh Container sandbox; DSec owns its lifecycle.
"""
import argparse
import asyncio
import gzip
import io
import json
import os
from pathlib import Path
import shlex
import time
import traceback
import uuid

from dsec.sdk import DSecClient,DSecContainerRunArgs
from dsec._credentials import secret_file
from dsec.contracts.rpc import ServiceError
from dsec_swegym_case.dataset import public_task,validate_task
from dsec_swegym_case.harness import OfficialHarness,HARNESS_COMMIT


async def verify(task,config,out,*,mode='baseline',patch=None):
    validate_task(task)
    if mode not in ('baseline','oracle','patch'):raise ValueError('Unsupported acceptance mode')
    if mode=='patch' and not isinstance(patch,str):raise ValueError('Supply a candidate patch')
    candidate='' if mode=='baseline' else task['patch'] if mode=='oracle' else patch
    from dsec.contracts.execution import command_timeout
    shell_timeout=command_timeout(config.get('command_timeout_ms',30000))
    verifier_timeout=command_timeout(config.get('verifier_timeout_ms',shell_timeout))
    harness=OfficialHarness(task)
    out=Path(out);out.mkdir(mode=0o700,parents=True,exist_ok=False)
    report=dict(schema='dsec.swegym.execution.v1',status='running',instance_id=task['instance_id'],
                mode=mode,harness_commit=HARNESS_COMMIT,score=None,steps=[],cleanup='not_created',
                command_timeout_ms=shell_timeout,verifier_timeout_ms=verifier_timeout)
    (out/'task-input.json').write_text(json.dumps(public_task(task),indent=2)+'\n')
    began=time.monotonic();box=None;error=None;client=None
    def save():
        temp=out/'report.tmp';temp.write_text(json.dumps(report,indent=2)+'\n');os.replace(temp,out/'report.json')
    async def step(name,command,*,log_path=None):
        rid=uuid.uuid4().hex
        budget=verifier_timeout if name=='official_verifier' else shell_timeout
        record=dict(name=name,request_id=rid,command=command,state='intent',started=time.time(),timeout_ms=budget)
        report['steps'].append(record);save()
        result=await box.run_shell(command,timeout_ms=budget,output_limit=262144,request_id=rid)
        record.update(state='done',result=result,completed=time.time());save()
        if log_path is not None:
            # Capture the file even when the command timed out. Do not rely on
            # bounded shell stdout to hold a complete official test log.
            try:
                data=await box.read_file('/work/test_output.txt',max_bytes=8*1024**2)
            except ValueError as exc:
                if str(exc)!='File exceeds read budget':raise
                # A shallow root commit makes the official `git show` expand
                # the whole baseline. Transfer its complete log compressed;
                # do not trim or change the official verifier's commands.
                archived=await step('archive_verifier_log',
                    'gzip -c -- /work/test_output.txt > /work/test_output.txt.gz')
                if archived['exit_code']:raise RuntimeError('Verifier log compression failed')
                packed=await box.read_file('/work/test_output.txt.gz',max_bytes=8*1024**2)
                log_path.with_suffix('.txt.gz').write_bytes(packed)
                record['compressed_log_file']=str(log_path.with_suffix('.txt.gz').relative_to(out));save()
                with gzip.GzipFile(fileobj=io.BytesIO(packed)) as stream:
                    data=stream.read(128*1024**2+1)
                if len(data)>128*1024**2:raise RuntimeError('Verifier log exceeds expanded evidence budget; compressed log retained')
            log_path.write_bytes(data);record['log_file']=str(log_path.relative_to(out));save()
        if result['timed_out'] or result['truncated']:raise RuntimeError(name+': incomplete command evidence')
        return result
    save()
    try:
        api=json.loads(Path(config['api_config']).read_text())
        client=DSecClient(**api['client'],token=secret_file(api['token_file']));await client.open()
        rid=uuid.uuid4().hex;report.update(create_request_id=rid,cleanup='creation_pending');save()
        box=await client.run_container(DSecContainerRunArgs(**config['spec']),request_id=rid,resource_demand=config['resource_demand'])
        report.update(sandbox_id=box.id,cleanup='pending');save()
        initial=await step('repository_identity','git config --global --add safe.directory /testbed && cd /testbed && git rev-parse HEAD && id -u')
        if initial['exit_code'] or initial['output'].splitlines()!=[task['base_commit'],'1000']:raise RuntimeError('Prepared image has wrong task base commit or runtime user')
        if config.get('repository_baseline'):
            from .repository import verification_command
            from .common import task_shell
            expected=config['repository_baseline']
            if expected['base_commit']!=task['base_commit']:raise ValueError('Repository policy differs from task baseline')
            proof=await step('repository_baseline',task_shell(verification_command(expected)))
            if proof['exit_code']:raise RuntimeError('Baseline-only Git verification failed')
            report['repository_baseline']=json.loads(proof['output']);save()
        result=await step('repository_observation','cd /testbed && git status --porcelain && git log -1 --oneline')
        if result['exit_code']:raise RuntimeError('Repository observation failed')
        await box.write_file('/work/prediction.patch',candidate.encode())
        # Older official images predate git apply --allow-empty. An empty
        # prediction leaves the checkout untouched, but still runs all tests.
        result=await step('apply_candidate','cd /testbed && { test ! -s /work/prediction.patch || git apply -v /work/prediction.patch; }')
        if result['exit_code'] in (126,127,129):raise RuntimeError('Patch application tool unavailable or incompatible')
        if result['exit_code']:
            report.update(status='scored',score=0,reason='candidate_patch_rejected')
        else:
            await box.write_file('/work/eval.sh',harness.script.encode(),mode=0o700)
            marker=harness.modules['constants'].APPLY_PATCH_PASS+' (pred)'
            logs=out/task['instance_id'].lower();logs.mkdir();path=logs/'test_output.txt'
            result=await step('official_verifier',"printf '%s\\n' "+shlex.quote(marker)+
                ' > /work/test_output.txt; bash /work/eval.sh >> /work/test_output.txt 2>&1',log_path=path)
            report.update(status='scored',**harness.grade(path,candidate,result))
    except BaseException as exc:
        error=exc;report.update(status='error',error=dict(type=type(exc).__name__,message=str(exc)),traceback=traceback.format_exc())
        if isinstance(exc,ServiceError):
            report['error'].update(kind=exc.kind,details=exc.details)
            if box is None and exc.kind=='ExecutionCreateFailed' and exc.details.get('cleanup_complete') is True:
                report['cleanup']='not_created_proved'
    finally:
        if box is not None:
            rid=uuid.uuid4().hex;report['stop_request_id']=rid;save()
            try:
                stopped=await box.stop(request_id=rid)
                if stopped.get('state')!='STOPPED':raise RuntimeError('Sandbox stop did not prove completion')
                report['cleanup']='stopped'
            except BaseException as exc:
                report['cleanup']='unknown';report['cleanup_error']=dict(type=type(exc).__name__,message=str(exc))
                if error is None:error=exc;report['status']='error'
        if client is not None:
            try:await client.close()
            except BaseException as exc:
                report['client_close_error']=str(exc)
                if error is None:error=exc;report['status']='error'
        report['elapsed_s']=time.monotonic()-began;save()
    if error is not None:raise RuntimeError('SWE-Gym execution failed; evidence: '+str(out/'report.json')) from error
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task',type=Path,required=True);parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--out',type=Path,required=True);parser.add_argument('--mode',choices=('baseline','oracle','patch'),default='baseline')
    parser.add_argument('--patch',type=Path)
    args=parser.parse_args()
    report=asyncio.run(verify(json.loads(args.task.read_text()),json.loads(args.config.read_text()),args.out,
        mode=args.mode,patch=args.patch.read_text() if args.patch else None))
    print(json.dumps(dict(status=report['status'],score=report['score'],cleanup=report['cleanup'],report=str(args.out/'report.json'))))


if __name__=='__main__':main()
