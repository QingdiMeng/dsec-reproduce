import asyncio
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,patch

from dsec.contracts.errors import RequestOutcomeUnknown
from dsec.contracts.rpc import ServiceError
from dsec_swegym_case.dataset import public_task,validate_task
from dsec_swegym_case.harness import OfficialHarness
from dsec_swegym_case.acceptance import verify
from dsec_swegym_case.agent import rollout
from dsec_swegym_case.policy import (CODING_SAMPLING,SGLangPolicy,TokenContext,
                                    ToolFormatError,checked_generation,parse_calls)


def task():
    return dict(instance_id='python__mypy-15413',repo='python/mypy',version='1.4',base_commit='a'*40,
        problem_statement='Fix a lambda diagnostic',hints_text='not part of the prompt',patch='GOLD_ANSWER',
        test_patch='diff --git a/test-data/unit/check.test b/test-data/unit/check.test\n[case testReturnAnyLambda]\n',
        FAIL_TO_PASS=['mypy/test/testcheck.py::TypeCheckSuite::check-flags.test::testReturnAnyLambda'],PASS_TO_PASS=[])


class TaskTests(unittest.TestCase):
    def test_prompt_excludes_gold_and_hidden_tests(self):
        result=public_task(task())
        self.assertEqual(set(result),{'instance_id','repo','base_commit','problem_statement'})
        self.assertNotIn('GOLD_ANSWER',json.dumps(result))

    def test_traversal_and_vacuous_test_sets_are_rejected(self):
        for changes in ({'instance_id':'../escape'},{'FAIL_TO_PASS':[]},
                        {'PASS_TO_PASS':task()['FAIL_TO_PASS']},{'base_commit':'main'}):
            with self.subTest(changes=changes),self.assertRaises(ValueError):validate_task(dict(task(),**changes))


class ExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        token=self.root/'token';token.write_text('a'*64+'\n');token.chmod(0o600)
        api=self.root/'api.json';api.write_text(json.dumps(dict(client=dict(endpoints=['https://localhost:8443'],project='test'),token_file=str(token))))
        self.config=dict(api_config=str(api),spec=dict(environment_id='swegym-mypy-15413'),resource_demand=dict(cpu=1,memory_mb=1280,disk_mb=8192,network_mbps=0))
        self.box=SimpleNamespace(id='box',write_file=AsyncMock(),read_file=AsyncMock(return_value=b'partial official log'),stop=AsyncMock(return_value=dict(state='STOPPED')))
        async def execute(command,**kw):
            output='a'*40+'\n1000\n' if 'git rev-parse' in command else ''
            return dict(exit_code=0,timed_out=False,truncated=False,output=output)
        self.box.run_shell=AsyncMock(side_effect=execute)
        self.client=SimpleNamespace(open=AsyncMock(),close=AsyncMock(),run_container=AsyncMock(return_value=self.box))
        harness=SimpleNamespace(script='#!/bin/bash\ntrue\n',modules=dict(constants=SimpleNamespace(APPLY_PATCH_PASS='>>>>> Applied Patch')),grade=lambda *args:dict(score=1))
        self.enterContext(patch('dsec_swegym_case.acceptance.DSecClient',return_value=self.client))
        self.enterContext(patch('dsec_swegym_case.acceptance.OfficialHarness',return_value=harness))

    async def test_multistep_oracle_preserves_evidence_and_stops_its_sandbox(self):
        output=self.root/'success'
        report=await verify(task(),self.config,output,mode='oracle')
        self.assertEqual((report['score'],report['cleanup']),(1,'stopped'))
        self.assertEqual(len({v['request_id'] for v in report['steps']}),4)
        self.assertEqual((output/task()['instance_id']/'test_output.txt').read_bytes(),b'partial official log')
        self.assertNotIn('GOLD_ANSWER',(output/'task-input.json').read_text())
        self.box.stop.assert_awaited_once()

    async def test_tool_and_verifier_budgets_are_distinct_and_invalid_limits_have_no_effect(self):
        await verify(task(),dict(self.config,command_timeout_ms=45000,verifier_timeout_ms=120000),self.root/'budgets')
        self.assertEqual([call.kwargs['timeout_ms'] for call in self.box.run_shell.await_args_list],
                         [45000,45000,45000,120000])
        self.client.run_container.reset_mock()
        for value in (True,0,300001):
            with self.assertRaises(ValueError):
                await verify(task(),dict(self.config,verifier_timeout_ms=value),self.root/'bad')
        self.client.run_container.assert_not_awaited()

    async def test_empty_baseline_reaches_official_verifier_without_new_git_options(self):
        report=await verify(task(),self.config,self.root/'baseline')
        self.assertEqual([v['name'] for v in report['steps']],
            ['repository_identity','repository_observation','apply_candidate','official_verifier'])
        command=report['steps'][2]['command']
        self.assertIn('test ! -s /work/prediction.patch || git apply -v',command)
        self.assertNotIn('--allow-empty',command)
        self.assertEqual(report['steps'][-1]['state'],'done')

    async def test_large_official_log_is_transferred_completely_without_changing_verifier(self):
        import gzip
        content=b'baseline root diff\n'*(500000)+b'PASSED expected-test\n'
        packed=gzip.compress(content)
        async def read(path,**kwargs):
            if path=='/work/test_output.txt':raise ValueError('File exceeds read budget')
            self.assertEqual(path,'/work/test_output.txt.gz')
            return packed
        self.box.read_file.side_effect=read
        output=self.root/'large-log'
        report=await verify(task(),self.config,output)
        self.assertEqual(report['status'],'scored')
        self.assertEqual((output/task()['instance_id']/'test_output.txt').read_bytes(),content)
        self.assertEqual((output/task()['instance_id']/'test_output.txt.gz').read_bytes(),packed)
        commands=[v['command'] for v in report['steps']]
        self.assertEqual(sum('bash /work/eval.sh' in c for c in commands),1)
        self.assertEqual(commands[-1],'gzip -c -- /work/test_output.txt > /work/test_output.txt.gz')
        self.box.stop.assert_awaited_once()

    async def test_patch_tool_incompatibility_is_invalid_not_negative_reward(self):
        original=self.box.run_shell.side_effect
        async def execute(command,**kw):
            result=await original(command,**kw)
            if 'git apply' in command:result.update(exit_code=129,output='unknown option')
            return result
        self.box.run_shell.side_effect=execute;output=self.root/'incompatible'
        with self.assertRaises(RuntimeError):await verify(task(),self.config,output)
        value=json.loads((output/'report.json').read_text())
        self.assertIsNone(value['score']);self.assertEqual(value['cleanup'],'stopped')
        self.assertIn('incompatible',value['error']['message'])

    async def test_timeout_retains_partial_log_and_primary_error_when_cleanup_fails(self):
        original=self.box.run_shell.side_effect
        async def execute(command,**kw):
            result=await original(command,**kw)
            if 'bash /work/eval.sh' in command:result['timed_out']=True
            return result
        self.box.run_shell.side_effect=execute;self.box.stop.side_effect=RuntimeError('cleanup busy')
        output=self.root/'failed'
        with self.assertRaises(RuntimeError):await verify(task(),self.config,output)
        value=json.loads((output/'report.json').read_text())
        self.assertIn('incomplete',value['error']['message']);self.assertEqual(value['cleanup'],'unknown')
        self.assertIn('cleanup busy',value['cleanup_error']['message']);self.assertIsNone(value['score'])
        self.assertEqual((output/task()['instance_id']/'test_output.txt').read_bytes(),b'partial official log')

    async def test_unknown_creation_is_not_replayed_or_falsely_reported_clean(self):
        self.client.run_container.side_effect=RequestOutcomeUnknown('lost create reply','b'*32)
        output=self.root/'unknown'
        with self.assertRaises(RuntimeError):await verify(task(),self.config,output)
        value=json.loads((output/'report.json').read_text())
        self.assertEqual(value['cleanup'],'creation_pending');self.assertIn('create_request_id',value)
        self.client.run_container.assert_awaited_once();self.box.stop.assert_not_awaited()

    async def test_api_open_failure_still_writes_evidence(self):
        self.client.open.side_effect=RuntimeError('unavailable')
        output=self.root/'offline'
        with self.assertRaises(RuntimeError):await verify(task(),self.config,output)
        value=json.loads((output/'report.json').read_text())
        self.assertEqual(value['error']['message'],'unavailable');self.assertEqual(value['cleanup'],'not_created')

    async def test_failed_create_with_cleanup_proof_is_not_left_as_unknown(self):
        self.client.run_container.side_effect=ServiceError('ExecutionCreateFailed','failed',dict(cleanup_complete=True))
        output=self.root/'create-failed'
        with self.assertRaises(RuntimeError):await verify(task(),self.config,output)
        value=json.loads((output/'report.json').read_text())
        self.assertEqual(value['cleanup'],'not_created_proved');self.assertEqual(value['error']['kind'],'ExecutionCreateFailed')
        self.client.run_container.assert_awaited_once();self.box.stop.assert_not_awaited()


