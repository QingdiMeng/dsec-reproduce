"""Verify prepared-state episode reuse with an installed, isolated DSec instance.

One baseline and one branch at a time fit a two-slot installation. This checks
correctness across two episodes, not parallel fork throughput or model quality.
Invoke with the installed Python -I, outside the source tree.
"""
import argparse
import asyncio
from dataclasses import asdict
import hashlib
import importlib.metadata
import json
from pathlib import Path
import shlex
import subprocess
import time
import traceback

from agent_environment import DSecAgentEnvironment, EnvironmentAction
from dsec_adapters.tb2_dsec_environment import TB2DSecEnvironment
from dsec_host import load_config, wait_ready
from request_journal import atomic_json
from scheduled_dsec import ScheduledDSecClient


SERVER = '''from http.server import HTTPServer, BaseHTTPRequestHandler
value=7
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        global value
        self.send_response(200); self.end_headers()
        self.wfile.write(str(value).encode()); value+=1
HTTPServer(('127.0.0.1',17877),Handler).serve_forever()
'''
FETCH = 'python3 -c '+shlex.quote(
    "import urllib.request; print(urllib.request.build_opener(urllib.request.ProxyHandler({}))"
    ".open('http://127.0.0.1:17877').read().decode(),end='')")
PREPARE = ('printf %s '+shlex.quote(SERVER)+' > /tmp/dsec-release-state.py; '
           'setsid python3 /tmp/dsec-release-state.py </dev/null '
           '>/tmp/dsec-release-state.log 2>&1 & sleep .3; '
           'printf prepared > /tmp/dsec-prepared; '+FETCH)


