"""Run a bounded OpenHands/DSec SAO training-and-evaluation acceptance.

An existing DSec API deployment owns sandbox placement and isolation. This
optional application owns its model/trainer subprocesses and passes only the
public SWE-Gym issue to the unchanged OpenHands agent.
"""
import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import urllib.request

from dsec_openhands.sglang_training import write_json
from dsec_openhands.training import SampledGeneration, episode_actions


def validated_before(path,adapter,count):
    """Reuse the fixed validation episodes without sampling them a second time."""
    read=lambda p:json.loads(p.read_text())
    path=Path(path);report=read(path)
    if (report.get('status')!='passed' or report.get('gate_passed') is not True or
            report.get('critic_unchanged') is not True or report.get('optimizer_steps')!=0 or
            report.get('actor_updates')!=0):
        raise ValueError('Passed read-only critic validation required before actor training')
    if adapter is None or hashlib.sha256((Path(adapter)/'adapter_model.safetensors').read_bytes()).hexdigest()!=report['adapter_file_sha256']:
        raise ValueError('Validated critic checkpoint differs from requested training checkpoint')
    manifest=Path(report['manifest'])
    if hashlib.sha256(manifest.read_bytes()).hexdigest()!=report['manifest_sha256']:
        raise ValueError('Validation manifest changed')
    rows=report['episodes'];paths=[r['path'] for r in rows]
    if len(rows)!=count or len(set(paths))!=count or paths!=read(manifest)['validation']:
        raise ValueError('Before evaluation must reuse every prespecified validation episode')
    results=[]
    for row in rows:
        case=Path(row['path'])/'case';verdict=case/'report.json';official=case/'verification/report.json'
        if (hashlib.sha256(verdict.read_bytes()).hexdigest()!=row['case_report_sha256'] or
                hashlib.sha256(official.read_bytes()).hexdigest()!=row['official_sha256']):
            raise ValueError('Validated episode evidence changed')
        value=read(verdict)
        if value['status']!='scored' or value['cleanup']!='stopped' or value['score']!=row['reward']:
            raise ValueError('Before episode lacks its validated official result')
        results.append((value,case))
    return results,dict(path=str(path),sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        adapter_file_sha256=report['adapter_file_sha256'],protocol_signature=report['protocol_signature'])


