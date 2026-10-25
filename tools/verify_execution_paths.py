"""Installed public HTTPS SDK acceptance for FnCall, Container and MicroVM.

Uses only configured environment IDs and quotas, never host artifacts or Docker.
Owns at most two sandboxes concurrently and always requests their retirement.
This is execution acceptance, not a security audit or a throughput benchmark.
"""
import argparse
import asyncio
import json
from pathlib import Path
import time
import traceback
import uuid

from dsec._credentials import secret_file
from dsec._persistence import atomic_json
from dsec.contracts.sandbox import DSecFnCallRunArgs, DSecContainerRunArgs, DSecMicroVMRunArgs
from dsec.sdk import DSecClient


async def verify(config, output):
    if output.exists():
        raise FileExistsError(output)
    report = dict(status='running',checks=[],sandboxes=[],cleanup_errors=[],
                  boundary='Networkless three-path execution via public HTTPS API; no production security/HA claim')
    def passed(name, **facts):
        report['checks'].append(dict(name=name,**facts)); atomic_json(output,report)
        print(json.dumps(dict(passed=name,**facts)),flush=True)
    clients = DSecClient(**config['client'],token=secret_file(config['token_file']))
    live = []
    async def capacity(client, demand):
        # A stop reply is authoritative for that owner. Placement views refresh
        # asynchronously, so do not turn a known stale full view into a failed
        # execution test. This is read-only waiting, not replay of a create.
        for _ in range(100):
            views = (await client.node_status())['nodes'].values()
            if any(view['available']['concurrency'] >= 1 and all(
                    view['available'][key] >= value for key,value in demand.items()
                    if key != 'disk_io_mbps') for view in views):
                return
            await asyncio.sleep(.1)
        raise TimeoutError('No fresh advertised capacity for next acceptance phase')
    async def retire(box):
        result = await box.stop()
        if result['state'] != 'STOPPED':
            raise AssertionError('Stop did not complete')
        live.remove(box)
    try:
        async with clients as client:
            options = config['paths']['fncall']
            await capacity(client,options['demand'])
            began = time.monotonic()
            fn = await client.run_fncall(DSecFnCallRunArgs(**options['spec']),resource_demand=options['demand'])
            live.append(fn); report['sandboxes'].append(fn.id)
            status = await fn.status()
            assert status['containment'] == 'qemu-kvm' and status['execution_path'] == 'fncall',status
            passed('fncall_actual_qemu_container',create_s=time.monotonic()-began,status=status)
            rid = uuid.uuid4().hex
            first = await fn.invoke('echo dirty >/work/marker; sleep 12345 >/dev/null 2>&1 & echo $!',request_id=rid)
            assert first['exit_code'] == 0,first
            pid = int(first['output'].strip())
            again = await fn.invoke('echo dirty >/work/marker; sleep 12345 >/dev/null 2>&1 & echo $!',request_id=rid)
            assert first == again
            second = await fn.invoke(f"test ! -e /work/marker && python3 -c 'from pathlib import Path; "
                f"p=Path(\"/proc/{pid}/cmdline\"); assert not p.exists() or b\"sleep\\x0012345\" not in p.read_bytes()'")
            assert second['exit_code'] == 0,second
            assert (await fn.status())['guest']['container_id'] == status['guest']['container_id']
            proof = await fn.query_request(rid)
            assert proof['state'] == 'DONE' and proof['response']['result'] == first,proof
            passed('fncall_clean_between_invocations_same_precreated_container_and_dedup')
            timeout = await fn.invoke('sleep 5',timeout_ms=50)
            assert timeout['timed_out'],timeout
            assert (await fn.invoke('printf recovered'))['output'] == 'recovered'
            passed('fncall_timeout_cleanup_allows_next_independent_call')
            await retire(fn)

            options = config['paths']['container']
            await capacity(client,options['demand'])
            began = time.monotonic()
            box = await client.run_container(DSecContainerRunArgs(**options['spec']),resource_demand=options['demand'])
            live.append(box); report['sandboxes'].append(box.id)
            status = await box.status()
            assert status['containment'] == 'qemu-kvm' and status['execution_path'] == 'container',status
            passed('container_actual_qemu_containment',create_s=time.monotonic()-began,status=status)
            assert (await box.run_shell('echo persistent >/work/marker'))['exit_code'] == 0
            assert (await box.run_shell('cat /work/marker'))['output'].strip() == 'persistent'
            session = await box.open_session()
            result = await session.run_shell('cd /work; export DSEC_CASE=kept; printf "$DSEC_CASE"')
            assert result['stdout'] == 'kept',result
            assert (await session.run_shell('printf "$DSEC_CASE"'))['stdout'] == 'kept'
            await box.write_file('/work/binary',b'\x00\xffpersist')
            assert await box.read_file('/work/binary') == b'\x00\xffpersist'
            assert (await box.run_shell('test ! -e /var/run/docker.sock && test ! -e /dev/kvm'))['exit_code'] == 0
            passed('container_persistent_shell_files_and_no_host_devices_or_docker_socket')

            await capacity(client,options['demand'])
            peer = await client.run_container(DSecContainerRunArgs(**options['spec']),resource_demand=options['demand'])
            live.append(peer); report['sandboxes'].append(peer.id)
            assert (await peer.run_shell('test ! -e /work/marker'))['exit_code'] == 0
            assert (await peer.status())['guest_cid'] != status['guest_cid']
            passed('independent_container_guests_have_distinct_cids_and_private_workspaces')
            await retire(peer)
            await session.close(); await retire(box)

            options = config['paths']['microvm']
            await capacity(client,options['demand'])
            began = time.monotonic()
            micro = await client.run_microvm(DSecMicroVMRunArgs(**options.get('spec',{})),resource_demand=options['demand'])
            live.append(micro); report['sandboxes'].append(micro.id)
            assert (await micro.run_shell('echo micro >/tmp/execution-path-marker'))['exit_code'] == 0
            await micro.pause()
            assert (await micro.status())['state'] == 'PAUSED'
            await micro.resume()
            assert (await micro.run_shell('cat /tmp/execution-path-marker'))['output'].strip() == 'micro'
            session = await micro.open_session()
            assert (await session.run_shell('printf native'))['stdout'] == 'native'
            await session.close()
            passed('microvm_shell_native_and_pause_resume',wall_s=time.monotonic()-began,status=await micro.status())
            await retire(micro)
            report['status'] = 'passed'
    except BaseException:
        report['status'] = 'failed'; report['traceback'] = traceback.format_exc()
        raise
    finally:
        for box in reversed(live):
            try:
                await box.stop()
            except Exception as exc:
                report['cleanup_errors'].append(dict(id=box.id,error=repr(exc)))
        if report['cleanup_errors']:
            report['status'] = 'failed'
        atomic_json(output,report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--out',type=Path,required=True)
    args = parser.parse_args()
    result = asyncio.run(verify(json.loads(args.config.read_text()),args.out.resolve()))
    raise SystemExit(0 if result['status'] == 'passed' else 1)


if __name__ == '__main__':
    main()
