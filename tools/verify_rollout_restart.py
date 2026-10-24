"""Operator-controlled service restart acceptance for one live API episode.

The plan supplies explicit argv arrays for dedicated service stop/start and a
read-only Node inspection. No shell, provisioning, credential contents or
automatic retries of mutations. The Edge/VMM proof is separate from the API
episode identity; this test does not establish physical-host recovery.
"""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import uuid

from dsec._persistence import atomic_json
from dsec.sdk import RolloutAPIClient, ScheduledOutcomeUnknown
from dsec.sdk.rollout_transport import RolloutServiceError, RolloutOutcomeUnknown


def load_plan(path):
    plan = json.loads(path.read_text())
    if (not isinstance(plan, dict) or set(plan)-{'inspect', 'inspect_outage', 'phases'}
            or not {'inspect', 'phases'} <= set(plan)):
        raise ValueError('Plan requires inspect and phases')
    def argv(value):
        if (not isinstance(value, list) or not value or
                any(not isinstance(v, str) or not v or '\0' in v for v in value)):
            raise ValueError('Explicit nonempty command argv required')
    argv(plan['inspect'])
    if 'inspect_outage' in plan:
        argv(plan['inspect_outage'])
    if not isinstance(plan['phases'], list) or not plan['phases']:
        raise ValueError('At least one restart phase required')
    names = set()
    for phase in plan['phases']:
        if not isinstance(phase, dict) or set(phase) != {'name', 'stop', 'start'}:
            raise ValueError('Phase requires name, stop and start')
        name = phase['name']
        if (not isinstance(name, str) or not name or not name.replace('_', '').isalnum()
                or name in names):
            raise ValueError('Unique phase name required')
        names.add(name)
        for key in ('stop', 'start'):
            if not isinstance(phase[key], list) or not phase[key]:
                raise ValueError('Explicit stop/start commands required')
            for command in phase[key]:
                argv(command)
    return plan


def stable_view(view):
    return {key: view[key] for key in ('rollout_id', 'sandbox_id', 'profile',
        'task_id', 'state', 'next_step', 'history', 'dialogue_seed',
        'dialogue_feedback_version', 'pending', 'uncertain', 'reward', 'lease_held')}


def physical(proof):
    value = proof['physical']
    if (set(value) != {'sandbox_id', 'pid', 'start_ticks', 'boot_id'} or
            type(value['pid']) is not int or value['pid'] <= 0 or
            type(value['start_ticks']) is not int or value['start_ticks'] <= 0):
        raise ValueError('Attested VMM identity required from Node inspection')
    return value