class Pilot:
    def __init__(self,args):
        self.args=args
        self.root=args.out
        self.children={}
        self.logs={}
        self.owns_root=False
        self.report=dict(status='preparing',scope='single-task SAO integration acceptance',
            held_out_evaluation=False,group_size=1,policy_updates=args.steps,critic_updates_per_policy=2,
            single_gpu_timesharing=True,events=[])
        self.base=json.loads(args.case_config.read_text())
        self.env=dict(os.environ,LITELLM_LOCAL_MODEL_COST_MAP='True',OPENHANDS_SUPPRESS_BANNER='1',
            TOKENIZERS_PARALLELISM='false',HTTP_PROXY='',HTTPS_PROXY='',ALL_PROXY='',
            NO_PROXY='localhost,127.0.0.1')
        self.env['PATH']=str(args.trainer_python.parent)+':'+self.env.get('PATH',os.defpath)

    def event(self,kind,**fields):
        self.report['events'].append(dict(kind=kind,time=time.time(),**fields))
        write_json(self.root/'report.json',self.report)
        print(json.dumps(dict(kind=kind,**fields)),flush=True)

    def start(self,name,argv):
        log=(self.root/(name+'.log')).open('wb')
        self.logs[name]=log
        child_env=self.env
        if name=='trainer':
            # Variable native prompt lengths otherwise leave unusable allocator
            # splits across an episode. Configure before this child's CUDA init.
            # SGLang owns its memory-saver allocator and keeps its own settings.
            child_env=dict(self.env)
            child_env.pop('PYTORCH_CUDA_ALLOC_CONF',None)
            child_env['PYTORCH_ALLOC_CONF']='expandable_segments:True'
        self.children[name]=subprocess.Popen(argv,stdin=subprocess.DEVNULL,stdout=log,
            stderr=subprocess.STDOUT,env=child_env)
        self.event('process_started',name=name,pid=self.children[name].pid,argv=argv)
        return self.children[name]

    def wait(self,predicate,seconds):
        deadline=time.monotonic()+seconds
        while not predicate():
            self.check_children()
            if time.monotonic()>deadline:
                raise TimeoutError('Pilot stage deadline exceeded')
            time.sleep(.2)

    def check_children(self):
        for name in ('trainer','model','gateway'):
            child=self.children.get(name)
            if child is not None and child.poll() is not None:
                path=self.root/'trainer/report.json'
                done=name=='trainer' and path.exists() and json.loads(path.read_text()).get('status')=='passed'
                if not done:
                    raise RuntimeError(name+' exited; inspect its log')

    @staticmethod
    def health(port):
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
                    f'http://127.0.0.1:{port}/health',timeout=2) as response:
                return response.status==200
        except Exception:
            return False

    def episode(self,name):
        config=deepcopy(self.base)
        config['openhands'].update(base_url=f'http://127.0.0.1:31027/episodes/{name}/v1',
            model='openai/dsec-swegym-minicpm5-2b',custom_tokenizer=str(self.args.model),
            max_iterations=self.args.max_iterations,
            temperature=1.,top_p=.95,
            capability_overrides=dict(supports_sampling_params=True,supports_reasoning_effort=False))
        if name.startswith('train-'):
            # A single GPU may spend many minutes updating another episode.
            # Do not silently extend evaluation budgets to hide that queue wait.
            # Training permits the bounded wait; gpu-timeline records it separately.
            config['openhands'].update(episode_seconds=7200,model_timeout_seconds=7200)
        config['openhands']['sampling_extra']=dict(top_k=20,min_p=0.,presence_penalty=0.,
            repetition_penalty=1.,chat_template_kwargs=dict(enable_thinking=True))
        path=self.root/(name+'-case.json')
        write_json(path,config)
        out=self.root/'episodes'/name/'case'
        child=self.start(name,[str(self.args.sdk_python),'-m','dsec_swegym_case.openhands_sdk',
            '--task',str(self.args.task),'--config',str(path),'--out',str(out)])
        self.event('episode_started',episode=name,
            policy_version=json.loads((self.root/'policy.json').read_text())['version'],
            max_iterations=self.args.max_iterations,
            episode_seconds=config['openhands'].get('episode_seconds',1200),
            model_timeout_seconds=config['openhands'].get('model_timeout_seconds',600))
        return child,out

    def scored(self,name,child,out):
        if child.poll() is None:
            return None
        report=json.loads((out/'report.json').read_text())
        if child.returncode or report.get('status')!='scored' or report.get('cleanup')!='stopped':
            raise RuntimeError('Unscored or incompletely retired episode: '+name)
        self.event('episode_scored',episode=name,reward=report['score'],termination=report['termination'])
        return report

    def admit(self,name,out,step):
        path=self.root/'episodes'/name/'generations'
        records=[]
        for p in path.glob('*.json'):
            data=json.loads(p.read_text())
            if 'error' in data:
                raise RuntimeError('Native training capture failed: '+str(p))
            records.append(SampledGeneration(**data['sample']))
        completions=[json.loads(p.read_text()) for p in (out/'completions').glob('*.json')]
        completions.sort(key=lambda x:float(x['timestamp']))
        ids=[d['response']['id'] for d in completions if 'response' in d]
        selected=episode_actions(records,ids)
        verdict=json.loads((out/'report.json').read_text())
        payload=dict(episode=name,reward=verdict['score'],official_report=str(out/'verification/report.json'),
                     actions=[s.as_dict() for s in selected])
        target=self.root/'episodes'/name/'training-episode.json'
        write_json(target,payload)
        write_json(self.root/'queue/requests'/f'{step}.json',dict(episode_file=str(target)))
        self.event('rollout_admitted',episode=name,step=step,actions=len(selected),
            action_tokens=sum(len(s.response_ids) for s in selected),
            behavior_versions=sorted({s.policy_version for s in selected}),
            other_episode_processes_running=[key for key,p in self.children.items()
                if key.startswith('train-') and key!=name and p.poll() is None])

    def evaluate(self,stage,reused=None):
        count=getattr(self.args,'eval_episodes',1)
        results=[] if reused is None else reused
        if reused is None:
            for index in range(count):
                name=stage if count==1 else stage+'-'+str(index+1)
                child,out=self.episode(name)
                self.wait(lambda:child.poll() is not None,3900)
                results.append((self.scored(name,child,out),out))
        if len(results)!=count:raise ValueError('Incomplete evaluation episode count')
        if count==1:return results[0][0]
        return dict(score=sum(r['score'] for r,_ in results)/count,count=count,
            episodes=[dict(score=r['score'],termination=r['termination'],report=str(out/'report.json')) for r,out in results])

    def run(self):
        reused=None
        if getattr(self.args,'before_validation',None):
            reused,proof=validated_before(self.args.before_validation,self.args.critic_adapter,
                getattr(self.args,'eval_episodes',1))
            signature=proof['protocol_signature'];options=self.base['openhands']
            repository=signature.get('repository_baseline') or {}
            expected_repository=self.base.get('repository_baseline') or {}
            if (signature['max_iterations']!=self.args.max_iterations or
                    signature['instance_id']!=json.loads(self.args.task.read_text())['instance_id'] or
                    any(repository.get(k)!=expected_repository.get(k) for k in
                        ('base_commit','tree','objects','commits','object_set_sha256','commit_set_sha256','history')) or
                    signature['episode_seconds']!=options.get('episode_seconds',1200) or
                    signature['token_budgets']['context']!=options.get('context_length',65536) or
                    signature['token_budgets']['response']!=options.get('response_length',32768) or
                    signature['unparsed_tool_recovery']!=options.get('unparsed_tool_recovery') or
                    signature['recovery_limit']!=options.get('recovery_limit',3)):
                raise ValueError('Training/evaluation settings differ from the fixed before protocol')
            self.report['before_validation']=proof
        self.root.mkdir(parents=True,exist_ok=False)
        self.owns_root=True
        write_json(self.root/'policy.json',dict(version=0,adapter=None,phase='ready'))
        trainer=[str(self.args.trainer_python),'-m','dsec_swegym_case.sao_verl',
            '--model',str(self.args.model),'--out',str(self.root/'trainer'),
            '--queue',str(self.root/'queue'),'--gateway-root',str(self.root),'--steps',str(self.args.steps)]
        if getattr(self.args,'critic_adapter',None):
            trainer.extend(['--critic-adapter',str(self.args.critic_adapter)])
        if getattr(self.args,'critic_value_prior',None) is not None:
            trainer.extend(['--critic-value-prior',str(self.args.critic_value_prior)])
        self.start('trainer',trainer)
        self.wait(lambda:(self.root/'queue/ready.json').exists(),600)
        # Preserve the validated inference profile; add native training handoff support.
        argv=[str(self.args.trainer_python),'-m','sglang.launch_server','--model-path',str(self.args.model),
            '--served-model-name','dsec-swegym-minicpm5-2b','--host','127.0.0.1','--port','31026',
            '--context-length','65536','--mem-fraction-static','0.65','--max-running-requests','4',
            '--chunked-prefill-size','2048','--cuda-graph-backend-decode','disabled',
            '--cuda-graph-backend-prefill','disabled','--tool-call-parser','minicpm5',
            '--reasoning-parser','qwen3','--log-requests','--log-requests-level','3',
            '--log-requests-format','json','--log-requests-target',str(self.root/'native-requests'),
            '--enable-metrics','--enable-lora','--max-lora-rank','8','--max-loaded-loras','4',
            '--max-loras-per-batch','2',
            '--lora-target-modules','gate_proj','up_proj','down_proj',
            '--enable-memory-saver','--enable-weights-cpu-backup']
        self.start('model',argv)
        self.wait(lambda:self.health(31026),600)
        self.start('gateway',[str(self.args.sdk_python),'-m','dsec_openhands.sglang_training',
            '--root',str(self.root),'--native-logs',str(self.root/'native-requests')])
        self.wait(lambda:self.health(31027),60)
        api_path=Path(self.base['api_config'])
        self.wait(api_path.exists,900)
        self.report['before']=self.evaluate('before',reused)
        self.event('before_evaluation_completed',reused=reused is not None,
            episodes=getattr(self.args,'eval_episodes',1),score=self.report['before']['score'])
        running={};next_episode=1;admitted=0
        child,out=self.episode('train-1');running['train-1']=(child,out);next_episode=2
        self.report['status']='training'
        training_deadline=time.monotonic()+7200
        while admitted<self.args.steps:
            self.check_children()
            if time.monotonic()>training_deadline:
                raise TimeoutError('Training rollout stage exceeded two hours')
            # Start another independent episode as the preceding agent retires;
            # its verifier overlaps the next sandbox's execution. No reward group.
            if next_episode<=self.args.steps and len(running)<2:
                for _name,(_child,_out) in list(running.items()):
                    p=_out/'report.json'
                    state=json.loads(p.read_text()) if p.exists() else {}
                    if state.get('termination'):
                        name='train-'+str(next_episode)
                        running[name]=self.episode(name);next_episode+=1
                        break
            for name,(child,out) in list(running.items()):
                if child.poll() is None:
                    continue
                self.scored(name,child,out)
                admitted+=1
                self.admit(name,out,admitted)
                del running[name]
            # The learner consumes completed episodes independently, not at a
            # synchronization barrier after all episodes have returned.
            time.sleep(.2)
        self.wait(lambda:(self.root/'queue/results'/f'{self.args.steps}.json').exists(),3600)
        self.report['updates']=[json.loads((self.root/'queue/results'/f'{i}.json').read_text())
                                for i in range(1,self.args.steps+1)]
        self.event('training_updates_completed',completed_policy_updates=len(self.report['updates']))
        self.report['after']=self.evaluate('after')
        if not any(update['actor_changed'] for update in self.report['updates']):
            raise RuntimeError('All updates completed but no actor parameter change was observed')
        self.report['status']='passed'
        self.event('training_and_evaluation_completed',final_policy_version=self.args.steps)

    def close(self):
        errors=[]
        # Controllers own sandbox retirement. Request SIGINT and let them drain
        # before stopping the gateway or model they may still be awaiting.
        import signal
        for name,child in self.children.items():
            if name in ('trainer','gateway','model') or child.poll() is not None:
                continue
            child.send_signal(signal.SIGINT)
            try:child.wait(timeout=150)
            except subprocess.TimeoutExpired:errors.append(name+' controller did not retire')
        for name in ('trainer','gateway','model'):
            child=self.children.get(name)
            if child is not None and child.poll() is None:
                child.terminate()
                try:child.wait(timeout=120)
                except subprocess.TimeoutExpired:errors.append(name+' did not stop')
        for log in self.logs.values():log.close()
        self.report['cleanup_errors']=errors
        if errors:self.report['status']='failed'
        write_json(self.root/'report.json',self.report)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('task','case-config','model','out','sdk-python','trainer-python'):
        parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--steps',type=int,default=2)
    parser.add_argument('--max-iterations',type=int,default=100)
    parser.add_argument('--critic-adapter',type=Path)
    parser.add_argument('--critic-value-prior',type=float)
    parser.add_argument('--eval-episodes',type=int,default=1)
    parser.add_argument('--before-validation',type=Path)
    args=parser.parse_args()
    if args.critic_adapter and args.critic_value_prior is not None:
        parser.error('Restored critic heads are preserved; a prior is only for cold initialization')
    if args.critic_value_prior is not None and not 0<=args.critic_value_prior<=1:
        parser.error('Binary-reward value prior must be finite and in [0,1]')
    if args.steps<2 or args.max_iterations<1:
        parser.error('At least two policy updates and positive iteration budget required')
    if not 1<=args.eval_episodes<=16:
        parser.error('Evaluation episode count must be between 1 and 16')
    if args.before_validation and not args.critic_adapter:
        parser.error('Reused validation requires its exact critic adapter')
    if args.out.exists():
        parser.error('Output directory must be new; existing evidence will not be overwritten')
    pilot=Pilot(args)
    try:
        pilot.run()
    except BaseException as exc:
        import traceback
        pilot.report.update(status='failed',error_type=type(exc).__name__,error=str(exc),
                            traceback=traceback.format_exc())
        raise
    finally:
        if pilot.owns_root:pilot.close()


if __name__=='__main__':
    main()
