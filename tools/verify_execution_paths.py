"""Installed public HTTPS SDK acceptance for FnCall, Container, MicroVM and FullVM.

Uses only configured environment IDs and quotas, never host artifacts or Docker.
Owns at most two sandboxes concurrently and always requests their retirement.
This is execution acceptance, not a security audit or a throughput benchmark.
"""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import time
import traceback
import uuid

from dsec._credentials import secret_file
from dsec._persistence import atomic_json
from dsec.contracts.sandbox import DSecFnCallRunArgs, DSecContainerRunArgs, DSecMicroVMRunArgs, DSecFullVMRunArgs
from dsec.sdk import DSecClient, ServiceError


async def verify(config, output, *, fullvm_only=False, desktop_only=False):
    if output.exists():
        raise FileExistsError(output)
    report = dict(status='running',checks=[],sandboxes=[],cleanup_errors=[],
                  boundary='Networkless execution via public HTTPS API; no production security/HA claim')
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
        if result['state'] != 'STOPPED' or (getattr(box,'backend',None) == 'fullvm' and
                result.get('resource_cleanup_complete') is not True):
            raise AssertionError('Stop did not complete')
        live.remove(box)
    try:
        async with clients as client:
            if desktop_only:
                options = config['paths']['fullvm']
                spec = DSecFullVMRunArgs(**options['spec'])
                await capacity(client, options['demand'])
                box = await client.run_fullvm(spec,resource_demand=options['demand'])
                live.append(box); report['sandboxes'].append(box.id)
                status = await box.status()
                assert status.get('display') == 'virtio-2d' and status['backend'] == 'fullvm',status
                os_info = await box.run_shell('test -d /sys/module/virtio_gpu && cat /etc/os-release /sys/class/drm/card0/device/uevent')
                assert os_info['exit_code']==0 and 'ID=ubuntu' in os_info['output'] and 'PCI_ID=1AF4:1050' in os_info['output'],os_info
                for _ in range(50):
                    geometry = await box.run_shell(
                        "DISPLAY=:0 xdotool search --onlyvisible --name '^DSecDesktop$' getwindowgeometry --shell")
                    if geometry['exit_code'] == 0 and 'WIDTH=' in geometry['output']:
                        break
                    await asyncio.sleep(.2)
                else:
                    raise RuntimeError('Desktop test terminal did not become visible')
                first = await box.screenshot()
                assert first['width']>=640 and first['height']>=480
                screenshot_before = output.with_name(output.stem+'-before.png')
                screenshot_after = output.with_name(output.stem+'-after.png')
                with screenshot_before.open('xb') as stream:
                    stream.write(first['data'])
                passed('virtio_gpu_ubuntu_desktop_png', width=first['width'],height=first['height'],
                       sha256=hashlib.sha256(first['data']).hexdigest(),file=screenshot_before.name)
                dimensions = dict(line.split('=',1) for line in geometry['output'].splitlines() if '=' in line)
                x, y = int(dimensions['X'])+30,int(dimensions['Y'])+40
                assert 0<x<first['width'] and 0<y<first['height'],dimensions
                events = [dict(type='abs',data=dict(axis=axis,value=round(32767*point/(size-1))))
                          for axis,point,size in (('x',x,first['width']),('y',y,first['height']))]
                events.extend(dict(type='btn',data=dict(button='left',down=down)) for down in (True,False))
                pointer_id = uuid.uuid4().hex
                result = await box.desktop_input(events,request_id=pointer_id)
                assert await box.desktop_input(events,request_id=pointer_id) == result
                proof = await box.query_request(pointer_id)
                assert proof['state'] == 'DONE' and proof['response']['result'] == result,proof
                await asyncio.sleep(.2)
                pointer = await box.run_shell('DISPLAY=:0 xdotool getmouselocation --shell')
                got = dict(line.split('=',1) for line in pointer['output'].splitlines() if '=' in line)
                assert abs(int(got['X'])-x)<=4 and abs(int(got['Y'])-y)<=4,(got,x,y)
                command = 'touch /work/desktop-ok'
                keys = [{' ':'spc','/':'slash','-':'minus'}.get(ch,ch) for ch in command] + ['ret']
                for key in keys:
                    await box.desktop_input([dict(type='key',data=dict(down=down,key=dict(type='qcode',data=key)))
                                             for down in (True,False)])
                for _ in range(25):
                    if (await box.run_shell('test -f /work/desktop-ok'))['exit_code'] == 0:
                        break
                    await asyncio.sleep(.2)
                else:
                    raise AssertionError('Keyboard action did not reach the Ubuntu desktop terminal')
                last = await box.screenshot()
                assert first['data'] != last['data']
                with screenshot_after.open('xb') as stream:
                    stream.write(last['data'])
                passed('virtio_pointer_and_keyboard_effect', target=[x,y], observed=[int(got['X']),int(got['Y'])],
                       sha256=hashlib.sha256(last['data']).hexdigest(),file=screenshot_after.name)
                await retire(box)
                assert (await box.status())['resource_cleanup_complete'] is True
                passed('desktop_fullvm_stop_proof')
                report['status'] = 'passed'
                return report

            if fullvm_only:
                options = config['paths']['fullvm']
                spec = DSecFullVMRunArgs(**options['spec'])
                demand = options['demand']
                await capacity(client,demand)
                # A wrong backend or a changed pinned VM size cannot start a VMM.
                try:
                    invalid = await client.run_container(DSecContainerRunArgs(
                        environment_id=spec.environment_id),resource_demand=demand)
                except ServiceError as exc:
                    if exc.kind != 'NoPlacement':
                        raise
                else:
                    live.append(invalid)
                    raise AssertionError('FullVM environment admitted under Container')
                try:
                    invalid = await client.run_fullvm(DSecFullVMRunArgs(
                        environment_id=spec.environment_id,memory_limit_mb=spec.memory_limit_mb+1,
                        cpu_cores_limit=spec.cpu_cores_limit),resource_demand=dict(demand,memory_mb=demand['memory_mb']+1))
                except ServiceError as exc:
                    if exc.kind != 'AdmissionDenied':
                        raise
                else:
                    live.append(invalid)
                    raise AssertionError('Changed FullVM size was admitted')
                passed('fullvm_wrong_backend_and_pinned_limits_rejected')
                creation_id = uuid.uuid4().hex
                box = await client.run_fullvm(spec,resource_demand=demand,request_id=creation_id)
                live.append(box); report['sandboxes'].append(box.id)
                status = await box.status()
                assert status['backend'] == status['execution_path'] == 'fullvm',status
                assert status['containment'] == 'qemu-kvm' and status['guest']['mode'] == 'fullvm',status
                proof = await client.lookup_container_request(creation_id)
                assert proof['state'] == 'DONE' and proof['response']['result']['id'] == box.id,proof
                passed('fullvm_admission_and_attested_qemu_guest', status=status)
                rid = uuid.uuid4().hex
                command = 'printf x >> /work/fullvm-marker; id -u; test ! -S /var/run/docker.sock'
                result = await box.run_shell(command,request_id=rid)
                assert result['exit_code'] == 0 and result['output'].strip() == '1000',result
                assert await box.run_shell(command,request_id=rid) == result
                assert (await box.query_request(rid))['response']['result'] == result
                box = await client.attach_fullvm(box.id,spec)
                assert (await box.run_shell('cat /work/fullvm-marker'))['output'] == 'x'
                session = await box.open_session()
                try:
                    assert (await session.run_shell('cat /work/fullvm-marker'))['stdout'] == 'x'
                    await box.write_file('/work/binary', b'\x00\xfffullvm')
                    assert await box.read_file('/work/binary') == b'\x00\xfffullvm'
                finally:
                    await session.close()
                passed('fullvm_guest_user_shell_dedup_and_native_files')
                await retire(live[-1])
                assert (await box.status())['resource_cleanup_complete'] is True
                await capacity(client,demand)
                clean = await client.run_fullvm(spec,resource_demand=demand)
                live.append(clean); report['sandboxes'].append(clean.id)
                assert (await clean.run_shell('test ! -e /work/fullvm-marker'))['exit_code'] == 0
                await retire(clean)
                passed('fullvm_private_disk_reset_and_stop_proofs')
                report['status'] = 'passed'
                return report

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
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--fullvm-only',action='store_true')
    mode.add_argument('--desktop-only',action='store_true')
    args = parser.parse_args()
    result = asyncio.run(verify(json.loads(args.config.read_text()),args.out.resolve(),fullvm_only=args.fullvm_only,desktop_only=args.desktop_only))
    raise SystemExit(0 if result['status'] == 'passed' else 1)


if __name__ == '__main__':
    main()
