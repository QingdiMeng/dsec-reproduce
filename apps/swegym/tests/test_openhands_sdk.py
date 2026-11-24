import importlib.util
import json
import asyncio
import os
from pathlib import Path
import signal
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from dsec_swegym_case import openhands_sdk
from dsec.contracts.errors import RequestOutcomeUnknown


def sdk_available():
    try:
        return importlib.util.find_spec('openhands.sdk') is not None
    except ModuleNotFoundError:
        return False


class ControllerLaunchTests(unittest.TestCase):
    def test_sdk_suppressed_cancellation_still_records_episode_deadline(self):
        async def arun():
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                return  # The official SDK transitions to PAUSED this way.
        self.assertTrue(asyncio.run(openhands_sdk.run_with_deadline(Mock(arun=arun), 0)))
        async def provider_failure():
            raise TimeoutError('provider timeout before episode deadline')
        with self.assertRaisesRegex(TimeoutError, 'provider timeout'):
            asyncio.run(openhands_sdk.run_with_deadline(Mock(arun=provider_failure), 60))
    def test_iteration_and_episode_caps_are_scorable_but_infrastructure_errors_are_not(self):
        events = [dict(kind='ConversationErrorEvent', code='MaxIterationsReached')]
        self.assertEqual(openhands_sdk.policy_termination('error', events, False), 'iteration_limit')
        self.assertEqual(openhands_sdk.policy_termination('paused', [], True), 'episode_deadline')
        self.assertEqual(openhands_sdk.policy_termination('stuck', [], False), 'agent_stuck')
        for status, events in [('error', []), ('error', [dict(kind='ConversationErrorEvent', code='Other')]), ('paused', [])]:
            with self.assertRaises(RuntimeError):
                openhands_sdk.policy_termination(status, events, False)
    def test_total_window_reserves_the_requested_response_and_template_margin(self):
        self.assertEqual(openhands_sdk.token_budgets(dict(context_length=65536, response_length=32768)),
                         dict(context=65536, response=32768, safety=1024, input=31744))
        for options in (dict(context_length=32768), dict(response_length=True), dict(context_safety_tokens=0)):
            with self.assertRaises(ValueError):
                openhands_sdk.token_budgets(options)
    def test_existing_thinking_profiles_keep_their_provider_history_contract(self):
        self.assertEqual(openhands_sdk.thinking_policy(dict(sampling_extra=dict(
            chat_template_kwargs=dict(enable_thinking=True)))), 'current_user_turn')
        self.assertEqual(openhands_sdk.thinking_policy(dict(sampling_extra=dict(
            thinking=dict(type='enabled')))), 'all_assistant_turns')
        self.assertEqual(openhands_sdk.thinking_policy({}), 'disabled')
        with self.assertRaises(ValueError):
            openhands_sdk.thinking_policy(dict(sampling_extra=dict(
                chat_template_kwargs=dict(enable_thinking=True), thinking=dict(type='enabled'))))
    def test_personal_home_and_persistence_cannot_leak_into_controller_child(self):
        with tempfile.TemporaryDirectory() as root:
            def child(argv, *, env, cwd):
                self.assertEqual(Path(env['HOME']), Path(cwd))
                self.assertEqual(env['OH_PERSISTENCE_DIR'], str(Path(cwd) / '.openhands'))
                self.assertEqual(list(Path(cwd).iterdir()), [])
                self.assertTrue(all(Path(argv[index + 1]).is_absolute() for index in (3, 5, 7)))
                process = Mock()
                process.wait.return_value = 0
                return process
            with (patch.dict(os.environ, HOME='/personal-home', OH_PERSISTENCE_DIR='/personal-plugins'),
                  patch.object(sys, 'argv', ['dsec-swegym-openhands', '--task', 'task.json',
                                            '--config', 'config.json', '--out', str(Path(root) / 'out')]),
                  patch.object(openhands_sdk.subprocess, 'Popen', side_effect=child) as process):
                with self.assertRaises(SystemExit) as outcome:
                    openhands_sdk.main()
                self.assertEqual(outcome.exception.code, 0)
                self.assertEqual(process.call_count, 1)
                self.assertEqual(os.environ['HOME'], '/personal-home')

    def test_parent_forwards_stop_and_waits_for_child_cleanup_before_returning(self):
        handlers = {}
        process = Mock()
        process.poll.return_value = None
        def install(signum, handler):
            previous = handlers.get(signum, signal.SIG_DFL)
            handlers[signum] = handler
            return previous
        def wait():
            handlers[signal.SIGTERM](signal.SIGTERM, None)
            process.send_signal.assert_called_once_with(signal.SIGTERM)
            return 143
        process.wait.side_effect = wait
        with (patch.object(sys, 'argv', ['dsec-swegym-openhands', '--task', 'task.json',
                                        '--config', 'config.json', '--out', 'out']),
              patch.object(openhands_sdk.subprocess, 'Popen', return_value=process),
              patch.object(openhands_sdk.signal, 'signal', side_effect=install)):
            with self.assertRaises(SystemExit) as outcome:
                openhands_sdk.main()
            self.assertEqual(outcome.exception.code, 143)
            self.assertEqual(handlers[signal.SIGTERM], signal.SIG_DFL)


