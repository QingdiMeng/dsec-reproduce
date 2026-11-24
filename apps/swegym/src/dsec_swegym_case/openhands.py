"""Run the pinned upstream CodeActAgent with DSecRuntime and fresh SWE-Gym grading.

Agent prompting, tool parsing, history construction and stepping remain upstream.
The model boundary restores separate Thinking fields lost by the pinned provider
and history serializers. Evaluation records are not accepted as SAO training
token/logprob tensors.
"""
import argparse
import asyncio
import hashlib
import inspect
import json
import os
from pathlib import Path
import time
import traceback
import uuid

from .common import write_json, task_shell
from .dataset import public_task, validate_task
from .patch import capture_command
from .acceptance import verify
from .openhands_reasoning import reasoning_scope


def policy_stuck(state):
    """Recognize only the pinned controller's evidenced policy-loop terminal.

    Other ERROR states (model transport, runtime loss, unknown effects) remain
    unscored. Even this outcome still requires actual patch capture and grading.
    """
    from openhands.core.schema import AgentState
    from openhands.controller.stuck import StuckDetector
    return (state.agent_state == AgentState.ERROR and
            state.last_error == 'Agent got stuck in a loop' and
            StuckDetector(state).is_stuck())


def policy_parse_failure(state, completion, tools):
    """Match a pinned parser rejection to the actual completed model reply.

    Rejected names/arguments are never repaired or dispatched. Unmatched errors
    and incomplete/provider-failed completions remain unscored.
    """
    from openhands.core.schema import AgentState
    if state.agent_state != AgentState.ERROR or completion.get('error') or not completion.get('completed'):
        return None
    choices = completion.get('response', {}).get('choices', [])
    if len(choices) != 1:
        return None
    declared = {tool['function']['name'] for tool in tools}
    for call in choices[0]['message'].get('tool_calls') or []:
        function = call['function']
        try:
            json.loads(function['arguments'])
        except json.JSONDecodeError:
            if state.last_error == 'Failed to parse tool call arguments: ' + function['arguments']:
                return 'invalid_tool_arguments'
            return None
        if function['name'] not in declared:
            if state.last_error == 'Unknown tool call: ' + function['name']:
                return 'unknown_tool'
            return None
    return None


def instrument_llm(config, options, directory):
    from openhands.llm.llm import LLM

    class EndpointLLM(LLM):
        def is_function_calling_active(self):
            # The 2024 fork otherwise decides from an old hardcoded model-name
            # allowlist. This explicit endpoint capability changes no parser.
            return options.get('native_tool_calling', False)

    llm = EndpointLLM(config)
    thinking = reasoning_scope(options)
    bridge = None
    if thinking:
        if not options.get('native_tool_calling') or not config.model.startswith('openai/'):
            raise ValueError('Thinking compatibility requires the explicit native OpenAI-compatible endpoint')
        from .openhands_reasoning import ThinkingBridge
        bridge = ThinkingBridge(config, directory, scope=thinking)
    original = llm.completion
    sequence = 0

    def completion(*args, **kwargs):
        nonlocal sequence
        number = sequence
        sequence += 1
        extra = options.get('sampling_extra', {})
        if extra:
            kwargs['extra_body'] = dict(kwargs.get('extra_body', {}), **extra)
        # This provider retry layer is independent of OpenHands' num_retries.
        kwargs['max_retries'] = 0
        record = dict(index=number, started=time.time(),
            request={key: kwargs[key] for key in ('messages', 'tools', 'stop', 'tool_choice', 'extra_body', 'max_retries') if key in kwargs})
        destination = directory / f'{number:04d}.json'
        write_json(destination, record)
        try:
            result = (bridge.complete(original, args, kwargs, record,
                        lambda: write_json(destination, record)) if bridge else original(*args, **kwargs))
            record['response'] = result.model_dump()
            return result
        except BaseException as exc:
            record['error'] = dict(type=type(exc).__name__, message=str(exc))
            raise
        finally:
            record['completed'] = time.time()
            write_json(destination, record)

    llm._completion = completion
    return llm


