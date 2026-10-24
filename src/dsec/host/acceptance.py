"""Opt-in host smoke acceptance; never called by configuration or planning."""
import asyncio
import importlib.metadata
from pathlib import Path
import subprocess
import time
import traceback
from dsec._persistence import atomic_json
from dsec.host.services import wait_ready


async def smoke(cfg, out, *, restart_services=False):
    from dsec.rollout.environment import DSecAgentEnvironment, EnvironmentAction
    from dsec_adapters.counter_dsec_environment import CounterDSecEnvironment
    from dsec.sdk.scheduled import ScheduledDSecClient
    report = {'status':'running', 'instance':cfg['instance'],
              'version':importlib.metadata.version('dsec-reproduce')}
    destination = Path(out).resolve()
    if destination.exists():
        raise FileExistsError('Refusing to overwrite acceptance evidence')
    destination.parent.mkdir(parents=True, exist_ok=True)
    environment = None
    async def restart(*, automatic=False):
        names = ['dsec-'+cfg['instance']+'-'+role+'.service' for role in ('sandbox', 'worker')]
        if automatic:
            from dsec.sdk.sandbox_transport import SandboxClient
            from dsec.sdk.rollout_transport import RolloutClient
            daemon = SandboxClient(Path(cfg['state_root'])/'sandboxes/service.sock')
            worker = RolloutClient(Path(cfg['state_root'])/'worker/worker.sock')
            old_daemon = await asyncio.to_thread(daemon.call, 'health')
            old_worker = await asyncio.to_thread(worker.call, 'health')
            # Kill only this instance's supervised launcher. ExecStartPre reaps
            # its orphaned daemon, while the original VMM and worker survive.
            await asyncio.to_thread(subprocess.run,
                ['systemctl', '--user', 'kill', '--kill-whom=main', '--signal=SIGKILL', names[0]],
                check=True, capture_output=True, text=True, timeout=10)
            deadline = time.monotonic()+120
            while time.monotonic() < deadline:
                try:
                    health = await asyncio.to_thread(daemon.call, 'health')
                    if health['pid'] != old_daemon['pid']:
                        break
                except Exception:
                    pass
                await asyncio.sleep(.1)
            else:
                raise TimeoutError('Supervisor did not restart the sandbox service')
            current_worker = await asyncio.to_thread(worker.call, 'health')
            if current_worker['pid'] != old_worker['pid']:
                raise RuntimeError('Sandbox supervisor failure interrupted the worker')
            report['automatic_supervision'] = {'new_daemon_pid':health['pid'],
                                               'same_worker_pid':current_worker['pid']}
        else:
            await asyncio.to_thread(subprocess.run, ['systemctl', '--user', 'restart', *names],
                                    check=True, capture_output=True, text=True, timeout=120)
        await asyncio.to_thread(wait_ready, cfg, 120)
    client = ScheduledDSecClient(Path(cfg['state_root'])/'worker/worker.sock')
    try:
        await client.open()
        started = time.monotonic()
        environment = DSecAgentEnvironment(client, CounterDSecEnvironment(3), 'counter-example',
                                           client.new_rollout_id())
        report['rollout_id'] = environment.rollout_id
        await environment.reset([{'role':'system','content':'Execute one shell action per turn.'}])
        report['create_s'] = time.monotonic()-started
        report['sandbox_id'] = environment.sandbox.sandbox_id
        command = 'test ! -e /rl-counter && printf 3 > /rl-counter'
        action = EnvironmentAction.shell(step_id=0, action_id='accept-write',
                                         command=command, timeout_ms=5000)
        message = {'role':'assistant','content':'```bash\n'+command+'\n```'}
        first = await environment.step(action, policy_message=message)
        if first.result['exit_code'] != 0:
            raise RuntimeError('Write action failed')
        duplicate = await environment.step(action, policy_message=message)
        if duplicate != first:
            raise RuntimeError('Repeated action did not return the committed observation')
        if restart_services:
            from dsec.sdk.sandbox_transport import SandboxClient
            daemon = SandboxClient(Path(cfg['state_root'])/'sandboxes/service.sock')
            before = await asyncio.to_thread(daemon.call, 'status', environment.sandbox.sandbox_id)
            await restart(automatic=True)
            after = await asyncio.to_thread(daemon.call, 'status', environment.sandbox.sandbox_id)
            if before['pid'] != after['pid'] or after['state'] != 'RUNNING':
                raise RuntimeError('Live VMM identity was not preserved across service restart')
            if await environment.step(action, policy_message=message) != first:
                raise RuntimeError('Committed action changed after service restart')
            report['live_restart'] = {'same_vmm_pid':after['pid'], 'action_deduplicated':True}
        await environment.sandbox.pause()
        if restart_services:
            await restart()
            view = await environment.sandbox.refresh()
            if view['state'] != 'PAUSED' or not view.get('lease_held'):
                raise RuntimeError('Paused rollout or lease was not restored')
            report['paused_restart'] = True
        observed = await environment.step(EnvironmentAction.shell(step_id=1,
                action_id='accept-read', command='cat /rl-counter', timeout_ms=5000),
                policy_message={'role':'assistant','content':'```bash\ncat /rl-counter\n```'})
        if observed.result['exit_code'] != 0 or observed.result['output'].strip() != '3':
            raise RuntimeError('Pause/resume lost the private file')
        verdict = await environment.evaluate()
        if verdict.score != 1:
            raise RuntimeError('Counter verifier did not return one')
        report.update(status='passed', reward=verdict.score, action_deduplicated=True,
                      pause_resume=True, dialogue=await environment.dialogue())
    except BaseException as exc:
        report.update(status='failed', error=repr(exc), traceback=traceback.format_exc())
        raise
    finally:
        if environment and environment.sandbox:
            try:
                await environment.stop()
                record = await environment.sandbox.refresh()
                report['cleanup'] = {k:record.get(k) for k in ('state','pending','lease_held')}
                if record['state'] != 'STOPPED' or record.get('pending') or record.get('lease_held'):
                    raise RuntimeError('Acceptance did not release the sandbox and lease')
            except Exception as exc:
                report.update(status='failed', cleanup_error=repr(exc))
        await client.close()
        atomic_json(destination, report)
    if report['status'] != 'passed':
        raise RuntimeError('Acceptance cleanup failed; see saved evidence')
    return {'status':report['status'], 'result':str(destination), 'reward':report['reward']}
