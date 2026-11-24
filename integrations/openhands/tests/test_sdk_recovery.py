"""Replay a captured invalid tool response through the upstream Stop contract."""
import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from dsec_openhands.sdk_recovery import UnparsedToolRecovery, function_names


# Original response ffc0203306bc4bbf822c78efd80097ff, content preserved exactly.
CAPTURED_TEXT='\n\nI\'ve reproduced the issue. Now let me find the relevant code for the `--warn-return-any` flag.\n\n\n<function name="execute_bash"><param name="command"><![CDATA[cd /testbed && grep -r "warn-return-any" --include="*.py" | head -30]]></param></function>'


class RecoveryTests(unittest.TestCase):
    def event(self,text=CAPTURED_TEXT):
        return SimpleNamespace(model_dump=lambda **kw:dict(kind='MessageEvent',source='agent',
            llm_response_id='captured-response',llm_message=dict(content=[dict(type='text',text=text)])))

    def test_captured_reply_gets_feedback_without_alias_or_argument_execution(self):
        with tempfile.TemporaryDirectory() as root:
            guard=UnparsedToolRecovery(root,tool_names=lambda:['terminal','file_editor','finish','think'],limit=3)
            guard.on_event(self.event())
            decision=json.loads(guard.path.read_text())
            self.assertEqual(decision['decision'],'deny')
            self.assertIn('execute_bash',decision['reason'])
            self.assertIn('no operation',decision['reason'])
            self.assertNotIn('grep',decision['reason'])
            self.assertNotIn('execute_bash',guard.command)

    def test_final_text_and_fenced_or_inline_examples_are_not_recovered(self):
        for text in ['Done.','Example:\n```xml\n'+CAPTURED_TEXT+'\n```',
                     '`<function name="example">` is XML.']:
            self.assertEqual(function_names(dict(content=[dict(type='text',text=text)])),[])
        self.assertEqual(function_names(dict(content=[dict(type='text',text=CAPTURED_TEXT)],tool_calls=[{}])),[])

    def test_recovery_is_bounded_and_a_later_valid_action_clears_the_stop_decision(self):
        with tempfile.TemporaryDirectory() as root:
            guard=UnparsedToolRecovery(root,tool_names=lambda:['terminal'],limit=1)
            guard.on_event(self.event());self.assertEqual(guard.decision['decision'],'deny')
            guard.on_event(SimpleNamespace(model_dump=lambda **kw:dict(kind='ActionEvent',source='agent')))
            self.assertEqual(guard.decision['decision'],'allow')
            guard.on_event(self.event());self.assertEqual(guard.decision['decision'],'allow')
            self.assertFalse(guard.attempts[-1]['feedback_requested'])

    def test_failed_own_hook_is_infrastructure_failure(self):
        with tempfile.TemporaryDirectory() as root:
            guard=UnparsedToolRecovery(root,tool_names=lambda:['terminal'],limit=3)
            event=SimpleNamespace(model_dump=lambda **kw:dict(kind='HookExecutionEvent',
                hook_command=guard.command,success=False,exit_code=1,blocked=False))
            with self.assertRaisesRegex(RuntimeError,'cannot be scored'):
                guard.on_event(event)


try:
    import openhands.sdk
except ImportError:
    SDK=False
else:
    SDK=True


@unittest.skipUnless(SDK,'requires installed OpenHands SDK extra and integration package')
class NativeStopTests(unittest.TestCase):
    def assert_feedback_history(self,requests):
        self.assertEqual(len(requests),2)
        def content(message):
            value=message.get('content',[])
            return value if isinstance(value,str) else ''.join(c.get('text','') for c in value)
        self.assertTrue(any(CAPTURED_TEXT in content(m) for m in requests[1] if m['role']=='assistant'))
        self.assertTrue(any('no operation' in content(m) for m in requests[1] if m['role']=='user'))

    def run_conversation(self,*,asynchronous,limit=3,iterations=4,repeat=False):
        from dsec_openhands.sdk import create_conversation
        from dsec_openhands.sdk_llm import EndpointLLM
        from openhands.sdk.llm import Message, TextContent, MessageToolCall, LLMResponse
        from openhands.sdk.llm.utils.metrics import MetricsSnapshot
        from litellm import ModelResponse
        requests=[]
        def reply(llm,messages,**kwargs):
            requests.append(llm._to_chat_dicts(messages))
            if len(requests)==1 or repeat:
                message=Message(role='assistant',content=[TextContent(text=CAPTURED_TEXT)])
            else:
                message=Message(role='assistant',content=[],tool_calls=[MessageToolCall(
                    id='finish-call',name='finish',arguments='{"message":"done"}',origin='completion')])
            return LLMResponse(message=message,metrics=MetricsSnapshot(),raw_response=ModelResponse(id='reply-'+str(len(requests))))
        async def areply(llm,messages,**kwargs):
            return reply(llm,messages,**kwargs)
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);home=root/'home';home.mkdir()
            sandbox=Mock(cwd='/testbed')
            sandbox.guest_description=dict(tools={name:dict(description='guest',annotations={})
                for name in ('terminal','file_editor')})
            events=[]
            with (patch.dict(os.environ,HOME=str(home),OH_PERSISTENCE_DIR=str(home/'.openhands')),
                  patch('openhands.sdk.llm.llm_profile_store._DEFAULT_PROFILE_DIR',home/'.openhands/profiles'),
                  patch('openhands.sdk.plugin.discovery.USER_PLUGINS_DIRS',[])):
                c,binding=create_conversation(sandbox,EndpointLLM(model='openai/gpt-4o',api_key='EMPTY'),
                    metadata_dir=root/'controller',home_dir=home,max_iterations=iterations,
                    callbacks=[lambda e:events.append(e.model_dump(mode='json'))],
                    unparsed_tool_recovery='minicpm5',recovery_limit=limit)
                try:
                    c.send_message('Fix the repository.')
                    with patch.object(EndpointLLM,'acompletion' if asynchronous else 'completion',areply if asynchronous else reply):
                        asyncio.run(c.arun()) if asynchronous else c.run()
                    binding.check()
                    sandbox.action.assert_not_called()
                    self.assertEqual(c.state.execution_status.value,'finished')
                    self.assertTrue(any(e['kind']=='HookExecutionEvent' and e.get('blocked') for e in events))
                finally:c.close()
        return requests

    def test_sync_native_stop_preserves_assistant_and_adds_feedback_to_next_call(self):
        requests=self.run_conversation(asynchronous=False)
        self.assert_feedback_history(requests)

    def test_async_native_stop_uses_same_recovery_contract(self):
        requests=self.run_conversation(asynchronous=True)
        self.assert_feedback_history(requests)

    def test_repeated_invalid_replies_stop_at_recovery_budget(self):
        self.assertEqual(len(self.run_conversation(asynchronous=True,limit=1,repeat=True)),2)

    def test_recovery_does_not_restart_or_extend_iteration_budget(self):
        self.assertEqual(len(self.run_conversation(asynchronous=True,iterations=2,repeat=True)),2)

    def test_failed_native_stop_hook_aborts_instead_of_becoming_a_policy_finish(self):
        from openhands.sdk.hooks.executor import HookResult
        from openhands.sdk.conversation.exceptions import ConversationRunError
        with patch('openhands.sdk.hooks.executor.HookExecutor.execute_all',
                   return_value=[HookResult(success=False,exit_code=1)]):
            with self.assertRaises(ConversationRunError):
                self.run_conversation(asynchronous=True)


if __name__=='__main__':unittest.main()