async def run(task, config, out):
    from openhands.agenthub.codeact_agent.codeact_agent import CodeActAgent
    from openhands.controller import AgentController
    from openhands.core.config import AppConfig, AgentConfig, LLMConfig, SandboxConfig
    from openhands.core.loop import run_agent_until_done
    from openhands.core.schema import AgentState
    from openhands.events import EventStream, EventSource, EventStreamSubscriber
    from openhands.events.action import CmdRunAction, MessageAction
    from openhands.events.observation import AgentStateChangedObservation
    from openhands.events.serialization.event import event_to_dict
    from openhands.resolver.utils import codeact_user_response
    from openhands.storage.local import LocalFileStore
    from dsec_openhands import UPSTREAM_COMMIT
    from dsec_openhands.runtime import DSecRuntime

    validate_task(task)
    options = config['openhands']
    maximum = options.get('max_iterations', 100)
    seconds = options.get('episode_seconds', 1200)
    if type(maximum) is not int or not 1 <= maximum <= 300 or not 1 <= seconds <= 7200:
        raise ValueError('Bounded OpenHands iteration and time limits required')
    out = Path(out)
    out.mkdir(mode=0o700, parents=True, exist_ok=False)
    (out / 'completions').mkdir(mode=0o700)
    report = dict(schema='dsec.swegym.openhands.v1', status='running', score=None,
        instance_id=task['instance_id'], upstream_commit=UPSTREAM_COMMIT,
        agent_class='CodeActAgent', max_iterations=maximum, episode_seconds=seconds,
        cleanup='not_created', training_tokens_accepted=False)
    def save():
        write_json(out / 'report.json', report)
    write_json(out / 'task-input.json', public_task(task))
    save()
    api_key = os.environ[options['api_key_env']] if options.get('api_key_env') else 'EMPTY'
    model_config = LLMConfig(model=options['model'], base_url=options['base_url'],
        api_key=api_key, temperature=options.get('temperature', .6), top_p=options.get('top_p', .95),
        max_input_tokens=options.get('context_length', 65536),
        max_output_tokens=options.get('response_length', 32768),
        num_retries=0, timeout=options.get('model_timeout_seconds', min(600, seconds)),
        drop_params=False, disable_vision=True, caching_prompt=False,
        input_cost_per_token=0, output_cost_per_token=0)
    app = AppConfig(default_agent='CodeActAgent', max_iterations=maximum,
        file_store='local', file_store_path=str(out / 'events'),
        workspace_base=None, workspace_mount_path=None,
        sandbox=SandboxConfig(timeout=config.get('command_timeout_ms', 30000) // 1000))
    app.set_llm_config(model_config)
    # Same coding tool selection as the pinned upstream SWE-Gym evaluator.
    agent_config = AgentConfig(codeact_enable_jupyter=False, codeact_enable_browsing=False,
                               codeact_enable_llm_editor=False)
    app.set_agent_config(agent_config)
    llm = instrument_llm(model_config, options, out / 'completions')
    agent = CodeActAgent(llm, agent_config)
    source = Path(inspect.getfile(CodeActAgent))
    report.update(agent_source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        model=options['model'], sampling={key: options.get(key) for key in ('temperature', 'top_p', 'sampling_extra')},
        native_tool_calling=llm.is_function_calling_active(),
        thinking_history=reasoning_scope(options) or 'disabled')
    sid = uuid.uuid4().hex
    stream = EventStream(sid, LocalFileStore(str(out / 'events')))
    settings = {key: config[key] for key in ('api_config', 'spec', 'resource_demand', 'command_timeout_ms') if key in config}
    settings.update(options['runtime'])
    runtime = DSecRuntime(config=app, event_stream=stream, sid=sid,
        plugins=agent.sandbox_plugins, headless_mode=True, dsec_config=settings)
    controller = None
    error = patch = None
    began = time.monotonic()
    try:
        await runtime.connect()
        report.update(sandbox_id=runtime.backend.id, cleanup='pending', capabilities=runtime.backend.capabilities)
        save()
        identity = CmdRunAction(command='git config --global --add safe.directory /testbed && git rev-parse HEAD && id -u')
        identity.timeout = app.sandbox.timeout
        observation = await asyncio.to_thread(runtime.run, identity)
        if observation.exit_code != 0 or observation.content.split() != [task['base_commit'], '1000']:
            raise RuntimeError('Prepared repository/user differs: ' + observation.content)
        if config.get('repository_baseline'):
            from .repository import verification_command
            expected = config['repository_baseline']
            if expected['base_commit'] != task['base_commit']:
                raise ValueError('Repository policy differs from task baseline')
            action = CmdRunAction(command=task_shell(verification_command(expected)));action.timeout = app.sandbox.timeout
            verified = await asyncio.to_thread(runtime.run, action)
            if verified.exit_code != 0:
                raise RuntimeError('Baseline-only Git preflight failed: ' + verified.content)
            proof = json.loads(verified.content)
            write_json(out / 'repository-check.json', proof)
            report['repository_baseline'] = proof
            save()
        controller = AgentController(agent=agent, max_iterations=maximum,
            max_budget_per_task=None, event_stream=stream,
            agent_to_llm_config=app.get_agent_to_llm_config_map(), headless_mode=True)

        async def headless(event):
            if isinstance(event, AgentStateChangedObservation) and event.agent_state == AgentState.AWAITING_USER_INPUT:
                stream.add_event(MessageAction(content=codeact_user_response(controller.get_state())), EventSource.USER)
        stream.subscribe(EventStreamSubscriber.MAIN, headless, sid)
        instruction = ('The repository is prepared at ' + runtime.backend.cwd + '.\n'
            'Implement the repository fix described below. Keep the existing tests unchanged.\n\n'
            '<issue>\n' + task['problem_statement'] + '\n</issue>')
        write_json(out / 'instruction.json', dict(content=instruction))
        stream.add_event(MessageAction(content=instruction), EventSource.USER)
        end_states = [AgentState.FINISHED, AgentState.PAUSED, AgentState.STOPPED, AgentState.ERROR, AgentState.REJECTED]
        try:
            await asyncio.wait_for(run_agent_until_done(controller, runtime, end_states), timeout=seconds)
        except asyncio.TimeoutError:
            report['termination'] = 'episode_deadline'
        finally:
            # Upstream's wait helper does not cancel its step task when the
            # surrounding harness times out. Retain ownership and join it.
            if controller.agent_task and not controller.agent_task.done():
                controller.agent_task.cancel()
                await asyncio.gather(controller.agent_task, return_exceptions=True)
        state = controller.get_state()
        report.update(agent_state=state.agent_state.value, iterations=state.iteration,
                      last_error=state.last_error, termination=report.get('termination') or state.agent_state.value)
        write_json(out / 'history.json', [event_to_dict(event) for event in state.history])
        completions = sorted((out / 'completions').glob('*.json'))
        parse_failure = (policy_parse_failure(state, json.loads(completions[-1].read_text()), agent.tools)
                         if completions else None)
        if policy_stuck(state):
            report['termination'] = 'agent_stuck'
        elif parse_failure:
            report.update(termination=parse_failure, policy_failure_completion=completions[-1].name)
        elif state.agent_state == AgentState.ERROR:
            raise RuntimeError('Upstream agent failed: ' + str(state.last_error))
        if runtime.backend.cleanup != 'pending':
            raise RuntimeError('Runtime closed before candidate capture')
        capture = CmdRunAction(command=task_shell(capture_command()))
        capture.timeout = app.sandbox.timeout
        captured = await asyncio.to_thread(runtime.run, capture)
        report['capture'] = dict(exit_code=captured.exit_code, output=captured.content)
        if captured.exit_code:
            raise RuntimeError('Candidate patch capture failed')
        patch = await asyncio.to_thread(runtime.backend.read_file, '/work/solution.patch')
        (out / 'solution.patch').write_bytes(patch)
        os.chmod(out / 'solution.patch', 0o600)
    except BaseException as exc:
        error = exc
        report.update(status='error', error=dict(type=type(exc).__name__, message=str(exc)), traceback=traceback.format_exc())
        from dsec.contracts.rpc import ServiceError
        if isinstance(exc, ServiceError):
            report['error'].update(kind=exc.kind, details=exc.details)
    finally:
        if controller is not None:
            try:
                await controller.close()
            except BaseException as exc:
                report['controller_close_error'] = str(exc)
                if error is None:
                    error = exc
                    report['status'] = 'error'
        report['operations'] = runtime.backend.operations
        report['sandbox_id'] = runtime.backend.id
        try:
            await asyncio.to_thread(runtime.close)
        except BaseException as exc:
            report['cleanup_error'] = str(exc)
            if error is None:
                error = exc
                report['status'] = 'error'
        report['cleanup'] = runtime.backend.cleanup
        report['agent_elapsed_s'] = time.monotonic() - began
        save()
    if error is None and patch is not None:
        try:
            verdict = await verify(task, config, out / 'verification', mode='patch', patch=patch.decode())
            report.update(status='scored', score=verdict['score'], verification_cleanup=verdict['cleanup'])
        except BaseException as exc:
            error = exc
            report.update(status='error', verification_error=str(exc))
    report['elapsed_s'] = time.monotonic() - began
    save()
    if error is not None:
        raise RuntimeError('OpenHands SWE-Gym run failed; inspect ' + str(out / 'report.json')) from error
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('task', 'config', 'out'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    result = asyncio.run(run(json.loads(args.task.read_text()), json.loads(args.config.read_text()), args.out))
    print(json.dumps({key: result[key] for key in ('status', 'score', 'termination', 'cleanup', 'elapsed_s')}))


if __name__ == '__main__':
    main()
