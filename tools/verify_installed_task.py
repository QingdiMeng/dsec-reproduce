"""Run a fixed task trajectory against installed DSec; save raw verdict and failure evidence.

Invoke with the installed venv Python's -I flag, outside the source tree.
This measures harness functionality, not model quality or full backend costs.
"""
import argparse
import asyncio
from dataclasses import asdict
import hashlib
import importlib.metadata
import json
from pathlib import Path
import time
import traceback
import subprocess

from agent_environment import DSecAgentEnvironment, EnvironmentAction
from dsec_adapters.counter_dsec_environment import CounterDSecEnvironment
from dsec_adapters.tb2_dsec_environment import TB2DSecEnvironment
from dsec_host import load_config, wait_ready
from request_journal import atomic_json
from scheduled_dsec import ScheduledDSecClient


async def verify(args):
    cfg = load_config(args.config)
    fixture = json.loads(args.trajectory.read_text())
    commands = fixture['commands']
    if not isinstance(commands, list) or not commands or any(
            not isinstance(command, str) or not command for command in commands):
        raise ValueError('Trajectory commands must be a nonempty string list')
    destination = args.out.resolve()
    if destination.exists():
        raise FileExistsError('Acceptance evidence already exists')
    destination.parent.mkdir(parents=True, exist_ok=True)
    if args.adapter == 'tb2':
        adapter = TB2DSecEnvironment(Path(cfg['worker']['tb2_tasks_dir']),
                    environment_catalog=Path(cfg['sandbox']['microvm_environment_catalog']))
    else:
        adapter = CounterDSecEnvironment()
    client = ScheduledDSecClient(Path(cfg['state_root'])/'worker/worker.sock')
    environment = None
    report = {'status':'running', 'version':importlib.metadata.version('dsec-reproduce'),
              'instance':cfg['instance'], 'task_id':fixture['task_id'], 'steps':[],
              'trajectory_sha256':hashlib.sha256(args.trajectory.read_bytes()).hexdigest()}
    try:
        if args.adapter == 'tb2':
            manifest_file = Path(cfg['sandbox']['tb2_manifest'])
            manifest = json.loads(manifest_file.read_text())
            report['task_revision'] = manifest['source_commit']
            report['task_manifest_sha256'] = hashlib.sha256(manifest_file.read_bytes()).hexdigest()
            if fixture.get('task_revision') != manifest['source_commit']:
                raise ValueError('Trajectory task revision differs from the pinned suite')
        atomic_json(destination, report)
        await client.open()
        environment = DSecAgentEnvironment(client, adapter, fixture['task_id'], client.new_rollout_id())
        report['rollout_id'] = environment.rollout_id
        atomic_json(destination, report)
        started = time.monotonic()
        await environment.reset([{'role':'system','content':'Execute one shell action per turn.'}])
        report['create_s'] = time.monotonic()-started
        report['sandbox_id'] = environment.sandbox.sandbox_id
        for index, command in enumerate(commands):
            started = time.monotonic()
            observation = await environment.step(
                EnvironmentAction.shell(step_id=index, action_id='fixture-'+str(index),
                                        command=command, timeout_ms=args.command_timeout_ms),
                policy_message={'role':'assistant','content':'```bash\n'+command+'\n```'})
            report['steps'].append({'wall_s':time.monotonic()-started,
                                    'observation':asdict(observation)})
            atomic_json(destination, report)
            if observation.result['exit_code'] != 0:
                raise RuntimeError('Trajectory command failed at step '+str(index))
        if args.restart_services:
            from sandbox_client import SandboxClient
            daemon = SandboxClient(Path(cfg['state_root'])/'sandboxes/service.sock')
            names = ['dsec-'+cfg['instance']+'-'+role+'.service' for role in ('sandbox', 'worker')]
            async def restart():
                await asyncio.to_thread(subprocess.run, ['systemctl', '--user', 'restart', *names],
                                        check=True, capture_output=True, timeout=120)
                await asyncio.to_thread(wait_ready, cfg, 30)
            before = await asyncio.to_thread(daemon.call, 'status', environment.sandbox.sandbox_id)
            await restart()
            after = await asyncio.to_thread(daemon.call, 'status', environment.sandbox.sandbox_id)
            if before['pid'] != after['pid'] or after['state'] != 'RUNNING':
                raise RuntimeError('Live restart lost the original VMM')
            duplicate = await environment.step(
                EnvironmentAction.shell(step_id=index, action_id='fixture-'+str(index),
                                        command=command, timeout_ms=args.command_timeout_ms),
                policy_message={'role':'assistant','content':'```bash\n'+command+'\n```'})
            if duplicate != observation:
                raise RuntimeError('Committed task action changed after restart')
            report['live_restart'] = {'same_vmm_pid':after['pid'], 'action_deduplicated':True}
            await environment.sandbox.pause()
            await restart()
            view = await environment.sandbox.refresh()
            if view['state'] != 'PAUSED' or not view.get('lease_held'):
                raise RuntimeError('Paused task or lease was not restored')
            report['paused_restart'] = True
            atomic_json(destination, report)
        started = time.monotonic()
        verdict = await environment.evaluate()
        report['verifier_s'] = time.monotonic()-started
        report['verdict'] = asdict(verdict)
        report['dialogue'] = await environment.dialogue()
        if args.expect_score is not None and verdict.score != args.expect_score:
            raise RuntimeError('Verifier score differed from expected acceptance score')
        report['status'] = 'passed'
    except BaseException as exc:
        report.update(status='failed', error=repr(exc), traceback=traceback.format_exc())
        raise
    finally:
        if environment and environment.sandbox:
            try:
                await environment.stop()
                view = await environment.sandbox.refresh()
                report['cleanup'] = {key:view.get(key) for key in ('state', 'pending', 'lease_held')}
                if view['state'] != 'STOPPED' or view.get('pending') or view.get('lease_held'):
                    raise RuntimeError('Sandbox or lease did not stop')
            except Exception as exc:
                report.update(status='failed', cleanup_error=repr(exc))
        await client.close()
        atomic_json(destination, report)
    if report['status'] != 'passed':
        raise RuntimeError('Cleanup failed; see acceptance evidence')
    print(json.dumps({'status':report['status'], 'result':str(destination),
                      'score':report['verdict']['score']}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--trajectory', type=Path, required=True)
    parser.add_argument('--adapter', choices=('tb2', 'counter'), default='tb2')
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--expect-score', type=float, choices=(0.0, 1.0))
    parser.add_argument('--command-timeout-ms', type=int, default=30000)
    parser.add_argument('--restart-services', action='store_true',
                        help='restart this isolated instance before and after pause')
    args = parser.parse_args()
    if not 1 <= args.command_timeout_ms <= 900000:
        parser.error('Invalid command timeout')
    asyncio.run(verify(args))


if __name__ == '__main__':
    main()
