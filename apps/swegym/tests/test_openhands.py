import importlib.util
import unittest
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

from dsec_swegym_case.openhands import policy_stuck, policy_parse_failure, instrument_llm
from dsec_swegym_case.openhands_reasoning import ThinkingHistory, reasoning_scope


class ThinkingHistoryTests(unittest.TestCase):
    def test_deepseek_keeps_all_turns_including_plain_assistant_replies(self):
        self.assertEqual(reasoning_scope(dict(sampling_extra=dict(thinking=dict(type='enabled')))),
                         'deepseek_all_assistant_turns')
        history = ThinkingHistory('deepseek_all_assistant_turns')
        first = dict(role='assistant',content='Need more information.')
        second = dict(role='assistant',content='Need more information.')
        history.remember(dict(first,reasoning_content='first reasoning'))
        history.remember(dict(second,reasoning_content='second reasoning'))
        call = dict(role='assistant',content='',tool_calls=[dict(id='tool',type='function',
            function=dict(name='execute_bash',arguments='{"command":"pwd"}'))])
        history.remember(dict(call,reasoning_content='tool reasoning'))
        messages = [dict(role='user',content='First task'),first,dict(role='user',content='Second task'),
                    second,call,dict(role='tool',content='output',tool_call_id='tool')]
        restored = history.restore(messages)
        self.assertEqual([m['reasoning_content'] for m in restored if m['role']=='assistant'],
                         ['first reasoning','second reasoning','tool reasoning'])
        self.assertNotIn('reasoning_content',first)
        with self.assertRaisesRegex(ValueError,'lost'):
            history.restore(messages[:-3])
        messages[1] = dict(first,content='Changed')
        with self.assertRaisesRegex(ValueError,'order/content'):
            history.restore(messages)
        with self.assertRaisesRegex(ValueError,'Conflicting'):
            reasoning_scope(dict(sampling_extra=dict(thinking=dict(type='enabled'),
                chat_template_kwargs=dict(enable_thinking=True))))

    def test_restore_is_exact_scoped_and_does_not_mutate_upstream_history(self):
        history = ThinkingHistory()
        call = dict(id='original', type='function', function=dict(name='execute_bash', arguments='{"command":"pwd"}'))
        assistant = dict(role='assistant', content='', tool_calls=[call])
        history.remember(dict(assistant, reasoning_content='Inspect the repository first.'))
        messages = [dict(role='user', content='Fix the issue'), assistant,
                    dict(role='tool', content='repository', tool_call_id='original')]
        restored = history.restore(messages)
        self.assertEqual(restored[1]['reasoning_content'], 'Inspect the repository first.')
        self.assertNotIn('reasoning_content', assistant)
        self.assertEqual(restored[1]['tool_calls'], assistant['tool_calls'])
        new_turn = history.restore(restored + [dict(role='user', content='A different task')])
        self.assertNotIn('reasoning_content', new_turn[1])
        wrapped = history.restore(messages + [dict(role='user', content='<tool_response>output</tool_response>')])
        self.assertEqual(wrapped[1]['reasoning_content'], restored[1]['reasoning_content'])

    def test_unmatched_or_reused_tool_calls_fail_without_guessing(self):
        history = ThinkingHistory()
        assistant = dict(role='assistant', content='', tool_calls=[dict(id='id',type='function',
            function=dict(name='execute_bash',arguments='{"command":"pwd"}'))])
        messages = [dict(role='user',content='Fix'), assistant]
        with self.assertRaisesRegex(ValueError, 'Unmatched'):
            history.restore(messages)
        history.remember(dict(assistant, reasoning_content='original thinking'))
        with self.assertRaisesRegex(ValueError, 'reused'):
            history.remember(dict(assistant, reasoning_content='different thinking'))
        assistant['tool_calls'][0]['function']['arguments'] = '{"command":"ls"}'
        with self.assertRaisesRegex(ValueError, 'Unmatched'):
            history.restore(messages)


def legacy_openhands_available():
    try:
        return importlib.util.find_spec('openhands.core') is not None
    except ModuleNotFoundError:
        return False