async def verify(args):
    plan = load_plan(args.plan)
    root = args.out.resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    rid = uuid.uuid4().hex
    report = dict(status='running', rollout_id=rid, checks=[], cleanup_errors=[],
        scope='service restart with a surviving host and VMM; no host-loss claim',
        tool_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        plan_sha256=hashlib.sha256(args.plan.read_bytes()).hexdigest())
    atomic_json(root/'report.json', report)
    def passed(name, **details):
        report['checks'].append(dict(name=name, **details))
        atomic_json(root/'report.json', report)
    serial = 0
    async def command(argv, name, inspect=False):
        nonlocal serial
        serial += 1
        argv = [item.replace('{rollout_id}', rid) for item in argv]
        log = root/f'{serial:02d}-{name}.log'
        def run():
            with log.open('wb') as stream:
                subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=stream,
                               stderr=subprocess.STDOUT, timeout=45, check=True)
            if inspect:
                if log.stat().st_size > 1024**2:
                    raise ValueError('Oversized inspection proof')
                return json.loads(log.read_text())
        return await asyncio.to_thread(run)
    async def controls(phase, operation):
        for argv in phase[operation]:
            await command(argv, phase['name']+'-'+operation)
    async def connected(require_active=True):
        end = time.monotonic()+30
        while True:
            client = RolloutAPIClient.from_config(args.api_config)
            try:
                await client.open()
                episode = await client.attach(rid)
                if require_active and episode.state != 'ACTIVE':
                    raise AssertionError('Recovered episode is not ACTIVE')
                return client, episode
            except RolloutOutcomeUnknown:
                await client.close()
            except RolloutServiceError as exc:
                await client.close()
                if exc.kind != 'CoordinationUnavailable':
                    raise
            except Exception:
                await client.close()
                raise
            if time.monotonic() >= end:
                raise TimeoutError('Read-only reattachment readiness expired')
            await asyncio.sleep(.2)
    client = RolloutAPIClient.from_config(args.api_config)
    episode = None
    recovering = None
    admitted = False
    action = dict(step_id=0, action_id='committed-before-restart', timeout_ms=5000)
    shell = "printf 'once\\n' >> /restart-once; printf 3 > /rl-counter; printf 'committed\\n'"
    assistant = {'role':'assistant', 'content':'```bash\n'+shell+'\n```'}
    try:
        await client.open()
        admitted = True  # an interrupted create still needs original-ID inspection
        episode = await client.create(task_id='counter-example', rollout_id=rid,
            profile={}, resources=dict(cpu=1, memory_mb=512, disk_mb=1024,
                                      network_mbps=1, api_episode_slots=1),
            ttl_running_stop=900)
        await episode.wait_ready(timeout=60)
        await episode.start_dialogue([{'role':'user', 'content':'Write counter 3 once.'}])
        result = await episode.agent_step(shell, assistant_message=assistant, **action)
        assert result['entry']['result']['exit_code'] == 0
        before = stable_view(await episode.refresh())
        dialogue = await episode.dialogue()
        proof = await command(plan['inspect'], 'initial-inspection', inspect=True)
        identity = physical(proof)
        assert proof['held_concurrency'] == 1 and proof['edge_leases'] == 1
        report['sandbox_id'] = episode.sandbox_id
        passed('live_episode_and_committed_dialogue', physical=identity)
        for phase in plan['phases']:
            recovering = phase
            await controls(phase, 'stop')
            try:
                await episode.refresh()
            except ScheduledOutcomeUnknown:
                pass
            else:
                raise AssertionError('Stopped service was still reachable')
            if 'inspect_outage' in plan:
                outage = await command(plan['inspect_outage'], phase['name']+'-outage-inspection', inspect=True)
                assert outage['held_concurrency'] == 1 and outage['admission_state'] == 'BOUND'
            passed(phase['name']+'_outage_typed_unknown', quota_inspected='inspect_outage' in plan)
            await controls(phase, 'start')
            recovering = None
            await client.close()
            client, episode = await connected()
            assert stable_view(await episode.refresh()) == before
            assert await episode.dialogue() == dialogue
            proof = await command(plan['inspect'], phase['name']+'-inspection', inspect=True)
            assert physical(proof) == identity
            assert proof['held_concurrency'] == 1 and proof['edge_leases'] == 1
            # Explicitly ask for the SAME committed action. The journal must
            # return its result, not run its command or append a second message.
            repeated = await episode.agent_step(shell, assistant_message=assistant, **action)
            assert repeated['entry'] == result['entry']
            assert stable_view(await episode.refresh()) == before
            assert await episode.dialogue() == dialogue
            passed(phase['name']+'_same_vmm_state_dialogue_quota_no_replay', physical=identity)
        inspect_shell = 'cat /restart-once; cat /rl-counter'
        response = await episode.agent_step(inspect_shell, step_id=1,
            action_id='inspect-after-restarts', timeout_ms=5000, assistant_message={
                'role':'assistant', 'content':'```bash\n'+inspect_shell+'\n```'})
        value = response['entry']['result']
        assert value['output'].replace('\r','').strip() == 'once\n3', value
        reward = await episode.evaluate_counter(3)
        assert reward['value'] == 1 and reward['observed'] == '3', reward
        passed('continued_work_and_verifier_after_restarts', score=1)
        await episode.stop()
        stopped = await episode.refresh()
        assert stopped['state'] == 'STOPPED' and not stopped['lease_held']
        proof = await command(plan['inspect'], 'cleanup-inspection', inspect=True)
        assert proof['held_concurrency'] == 0 and proof['edge_leases'] == 0
        assert proof['admission_state'] == 'RELEASED'
        episode = None
        passed('authoritative_stop_and_zero_held_resources')
        report['status'] = 'passed'
    except BaseException as exc:
        report.update(status='failed', error={'type':type(exc).__name__, 'message':str(exc)})
        raise
    finally:
        if recovering is not None:
            try:
                await controls(recovering, 'start')
            except Exception as exc:
                report['cleanup_errors'].append('service recovery: '+type(exc).__name__)
        if admitted and report['status'] != 'passed':
            try:
                await client.close()
                client, episode = await connected(require_active=False)
                await episode.stop()
            except Exception as exc:
                report['cleanup_errors'].append('original episode cleanup: '+type(exc).__name__)
        await client.close()
        if report['cleanup_errors']:
            report['status'] = 'failed'
        atomic_json(root/'report.json', report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--api-config', type=Path, required=True)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    report = asyncio.run(verify(args))
    print(json.dumps(dict(status=report['status'], checks=len(report['checks']))))


if __name__ == '__main__':
    main()
