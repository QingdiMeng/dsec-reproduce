"""Controlled two-controller failure acceptance against an already provisioned Node.

Owns two local test controllers and at most one sandbox. Does not provision or
signal the Node, change host services, or create an etcd cluster. Configurations
must use dedicated free API ports and verified TLS; credentials stay in files.
"""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'src'), str(ROOT)]
from dsec.contracts.errors import RequestOutcomeUnknown
from dsec.contracts.resources import NodeDemand
from dsec.control.cluster_api import ClusterAPI
from dsec.host.cluster import main as service_main, secret_file
from dsec.sdk.cluster import ClusterClient


def controller(config, root):
    def checkpoint(self, stage, envelope):
        gate = root/'gate.json'
        if not gate.exists():
            return
        target = json.loads(gate.read_text())
        if stage == 'result_before_reply' and envelope['request_id'] == target['request_id']:
            (root/'boundary.json').write_text(json.dumps(dict(stage=stage,
                request_id=envelope['request_id'], pid=os.getpid())))
            deadline = time.monotonic()+45
            while time.monotonic() < deadline:
                time.sleep(.05)
            raise RuntimeError('Controlled controller-loss gate expired')
    ClusterAPI._checkpoint = checkpoint
    sys.argv = ['cluster', 'controller', '--config', str(config)]
    service_main()