@unittest.skipUnless(sdk_available(), 'requires the isolated SDK extra')
class SDKFailureEvidenceTests(unittest.TestCase):
    def test_endpoint_sampling_capability_reaches_real_sdk_request_options(self):
        from openhands.sdk.llm.options.chat_options import select_chat_options
        task = dict(instance_id='python__mypy-15413', repo='python/mypy', version='1.4',
            base_commit='a' * 40, problem_statement='Public issue', hints_text='', patch='',
            test_patch='hidden test patch', FAIL_TO_PASS=['test'], PASS_TO_PASS=[])
        config = dict(api_config='api.json', spec=dict(environment_id='task'), resource_demand={},
            openhands=dict(runtime={}, model='openai/local-endpoint', base_url='http://localhost:31026/v1',
                temperature=.9, top_p=.95, capability_overrides=dict(supports_sampling_params=True),
                sampling_extra=dict(top_k=20, chat_template_kwargs=dict(enable_thinking=True))))
        sandbox = Mock(id='test-instance', cwd='/testbed', cleanup='stopped', operations=[], capabilities=[], startup_evidence={})
        sandbox.run_shell.return_value = dict(exit_code=0, timed_out=False, truncated=False,
            output=task['base_commit'] + '\n1000\n')
        with tempfile.TemporaryDirectory() as root:
            with (patch('dsec_openhands.sdk.SDKSandbox', return_value=sandbox),
                  patch('dsec_openhands.sdk.create_conversation', side_effect=RuntimeError('capture constructor')) as create,
                  patch.object(openhands_sdk, 'verify', new_callable=AsyncMock) as verify):
                with self.assertRaises(RuntimeError):
                    openhands_sdk.run(task, config, Path(root) / 'out', home_dir=root)
                llm = create.call_args.args[1]
                self.assertTrue(llm._model_features().supports_sampling_params)
                actual = select_chat_options(llm, {}, has_tools=True)
                self.assertEqual((actual['temperature'], actual['top_p']), (.9, .95))
                verify.assert_not_called()
                sandbox.close.assert_called_once()

    def test_unknown_sandbox_creation_keeps_evidence_and_is_never_scored(self):
        task = dict(instance_id='python__mypy-15413', repo='python/mypy', version='1.4', base_commit='a' * 40,
            problem_statement='Public issue', hints_text='private hint', patch='private gold',
            test_patch='private tests', FAIL_TO_PASS=['test'], PASS_TO_PASS=[])
        config = dict(api_config='api.json', spec=dict(environment_id='task'), resource_demand={},
            command_timeout_ms=30000, openhands=dict(runtime=dict(transport_timeout_ms=120000)))
        sandbox = Mock(id='pending', cleanup='unknown', operations=[dict(kind='create')])
        sandbox.connect.side_effect = RequestOutcomeUnknown('create result incomplete')
        sandbox.close.side_effect = RequestOutcomeUnknown('retirement incomplete')
        with tempfile.TemporaryDirectory() as root:
            out = Path(root) / 'out'
            with (patch('dsec_openhands.sdk.SDKSandbox', return_value=sandbox),
                  patch.object(openhands_sdk, 'verify', new_callable=AsyncMock) as verify):
                with self.assertRaises(RuntimeError):
                    openhands_sdk.run(task, config, out, home_dir=root)
                verify.assert_not_called()
                report = json.loads((out / 'report.json').read_text())
                self.assertEqual((report['status'], report['score'], report['cleanup']), ('error', None, 'unknown'))
                self.assertEqual(report['error']['type'], 'RequestOutcomeUnknown')
                self.assertNotIn('private', (out / 'task-input.json').read_text())
                self.assertFalse((out / 'solution.patch').exists())
