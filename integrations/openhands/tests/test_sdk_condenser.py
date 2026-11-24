"""Regressions for evidence lost by the pinned upstream display serializer."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

try:
    import openhands.sdk
except ImportError:
    SDK = False
else:
    SDK = True


@unittest.skipUnless(SDK, 'requires the isolated [sdk] extra')
class CondenserTests(unittest.TestCase):
    def setUp(self):
        from openhands.sdk.context.prompts.prompt import _get_env
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        cache = patch('openhands.sdk.context.prompts.prompt.get_user_persistence_dir', return_value=Path(home.name))
        cache.start()
        self.addCleanup(cache.stop)
        _get_env.cache_clear()
        self.addCleanup(_get_env.cache_clear)
        from dsec_openhands.sdk_condenser import evidence_condenser
        from dsec_openhands.sdk_llm import EndpointLLM
        self.llm = EndpointLLM(model='openai/local-qwen', api_key='EMPTY',
            max_input_tokens=31744, max_output_tokens=32768,
            thinking_history='current_user_turn', temperature=.6, top_p=.95)
        self.condenser = evidence_condenser(self.llm)

    def events(self):
        from openhands.sdk.event import ActionEvent, ObservationEvent, AgentErrorEvent
        from openhands.sdk.llm import MessageToolCall, TextContent
        from openhands.tools.terminal.definition import TerminalAction, TerminalObservation
        command = "git show HEAD -- mypy/checker.py"
        call = MessageToolCall(id='history-read', name='terminal',
            arguments=json.dumps(dict(command=command)), origin='completion')
        action = ActionEvent(thought=[TextContent(text='visible plan')],
            reasoning_content='Read a historical diff; this is NOT an applied source edit.',
            action=TerminalAction(command=command), tool_name='terminal',
            tool_call_id=call.id, tool_call=call, llm_response_id='model-reply')
        observation = ObservationEvent(tool_name='terminal', tool_call_id=call.id, action_id=action.id,
            observation=TerminalObservation.from_text('header\n' + 'x' * 1500 +
                '\nUNIQUE_TAIL: and not is_lambda\n', command=command, exit_code=17))
        error = AgentErrorEvent(tool_name='shell', tool_call_id='rejected-edit',
            error="Tool 'shell' not found; no edit executed")
        return [action, observation, error]

    @staticmethod
    def response(text='Observed history read only; worktree changes unknown.'):
        from openhands.sdk.llm import Message, TextContent
        return SimpleNamespace(id='summary-reply', message=Message(role='assistant',
            content=[TextContent(text=text)]))

    def test_full_native_records_preserve_arguments_thinking_tail_status_and_errors(self):
        events = self.events()
        self.assertNotIn('UNIQUE_TAIL', str(events[1]))  # Reproduces upstream loss.
        self.assertNotIn('git show HEAD', str(events[0]))
        records = self.condenser._records(events)
        self.assertEqual([json.loads(record) for record in records],
            [event.model_dump(mode='json', exclude_none=True) for event in events])
        self.assertIn('UNIQUE_TAIL', records[1])
        self.assertEqual(json.loads(records[1])['observation']['exit_code'], 17)
        self.assertIn('NOT an applied source edit', records[0])
        self.assertIn('no edit executed', records[2])

    def test_sync_and_async_use_identical_complete_inputs_even_with_hard_reset_preview_limit(self):
        from dsec_openhands.sdk_llm import EndpointLLM
        events = self.events()
        requests = []
        def generate(llm, *, messages, store):
            requests.append(llm._to_chat_dicts(messages))
            self.assertFalse(store)
            return self.response()
        async def agenerate(llm, **kwargs):
            return generate(llm, **kwargs)
        with (patch.object(EndpointLLM, 'generate', generate),
              patch.object(EndpointLLM, 'agenerate', agenerate)):
            sync = self.condenser._generate_condensation(events, 4, max_event_str_length=500)
            boundary = len(requests)
            asynchronous = asyncio.run(self.condenser._agenerate_condensation(events, 4, max_event_str_length=500))
        self.assertEqual(requests[:boundary], requests[boundary:])
        payload = str(requests[:boundary])
        for text in ('git show HEAD', 'UNIQUE_TAIL', 'reasoning_content', 'exit_code', 'rejected-edit'):
            self.assertIn(text, payload)
        for result in (sync, asynchronous):
            self.assertEqual(result.forgotten_event_ids, {event.id for event in events})
            self.assertEqual(result.summary_offset, 4)
            self.assertEqual(result.llm_response_id, 'summary-reply')
        self.assertIn('does NOT modify', payload)

    def test_default_triggers_and_model_sampling_do_not_change(self):
        from openhands.sdk.context.condenser import default_condenser
        upstream = default_condenser(self.llm)
        self.assertEqual(upstream.model_dump(exclude={'llm', 'kind'}),
                         self.condenser.model_dump(exclude={'llm', 'kind', 'task_instruction'}))
        for field in ('model', 'temperature', 'top_p', 'max_input_tokens', 'max_output_tokens', 'thinking_history'):
            self.assertEqual(getattr(self.llm, field), getattr(self.condenser.llm, field))

    def test_actual_sdk_condensation_replaces_only_selected_events_in_next_context(self):
        from openhands.sdk.event import MessageEvent
        from openhands.sdk.event.base import LLMConvertibleEvent
        events = self.events()
        from openhands.sdk.llm import Message, TextContent
        instruction = MessageEvent(source='user', llm_message=Message(role='user',
            content=[TextContent(text='Fix warn-return-any on lambdas.')]))
        recent = MessageEvent(source='user', llm_message=Message(role='user',
            content=[TextContent(text='Keep recent evidence.')]))
        result = self.condenser._result(events, 1, self.response('Read history; no observed source edits.'))
        view = result.apply([instruction, *events, recent])
        messages = LLMConvertibleEvent.events_to_messages(view)
        payload = str(self.llm._to_chat_dicts(messages))
        self.assertIn('Fix warn-return-any on lambdas.', payload)
        self.assertIn('no observed source edits', payload)
        self.assertIn('Keep recent evidence.', payload)
        self.assertEqual(len(view), 3)

    def test_oversized_unicode_record_is_fragmented_without_dropping_a_character(self):
        from dsec_openhands.sdk_condenser import EvidenceSummarizingCondenser
        record = json.dumps(dict(kind='ObservationEvent', output='头🙂尾' * 2000), ensure_ascii=False)
        with patch.object(EvidenceSummarizingCondenser, '_fits', lambda _, records: sum(map(len, records)) < 1300):
            batches = list(self.condenser._batches([record]))
        fragments = [json.loads(batch[0]) for batch in batches]
        self.assertGreater(len(fragments), 1)
        self.assertEqual(''.join(part['text'] for part in fragments), record)
        offset = 0
        for part in fragments:
            self.assertEqual(part['offset'], offset)
            self.assertEqual(part['record_sha256'], hashlib.sha256(record.encode()).hexdigest())
            offset = part['end']
        self.assertEqual(offset, len(record))

    def test_every_over_budget_chunk_is_summarized_and_merged_in_both_modes(self):
        from dsec_openhands.sdk_condenser import EvidenceSummarizingCondenser
        from dsec_openhands.sdk_llm import EndpointLLM
        events = self.events()
        for asynchronous in (False, True):
            requests = []
            def generate(llm, *, messages, store):
                requests.append(str(llm._to_chat_dicts(messages)))
                return self.response('short evidence summary')
            async def agenerate(llm, **kwargs):
                return generate(llm, **kwargs)
            with (patch.object(EvidenceSummarizingCondenser, '_fits', lambda _, records: sum(map(len, records)) < 1600),
                  patch.object(EndpointLLM, 'generate', generate),
                  patch.object(EndpointLLM, 'agenerate', agenerate)):
                if asynchronous:
                    result = asyncio.run(self.condenser._agenerate_condensation(events, 4))
                else:
                    result = self.condenser._generate_condensation(events, 4)
            self.assertGreater(len(requests), 2)
            self.assertIn('UNIQUE_TAIL', ''.join(requests))
            self.assertIn('partial_evidence_summary', requests[-1])
            self.assertEqual(result.forgotten_event_ids, {event.id for event in events})

    def test_empty_summaries_are_not_accepted_as_memory(self):
        from openhands.sdk.context.condenser.base import NoCondensationAvailableException
        with self.assertRaisesRegex(NoCondensationAvailableException, 'Empty'):
            self.condenser._result(self.events(), 4, self.response('  '))

    def test_recovery_reduces_request_budget_but_reconstructs_every_original_record(self):
        records = self.condenser._records(self.events())
        limits = []
        for cap in (500, 250):
            limit = self.condenser._recovery_input_limit(records, cap)
            limits.append(limit)
            restored = {}
            fragments = {}
            for batch in self.condenser._batches(records, input_limit=limit):
                self.assertLessEqual(self.llm.get_token_count(self.condenser._messages(batch)), limit)
                for record in batch:
                    value = json.loads(record)
                    if value.get('kind') == 'event_json_fragment':
                        fragments.setdefault(value['record_sha256'], []).append(value)
                    else:
                        restored[hashlib.sha256(record.encode()).hexdigest()] = record
            for digest, parts in fragments.items():
                parts.sort(key=lambda p:p['offset'])
                offset = 0
                for part in parts:
                    self.assertEqual(part['offset'], offset)
                    offset = part['end']
                restored[digest] = ''.join(part['text'] for part in parts)
            self.assertEqual(restored, {hashlib.sha256(r.encode()).hexdigest():r for r in records})
        self.assertLess(limits[1], limits[0])
        self.assertLess(limits[0], self.llm.max_input_tokens)

    def test_empty_first_chunk_stops_before_generating_later_chunks(self):
        from dsec_openhands.sdk_condenser import EvidenceSummarizingCondenser
        from dsec_openhands.sdk_llm import EndpointLLM
        from openhands.sdk.context.condenser.base import NoCondensationAvailableException
        for asynchronous in (False, True):
            requests = []
            def generate(llm, **kwargs):
                requests.append(kwargs)
                return self.response(' ')
            async def agenerate(llm, **kwargs):
                return generate(llm, **kwargs)
            with (patch.object(EvidenceSummarizingCondenser, '_fits', lambda _, records: sum(map(len, records)) < 1600),
                  patch.object(EndpointLLM, 'generate', generate),
                  patch.object(EndpointLLM, 'agenerate', agenerate)):
                with self.assertRaisesRegex(NoCondensationAvailableException, 'Empty'):
                    if asynchronous:
                        asyncio.run(self.condenser._agenerate_condensation(self.events(), 4))
                    else:
                        self.condenser._generate_condensation(self.events(), 4)
            self.assertEqual(len(requests), 1)

    def test_retained_task_is_present_in_every_chunk_and_merge_without_forgetting_it(self):
        from dsec_openhands.sdk_condenser import EvidenceSummarizingCondenser
        from dsec_openhands.sdk_llm import EndpointLLM
        self.condenser.task_instruction = 'FIX FALSE POSITIVE: lambda must NOT warn; leave normal function warnings intact.'
        requests = []
        events = self.events()
        async def summarize(llm, *, messages, store):
            requests.append(llm._to_chat_dicts(messages))
            return self.response('Observed investigation only.')
        with (patch.object(EvidenceSummarizingCondenser, '_fits', lambda _, records: sum(map(len, records)) < 1600),
              patch.object(EndpointLLM, 'agenerate', summarize)):
            result = asyncio.run(self.condenser._agenerate_condensation(events, 4))
        self.assertGreater(len(requests), 2)
        for request in requests:
            payload = str(request)
            self.assertIn('retained_original_task', payload)
            self.assertIn(self.condenser.task_instruction, payload)
        self.assertEqual(result.forgotten_event_ids, {event.id for event in events})

    def test_official_async_loop_receives_summary_and_retains_current_tool_chain(self):
        from dsec_openhands.sdk import create_conversation
        from dsec_openhands.sdk_llm import EndpointLLM
        from openhands.sdk.llm import Message, MessageToolCall, LLMResponse
        from openhands.sdk.llm.utils.metrics import MetricsSnapshot
        from openhands.tools.terminal.definition import TerminalObservation
        from litellm import ModelResponse
        agent_requests, summary_requests, events = [], [], []
        async def complete(llm, messages, **kwargs):
            agent_requests.append(llm._to_chat_dicts(messages))
            index = len(agent_requests)
            name = 'terminal' if index <= 10 else 'finish'
            arguments = dict(command=f'read-{index}') if name == 'terminal' else dict(message='done')
            return LLMResponse(message=Message(role='assistant', content=[], reasoning_content='Read, not edit.',
                tool_calls=[MessageToolCall(id=f'call-{index}', name=name,
                    arguments=json.dumps(arguments), origin='completion')]),
                metrics=MetricsSnapshot(), raw_response=ModelResponse())
        async def summarize(llm, *, messages, **kwargs):
            if 'tools' in kwargs:
                return await complete(llm, messages, **kwargs)
            summary_requests.append(llm._to_chat_dicts(messages))
            return self.response('Verified reads only; no source edit observed. summary-marker')
        with tempfile.TemporaryDirectory() as root:
            home = Path(root) / 'home'
            home.mkdir()
            sandbox = Mock(cwd='/testbed')
            sandbox.guest_description = dict(tools={name: dict(description='guest', annotations={})
                for name in ('terminal', 'file_editor')})
            sandbox.action.return_value = TerminalObservation.from_text('x' * 1500 + 'LIVE_TAIL',
                command='read', exit_code=0).model_dump(mode='json')
            with (patch.dict(os.environ, HOME=str(home), OH_PERSISTENCE_DIR=str(home / '.openhands')),
                  patch('openhands.sdk.llm.llm_profile_store._DEFAULT_PROFILE_DIR', home / '.openhands/profiles'),
                  patch('openhands.sdk.plugin.discovery.USER_PLUGINS_DIRS', [])):
                condenser = self.condenser.model_copy(update={'max_size': 14})
                conversation, binding = create_conversation(sandbox, self.llm,
                    metadata_dir=Path(root) / 'controller', home_dir=home, max_iterations=20,
                    condenser=condenser, callbacks=[lambda event: events.append(event)])
                try:
                    conversation.send_message('Inspect without editing.')
                    with patch.object(EndpointLLM, 'agenerate', summarize):
                        asyncio.run(asyncio.wait_for(conversation.arun(), 5))
                    binding.check()
                    self.assertEqual(conversation.state.execution_status.value, 'finished')
                    self.assertEqual(sandbox.action.call_count, 10)
                    self.assertTrue(any(event.kind == 'Condensation' for event in events))
                    self.assertTrue(summary_requests)
                    self.assertIn('LIVE_TAIL', str(summary_requests))
                    self.assertIn('Read, not edit.', str(summary_requests))
                    self.assertIn('summary-marker', str(agent_requests[-1]))
                    self.assertIn('Inspect without editing.', str(agent_requests[-1]))
                    self.assertTrue(any(message.get('tool_call_id') == 'call-10' for message in agent_requests[-1]))
                finally:
                    conversation.close()