async def acceptance(args, report):
    children, clients, sandbox = {}, {}, None
    root = args.root.resolve()
    def save():
        (root/'report.json').write_text(json.dumps(report, indent=2)+'\n')
    def check(name, condition, **details):
        if not condition:
            raise AssertionError(name)
        report['checks'].append(dict(name=name, status='passed', **details)); save()
    def start(name):
        config = getattr(args, 'config_'+name).resolve()
        with (root/('controller-'+name+'.log')).open('ab') as log:
            child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()),
                '--child-config', str(config), '--root', str(root)],
                stdin=subprocess.DEVNULL, stdout=log, stderr=log)
        children[name] = child
        (root/'controllers.json').write_text(json.dumps({key:dict(pid=p.pid,command=p.args)
            for key,p in children.items()}, indent=2))
    async def ready(client):
        for _ in range(100):
            try:
                await client.open()
                if (await client.node_status())['nodes']:
                    return
            except Exception:
                pass
            await asyncio.sleep(.2)
        raise RuntimeError('Controller/Node not ready; see private controller logs')
    async def lose_reply(coroutine, rid):
        (root/'boundary.json').unlink(missing_ok=True)
        (root/'gate.json').write_text(json.dumps(dict(request_id=rid)))
        operation = asyncio.create_task(coroutine)
        try:
            for _ in range(600):
                if operation.done():
                    await operation
                    raise AssertionError('Expected controlled boundary was not reached')
                if (root/'boundary.json').exists():
                    boundary = json.loads((root/'boundary.json').read_text())
                    if boundary['pid'] != children['a'].pid or boundary['request_id'] != rid:
                        raise AssertionError('Boundary belongs to another process/request')
                    break
                await asyncio.sleep(.05)
            else:
                raise TimeoutError('Controlled result boundary was not reached')
            children['a'].kill(); children['a'].wait(timeout=5)
            try:
                await asyncio.wait_for(operation, 10)
            except RequestOutcomeUnknown:
                return
            raise AssertionError('Lost mutation reply must report unknown')
        finally:
            (root/'gate.json').unlink(missing_ok=True)
            if not operation.done():
                operation.cancel()
    create_id = uuid.uuid4().hex
    try:
        addresses = []
        for name in ('a', 'b'):
            spec = json.loads(getattr(args,'config_'+name).read_text())['server']
            if not spec.get('cert_file') or not spec.get('key_file'):
                raise ValueError('This real-Node acceptance requires API TLS')
            addresses.append((spec['host'],spec['port']))
            if spec['host'] in ('0.0.0.0', '::'):
                raise ValueError('Use an explicit test listener hostname/address')
            scheme = 'https://'+spec['host']+':'+str(spec['port'])
            clients[name] = ClusterClient([scheme], token=secret_file(args.token_file),
                project=args.project, ca_file=str(args.ca_file))
            start(name)
        if addresses[0] == addresses[1]:
            raise ValueError('Distinct dedicated controller addresses required')
        await ready(clients['a']); await ready(clients['b'])
        check('verified-node-ready', True)
        await lose_reply(clients['a'].run_microvm(request_id=create_id,
            resource_demand=NodeDemand(cpu=args.cpu,memory_mb=args.memory_mb,
                                      disk_mb=args.disk_mb,network_mbps=args.network_mbps)), create_id)
        recovered = await clients['b'].lookup_request(create_id)
        check('creation-recovers-through-second-controller',
              recovered['state']=='DONE' and recovered['response']['ok'])
        sid = recovered['response']['result']['id']
        sandbox = await clients['b'].attach(sid)
        report['sandbox_id'] = sid
        initial = await sandbox.status()
        session = await sandbox.open_session()
        payload = b'cluster-native\x00\xff\n'
        await sandbox.write_file('/tmp/cluster-binary',payload)
        check('binary-file-transfer',await sandbox.read_file('/tmp/cluster-binary')==payload)
        await session.run_shell('export DSEC_CLUSTER_CHECK=preserved')
        value = await session.run_shell('printf "%s" "$DSEC_CLUSTER_CHECK"')
        check('persistent-session',value['stdout']=='preserved')
        start('a'); await ready(clients['a'])
        remote = await clients['a'].attach(sid)
        remote_session = remote.attach_session(session.id)
        command_id = uuid.uuid4().hex
        await lose_reply(remote_session.run_shell(
            'printf "once\\n" >> /tmp/cluster-once',request_id=command_id), command_id)
        proof = await session.query(command_id)
        check('command-recovered-with-explicit-session-scope',proof['state']=='DONE' and proof['response']['ok'])
        check('command-not-replayed',await sandbox.read_file('/tmp/cluster-once')==b'once\n')
        start('a'); await ready(clients['a'])
        remote = await clients['a'].attach(sid); stream_id = uuid.uuid4().hex
        await lose_reply(remote._native('stream',_operation='native_stream',session_id=session.id,
            command='printf "stream\\n"; printf "one\\n" >> /tmp/cluster-stream',
            timeout_ms=5000,output_limit=65536,request_id=stream_id),stream_id)
        events = [event async for event in session.events(stream_id)]
        check('stream-reconnected',any(e['type']=='result' for e in events),
              event_types=[e['type'] for e in events])
        check('stream-not-replayed',await sandbox.read_file('/tmp/cluster-stream')==b'one\n')
        final = await sandbox.status()
        check('same-running-vm',final['state']=='RUNNING' and final['pid']==initial['pid'], vmm_pid=final['pid'])
        await session.close()
        report['status']='passed'
    finally:
        # Even an interrupted create may have produced a VM. Query its pinned
        # Node to reclaim it; never issue another create as compensation.
        if sandbox is None and clients.get('b') and clients['b']._opened:
            try:
                proof = await clients['b'].lookup_request(create_id)
                if proof['state']=='DONE' and proof['response']['ok']:
                    sandbox = await clients['b'].attach(proof['response']['result']['id'])
            except Exception as exc:
                report['cleanup_errors'].append('creation reconciliation: '+type(exc).__name__)
        if sandbox is not None:
            try:
                await sandbox.stop()
                status = await sandbox.status()
                check('sandbox-cleanup',status['state']=='STOPPED' and status.get('resource_cleanup_complete') is not False)
            except Exception as exc:
                report['cleanup_errors'].append('sandbox: '+type(exc).__name__)
        for child in children.values():
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    child.kill(); child.wait(timeout=5)
        (root/'gate.json').unlink(missing_ok=True)
        if report['cleanup_errors']:
            report['status']='failed'
        save()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--config-a', type=Path)
    parser.add_argument('--config-b', type=Path)
    parser.add_argument('--token-file', type=Path)
    parser.add_argument('--ca-file', type=Path)
    parser.add_argument('--project', default='root')
    parser.add_argument('--cpu', type=float, default=1)
    parser.add_argument('--memory-mb', type=int, default=512)
    parser.add_argument('--disk-mb', type=int, default=1024)
    parser.add_argument('--network-mbps', type=float, default=1)
    parser.add_argument('--child-config', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child_config:
        controller(args.child_config,args.root); return
    if not all((args.config_a,args.config_b,args.token_file,args.ca_file)):
        parser.error('Both controller configurations, token file and CA required')
    os.umask(0o077); args.root.mkdir(parents=True,exist_ok=False)
    report = dict(status='failed',checks=[],cleanup_errors=[],
        scope='two local API processes and one provisioned Node; no physical multi-host HA claim')
    try:
        asyncio.run(acceptance(args,report))
    except BaseException as exc:
        report.update(status='failed',error=type(exc).__name__+': '+str(exc))
        raise
    finally:
        report['source_sha256'] = {str(path.relative_to(ROOT)):hashlib.sha256(path.read_bytes()).hexdigest()
            for directory in ('src/dsec/control','src/dsec/host','src/dsec/sdk','src/dsec/contracts')
            for path in (ROOT/directory).glob('*.py') if path.stem.isidentifier()}
        (args.root/'report.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(dict(status=report['status'],checks=len(report['checks']),report=str(args.root/'report.json'))))
    raise SystemExit(report['status']!='passed')


if __name__ == '__main__':
    main()