def snapshot_hashes(root):
    hashes = {}
    for name in ('state', 'memory', 'disk-image.json'):
        digest = hashlib.sha256()
        with (root/name).open('rb') as stream:
            while block := stream.read(1024*1024):
                digest.update(block)
        hashes[name] = digest.hexdigest()
    return hashes


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
    state_root = Path(cfg['state_root'])/'sandboxes'
    shared_root = state_root/'.fork-layers'
    adapter = TB2DSecEnvironment(Path(cfg['worker']['tb2_tasks_dir']),
                    environment_catalog=Path(cfg['sandbox']['microvm_environment_catalog']))
    client = ScheduledDSecClient(Path(cfg['state_root'])/'worker/worker.sock')
    owned = []
    prepared_paths = []
    report = {'status':'running', 'version':importlib.metadata.version('dsec-reproduce'),
              'instance':cfg['instance'], 'task_id':fixture['task_id'], 'episodes':[],
              'rollout_ids':owned, 'cleanup':[],
              'condition':'two sequential branches from one prepared baseline'}

    async def act(env, command, step):
        observation = await env.step(
            EnvironmentAction.shell(step_id=step, action_id='fork-check-'+str(step),
                                    command=command, timeout_ms=30000),
            policy_message={'role':'assistant', 'content':'```bash\n'+command+'\n```'})
        report['episodes'][-1]['observations'].append(asdict(observation))
        atomic_json(destination, report)
        if observation.result['exit_code'] != 0:
            raise RuntimeError('Branch command failed: '+str(observation.result))
        return observation

    async def restart():
        names = ['dsec-'+cfg['instance']+'-'+role+'.service' for role in ('sandbox', 'worker')]
        await asyncio.to_thread(subprocess.run, ['systemctl', '--user', 'restart', *names],
                                check=True, capture_output=True, timeout=120)
        await asyncio.to_thread(wait_ready, cfg, 30)

    try:
        atomic_json(destination, report)
        manifest_path = Path(cfg['sandbox']['tb2_manifest'])
        manifest = json.loads(manifest_path.read_text())
        report['task_revision'] = manifest['source_commit']
        if fixture.get('task_revision') != manifest['source_commit']:
            raise ValueError('Trajectory task revision differs from the pinned suite')
        spec = adapter.prepare(fixture['task_id'])
        await client.open()
        source_id = client.new_rollout_id()
        owned.append(source_id)
        atomic_json(destination, report)
        source = await client.create(task_id=spec.task_id, rollout_id=source_id,
                                    profile=spec.profile, resources=spec.resources,
                                    ttl_running_stop=3600)
        if source.state == 'QUEUED':
            await source.wait_ready(timeout=60)
        prep = await source.run_shell(PREPARE, step_id=0, action_id='trusted-prepare', timeout_ms=10000)
        report['preparation'] = prep
        if prep['exit_code'] != 0 or prep['output'] != '7':
            raise RuntimeError('Preparation service failed')
        report['sealed'] = await source.seal_baseline(allow_prepared_state=True)
        source_status = await source._call('sandbox_status')
        checkpoint = state_root/source.sandbox_id/f"snapshot-{source_status['generation']}"
        image = json.loads((checkpoint/'disk-image.json').read_text())
        prepared_paths = [Path(layer['file']) for layer in image['lowers']
                          if Path(layer['file']).parent == shared_root/'objects']
        if not prepared_paths:
            raise RuntimeError('Prepared snapshot has no shared immutable lowers')
        inodes = {str(path):path.stat().st_ino for path in prepared_paths}
        report['shared_prepared_layers'] = inodes
        before = await asyncio.to_thread(snapshot_hashes, checkpoint)
        if args.restart_services:
            await restart()
            recovered = await source.refresh()
            core = await source._call('sandbox_status')
            if recovered['state'] != 'PAUSED' or not core['baseline_sealed']:
                raise RuntimeError('Restart lost the sealed baseline')
            report['sealed_baseline_reloaded'] = True
        for episode in range(2):
            rid = client.new_rollout_id()
            owned.append(rid)
            row = {'rollout_id':rid, 'observations':[]}
            report['episodes'].append(row)
            atomic_json(destination, report)
            env = DSecAgentEnvironment(client, adapter, spec.task_id, rid,
                                       baseline_rollout_id=source_id)
            started = time.monotonic()
            view = await env.reset([{'role':'system','content':'Execute one shell action per turn.'}])
            row['reset_s'] = time.monotonic()-started
            sid = env.sandbox.sandbox_id
            row['sandbox_id'] = sid
            if view['next_step'] != 0 or len(view['messages']) != 2:
                raise RuntimeError('Branch inherited an earlier policy history')
            child_image = json.loads((state_root/sid/'fork-image.json').read_text())
            if child_image['lowers'] != image['lowers']:
                raise RuntimeError('Branch has different backing layers')
            holds = json.loads((shared_root/'references'/(sid+'.json')).read_text())
            if not set(map(str, prepared_paths)).issubset(holds):
                raise RuntimeError('Branch has no durable backing hold')
            first_command = ('test "$(cat /tmp/dsec-prepared)" = prepared && '
                'test ! -e /tmp/dsec-episode-write && test ! -e /app/ssl/server.key && '
                'test "$(cat /run/dsec-episode-id)" = '+sid+' && '
                'printf %s '+sid+' > /tmp/dsec-episode-write && '+FETCH)
            initial = await act(env, first_command, 0)
            if initial.result['output'] != '8':
                raise RuntimeError('Branch lost prepared memory or inherited episode state')
            if episode == 0 and args.restart_services:
                core_before = await env.sandbox._call('sandbox_status')
                await restart()
                core_after = await env.sandbox._call('sandbox_status')
                if not core_before['pid'] or core_before['pid'] != core_after['pid']:
                    raise RuntimeError('Restart replaced the live branch VMM')
                duplicate = await act(env, first_command, 0)
                if duplicate != initial:
                    raise RuntimeError('Restart replayed a committed branch action')
                row['live_restart'] = {'same_vmm_pid':core_after['pid'], 'action_deduplicated':True}
            if episode == 1:
                if before != await asyncio.to_thread(snapshot_hashes, checkpoint):
                    raise RuntimeError('Prepared baseline changed between episodes')
                report['baseline_unchanged'] = True
                await source.stop()
                if any(path.stat().st_ino != inodes[str(path)] for path in prepared_paths):
                    raise RuntimeError('Source deletion removed a live branch backing')
                report['source_deleted_while_branch_live'] = True
                await env.sandbox.pause()
            restored = await act(env, 'test "$(cat /tmp/dsec-episode-write)" = '+sid+' && '+FETCH, 1)
            if restored.result['output'] != '9':
                raise RuntimeError('Branch lost its private disk or application memory')
            if episode == 1:
                row['pause_resume_after_source_stop'] = True
            if episode == 0:
                for index, command in enumerate(commands, start=2):
                    await act(env, command, index)
                verdict = await env.evaluate()
                row['verdict'] = asdict(verdict)
                if verdict.score != 1:
                    raise RuntimeError('Official branch verifier failed')
            row['dialogue'] = await env.dialogue()
            await env.stop()
            row['stopped'] = await env.sandbox.refresh()
            atomic_json(destination, report)
        report['status'] = 'passed'
    except BaseException as exc:
        report.update(status='failed', error=repr(exc), traceback=traceback.format_exc())
        raise
    finally:
        for rid in reversed(owned):
            try:
                sandbox = await client.attach(rid)
                await sandbox.stop()
                view = await sandbox.refresh()
                report['cleanup'].append({key:view.get(key) for key in
                                          ('rollout_id', 'state', 'pending', 'lease_held')})
                if view['state'] != 'STOPPED' or view.get('pending') or view.get('lease_held'):
                    raise RuntimeError('Branch resources were not released')
            except Exception as exc:
                report.update(status='failed')
                report['cleanup'].append({'rollout_id':rid, 'error':repr(exc)})
        report['shared_objects_reclaimed'] = bool(prepared_paths) and all(
            not path.exists() for path in prepared_paths)
        if prepared_paths and not report['shared_objects_reclaimed']:
            report.update(status='failed', cleanup_error='Shared backing objects leaked')
        await client.close()
        atomic_json(destination, report)
    if report['status'] != 'passed':
        raise RuntimeError('Fork cleanup failed; see saved evidence')
    print(json.dumps({'status':report['status'], 'result':str(destination)}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--trajectory', required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path)
    parser.add_argument('--restart-services', action='store_true')
    asyncio.run(verify(parser.parse_args()))


if __name__ == '__main__':
    main()