@unittest.skipUnless(legacy_openhands_available(), 'optional legacy OpenHands is not installed')
class OpenHandsOutcomeTests(unittest.TestCase):
    def test_deepseek_contract_reaches_the_real_provider_converter(self):
        self._provider_history_roundtrip('deepseek')

    def test_real_provider_converter_and_upstream_history_roundtrip(self):
        self._provider_history_roundtrip('qwen')

    def _provider_history_roundtrip(self, profile):
        import httpx
        from openhands.core.config import LLMConfig
        from openhands.agenthub.codeact_agent.codeact_agent import CodeActAgent
        from openhands.agenthub.codeact_agent.function_calling import response_to_actions
        requests = []
        marker = 'Preserve this exact reasoning across a tool step.'
        call = dict(id='original-call',type='function',function=dict(name='str_replace_editor',
            arguments='{"command":"view","path":"/testbed/mypy/checker.py"}'))
        def provider(request):
            body = json.loads(request.content)
            requests.append(body)
            if len(requests) == 1:
                message = dict(role='assistant',content='',reasoning_content=marker,tool_calls=[call])
            else:
                self.assertEqual(body['messages'][1]['reasoning_content'], marker)
                self.assertEqual(body['messages'][1]['tool_calls'][0]['function'], call['function'])
                message = dict(role='assistant',content='done',reasoning_content='Completed.')
            return httpx.Response(200,json=dict(id=f'response-{len(requests)}',created=1,model='dsec-test',
                object='chat.completion',choices=[dict(index=0,finish_reason='tool_calls' if len(requests)==1 else 'stop',message=message)],
                usage=dict(prompt_tokens=10,completion_tokens=5,total_tokens=15)))
        real_client = httpx.Client
        class MockHTTPClient(real_client):
            def __init__(self, **kwargs):
                super().__init__(transport=httpx.MockTransport(provider), **kwargs)
        config = LLMConfig(model='openai/dsec-test',api_key='TEST-SECRET-MUST-NOT-LOG',
            base_url='http://127.0.0.1:31026/v1',num_retries=0,timeout=600,
            max_input_tokens=65536,max_output_tokens=32768,temperature=.6,top_p=.95,
            input_cost_per_token=0,output_cost_per_token=0,disable_vision=True,caching_prompt=False)
        with tempfile.TemporaryDirectory() as directory, patch('httpx.Client', MockHTTPClient):
            extra = (dict(thinking=dict(type='enabled'),reasoning_effort='high') if profile=='deepseek'
                     else dict(chat_template_kwargs=dict(enable_thinking=True)))
            llm = instrument_llm(config, dict(native_tool_calling=True,sampling_extra=extra), Path(directory))
            user = dict(role='user',content='Fix the issue in /testbed')
            first = llm.completion(messages=[user])
            self.assertEqual(first.choices[0].message.reasoning_content, marker)
            pending = {}
            CodeActAgent.__new__(CodeActAgent).get_action_message(response_to_actions(first)[0], pending)
            assistant = pending[first.id].model_dump()
            self.assertNotIn('reasoning_content', assistant)
            llm.completion(messages=[user,assistant,dict(role='tool',content='source code',
                name='str_replace_editor',tool_call_id='original-call')])
            self.assertEqual(len(requests), 2)
            self.assertEqual(requests[1]['max_tokens'],32768)
            self.assertEqual(requests[1]['temperature'],.6)
            self.assertEqual(requests[1]['top_p'],.95)
            if profile=='deepseek':
                self.assertEqual(requests[1]['thinking'],dict(type='enabled'))
                self.assertEqual(requests[1]['reasoning_effort'],'high')
                self.assertNotIn('chat_template_kwargs',requests[1])
                self.assertIsInstance(requests[1]['messages'][1]['content'],str)
            files = list(Path(directory).rglob('*.json'))
            combined = '\n'.join(p.read_text() for p in files)
            self.assertNotIn('TEST-SECRET-MUST-NOT-LOG', combined)
            raw = json.loads((Path(directory)/'provider/0000.response.json').read_text())
            self.assertEqual(raw['choices'][0]['message']['reasoning_content'],marker)
            evidence = json.loads((Path(directory)/'0001.json').read_text())
            self.assertEqual(evidence['provider_request']['messages'][1]['reasoning_content'],marker)
            self.assertEqual(evidence['request']['max_retries'],0)

    def test_provider_retry_is_disabled_beneath_the_upstream_wrapper(self):
        from litellm import ModelResponse
        from openhands.core.config import LLMConfig
        response = ModelResponse(model='dsec-test', choices=[dict(index=0,finish_reason='stop',
            message=dict(role='assistant',content='done'))],
            usage=dict(prompt_tokens=1,completion_tokens=1,total_tokens=2))
        with tempfile.TemporaryDirectory() as directory, \
                patch('openhands.llm.llm.litellm_completion', return_value=response) as provider:
            config = LLMConfig(model='openai/dsec-test', api_key='EMPTY',
                base_url='http://127.0.0.1:31026/v1', num_retries=0,
                max_input_tokens=65536,max_output_tokens=32768, timeout=600,
                input_cost_per_token=0,output_cost_per_token=0,disable_vision=True,caching_prompt=False)
            llm = instrument_llm(config, dict(native_tool_calling=True), Path(directory))
            result = llm.completion(messages=[dict(role='user',content='hello')])
            self.assertEqual(result.choices[0].message.content, 'done')
            self.assertEqual(provider.call_args.kwargs['max_retries'], 0)
            self.assertEqual(provider.call_args.kwargs['timeout'], 600)
            evidence = json.loads((Path(directory)/'0000.json').read_text())
            self.assertEqual(evidence['request']['max_retries'], 0)

    def test_parser_rejections_require_matching_completed_model_evidence(self):
        from openhands.controller.state.state import State
        from openhands.core.schema import AgentState
        tools = [dict(function=dict(name='execute_bash'))]
        call = dict(function=dict(name='invented_tool', arguments='{"command":"true"}'))
        record = dict(completed=1, response=dict(choices=[dict(message=dict(tool_calls=[call]))]))
        state = State(agent_state=AgentState.ERROR,last_error='Unknown tool call: invented_tool')
        self.assertEqual(policy_parse_failure(state, record, tools), 'unknown_tool')
        state.last_error = 'Model endpoint unavailable'
        self.assertIsNone(policy_parse_failure(state, record, tools))
        state.last_error = 'Unknown tool call: invented_tool'
        self.assertIsNone(policy_parse_failure(state, dict(record,error={'type':'Timeout'}), tools))
        self.assertIsNone(policy_parse_failure(state, dict(record,completed=None), tools))
        call['function'].update(name='execute_bash',arguments='{"command":')
        state.last_error = 'Failed to parse tool call arguments: {"command":'
        self.assertEqual(policy_parse_failure(state, record, tools), 'invalid_tool_arguments')
        call['function']['arguments'] = '{"command":"true"}'
        self.assertIsNone(policy_parse_failure(state, record, tools))

    def test_only_an_evidenced_upstream_policy_loop_is_eligible_for_grading(self):
        from openhands.controller.state.state import State
        from openhands.core.schema import AgentState
        from openhands.events.action import CmdRunAction
        from openhands.events.observation import CmdOutputObservation

        history = []
        for index in range(4):
            history.extend([CmdRunAction(command='grep fixed source.py'),
                CmdOutputObservation(command_id=index, command='grep fixed source.py', content='same', exit_code=0)])
        state = State(agent_state=AgentState.ERROR, history=history, last_error='Agent got stuck in a loop')
        self.assertTrue(policy_stuck(state))
        state.last_error = 'Model endpoint disconnected'
        self.assertFalse(policy_stuck(state))
        state.last_error = 'Agent got stuck in a loop'
        state.history = []
        self.assertFalse(policy_stuck(state))
        state.history = history
        state.agent_state = AgentState.FINISHED
        self.assertFalse(policy_stuck(state))