@unittest.skipUnless(importlib.util.find_spec('swebench'),'optional official harness is not installed')
class OfficialScoringTests(unittest.TestCase):
    def test_official_parser_scores_actual_test_status_not_process_exit_code(self):
        harness=OfficialHarness(task());execution=dict(exit_code=0,timed_out=False,truncated=False)
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)/task()['instance_id'];root.mkdir();path=root/'test_output.txt'
            for status,score in (('FAILED',0),('PASSED',1)):
                path.write_text('>>>>> Applied Patch (pred)\n'+status+' '+task()['FAIL_TO_PASS'][0]+'\n')
                self.assertEqual(harness.grade(path,'patch',execution)['score'],score)
            path.write_text('>>>>> Applied Patch (pred)\ncollected 0 items\n')
            with self.assertRaises(RuntimeError):harness.grade(path,'patch',execution)
            with self.assertRaises(RuntimeError):harness.grade(path,'patch',dict(execution,timed_out=True))

    def test_official_script_keeps_installation_tests_and_reset(self):
        script=OfficialHarness(task()).script
        self.assertIn('pip install -r test-requirements.txt',script)
        self.assertIn('pytest -n0 -rA -k "testReturnAnyLambda"',script)
        self.assertIn(task()['test_patch'],script)


def generation(text,*,reason='stop',ids=None):
    ids=ids or [21,22,999]
    return dict(text=text,output_ids=ids,meta_info=dict(finish_reason=dict(type=reason),
        output_token_logprobs=[[-.5,token,None] for token in ids],weight_versions=['v1']*len(ids)))


def call(name,value):
    parameter='command' if name=='run_shell' else 'summary'
    return f'<tool_call>\n<function={name}>\n<parameter={parameter}>\n{value}\n</parameter>\n</function>\n</tool_call>'


class TemplateTokenizer:
    """Small structural template for token-boundary tests, not a model mock."""
    def encode(self,text,**kwargs):
        parts=text.split('<|im_end|>');result=[]
        for i,part in enumerate(parts):
            if i:result.append(999)
            result.extend(ord(c) for c in part)
        return result

    def apply_chat_template(self,messages,*,add_generation_prompt,tokenize,**kwargs):
        text=''.join('<|im_start|>'+m['role']+'\n'+m['content']+'<|im_end|>\n' for m in messages)
        if add_generation_prompt:text+='<|im_start|>assistant\n<think>\n'
        return self.encode(text) if tokenize else text


