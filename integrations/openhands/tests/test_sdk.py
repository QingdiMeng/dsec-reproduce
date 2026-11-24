"""Pinned-SDK contract tests; never start a task executor on the controller."""
import json
import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import uuid

try:
    import openhands.sdk
except ImportError:
    SDK = False
else:
    SDK = True


@unittest.skipUnless(SDK, 'requires the isolated [sdk] extra')
class SDKTests(unittest.TestCase):
    def test_async_unknown_tool_failure_aborts_official_loop_without_worker_pause_or_next_model_call(self):
        from dsec_openhands.sdk import create_conversation
        from dsec_openhands.sdk_llm import EndpointLLM
        from openhands.sdk.llm import Message, MessageToolCall, LLMResponse
        from openhands.sdk.llm.utils.metrics import MetricsSnapshot
        from openhands.sdk.conversation.exceptions import ConversationRunError
        from litellm import ModelResponse
        response = LLMResponse(message=Message(role='assistant', content=[], tool_calls=[MessageToolCall(
            id='unknown-once', name='terminal', arguments='{"command":"one-effect"}', origin='completion')]),
            metrics=MetricsSnapshot(), raw_response=ModelResponse())
        with tempfile.TemporaryDirectory() as root:
            home = Path(root) / 'home'; home.mkdir()
            sandbox = Mock(cwd='/testbed')
            sandbox.guest_description = dict(tools={name: dict(description='guest', annotations={})
                for name in ('terminal', 'file_editor')})
            sandbox.action.side_effect = TimeoutError('outcome unknown')
            with (patch.dict(os.environ, HOME=str(home), OH_PERSISTENCE_DIR=str(home / '.openhands')),
                  patch('openhands.sdk.llm.llm_profile_store._DEFAULT_PROFILE_DIR', home / '.openhands/profiles'),
                  patch('openhands.sdk.plugin.discovery.USER_PLUGINS_DIRS', [])):
                conversation, binding = create_conversation(sandbox,
                    EndpointLLM(model='openai/gpt-4o', api_key='EMPTY'), metadata_dir=Path(root) / 'controller',
                    home_dir=home, max_iterations=3)
                try:
                    conversation.send_message('Run once.')
                    with (patch.object(EndpointLLM, 'acompletion', return_value=response) as completion,
                          patch.object(type(conversation), 'pause', side_effect=AssertionError('worker must not take state lock')) as pause):
                        with self.assertRaises(ConversationRunError):
                            asyncio.run(asyncio.wait_for(conversation.arun(), 2))
                        self.assertEqual(completion.call_count, 1)
                        pause.assert_not_called()
                    self.assertEqual(sandbox.action.call_count, 1)
                    self.assertIsInstance(binding.failure, TimeoutError)
                finally:
                    conversation.close()

    def test_unsupported_tool_timeout_is_a_recoverable_observation_without_execution(self):
        from dsec_openhands.sdk_guest import SDKTools
        from openhands.tools.terminal.definition import TerminalAction, TerminalObservation
        tools = SDKTools.__new__(SDKTools)
        executor = Mock()
        tools.tools = dict(terminal=SimpleNamespace(action_type=TerminalAction, executor=executor))
        result = TerminalObservation.model_validate(tools.dispatch('terminal',
            dict(action=dict(command='must-not-start', timeout=180)), 45))
        self.assertTrue(result.is_error)
        self.assertIn('No command was executed', result.text)
        result = TerminalObservation.model_validate(tools.dispatch('terminal',
            dict(action=dict(command='C-c', reset=True, is_input=True)), 45))
        self.assertTrue(result.is_error)
        self.assertIn('session was not reset', result.text)
        executor.assert_not_called()

    def test_official_conversation_executes_remote_tool_and_keeps_feedback_in_next_turn(self):
        from dsec_openhands.sdk import create_conversation
        from dsec_openhands.sdk_llm import EndpointLLM
        from openhands.sdk.llm import Message, MessageToolCall, TextContent, LLMResponse
        from openhands.sdk.llm.utils.metrics import MetricsSnapshot
        from litellm import ModelResponse
        from openhands.tools.terminal.definition import TerminalObservation
        replies = [Message(role='assistant', content=[], reasoning_content='locate source', tool_calls=[
            MessageToolCall(id='call-first', name='terminal', arguments=json.dumps(dict(command='pwd')),
                            origin='completion')]),
            Message(role='assistant', content=[TextContent(text='done')], tool_calls=[
                MessageToolCall(id='call-finish', name='finish', arguments=json.dumps(dict(message='done')),
                                origin='completion')])]
        requests = []
        def complete(llm, messages, **kwargs):
            requests.append(llm._to_chat_dicts(messages))
            message = replies.pop(0)
            return LLMResponse(message=message, metrics=MetricsSnapshot(), raw_response=ModelResponse())
        with tempfile.TemporaryDirectory() as root:
            home = Path(root) / 'home'; home.mkdir()
            sandbox = Mock(cwd='/testbed')
            sandbox.guest_description = dict(tools={name: dict(description='guest /testbed', annotations={})
                for name in ('terminal', 'file_editor')})
            sandbox.action.return_value = TerminalObservation.from_text('unique-guest-feedback', command='pwd',
                exit_code=0).model_dump(mode='json')
            with (patch.dict(os.environ, HOME=str(home), OH_PERSISTENCE_DIR=str(home / '.openhands')),
                  patch('openhands.sdk.llm.llm_profile_store._DEFAULT_PROFILE_DIR', home / '.openhands/profiles'),
                  patch('openhands.sdk.plugin.discovery.USER_PLUGINS_DIRS', [])):
                conversation, binding = create_conversation(sandbox,
                    EndpointLLM(model='openai/gpt-4o', api_key='EMPTY', thinking_history='current_user_turn'),
                    metadata_dir=Path(root) / 'controller', home_dir=home, max_iterations=3)
                try:
                    with patch.object(EndpointLLM, 'completion', complete):
                        conversation.send_message('Inspect the prepared repository.')
                        conversation.run()
                    binding.check()
                    self.assertEqual(conversation.state.execution_status.value, 'finished')
                    self.assertEqual(sandbox.action.call_count, 1)
                    tool = next(value for value in requests[1] if value['role'] == 'tool')
                    self.assertEqual(tool['tool_call_id'], 'call-first')
                    self.assertIn('unique-guest-feedback', str(tool['content']))
                    assistant = next(value for value in requests[1] if value['role'] == 'assistant')
                    self.assertEqual(assistant['reasoning_content'], 'locate source')
                finally:
                    conversation.close()

    def test_native_schemas_and_remote_executor_never_construct_host_tools(self):
        from dsec_openhands.sdk import ControllerWorkspace, Binding, DSecTerminal, DSecFileEditor
        from openhands.tools.terminal.definition import TerminalAction, TerminalObservation
        from openhands.tools.file_editor.definition import FileEditorAction, FileEditorObservation
        with tempfile.TemporaryDirectory() as directory:
            workspace = ControllerWorkspace(working_dir=directory)
            backend = Mock()
            backend.guest_description = dict(tools={name: dict(description='guest /testbed', annotations={})
                for name in ('terminal', 'file_editor')})
            workspace._binding = Binding(backend)
            state = SimpleNamespace(workspace=workspace)
            with (patch('openhands.tools.terminal.impl.TerminalExecutor', side_effect=AssertionError('host terminal')),
                  patch('openhands.tools.file_editor.impl.FileEditorExecutor', side_effect=AssertionError('host editor'))):
                terminal = DSecTerminal.create(state)[0]
                editor = DSecFileEditor.create(state)[0]
            self.assertEqual(terminal.name, 'terminal')
            self.assertEqual(editor.name, 'file_editor')
            action = TerminalAction(command='touch /testbed/marker', timeout=3)
            value = TerminalObservation.from_text('guest output', command=action.command, exit_code=7)
            backend.action.return_value = value.model_dump(mode='json')
            self.assertEqual(terminal.executor(action), value)
            backend.action.assert_called_once_with('terminal', action=action.model_dump(mode='json'))
            backend.action.return_value = FileEditorObservation.from_text('edited', command='create').model_dump(mode='json')
            editor.executor(FileEditorAction(command='create', path='/testbed/file', file_text='x'))
            for method in (workspace.execute_command, workspace.file_upload, workspace.file_download,
                           workspace.git_changes, workspace.git_diff):
                with self.assertRaises(RuntimeError):
                    method('/must-not-execute')

    def test_transport_failure_fences_workers_and_aborts_once_without_taking_controller_locks(self):
        from dsec_openhands.sdk import Binding, RemoteExecutor
        from openhands.tools.terminal.definition import TerminalAction, TerminalObservation
        backend = Mock()
        backend.action.side_effect = TimeoutError('unknown mutation')
        binding = Binding(backend)
        executor = RemoteExecutor(binding, 'terminal', TerminalObservation)
        conversation = Mock()
        with self.assertRaises(TimeoutError):
            executor(TerminalAction(command='one-side-effect'), conversation)
        conversation.pause.assert_not_called()
        with self.assertRaises(RuntimeError):
            binding.on_event(object())
        binding.on_event(object())  # Error/cleanup callbacks can still persist.
        with self.assertRaises(RuntimeError):
            executor(TerminalAction(command='must-not-run'), conversation)
        self.assertEqual(backend.action.call_count, 1)

    def test_interrupt_does_not_wait_for_action_lock(self):
        from dsec_openhands.sdk import Binding, RemoteExecutor
        binding = Binding(Mock())
        with binding.lock:
            RemoteExecutor(binding, 'terminal', object).interrupt()
        binding.sandbox.action.assert_called_once_with('interrupt')

    def test_thinking_serializer_preserves_sdk_content_and_correct_scope(self):
        from dsec_openhands.sdk_llm import EndpointLLM
        from openhands.sdk.llm import Message, TextContent
        messages = [Message(role='user', content=[TextContent(text='first')]),
            Message(role='assistant', content=[TextContent(text='one')], reasoning_content='old'),
            Message(role='user', content=[TextContent(text='second')]),
            Message(role='assistant', content=[TextContent(text='two')], reasoning_content='current')]
        for scope, expected in [('current_user_turn', [None, 'current']),
                                ('all_assistant_turns', ['old', 'current'])]:
            llm = EndpointLLM(model='openai/local-qwen', api_key='EMPTY', thinking_history=scope,
                              caching_prompt=False, disable_vision=True)
            result = llm._to_chat_dicts(messages)
            self.assertEqual([result[i].get('reasoning_content') for i in (1, 3)], expected)
            self.assertEqual([result[i]['content'] for i in (1, 3)],
                             [[dict(type='text', text='one')], [dict(type='text', text='two')]])
        self.assertEqual(messages[1].reasoning_content, 'old')

    def test_sdk_detector_does_not_omit_third_item_in_four_action_streak(self):
        from openhands.sdk.conversation.stuck_detector import StuckDetector
        from openhands.sdk.event import ActionEvent, ObservationEvent
        from openhands.sdk.llm import MessageToolCall
        from openhands.tools.terminal.definition import TerminalAction, TerminalObservation
        detector = StuckDetector(Mock())
        def action(command):
            identity = uuid.uuid4().hex
            call = MessageToolCall(id=identity, name='terminal', arguments=json.dumps(dict(command=command)), origin='completion')
            return ActionEvent(thought=[], action=TerminalAction(command=command), tool_name='terminal',
                               tool_call_id=identity, tool_call=call, llm_response_id=uuid.uuid4().hex)
        actions = [action(command) for command in ('A', 'A', 'B', 'A')]
        observations = [ObservationEvent(observation=TerminalObservation.from_text('same output', command='', exit_code=0),
            tool_name='terminal', tool_call_id=item.tool_call_id, action_id=item.id) for item in actions]
        self.assertFalse(detector._is_stuck_repeating_action_observation(actions, observations))
        actions[2] = action('A')
        self.assertTrue(detector._is_stuck_repeating_action_observation(actions, observations))

    def test_request_journal_retains_unknown_and_interrupt_bypasses_busy_executor(self):
        from dsec_openhands.sdk_guest import SDKTools
        tools = SDKTools.__new__(SDKTools)
        tools.shell = tools
        tools.cwd = '/testbed'
        tools.epoch = uuid.uuid4().hex
        tools.ceiling = 30
        tools.lock = threading.Lock()
        tools.results = {}
        tools.interrupt_lock = threading.Lock()
        tools.interrupt_results = {}
        executor = Mock()
        tools.tools = dict(terminal=SimpleNamespace(executor=executor))
        request = dict(id=uuid.uuid4().hex, epoch=tools.epoch, kind='terminal', timeout_seconds=30, action={})
        with patch.object(tools, 'dispatch', side_effect=RuntimeError('lost response')) as dispatch:
            with self.assertRaises(RuntimeError):
                tools.execute(request)
            self.assertFalse(tools.execute(request)['ok'])
            self.assertEqual(dispatch.call_count, 1)
        interrupt = dict(id=uuid.uuid4().hex, epoch=tools.epoch, kind='interrupt')
        with tools.lock:
            self.assertTrue(tools.interrupt(interrupt)['ok'])
            tools.interrupt(interrupt)
        executor.session.terminal.send_keys.assert_called_once_with('C-c', enter=False)
