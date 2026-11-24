"""SWE-Gym through the pinned modern OpenHands SDK and DSec API.

The CLI starts a fresh controller process with an empty HOME before importing
OpenHands: ambient personal plugins/hooks must not participate in evaluation.
Only the public issue is shown to the agent; grading owns the hidden test patch.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import traceback

from .common import write_json, task_shell
from .dataset import public_task, validate_task
from .patch import capture_command
from .acceptance import verify


def thinking_policy(options):
    from .openhands_reasoning import reasoning_scope
    selected = reasoning_scope(options)  # Reject conflicting provider contracts.
    default = {None: 'disabled', 'qwen_current_user_tool_chain': 'current_user_turn',
               'deepseek_all_assistant_turns': 'all_assistant_turns'}[selected]
    return options.get('thinking_history', default)


def token_budgets(options):
    context = options.get('context_length', 65536)
    response = options.get('response_length', 32768)
    margin = options.get('context_safety_tokens', 1024)
    if any(type(value) is not int or value <= 0 for value in (context, response, margin)) or context <= response + margin:
        raise ValueError('Total context must leave positive input space after response and safety reserves')
    return dict(context=context, response=response, safety=margin, input=context-response-margin)


def policy_termination(status, events, deadline):
    if status == 'finished':
        return 'finished'
    if status == 'stuck':
        return 'agent_stuck'
    if status == 'paused' and deadline:
        return 'episode_deadline'
    last_error = next((event for event in reversed(events) if event.get('kind') == 'ConversationErrorEvent'), {})
    if status == 'error' and last_error.get('code') == 'MaxIterationsReached':
        return 'iteration_limit'
    raise RuntimeError('Upstream SDK did not finish a scorable policy episode: ' + status)


async def run_with_deadline(conversation, seconds):
    budget = asyncio.timeout(seconds)
    try:
        async with budget:
            await conversation.arun()
    except asyncio.TimeoutError:
        if not budget.expired():
            raise  # A model/tool timeout is not the episode's policy deadline.
    # SDK arun() deliberately consumes CancelledError and returns PAUSED.
    # wait_for() alone cannot distinguish that from an early normal return.
    return budget.expired()


def run(task, config, out, *, home_dir):
    from dsec_openhands import SDK_VERSION
    from dsec_openhands.sdk import SDKSandbox, create_conversation
    from dsec_openhands.sdk_llm import EndpointLLM
    from dsec_openhands.sdk_condenser import evidence_condenser
    validate_task(task)
    options = config['openhands']
    if options.get('unparsed_tool_recovery') not in (None,'minicpm5'):
        raise ValueError('Unsupported unparsed-tool recovery profile')
    recovery_limit=options.get('recovery_limit',3)
    if type(recovery_limit) is not int or not 1<=recovery_limit<=10:
        raise ValueError('Recovery limit must be between 1 and 10')
    history_policy = thinking_policy(options)
    budgets = token_budgets(options)
    if options.get('native_tool_calling', True) is not True:
        raise ValueError('This SDK profile requires native tool calling')
    maximum = options.get('max_iterations', 100)
    seconds = options.get('episode_seconds', 1200)
    if type(maximum) is not int or not 1 <= maximum <= 300 or not 1 <= seconds <= 7200:
        raise ValueError('Bounded OpenHands iteration and time limits required')
    out = Path(out)
    out.mkdir(mode=0o700, parents=True, exist_ok=False)
    report = dict(schema='dsec.swegym.openhands-sdk.v1', status='running', score=None,
        instance_id=task['instance_id'], sdk_version=SDK_VERSION,
        max_iterations=maximum, episode_seconds=seconds, training_tokens_accepted=False,
        model=options.get('model'), thinking_history=history_policy, token_budgets=budgets,
        condenser='EvidenceSummarizingCondenser.v3',
        unparsed_tool_recovery=options.get('unparsed_tool_recovery'),
        recovery_limit=options.get('recovery_limit',3))
    def save():
        write_json(out / 'report.json', report)
    write_json(out / 'task-input.json', public_task(task))
    save()
    settings = {key: config[key] for key in ('api_config', 'spec', 'resource_demand', 'command_timeout_ms') if key in config}
    settings.update(options['runtime'])
    sandbox = SDKSandbox(settings)
    conversation = None
    binding = None
    error = candidate = None
    began = time.monotonic()
    deadline = False
    events = []

    def harness_command(command):
        value = sandbox.run_shell(task_shell(command))
        if value['exit_code'] != 0 or value['timed_out'] or value['truncated']:
            raise RuntimeError('Prepared task command did not complete: ' + value['output'])
        return value['output']

    def event_callback(event):
        events.append(event.model_dump(mode='json'))
        write_json(out / 'history.json', events)

    try:
        sandbox.connect()
        report.update(sandbox_id=sandbox.id, capabilities=sandbox.capabilities,
                      startup=sandbox.startup_evidence)
        save()
        identity = harness_command('git config --global --add safe.directory /testbed && git rev-parse HEAD && id -u')
        if identity.split() != [task['base_commit'], '1000']:
            raise RuntimeError('Prepared repository/runtime user differs: ' + identity)
        if config.get('repository_baseline'):
            from .repository import verification_command
            expected = config['repository_baseline']
            if expected['base_commit'] != task['base_commit']:
                raise ValueError('Repository policy differs from task baseline')
            proof = json.loads(harness_command(verification_command(expected)))
            write_json(out / 'repository-check.json', proof)
            report['repository_baseline'] = proof
        key = os.environ[options['api_key_env']] if options.get('api_key_env') else 'EMPTY'
        llm = EndpointLLM(model=options['model'], base_url=options['base_url'], api_key=key,
            temperature=options.get('temperature', .6), top_p=options.get('top_p', .95),
            capability_overrides=options.get('capability_overrides', {}),
            max_input_tokens=budgets['input'], max_output_tokens=budgets['response'],
            custom_tokenizer=options.get('custom_tokenizer'),
            timeout=options.get('model_timeout_seconds', min(600, seconds)), num_retries=0,
            native_tool_calling=True, disable_vision=True, caching_prompt=False,
            litellm_extra_body=options.get('sampling_extra', {}),
            thinking_history=history_policy,
            log_completions=True, log_completions_folder=str(out / 'completions'))
        instruction = ('The repository is prepared at ' + sandbox.cwd + '.\n'
            'Implement the repository fix described below. Keep the existing tests unchanged.\n\n'
            '<issue>\n' + task['problem_statement'] + '\n</issue>')
        conversation, binding = create_conversation(sandbox, llm, metadata_dir=out / 'controller',
            home_dir=home_dir, max_iterations=maximum, callbacks=[event_callback],
            condenser=evidence_condenser(llm, task_instruction=instruction),
            unparsed_tool_recovery=options.get('unparsed_tool_recovery'),
            recovery_limit=options.get('recovery_limit',3))
        write_json(out / 'instruction.json', dict(content=instruction))
        conversation.send_message(instruction)
        # Upstream arun() drains cancelled tools through executor.interrupt();
        # synchronous pause() only takes effect between model/agent steps.
        deadline = asyncio.run(run_with_deadline(conversation, seconds))
        binding.check()
        state = conversation.state
        report.update(termination=policy_termination(state.execution_status.value, events, deadline),
                      model=options['model'], thinking_history=llm.thinking_history,
                      model_replies=sum('response' in json.loads(path.read_text()) for path in (out / 'completions').glob('*.json')),
                      tool_calls=sum(event.get('kind') == 'ActionEvent' for event in events),
                      sampling={name: options.get(name) for name in ('temperature', 'top_p', 'sampling_extra')})
        if (state.execution_status.value=='finished' and binding.tool_recovery
                and binding.tool_recovery.last_unparsed_response):
            report.update(native_termination=report['termination'],termination='unparsed_tool_call')
        sandbox.action('settle')
        harness_command(capture_command())
        candidate = sandbox.read_file('/work/solution.patch')
        (out / 'solution.patch').write_bytes(candidate)
        (out / 'solution.patch').chmod(0o600)
    except BaseException as exc:
        error = exc
        report.update(status='error', error=dict(type=type(exc).__name__, message=str(exc)), traceback=traceback.format_exc())
        from dsec.contracts.rpc import ServiceError
        if isinstance(exc, ServiceError):
            report['error'].update(kind=exc.kind, details=exc.details)
    finally:
        if binding and binding.tool_recovery:
            report['tool_recovery']=binding.tool_recovery.summary()
        if conversation:
            try:
                conversation.close()
            except BaseException as exc:
                error = error or exc
                report.update(status='error', controller_close_error=str(exc))
        try:
            sandbox.close()
        except BaseException as exc:
            error = error or exc
            report.update(status='error', cleanup_error=str(exc))
        report.update(cleanup=sandbox.cleanup, sandbox_id=sandbox.id,
                      operations=sandbox.operations, agent_elapsed_s=time.monotonic() - began)
        save()
    if error is None and candidate is not None:
        try:
            verdict = asyncio.run(verify(task, config, out / 'verification', mode='patch', patch=candidate.decode()))
            report.update(status='scored', score=verdict['score'], verification_cleanup=verdict['cleanup'])
        except BaseException as exc:
            error = exc
            report.update(status='error', verification_error=str(exc))
    report['elapsed_s'] = time.monotonic() - began
    save()
    if error:
        raise RuntimeError('SDK SWE-Gym run failed; inspect ' + str(out / 'report.json')) from error
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('task', 'config', 'out'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    # Do not mutate process-global HOME in a host application or thread. Exec a
    # dedicated process, with absolute paths and no upstream imports beforehand.
    with tempfile.TemporaryDirectory(prefix='dsec-openhands-controller-') as home:
        env = dict(os.environ, HOME=home, OPENHANDS_SUPPRESS_BANNER='1',
                   OH_PERSISTENCE_DIR=str(Path(home) / '.openhands'),
                   LITELLM_LOCAL_MODEL_COST_MAP='True', DSEC_OPENHANDS_CONTROLLER_HOME=home)
        command = [sys.executable, '-m', 'dsec_swegym_case.openhands_sdk', '--task', str(args.task.resolve()),
                   '--config', str(args.config.resolve()), '--out', str(args.out.resolve())]
        child = subprocess.Popen(command, env=env, cwd=home)
        previous = {}
        def forward(signum, _frame):
            if child.poll() is None:
                child.send_signal(signum)
        try:
            for signum in (signal.SIGINT, signal.SIGTERM):
                previous[signum] = signal.signal(signum, forward)
            code = child.wait()
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)
        raise SystemExit(code)


if __name__ == '__main__':
    home = os.environ.pop('DSEC_OPENHANDS_CONTROLLER_HOME', None)
    if home is None:
        main()
    else:
        stop_requested = False
        def stop(signum, _frame):
            global stop_requested
            if not stop_requested:
                stop_requested = True
                raise KeyboardInterrupt('Controller stop requested: ' + signal.Signals(signum).name)
        for signum in (signal.SIGINT, signal.SIGTERM):
            signal.signal(signum, stop)
        parser = argparse.ArgumentParser()
        for name in ('task', 'config', 'out'):
            parser.add_argument('--' + name, type=Path, required=True)
        args = parser.parse_args()
        result = run(json.loads(args.task.read_text()), json.loads(args.config.read_text()), args.out, home_dir=home)
        print(json.dumps({name: result[name] for name in ('status', 'score', 'termination', 'cleanup', 'elapsed_s')}))