class PolicyTests(unittest.TestCase):
    def test_xml_batch_executes_in_order_and_preserves_multiline_command(self):
        command="python - <<'PY'\nprint('hello')\nPY"
        values=parse_calls('reasoning\n</think>\n'+call('run_shell','pwd')+'\n'+call('run_shell',command))
        self.assertEqual([v['arguments']['command'] for v in values],['pwd',command])

    def test_entire_batch_is_rejected_for_partial_unknown_or_mixed_finish(self):
        valid=call('run_shell','pwd')
        for text in [valid,valid+'<tool_call>',valid+call('finish','done'),
                     valid+call('unknown','x'),valid.replace('</parameter>','</parameter><parameter=evil>x</parameter>')]:
            with self.subTest(text=text),self.assertRaises(ToolFormatError):
                parse_calls(text if text==valid else '</think>'+text)
        with self.assertRaises(ToolFormatError):parse_calls('</think></think>'+valid)
        self.assertEqual(parse_calls(call('finish','done'),thinking=False)[0]['name'],'finish')

    def test_token_and_logprob_mismatches_never_become_training_evidence(self):
        good=generation('</think>'+call('finish','done'))
        self.assertEqual(checked_generation(good)[0],[21,22,999])
        for rows in ([[-.5,21,None]],[[-.5,999,None]]*3,[[float('nan'),t,None] for t in [21,22,999]]):
            broken=dict(good,meta_info=dict(good['meta_info'],output_token_logprobs=rows))
            with self.assertRaises(ValueError):checked_generation(broken)

    def test_observations_extend_exact_sampled_ids_and_mask_only_policy_tokens(self):
        context=TokenContext(TemplateTokenizer(),'instruction');initial=len(context.ids)
        context.generated(generation('text that must never be re-tokenized'),policy_version='frozen-v1')
        before=list(context.ids);context.observe(['exit_code=1; full feedback'])
        self.assertEqual(context.ids[:len(before)],before)
        self.assertEqual(context.mask[initial:initial+3],[1,1,1])
        self.assertFalse(any(context.mask[initial+3:]));self.assertEqual(context.logprobs[initial:initial+3],[-.5]*3)
        self.assertEqual(context.spans[0]['policy_version'],'frozen-v1')
        self.assertEqual(len(context.ids),len(context.export()['loss_mask']))

    def test_generation_budgets_do_not_truncate_prior_context(self):
        policy=SGLangPolicy('http://localhost:31026',TemplateTokenizer(),policy_version='frozen-v1')
        context=policy.context('instruction');request=policy.request(context)
        self.assertEqual(request['sampling_params'],dict(CODING_SAMPLING,max_new_tokens=32768))
        self.assertEqual(request['input_ids'],context.ids)
        policy.context_length=len(context.ids)
        with self.assertRaises(OverflowError):policy.request(context)
        self.assertEqual(request['input_ids'],context.ids)

    def test_tokenizer_mapping_is_read_as_ids_not_mapping_keys(self):
        class MappingTokenizer(TemplateTokenizer):
            def apply_chat_template(self,*args,**kwargs):
                value=super().apply_chat_template(*args,**kwargs)
                return dict(input_ids=value,attention_mask=[1]*len(value)) if kwargs.get('tokenize') else value
        context=TokenContext(MappingTokenizer(),'instruction')
        self.assertTrue(all(type(token) is int for token in context.ids))
        self.assertNotIn('input_ids',context.ids)


class AgentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        token=self.root/'token';token.write_text('a'*64+'\n');token.chmod(0o600)
        api=self.root/'api.json';api.write_text(json.dumps(dict(client=dict(endpoints=['https://localhost:8443'],project='test'),token_file=str(token))))
        self.config=dict(api_config=str(api),spec=dict(environment_id='prepared'),resource_demand=dict(cpu=1,memory_mb=1280,disk_mb=8192,network_mbps=0))
        self.box=SimpleNamespace(id='box',read_file=AsyncMock(return_value=b'diff candidate'),stop=AsyncMock(return_value=dict(state='STOPPED')))
        async def execute(command,**kw):
            return dict(exit_code=0,timed_out=False,truncated=False,
                output='a'*40+'\n1000\n' if 'git rev-parse' in command else 'command observation')
        self.box.run_shell=AsyncMock(side_effect=execute)
        self.client=SimpleNamespace(open=AsyncMock(),close=AsyncMock(),run_container=AsyncMock(return_value=self.box))
        self.enterContext(patch('dsec_swegym_case.agent.DSecClient',return_value=self.client))
        self.policy=SGLangPolicy('http://localhost:31026',TemplateTokenizer(),policy_version='frozen-v1')
        self.policy.generate=AsyncMock(side_effect=[generation('</think>'+call('run_shell','ls')),generation('</think>'+call('run_shell','echo edit')),generation('</think>'+call('finish','fixed'))])
        async def evaluate(*args,**kwargs):
            self.box.stop.assert_awaited_once()
            self.assertEqual(kwargs['patch'],'diff candidate')
            return dict(score=1,cleanup='stopped')
        self.evaluator=AsyncMock(side_effect=evaluate)

    async def test_realistic_multiturn_tokens_feedback_patch_and_fresh_verification(self):
        out=self.root/'episode';report=await rollout(task(),self.config,out,self.policy,evaluator=self.evaluator)
        self.assertEqual((report['score'],report['termination'],report['cleanup']),(1,'finished','stopped'))
        self.assertEqual(len([v for v in report['actions'] if v['name']=='model_shell']),2)
        for action in report['actions']:
            if action['name']=='model_shell':
                self.assertIn('conda activate testbed',action['command'])
        self.assertTrue(any(v['name']=='task_environment' for v in report['actions']))
        requests=[json.loads(p.read_text()) for p in sorted(out.glob('turn-*/request.json'))]
        self.assertEqual(len(requests),3)
        for previous,next_value in zip(requests,requests[1:]):
            self.assertEqual(next_value['input_ids'][:len(previous['input_ids'])],previous['input_ids'])
        trajectory=json.loads((out/'trajectory.json').read_text());self.assertEqual(sum(trajectory['loss_mask']),9)
        self.assertEqual(len(trajectory['policy_spans']),3)
        self.assertNotIn('GOLD_ANSWER',(out/'task-input.json').read_text())
        self.evaluator.assert_awaited_once()

    async def test_model_commands_fit_remaining_episode_budget(self):
        report=await rollout(task(),dict(self.config,command_timeout_ms=120000),self.root/'budget',self.policy,
                             episode_seconds=1,evaluator=self.evaluator)
        commands=[action for action in report['actions'] if action['name']=='model_shell']
        self.assertTrue(commands)
        self.assertTrue(all(0<action['timeout_ms']<=1000 for action in commands))
        self.assertEqual(next(action for action in report['actions'] if action['name']=='capture_candidate')['timeout_ms'],120000)

    async def test_format_repair_does_not_execute_hidden_or_partial_calls(self):
        self.policy.generate.side_effect=[generation('</think>'+call('run_shell','danger')+'<tool_call>'),generation('</think>'+call('finish','done'))]
        report=await rollout(task(),self.config,self.root/'repair',self.policy,evaluator=self.evaluator)
        self.assertFalse(any(v['name']=='model_shell' for v in report['actions']))
        self.assertIn('format_error',report['turns'][0]);self.assertEqual(report['score'],1)

    async def test_unknown_shell_is_not_retried_or_given_a_negative_reward(self):
        original=self.box.run_shell.side_effect
        async def execute(command,**kw):
            if 'bash -lc' in command and 'sys.prefix' not in command:
                raise RequestOutcomeUnknown('lost shell reply',kw['request_id'])
            return await original(command,**kw)
        self.box.run_shell.side_effect=execute;out=self.root/'unknown'
        with self.assertRaises(RuntimeError):await rollout(task(),self.config,out,self.policy,evaluator=self.evaluator)
        report=json.loads((out/'report.json').read_text());self.assertIsNone(report['score'])
        self.assertEqual(report['actions'][-1]['state'],'intent');self.policy.generate.assert_awaited_once()
        self.evaluator.assert_not_awaited();self.box.stop.assert_awaited_once()

    async def test_step_limit_still_gets_the_actual_official_score(self):
        self.evaluator.side_effect=None;self.evaluator.return_value=dict(score=0,cleanup='stopped')
        report=await rollout(task(),self.config,self.root/'limit',self.policy,max_steps=1,evaluator=self.evaluator)
        self.assertEqual((report['score'],report['termination']),(0,'step_limit'));self.evaluator.assert_awaited_once()

    async def test_truncated_response_never_executes_a_partially_generated_command(self):
        self.policy.generate.side_effect=[generation('</think>'+call('run_shell','do not run'),reason='length')]
        report=await rollout(task(),self.config,self.root/'truncated',self.policy,evaluator=self.evaluator)
        self.assertEqual(report['termination'],'response_length')
        self.assertFalse(any(v['name']=='model_shell' for v in report['actions']))

    async def test_template_failure_saves_evidence_without_creating_a_sandbox(self):
        self.policy.context=lambda _:(_ for _ in ()).throw(ValueError('bad template'));out=self.root/'template'
        with self.assertRaises(RuntimeError):await rollout(task(),self.config,out,self.policy,evaluator=self.evaluator)
        report=json.loads((out/'report.json').read_text());self.assertEqual(report['cleanup'],'not_created')
        self.assertIn('bad template',report['error']['message']);self.client.run_container.assert_not_awaited()

    async def test_missing_task_environment_fails_before_model_sampling(self):
        original=self.box.run_shell.side_effect
        async def execute(command,**kw):
            result=await original(command,**kw)
            if 'sys.prefix' in command:result.update(exit_code=1,output='missing task dependency')
            return result
        self.box.run_shell.side_effect=execute;out=self.root/'environment'
        with self.assertRaises(RuntimeError):await rollout(task(),self.config,out,self.policy,evaluator=self.evaluator)
        report=json.loads((out/'report.json').read_text());self.assertIsNone(report['score'])
        self.assertEqual(report['cleanup'],'stopped');self.policy.generate.assert_not_awaited();self.evaluator.assert_not_awaited()
