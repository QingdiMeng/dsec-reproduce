"""Opt-in real SDK executors in a DSec API-owned Linux sandbox."""
import json
import asyncio
import os
import shlex
from pathlib import Path
import threading
import time
import traceback
import unittest
from unittest.mock import patch


@unittest.skipUnless(os.environ.get('DSEC_OPENHANDS_SDK_CONFIG'), 'requires the real SDK guest environment')
class SDKLinuxTests(unittest.TestCase):
    def test_async_agent_cancellation_interrupts_admitted_tool_before_retirement(self):
        from dsec_openhands.sdk import SDKSandbox, create_conversation
        from dsec_openhands.sdk_llm import EndpointLLM
        from dsec.contracts.rpc import ServiceError
        from openhands.sdk.llm import Message, MessageToolCall, LLMResponse
        from openhands.sdk.llm.utils.metrics import MetricsSnapshot
        from litellm import ModelResponse
        config = json.loads(Path(os.environ['DSEC_OPENHANDS_SDK_CONFIG']).read_text())
        out = Path(os.environ['DSEC_OPENHANDS_SDK_OUT'] + '-cancel')
        out.mkdir(mode=0o700, parents=True, exist_ok=False)
        box = SDKSandbox(config)
        conversation = None
        report = dict(status='running')
        async def complete(*args, **kwargs):
            return LLMResponse(message=Message(role='assistant', content=[], tool_calls=[MessageToolCall(
                id='controlled-cancel', name='terminal', origin='completion', arguments=json.dumps(dict(
                    command='printf admitted > /tmp/sdk-agent-cancel-ready; sleep 30', timeout=40)))]),
                metrics=MetricsSnapshot(), raw_response=ModelResponse())
        async def drive():
            running = asyncio.create_task(conversation.arun())
            try:
                for _ in range(200):
                    if running.done():
                        await running
                        self.fail('Agent ended before the controlled admission barrier')
                    try:
                        if await asyncio.to_thread(box.read_file, '/tmp/sdk-agent-cancel-ready') == b'admitted':
                            break
                    except ServiceError as exc:
                        if exc.kind != 'FileNotFoundError':
                            raise
                    await asyncio.sleep(.05)
                else:
                    self.fail('Tool was not admitted in the guest')
                started = time.monotonic()
                conversation.interrupt()
                await asyncio.wait_for(asyncio.gather(running, return_exceptions=True), timeout=8)
                report['cancel_elapsed_s'] = time.monotonic() - started
                self.assertEqual(conversation.state.execution_status.value, 'paused')
                box.portal.drain(2)
                report['no_admitted_requests_pending'] = not box.portal.has_pending()
                self.assertTrue(report['no_admitted_requests_pending'])
            finally:
                if not running.done():
                    conversation.interrupt()
                    await asyncio.gather(running, return_exceptions=True)
        try:
            box.connect()
            conversation, binding = create_conversation(box,
                EndpointLLM(model='openai/gpt-4o', api_key='EMPTY'), metadata_dir=out / 'controller',
                home_dir=Path.home(), max_iterations=3)
            conversation.send_message('Run the controlled command.')
            with patch.object(EndpointLLM, 'acompletion', side_effect=complete) as completion:
                asyncio.run(drive())
                self.assertEqual(completion.call_count, 1)
            binding.check()
            self.assertTrue(box.action('settle')['settled'])
            report['status'] = 'passed'
        except BaseException:
            report.update(status='failed', traceback=traceback.format_exc())
            raise
        finally:
            if conversation:
                conversation.close()
            try:
                box.close()
                self.assertEqual(box.cleanup, 'stopped')
            finally:
                report.update(cleanup=box.cleanup, operations=box.operations, sandbox_id=box.id)
                (out / 'report.json').write_text(json.dumps(report, indent=2))

    def test_guest_tools_and_retirement(self):
        from dsec_openhands.sdk import SDKSandbox
        from dsec.contracts.rpc import ServiceError
        from openhands.tools.terminal.definition import TerminalObservation
        from openhands.tools.file_editor.definition import FileEditorObservation
        config = json.loads(Path(os.environ['DSEC_OPENHANDS_SDK_CONFIG']).read_text())
        out = Path(os.environ['DSEC_OPENHANDS_SDK_OUT'])
        out.mkdir(mode=0o700, parents=True, exist_ok=False)
        box = SDKSandbox(config)
        report = dict(status='running', checks=[], observations=[])
        def check(name, value):
            report['checks'].append(dict(name=name, passed=bool(value)))
            self.assertTrue(value, name)
        def terminal(command, **kwargs):
            value = box.action('terminal', action=dict(command=command, **kwargs))
            report['observations'].append(dict(tool='terminal', action=dict(command=command, **kwargs), result=value))
            result = TerminalObservation.model_validate(value)
            return result, ''.join(part.text for part in result.content if hasattr(part, 'text'))
        def editor(command, path, **kwargs):
            value = box.action('file_editor', action=dict(command=command, path=path, **kwargs))
            report['observations'].append(dict(tool='file_editor', action=dict(command=command, path=path, **kwargs), result=value))
            return FileEditorObservation.model_validate(value)
        try:
            box.connect()
            report['description'] = box.guest_description
            result, text = terminal('export DSEC_SDK_MARKER=retained; mkdir -p /tmp/sdk-contract; cd /tmp/sdk-contract; printf first')
            check('upstream_terminal_command', result.exit_code == 0 and 'first' in text)
            result, text = terminal('printf "%s:%s" "$PWD" "$DSEC_SDK_MARKER"')
            check('cwd_and_environment_persist', '/tmp/sdk-contract:retained' in text)
            result, text = terminal("printf rejected-first\nprintf rejected-second")
            check('upstream_multiple_command_rejection_is_preserved', result.is_error and 'Cannot execute multiple commands' in text)
            script = "cat <<'EOF'\nmultiline-feedback\nEOF\nprintf 'last-command'\nfalse"
            result, text = terminal('bash -c ' + shlex.quote(script))
            check('multiline_completion', result.exit_code == 1 and 'multiline-feedback' in text and 'last-command' in text)
            result, text = terminal('printf fresh-next-command')
            check('next_feedback_not_shifted', result.exit_code == 0 and 'fresh-next-command' in text and 'last-command' not in text)
            result, text = terminal('sleep 1; printf polled-output', timeout=.1)
            check('soft_timeout_keeps_command_running', result.exit_code == -1)
            result, text = terminal('', timeout=5)
            check('poll_preserves_result', result.exit_code == 0 and 'polled-output' in text)
            result, text = terminal('read -r answer; printf "input=%s" "$answer"', timeout=.1)
            check('foreground_waits_for_stdin', result.exit_code == -1)
            result, text = terminal('hello\n', is_input=True, timeout=5)
            check('foreground_stdin', result.exit_code == 0 and 'input=hello' in text)
            path = '/tmp/sdk-contract/editor.txt'
            check('create', not editor('create', path, file_text='alpha\n').is_error)
            check('replace', not editor('str_replace', path, old_str='alpha', new_str='beta').is_error)
            check('native_file_matches_editor', box.read_file(path) == b'beta\n')
            check('undo', not editor('undo_edit', path).is_error and box.read_file(path) == b'alpha\n')
            entered = threading.Event()
            outcome = {}
            def running():
                entered.set()
                try:
                    outcome['result'] = terminal('printf ready > /tmp/sdk-contract/interrupt-ready; sleep 20', timeout=30)
                except BaseException as exc:
                    outcome['error'] = repr(exc)
            worker = threading.Thread(target=running)
            worker.start()
            self.assertTrue(entered.wait(2))
            # Wait for a guest-side effect, not merely controller submission.
            end = time.monotonic() + 5
            while time.monotonic() < end:
                try:
                    if box.read_file('/tmp/sdk-contract/interrupt-ready') == b'ready':
                        break
                except (FileNotFoundError, ServiceError) as exc:
                    if isinstance(exc, ServiceError) and exc.kind != 'FileNotFoundError':
                        raise
                time.sleep(.02)
            else:
                self.fail('Guest foreground command did not become ready')
            interrupt_started = time.monotonic()
            box.action('interrupt')
            worker.join(timeout=10)
            check('interrupt_bypasses_busy_command', not worker.is_alive() and 'error' not in outcome and
                outcome['result'][0].exit_code not in (-1, 0) and time.monotonic() - interrupt_started < 10)
            report['interrupt_elapsed_s'] = time.monotonic() - interrupt_started
            result, text = terminal('printf after-interrupt')
            check('terminal_survives_interrupt', result.exit_code == 0 and 'after-interrupt' in text)
            result, text = terminal('touch /tmp/sdk-contract/must-not-start', timeout=180)
            check('unsupported_timeout_is_recoverable_rejection', result.is_error and 'No command was executed' in text)
            result, text = terminal('test ! -e /tmp/sdk-contract/must-not-start')
            check('rejected_timeout_has_no_side_effect_and_session_remains_usable', result.exit_code == 0)
            result, text = terminal('C-c', reset=True, is_input=True)
            check('incompatible_reset_input_is_recoverable_rejection', result.is_error and 'session was not reset' in text)
            result, text = terminal('printf "%s" "$DSEC_SDK_MARKER"')
            check('rejected_reset_keeps_prior_shell_state', result.exit_code == 0 and 'retained' in text)
            report['status'] = 'passed'
        except BaseException as exc:
            report.update(status='failed', traceback=traceback.format_exc())
            if isinstance(exc, ServiceError):
                report['service_error'] = dict(kind=exc.kind, details=exc.details)
            raise
        finally:
            try:
                box.close()
                check('sandbox_retirement_proved', box.cleanup in ('stopped', 'not_created_proved'))
            finally:
                report.update(cleanup=box.cleanup, operations=box.operations, startup=box.startup_evidence,
                              sandbox_id=box.id)
                (out / 'report.json').write_text(json.dumps(report, indent=2))
